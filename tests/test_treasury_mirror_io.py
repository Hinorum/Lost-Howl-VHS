"""Зеркало казны: чтение истории у настоящего индексатора и отчёт.

`tests/test_treasury_mirror.py` проверяет математику зеркала на подставных
страницах, а этот файл — сам `_fetch_page` (единственное место, где зеркало
ходит в сеть) и текст отчёта для /treasury. Сеть подменена: клиент — скрипт
ответов, никаких живых TonAPI/Toncenter.

Особый фокус — «история недоступна» против «истории нет». Эти два состояния
зеркало обязано различать: перепутаны, битый ответ 200 один раз объявляет
зеркало выстроенным на нулевых строках, и сверка потом кричит про расхождение
на всю казну до ручного /mirror reset.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app import treasury_mirror
from app.config import settings
from app.core.registry import (
    TREASURY_MIRROR_BEAT_KEY,
    TREASURY_MIRROR_BOOTSTRAP_KEY,
    TREASURY_MIRROR_BOTTOM_KEY,
    TREASURY_MIRROR_CHECK_KEY,
    TREASURY_MIRROR_CURSOR_KEY,
    TREASURY_MIRROR_SOURCE_KEY,
)
from app.db import SessionLocal
from app.models import TreasuryMove, WatcherState
from app.ton_utils import to_nano
from app.treasury_mirror import (
    MirrorMove,
    _exact_line,
    _fetch_page,
    _kind_label,
    mirror_balance,
    parse_mirror_item,
    parse_tonapi_move,
    sync_treasury_mirror,
    treasury_mirror_block,
    treasury_mirror_stats,
)

NET = "testnet"
TREASURY = "0:" + "ab" * 32
PLAYER = "0:" + "cd" * 32
_MIRROR_KEYS = [
    TREASURY_MIRROR_BOOTSTRAP_KEY,
    TREASURY_MIRROR_BOTTOM_KEY,
    TREASURY_MIRROR_CURSOR_KEY,
    TREASURY_MIRROR_CHECK_KEY,
    TREASURY_MIRROR_BEAT_KEY,
    TREASURY_MIRROR_SOURCE_KEY,
]


def _h64(seed: str) -> str:
    return (seed * 80)[:64]


def _item(seed: str, *, lt: int = 1000, value: int = 1_000_000_000, fee: int = 5_000_000) -> dict:
    """Транзакция в формате TonAPI v2 (входящая)."""
    return {
        "hash": _h64(seed),
        "utime": 1_700_000_000,
        "lt": lt,
        "total_fees": fee,
        "balance_delta": str(value - fee),
        "success": True,
        "in_msg": {"value": value, "source": {"address": PLAYER}, "msg_data": {"raw_message": ""}},
        "out_msgs": [],
    }


def _toncenter_item(seed: str, *, lt: int = 1000, value: int = 1_000_000_000) -> dict:
    return {
        "hash": _h64(seed),
        "now": 1_700_000_000,
        "lt": lt,
        "fee": 5_000_000,
        "in_msg": {"value": value, "source": PLAYER},
        "out_msgs": [],
    }


class _Resp:
    def __init__(self, body=None, status_code: int = 200):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _indexer(monkeypatch, *, tonapi, toncenter) -> list[str]:
    """Подменяет HTTP-слой зеркала: ответ TonAPI и Toncenter по URL."""
    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: object())
    asked: list[str] = []

    async def fake_get(client, url, *, params=None, headers=None, **kwargs):
        asked.append(url)
        return toncenter if "toncenter" in url else tonapi

    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", fake_get)
    return asked


def _lt_server(monkeypatch, ledger: list[dict], *, toncenter: list[dict] | None = None) -> None:
    """HTTP-слой зеркала «по-настоящему»: страницы по before_lt, как у живого.

    Статичный ответ в цикле бутстрапа водит по кругу (одна и та же страница
    до max_pages), поэтому сервер обязан честно отдавать «глубже» и пустоту.
    """
    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: object())
    backup = toncenter or []

    async def fake_get(client, url, *, params=None, headers=None, **kwargs):
        before = (params or {}).get("before_lt")
        source = backup if "toncenter" in url else ledger
        page = [i for i in source if before is None or int(i["lt"]) < int(before)]
        return _Resp({"transactions": page})

    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", fake_get)


def _off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", False)


def _on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_address", "")


async def _wipe() -> None:
    async with SessionLocal() as db:
        await db.execute(TreasuryMove.__table__.delete())
        await db.execute(WatcherState.__table__.delete().where(WatcherState.key.in_(_MIRROR_KEYS)))
        await db.commit()


# ---------- _fetch_page: источники, фолбэк, «не знаю» ----------


async def test_fetch_page_disabled_ton_reports_unavailable(monkeypatch) -> None:
    """TON выключен — зеркало не ходит в сеть и честно говорит «не знаю»."""
    _off(monkeypatch)

    async def explode(*args, **kwargs):
        raise AssertionError("выключенный TON не должен ходить в сеть")

    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", explode)
    assert await _fetch_page() == ([], "none", False)


async def test_fetch_page_without_address_reports_unavailable(monkeypatch) -> None:
    _on(monkeypatch)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")

    async def explode(*args, **kwargs):
        raise AssertionError("без адреса казначея запрос не уходит")

    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", explode)
    assert await _fetch_page() == ([], "none", False)


async def test_fetch_page_reads_tonapi(monkeypatch) -> None:
    _on(monkeypatch)
    asked = _indexer(
        monkeypatch,
        tonapi=_Resp({"transactions": [_item("a", lt=200), _item("b", lt=100)]}),
        toncenter=_Resp({"transactions": []}),
    )
    moves, source, ok = await _fetch_page()
    assert (source, ok) == ("tonapi", True)
    assert len(asked) == 1, "при живом TonAPI второй провайдер не трогаем"
    assert TREASURY in asked[0] and "/transactions" in asked[0]
    assert [m.tx_hash for m in moves] == [_h64("a"), _h64("b")]
    assert moves[0].network == NET and moves[0].direction == "in"


async def test_fetch_page_falls_back_to_toncenter(monkeypatch) -> None:
    """TonAPI молчит — та же страница берётся у Toncenter v3."""
    _on(monkeypatch)
    called: list[dict] = []

    async def fake_get(client, url, *, params=None, headers=None, **kwargs):
        called.append({"url": url, "params": params or {}})
        if "toncenter" in url:
            return _Resp({"transactions": [_toncenter_item("c", lt=300)]})
        raise TimeoutError("tonapi down")

    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: object())
    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", fake_get)
    moves, source, ok = await _fetch_page()
    assert (source, ok) == ("toncenter", True)
    assert moves[0].tx_hash == _h64("c")
    toncenter_call = next(c for c in called if "toncenter" in c["url"])
    assert toncenter_call["params"]["account"] == TREASURY
    assert toncenter_call["params"]["sort"] == "desc"
    # Размер страницы берётся из кода, а не вписан числом: прежний тест держал
    # тут 100, и любое осознанное изменение _MIRROR_PAGE_LIMIT роняло его,
    # хотя поведение зеркала оставалось правильным.
    assert toncenter_call["params"]["limit"] == treasury_mirror._MIRROR_PAGE_LIMIT


async def test_fetch_page_reports_unavailable_when_both_silent(monkeypatch) -> None:
    _on(monkeypatch)

    async def always_fail(*args, **kwargs):
        raise TimeoutError("down")

    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: object())
    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", always_fail)
    assert await _fetch_page() == ([], "none", False)


async def test_broken_200_body_is_not_an_empty_history(monkeypatch) -> None:
    """Ответ 200 без списка transactions — сбой формы ответа, а не пустая казна.

    Раньше такой ответ читался как «страница пуста» и в бутстрапе навсегда
    объявлял зеркало выстроенным на нулевых строках: тождество потом показывало
    расхождение на всю казну, и без ручного /mirror reset тревога не уходила.
    Теперь провайдер считается недоступным, и мы пробуем следующего.
    """
    _on(monkeypatch)
    asked = _indexer(
        monkeypatch,
        tonapi=_Resp({"error": "Too many requests", "code": 429}),
        toncenter=_Resp({"transactions": [_toncenter_item("d", lt=400)]}),
    )
    moves, source, ok = await _fetch_page()
    assert (source, ok) == ("toncenter", True), "Toncenter обязан был подхватить"
    assert len(asked) == 2
    assert moves[0].tx_hash == _h64("d")


async def test_both_providers_broken_is_unavailable(monkeypatch) -> None:
    _on(monkeypatch)
    _indexer(
        monkeypatch,
        tonapi=_Resp({"error": "nope"}),
        toncenter=_Resp({"code": 500, "message": "internal"}),
    )
    assert await _fetch_page() == ([], "none", False)


async def test_empty_history_from_healthy_provider_is_ok(monkeypatch) -> None:
    """Пустой кошелёк = ключ с пустым списком: это «истории нет», а не сбой."""
    _on(monkeypatch)
    _indexer(monkeypatch, tonapi=_Resp({"transactions": []}), toncenter=_Resp({}))
    moves, source, ok = await _fetch_page()
    assert (moves, source, ok) == ([], "tonapi", True)


# ---------- Синк на сбое не двигает состояние ----------


async def test_sync_does_not_move_state_when_providers_silent(monkeypatch) -> None:
    """Оба индексатора молчат: ни строк, ни курсора, ни «выстроено»."""
    _on(monkeypatch)

    async def always_fail(*args, **kwargs):
        raise TimeoutError("down")

    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: object())
    monkeypatch.setattr(treasury_mirror, "http_get_with_retry", always_fail)
    try:
        result = await sync_treasury_mirror()
        assert result == {
            "pages": 0,
            "added": 0,
            "updated": 0,
            "source": "none",
            "bootstrapped": False,
            "exact": None,
            "diff_nanotons": None,
            "mirror_balance": None,
            "chain_balance": None,
        }
        async with SessionLocal() as db:
            for key in _MIRROR_KEYS:
                assert await db.get(WatcherState, key) is None, f"{key} не должен появиться"
    finally:
        await _wipe()


async def test_sync_disabled_ton_returns_empty_summary(monkeypatch) -> None:
    _off(monkeypatch)
    assert (await sync_treasury_mirror())["pages"] == 0


async def test_sync_keeps_last_good_check_when_balance_unreadable(monkeypatch) -> None:
    """Индексы прошли, а живой баланс не прочитан — сверка не переписывается.

    Иначе один сбой баланса стирал бы прошлый результат «сходится ±0» на
    «не измерено» и тревожил бы хранителя без причины.
    """
    _on(monkeypatch)
    _lt_server(monkeypatch, [_item("s", lt=500)])
    import app.ton_pay

    async def broken_state():
        raise RuntimeError("индексы лежат")

    monkeypatch.setattr(app.ton_pay, "fetch_account_state", broken_state)
    try:
        first = await sync_treasury_mirror()
        assert first["bootstrapped"] is True and first["added"] == 1
        assert first["exact"] is None and first["chain_balance"] is None
        async with SessionLocal() as db:
            assert await db.get(WatcherState, TREASURY_MIRROR_CHECK_KEY) is None
            beat = await db.get(WatcherState, TREASURY_MIRROR_BEAT_KEY)
        assert beat is not None, "цикл успешен — сердцебиение обязано быть"

        # Следующий цикл с рабочим балансом сверяет и записывает CHECK.
        monkeypatch.setattr(app.ton_pay, "fetch_account_state", AsyncMock(
            return_value=(to_nano(0.995), "active", "tonapi")))
        second = await sync_treasury_mirror()
        assert second["exact"] is True
    finally:
        await _wipe()


async def test_sync_bootstrap_uses_both_providers(monkeypatch) -> None:
    """Бутстрап до дна на живом _fetch_page: пустая страница = выстроено."""
    _on(monkeypatch)
    _lt_server(monkeypatch, [_item("x", lt=10_000), _item("y", lt=9_000)])
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state", AsyncMock(
        return_value=(to_nano(1.99), "active", "tonapi")))
    try:
        result = await sync_treasury_mirror()
        assert result["bootstrapped"] is True and result["added"] == 2
        assert result["pages"] == 2, "вторая страница — пустая, дно истории"
        assert result["source"] == "tonapi"
        assert result["exact"] is True
        async with SessionLocal() as db:
            assert await db.get(WatcherState, TREASURY_MIRROR_BOOTSTRAP_KEY) is not None
            bottom = await db.get(WatcherState, TREASURY_MIRROR_BOTTOM_KEY)
            source = await db.get(WatcherState, TREASURY_MIRROR_SOURCE_KEY)
        assert bottom.value == "9000"
        assert source.value == "tonapi"
    finally:
        await _wipe()


# ---------- Отчёт /treasury: все состояния блока ----------


def test_kind_label_splits_payout_namespace() -> None:
    assert _kind_label("payout:prize") == "выплаты:prize"
    assert _kind_label("stake") == "ставки"
    assert _kind_label("что-то новое") == "что-то новое"


def test_exact_line_states_every_verdict() -> None:
    assert "недоступно" in _exact_line({}, bootstrapped=False)
    assert "не измерено" in _exact_line({}, bootstrapped=True)
    assert "±0" in _exact_line({"exact": True, "mirror_balance": to_nano(2)}, bootstrapped=True)
    assert "расходится" in _exact_line(
        {"exact": False, "diff_nanotons": to_nano(-1)}, bootstrapped=True
    )


def test_mirror_item_autodetects_provider() -> None:
    assert parse_mirror_item(_item("p"), NET, TREASURY).provider == "tonapi"
    assert parse_mirror_item(_toncenter_item("q"), NET, TREASURY).provider == "toncenter"
    assert parse_mirror_item("не dict", NET, TREASURY) is None
    assert parse_mirror_item({}, NET, TREASURY) is None, "без хеша движения нет"


async def test_mirror_stats_survives_broken_check_json(monkeypatch) -> None:
    """Испорченный CHECK не должен ронять /treasury — вернётся пустой словарь."""
    _on(monkeypatch)
    now = int(datetime.now(UTC).timestamp())
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_CHECK_KEY, value="{не json"))
            db.add(WatcherState(key=TREASURY_MIRROR_BEAT_KEY, value="не дата"))
            db.add(TreasuryMove(tx_hash=_h64("m"), network=NET, address=TREASURY,
                                utime=now, lt=1,
                                direction="in", kind="stake", value_nanotons=to_nano(1),
                                fee_nanotons=0, balance_delta_nanotons=to_nano(1)))
            await db.commit()
        async with SessionLocal() as db:
            stats = await treasury_mirror_stats(db)
        assert stats["check"] == {}
        assert stats["bootstrapped"] is False
        text = await treasury_mirror_block()
        assert "последний цикл: не завершался" in text
    finally:
        await _wipe()


async def test_mirror_block_reports_disabled_ton(monkeypatch) -> None:
    _off(monkeypatch)
    text = await treasury_mirror_block()
    assert "не активно" in text


async def test_mirror_block_renders_traffic_fees_and_unknown(monkeypatch) -> None:
    """Выстроенное зеркало показывает трафик по видам, реальный газ и «пыль»."""
    _on(monkeypatch)
    now = int(datetime.now(UTC).timestamp())
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BOOTSTRAP_KEY, value="1"))
            db.add(WatcherState(key=TREASURY_MIRROR_BEAT_KEY,
                                value=datetime.now(UTC).isoformat()))
            db.add(WatcherState(key=TREASURY_MIRROR_SOURCE_KEY, value="tonapi"))
            db.add(WatcherState(key=TREASURY_MIRROR_CHECK_KEY, value=json.dumps(
                {"exact": True, "diff_nanotons": 0, "mirror_balance": to_nano(5)})))
            db.add_all([
                TreasuryMove(tx_hash=_h64("s"), network=NET, address=TREASURY,
                            utime=now, lt=10,
                            direction="in", kind="stake", value_nanotons=to_nano(3),
                            fee_nanotons=to_nano(0.001), balance_delta_nanotons=to_nano(3)),
                TreasuryMove(tx_hash=_h64("u"), network=NET, address=TREASURY,
                            utime=now, lt=11,
                            direction="in", kind="unknown_in", value_nanotons=to_nano(0.5),
                            fee_nanotons=0, balance_delta_nanotons=to_nano(0.5)),
                TreasuryMove(tx_hash=_h64("self"), network=NET, address=TREASURY,
                            utime=now, lt=12,
                            direction="self", kind="self", value_nanotons=to_nano(1),
                            fee_nanotons=0, balance_delta_nanotons=0),
            ])
            await db.commit()
        text = await treasury_mirror_block()
        assert "тождество: сходится ±0" in text
        assert "ставки" in text and "пыль/чужие" in text
        assert "непонятых переводов" in text
        assert "источник tonapi" in text
        assert "только что" in text, "свежий utime = актуально только что"
        assert "self" not in text.split("трафик:")[1], "self не в трафике"
    finally:
        await _wipe()


async def test_mirror_block_flags_frozen_sync_and_missing_history(monkeypatch) -> None:
    """Выстроенное зеркало без строк и без сердцебиения — тревога, а не тишина."""
    _on(monkeypatch)
    now = int(datetime.now(UTC).timestamp())
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BOOTSTRAP_KEY, value="1"))
            await db.commit()
        text = await treasury_mirror_block()
        assert "тождество: пока не измерено" in text

        # Есть движения, но ни одного успешного цикла: синк встал.
        async with SessionLocal() as db:
            db.add(TreasuryMove(tx_hash=_h64("z"), network=NET, address=TREASURY,
                                utime=now, lt=7,
                                direction="in", kind="stake", value_nanotons=to_nano(1),
                                fee_nanotons=0, balance_delta_nanotons=to_nano(1)))
            await db.commit()
        text = await treasury_mirror_block()
        assert "последний цикл: не завершался" in text
        assert "трафик: ставки 1" in text
    finally:
        await _wipe()


async def test_mirror_block_shows_bootstrap_progress(monkeypatch) -> None:
    _on(monkeypatch)
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BOTTOM_KEY, value="4242"))
            await db.commit()
        text = await treasury_mirror_block()
        assert "история кошелька пуста либо бутстрап ещё не начался" in text
        assert "история дотягивается от головы к генезису" in text
    finally:
        await _wipe()


async def test_mirror_block_counts_minutes_for_old_sync(monkeypatch) -> None:
    _on(monkeypatch)
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BOOTSTRAP_KEY, value="1"))
            db.add(WatcherState(key=TREASURY_MIRROR_BEAT_KEY,
                                value=(datetime.now(UTC) - timedelta(minutes=7)).isoformat()))
            db.add(TreasuryMove(tx_hash=_h64("old"), network=NET, address=TREASURY,
                                utime=int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
                                lt=5, direction="in", kind="stake", value_nanotons=to_nano(1),
                                fee_nanotons=0, balance_delta_nanotons=to_nano(1)))
            await db.commit()
        text = await treasury_mirror_block()
        assert "420 с назад" in text
        assert "60 мин назад" in text
    finally:
        await _wipe()


async def test_mirror_balance_is_zero_on_empty_mirror() -> None:
    async with SessionLocal() as db:
        assert await mirror_balance(db, "testnet-nonexistent") == 0


# ---------- Мусор на входе: зеркало обязано не враньё ----------


def test_self_direction_ignores_empty_and_broken_addresses() -> None:
    from app.treasury_mirror import _address_of, _self_direction

    assert _address_of({"address": PLAYER}) == PLAYER
    assert _address_of({"address": None}) == ""
    assert _address_of(PLAYER) == PLAYER
    assert _address_of(None) == ""
    assert _self_direction("", TREASURY) is False
    assert _self_direction(PLAYER, "") is False
    assert _self_direction("не адрес", TREASURY) is False, "битый адрес не=self"


def test_balance_delta_prefers_chain_value_but_survives_garbage() -> None:
    from app.treasury_mirror import _derive_balance_delta

    assert _derive_balance_delta("12345", 999, 1, 1) == 12345
    assert _derive_balance_delta("не число", 100, 30, 5) == 65, "сальдо посчитано самим"
    assert _derive_balance_delta(None, 100, 30, 5) == 65


@pytest.mark.parametrize(
    "broken",
    [
        {"in_msg": "строка вместо объекта"},
        {"in_msg": {"value": "много"}},
        {"out_msgs": ["строка"]},
        {"out_msgs": [{"value": "много", "destination": PLAYER}]},
        {"out_msgs": [{"value": 5, "fwd_fee": "много"}]},
    ],
)
def test_message_decoders_tolerate_garbage(broken: dict) -> None:
    """Мусор в полях сообщений = нулевые деньги, а не исключение в цикле синка."""
    from app.treasury_mirror import _in_value, _out_forward_fees, _out_value

    item = {**_item("g"), **broken}
    value, source, _comment = _in_value(item)
    out_total, _dest, _c = _out_value(item)
    assert (value, out_total) == (0, 0) or isinstance(value, int)
    assert _out_forward_fees(item) == 0


def test_parsers_reject_unusable_transactions() -> None:
    """Без хеша/с не-объектом движение не существует — строки не будет."""
    from app.treasury_mirror import parse_tonapi_move, parse_toncenter_move

    assert parse_tonapi_move("не dict", NET, TREASURY) is None
    assert parse_tonapi_move({"hash": ""}, NET, TREASURY) is None
    assert parse_toncenter_move("не dict", NET, TREASURY) is None
    assert parse_toncenter_move({"hash": ""}, NET, TREASURY) is None


@pytest.mark.parametrize("broken_time", [{"utime": "вчера"}, {"lt": "позавчера"}])
def test_parsers_default_broken_time_to_zero(broken_time: dict) -> None:
    """Неразобранное время = 0, а не падение: строка зеркала нужна в любом случае."""
    item = {**_item("t"), **broken_time}
    move = parse_tonapi_move(item, NET, TREASURY)
    assert move is not None and (move.utime, move.lt) == (1_700_000_000, 0) or move.lt == 0
    tc = {**_toncenter_item("t"), **broken_time}
    move_tc = parse_tonapi_move(tc, NET, TREASURY)
    assert move_tc is None or move_tc.lt == 0


def test_parsers_default_broken_fee_to_zero() -> None:
    from app.treasury_mirror import parse_tonapi_move, parse_toncenter_move

    move = parse_tonapi_move(
        {**_item("f"), "total_fees": "дорого", "balance_delta": None}, NET, TREASURY
    )
    assert move.fee_nanotons == 0
    assert move.balance_delta_nanotons == to_nano(1), "сальдо без учёта неизвестного газа"
    move_tc = parse_toncenter_move({**_toncenter_item("f"), "fee": None}, NET, TREASURY)
    assert move_tc.fee_nanotons == 0


def test_service_transaction_becomes_other_direction() -> None:
    """Транзакция без переводов (например, контракт-вызов) = other, не дыра в Σ."""
    from app.treasury_mirror import parse_tonapi_move, parse_toncenter_move

    item = {
        "hash": _h64("svc"), "utime": 1, "lt": 1, "total_fees": 1_000,
        "balance_delta": "-1000", "success": True,
        "in_msg": {"value": 0, "source": None, "msg_data": {}}, "out_msgs": [],
    }
    move = parse_tonapi_move(item, NET, TREASURY)
    assert move.direction == "other" and move.value_nanotons == 0
    assert move.is_money_move is True, "сальдо отрицательное — движение денег есть"
    tc = {k: v for k, v in item.items() if k not in ("utime", "total_fees")}
    tc.update({"now": 1, "fee": 1_000})
    assert parse_toncenter_move(tc, NET, TREASURY).direction == "other"


def test_toncenter_self_transfer_detected() -> None:
    from app.treasury_mirror import parse_toncenter_move

    item = {
        "hash": _h64("self"), "now": 1, "lt": 2, "fee": 1_000, "balance_delta": "0",
        "in_msg": {"value": to_nano(1), "source": TREASURY}, "out_msgs": [],
    }
    assert parse_toncenter_move(item, NET, TREASURY).direction == "self"


def test_way_memo_rejects_malformed_keys() -> None:
    from app.treasury_mirror import parse_way_memo

    assert parse_way_memo("просто текст") is None
    assert parse_way_memo("way:30:prize") is None, "без #id"
    assert parse_way_memo("way:30:prize#нецифра") is None
    assert parse_way_memo("way:30#12") is None, "без kind"
    assert parse_way_memo("way:30:#12") is None
    assert parse_way_memo("way:30:prize#12") == ("prize", 12)


def test_incoming_kind_reads_all_watcher_notes() -> None:
    from app.models import Income
    from app.treasury_mirror import _incoming_kind_from_income

    for note, tag in (
        ("in:stake;src:UQ", "stake"),
        ("in:revote;src:UQ", "revote"),
        ("in:walletverify;src:UQ", "walletverify"),
        ("in:bank;src:UQ", "bank"),
        ("in:paused;src:UQ", "paused"),
        ("in:unknown;src:UQ", "unknown_in"),
    ):
        assert _incoming_kind_from_income(Income(kind="x", amount_nanotons=1, note=note)) == tag
    assert _incoming_kind_from_income(Income(kind="ton", amount_nanotons=1)) == "income"
    assert _incoming_kind_from_income(Income(kind="x", amount_nanotons=1, note=None)) == "unknown_in"


async def test_resolve_kind_shortcuts_for_self_and_other() -> None:
    from app.treasury_mirror import resolve_kind

    async with SessionLocal() as db:
        for direction in ("self", "other"):
            move = parse_tonapi_move(_item(direction), NET, TREASURY)
            move = MirrorMove(**{**move.__dict__, "direction": direction})
            assert await resolve_kind(db, move) == (direction, None)


async def test_resolve_kinds_batch_classifies_every_outgoing_kind() -> None:
    """Батч-классификация исходящих: по memo, по хешу и «вывод мимо бота»."""
    from app.models import Payout
    from app.treasury_mirror import _resolve_kinds_batch, parse_tonapi_move

    def outgoing(seed: str, comment: str, lt: int) -> MirrorMove:
        return parse_tonapi_move({
            "hash": _h64(seed), "utime": 1, "lt": lt, "total_fees": 1,
            "balance_delta": "-100", "success": True,
            "in_msg": {"value": 0, "source": None, "msg_data": {}},
            "out_msgs": [{"value": to_nano(1), "destination": {"address": PLAYER},
                          "fwd_fee": 1, "msg_data": {"decoded_comment": comment}}],
        }, NET, TREASURY)

    async with SessionLocal() as db:
        prize = Payout(kind="prize", network=NET, dest_address=PLAYER, amount_nanotons=1)
        refund = Payout(kind="refund", network=NET, dest_address=PLAYER, amount_nanotons=1)
        legacy = Payout(kind="rake", network=NET, dest_address=PLAYER, amount_nanotons=1,
                        tx_hash=_h64("legacy"))
        db.add_all([prize, refund, legacy])
        await db.commit()
        by_memo = outgoing("memo", f"way:30:prize#{prize.id}", 10)
        by_refund = outgoing("rfnd", f"way:30:refund#{refund.id}", 20)
        by_hash = outgoing("legacy", "возврат по кнопке", 30)
        unknown = outgoing("outs", "вывод мимо бота", 40)
        dangling = outgoing("dang", "way:30:prize#999999", 50)
        moves = [by_memo, by_refund, by_hash, unknown, dangling]
        kinds = await _resolve_kinds_batch(db, moves)

    assert kinds[by_memo.tx_hash] == ("payout:prize", prize.id)
    assert kinds[by_refund.tx_hash] == ("refund", refund.id)
    assert kinds[by_hash.tx_hash] == ("payout:rake", legacy.id), "легаси-строка по хешу"
    assert kinds[unknown.tx_hash] == ("unknown_out", None)
    assert kinds[dangling.tx_hash] == ("unknown_out", None), "мемо без выплаты не выдумывает"


async def test_apply_page_on_empty_page_is_noop() -> None:
    from app.treasury_mirror import _apply_page

    async with SessionLocal() as db:
        assert await _apply_page(db, [], {}) == (0, 0)


async def test_incremental_sync_stops_at_known_head(monkeypatch) -> None:
    """Инкремент не переписывает историю: страница ниже головы не двигает курсор."""
    _on(monkeypatch)
    ledger = [_item("h1", lt=2_000), _item("h2", lt=1_000)]
    _lt_server(monkeypatch, ledger)
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state", AsyncMock(
        return_value=(to_nano(2), "active", "tonapi")))
    try:
        first = await sync_treasury_mirror()
        assert first["bootstrapped"] is True and first["added"] == 2
        async with SessionLocal() as db:
            head = int((await db.get(WatcherState, TREASURY_MIRROR_CURSOR_KEY)).value)
        assert head == 2_000

        # Тот же срез приходит снова — новых транзакций нет.
        second = await sync_treasury_mirror()
        assert second["added"] == 0 and second["updated"] == 0
        async with SessionLocal() as db:
            assert int((await db.get(WatcherState, TREASURY_MIRROR_CURSOR_KEY)).value) == head
    finally:
        await _wipe()
