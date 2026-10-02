"""Глубокая сверка с историей казначея и аудит-отчёт блокчейн-контура.

Одна страница (128 tx) провайдера — слишком мелкое окно для анти-дубля:
memo «уже отправленного» в длинной очереди уходит за край, сверка решает
«перевода нет» и очередь плодит повторный перевод. Здесь проверяется
пагинация вглубь (TonAPI before_lt / Toncenter offset), стоп-условия, стопор
повтора при недоступной истории (защита от double-pay при 2ч requeue) и
/blockchain как точка входа в разбор «куда делось».
"""

import json
import os
import time
from unittest.mock import AsyncMock

import pytest

from app import ton_pay
from app.config import settings
from app.core.registry import BEAT_KEY, CURSOR_KEY, SOURCE_KEY, STUCK_TX_KEY
from app.db import SessionLocal
from app.http_utils import http_get_with_retry
from app.models import Payout, WatcherState

# ---------- Пагинация истории казначея ----------


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.AsyncClient-замена: страница = клиентский срез данных.

    Честно повторяет семантику обоих провайдеров: Toncenter срезает окно
    параметром offset (старт в непрерывном потоке), TonAPI — курсором
    before_lt (всё строго старше lt, «новые сверху»). always_first_page —
    «сломанная пагинация»: провайдер на любой параметр отдаёт первые записи
    (для проверки стоп-условия).
    """

    def __init__(
        self,
        items: list[dict],
        capture: list[dict],
        *,
        always_first_page: bool = False,
    ) -> None:
        self._items = items
        self._capture = capture
        self._always_first_page = always_first_page

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None, **kwargs) -> _FakeResp:
        params = dict(params or {})
        self._capture.append(params)
        limit = int(params.get("limit", 128))
        before_lt = params.get("before_lt")
        if self._always_first_page:
            page = self._items[:limit]
        elif before_lt is not None:
            # TonAPI: курсор ходит строго старше lt (список свёрнут «новые сверху»).
            before = int(before_lt)
            page = [it for it in self._items if int(it.get("lt") or 0) < before][:limit]
        else:
            offset = int(params.get("offset", 0))
            page = self._items[offset : offset + limit]
        return _FakeResp({"transactions": page})


def _tonapi_item(lt: int, utime: int, memo: str) -> dict:
    return {
        "hash": f"h{lt}",
        "lt": lt,
        "utime": utime,
        "out_msgs": [{"msg_data": {"decoded_comment": memo}}],
    }


def _toncenter_item(lt: int, utime: int, memo: str) -> dict:
    return {
        "hash": f"h{lt}",
        "lt": lt,
        "utime": utime,
        "out_msgs": [{"message_content": {"decoded": {"@type": "comment", "comment": memo}}}],
    }


def _build_stream(now: int, item_builder, count: int = 384) -> list[dict]:
    """Непрерывный поток исходящих казначея (страницы «новые сверху»).

    lt и utime убывают вдоль потока (как в реальном провайдере: свежие
    записи — самые большие lt), memo уникальны. Числовые поля провайдеров
    в статье трактуются строкой через str().
    """
    return [item_builder(1_000_000 + count - i, now - i, f"way:1:pr#{i}") for i in range(count)]


async def test_toncenter_reconcile_pagination_goes_deep(monkeypatch) -> None:
    """Сверка ищет memo НЕ только среди последних 128 tx: ходит страницами вглубь.

    Регрессия: прежний лимит 128 терял memo выпавших из окна старых выплат,
    и 2-часовой requeue переотправлял уже ушедший перевод (двойной платёж).
    Здесь memo из глубины (дальше 160 tx) должен найтись.
    """
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_toncenter()
    offsets = [p["offset"] for p in capture]
    # Страница (0) → шаг с перекрытием (112); следующая уже вне окна 200с — стоп.
    assert offsets == [0, 112]
    assert tx_map["way:1:pr#160"] == "h1000224"  # в глубине, за пределами 128 tx
    assert "way:1:pr#0" in tx_map  # первая страница тоже собрана
    assert len(tx_map) == 240  # 128 + (112 новых, стык 112..127 перечитан идемпотентно)


async def test_tonapi_reconcile_pagination_walks_by_lt(monkeypatch) -> None:
    """TonAPI ходит вглубь курсором before_lt, а не offset (offset в этом
    эндпоинте нет) — окно истории при этом тоже покрывается без дыр."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _tonapi_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_tonapi()
    cursors = [p.get("before_lt") for p in capture]
    # Первая страница без курсора; вторая — строго старше lt её последней записи.
    assert cursors == [None, "1000257"]
    assert tx_map["way:1:pr#160"] == "h1000224"  # в глубине, за пределами 128 tx
    assert len(tx_map) == 256  # 128 + 128 новых, без перечитывания стыка


async def test_toncenter_breaks_when_offset_not_honored(monkeypatch) -> None:
    """Провайдер проигнорировал offset (та же страница поверх) — не проходимся по кругу."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item, count=128)
    capture: list[dict] = []
    # На любой offset отдаём одну и ту же страницу: первый хеш повторится.
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture, always_first_page=True))

    tx_map = await ton_pay._tx_map_via_toncenter()
    assert len(capture) == 2, "провайдер без offset должен обрываться после повтора страницы"
    assert len(tx_map) == 128


async def test_toncenter_reconcile_overlap_covers_boundary(monkeypatch) -> None:
    """Шаг пагинации перекрывает стык страниц: memo на границе окна не теряется.

    Окно истории широкое (24ч), поток из 384 tx: страницы идут с перекрытием
    16 записей (offset 0, 112, 224, 336), и memo с самого края потока находится.
    """
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item, count=384)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_toncenter()
    offsets = [p["offset"] for p in capture]
    assert offsets == [0, 112, 224, 336]
    assert tx_map["way:1:pr#200"] == "h1000184"  # внутри второй окна шага
    assert tx_map["way:1:pr#340"] == "h1000044"  # хвост потока, частичная страница
    assert len(tx_map) == 384  # стыки перечитаны идемпотентно, дыр нет


async def test_toncenter_stops_at_partial_page(monkeypatch) -> None:
    """Хвост истории: неполная страница (< limit) — дальше запрос не нужен."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item, count=100)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_toncenter()
    assert len(capture) == 1
    assert len(tx_map) == 100


async def test_toncenter_stops_at_empty_history(monkeypatch) -> None:
    """Пустая страница завершает проход досрочно, без лишних запросов."""
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient([], capture))
    tx_map = await ton_pay._tx_map_via_toncenter()
    assert tx_map == {}
    assert len(capture) == 1


# ---------- Сверка «по-требованию»: досрочный стоп по целям ----------


async def test_toncenter_reconcile_stops_when_all_targets_found(monkeypatch) -> None:
    """targets найдены на 2-й странице — дальше вглубь, до конца окна, не ходим.

    Сверка sent-выплат не шерстит всю историю слепо: суммы к подтверждению
    почти всегда на первой-второй странице, и лишние запросы в провайдера = 0.
    """
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_toncenter(targets={"way:1:pr#160"})
    offsets = [p["offset"] for p in capture]
    assert offsets == [0, 112]
    assert tx_map["way:1:pr#160"] == "h1000224"  # найдена на второй странице


async def test_toncenter_reconcile_stops_at_first_page_when_target_clean(monkeypatch) -> None:
    """Цель лежит уже на первой странице — один запрос, ноль лишних."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _toncenter_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_toncenter(targets={"way:1:pr#5"})
    assert [p["offset"] for p in capture] == [0]
    assert tx_map["way:1:pr#5"] == "h1000379"


async def test_tonapi_reconcile_stops_at_first_page_when_target_clean(monkeypatch) -> None:
    """Та же досрочная остановка на основном провайдере (TonAPI)."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    items = _build_stream(now, _tonapi_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _FakeClient(items, capture))

    tx_map = await ton_pay._tx_map_via_tonapi(targets={"way:1:pr#5"})
    assert [p.get("before_lt") for p in capture] == [None]
    assert tx_map["way:1:pr#5"] == "h1000379"


async def test_fetch_broadcast_tx_map_passes_targets_to_fetchers(monkeypatch) -> None:
    """fetch_broadcast_tx_map пробрасывает цели в оба фетчера."""
    seen: dict[str, set[str]] = {}

    async def fake_api(targets: set[str] | None) -> dict[str, str]:
        seen["api"] = targets if targets is None or targets == set() else targets
        return {}

    monkeypatch.setattr(ton_pay, "_tx_map_via_tonapi", fake_api)
    result = await ton_pay.fetch_broadcast_tx_map(targets={"way:1:pr#1"})
    assert result == {}
    assert seen["api"] == {"way:1:pr#1"}


async def test_empty_200_without_transactions_is_unknown(monkeypatch) -> None:
    """HTTP 200 с телом БЕЗ transactions — аномалия провайдера, а не
    «история пуста»: сверка остаётся «не знаем», повторы заморожены.

    Именно такой ответ (дроссель/ошибка-обёртка в 200) молча превращался в
    «memo в цепочке нет», и повтор уезжал на уже ушедший платёж.
    """
    from app.ton_pay import state as _st

    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    empty = AsyncMock()
    empty.get = AsyncMock(return_value=_FakeResp({}))
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: empty)
    _st._RECONCILE_HISTORY_OK = True
    try:
        tx_map = await ton_pay.fetch_broadcast_tx_map(targets={"way:1:pr#1"})
        assert tx_map == {}
        assert _st._RECONCILE_HISTORY_OK is False
    finally:
        _st._RECONCILE_HISTORY_OK = True


async def test_empty_200_without_transactions_on_tonapi_also_unknown(monkeypatch) -> None:
    """То же на основном провайдере напрямую: без фолбэка его {}-ответ падает."""
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    empty = AsyncMock()
    empty.get = AsyncMock(return_value=_FakeResp({}))
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: empty)
    try:
        await ton_pay._tx_map_via_tonapi(targets={"way:1:pr#1"})
        pytest.fail("TonAPI с {} обязан падать, а не молчать")
    except RuntimeError as exc:
        assert "transactions" in str(exc)
    # Валидная пустая история (transactions: []) остаётся честной пустотой.
    empty.get = AsyncMock(return_value=_FakeResp({"transactions": []}))
    assert await ton_pay._tx_map_via_tonapi(targets={"way:1:pr#1"}) == {}


# ---------- Guard: повтор при недоступной истории ----------


async def _seed_payout(attempts: int) -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=42,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="pending",
        )
        session.add(payout)
        await session.flush()
        payout.attempts = attempts
        await session.commit()
        return payout.id


def _patch_send_environment(monkeypatch, transfer: AsyncMock, markers) -> None:
    """Общая обстановка dispatch-тестов: TON включён, маркеры подменены."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers)


async def test_retry_held_when_history_unavailable(monkeypatch) -> None:
    """Повтор (>1 попытки) при «история молчит» НЕ переотправляется: риск double-pay.

    2-часовой requeue: перевод мог уйти в прошлый цикл, а сверка не видит memo,
    потому что оба провайдера недоступны. Пустой ответ маркеров тут — «не знаю»,
    а не «перевода нет». Замораживаем строку вместо реального двойного платежа."""
    payout_id = await _seed_payout(attempts=1)

    transfer = AsyncMock(return_value="bcast:123")

    async def empty_markers() -> set[str]:
        ton_pay.state._RECONCILE_HISTORY_OK = False  # оба провайдера реально упали
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 0
        assert transfer.await_count == 0  # не вещали — денег не задвоили
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"  # вернётся, когда история оживёт
        assert "анти-дубль" in (row.last_error or "")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_retry_proceeds_when_history_known(monkeypatch) -> None:
    """История доступна, memo в ней нет — это честное «перевод не ушёл»: отправляем."""
    payout_id = await _seed_payout(attempts=1)

    transfer = AsyncMock(return_value="bcast:777")

    async def empty_markers() -> set[str]:
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_HISTORY_OK", True)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_first_attempt_not_blocked_by_unknown_history(monkeypatch) -> None:
    """Первая попытка (ещё ничего не вещали) не боится «не знаю»: отправляется."""
    payout_id = await _seed_payout(attempts=0)

    transfer = AsyncMock(return_value="bcast:999")

    async def empty_markers() -> set[str]:
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_HISTORY_OK", False)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


# ---------- Экспоненциальный backoff ----------


class _BoomClient:
    """Клиент, который сначала ловит транзиентные сбои, потом отвечает 200."""

    def __init__(self, failures: int) -> None:
        self._left = failures
        self.calls = 0

    async def get(self, url: str, params=None, headers=None):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise httpx_transport_error()
        return _FakeResp({"ok": True})


def httpx_transport_error():
    import httpx

    return httpx.ConnectError("нет соединения")


async def test_http_retry_uses_exponential_backoff(monkeypatch) -> None:
    """Паузы между ретраями растут: 1с → 2с → 4с, а не одна и та же задержка."""
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _BoomClient(failures=3)

    response = await http_get_with_retry(
        client, "https://ton.example/v2/ping", max_retries=3,
        retry_delay=1.0, backoff_factor=2.0, max_delay=30.0,
    )
    assert response.json() == {"ok": True}
    assert client.calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_http_retry_backoff_respects_cap(monkeypatch) -> None:
    """Задержка не растёт бесконечно: потолок max_delay обрезает экспоненту."""
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _BoomClient(failures=4)

    await http_get_with_retry(
        client, "https://ton.example/v2/ping", max_retries=4,
        retry_delay=10.0, backoff_factor=10.0, max_delay=25.0,
    )
    assert sleeps == [10.0, 25.0, 25.0, 25.0]


class _ThrottledClient:
    """Клиент, отвечающий 429 с заголовком Retry-After, потом 200."""

    def __init__(self, throttled: int, retry_after: str = "7") -> None:
        self._left = throttled
        self.calls = 0
        self.retry_after = retry_after

    async def get(self, url: str, params=None, headers=None):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            import httpx

            response = httpx.Response(429, headers={"Retry-After": self.retry_after}, request=None)
            return response
        return _FakeResp({"ok": True})


async def test_http_retry_429_respects_retry_after(monkeypatch) -> None:
    """Квота провайдера: респект Retry-After вместо тупого backoff.

    Реальный сценарий free tier TonAPI (1 rps/ключ): долбить в 429 — ловить
    бан ключа; провайдер прямо говорит «подожди N секунд».
    """
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _ThrottledClient(throttled=2, retry_after="7")

    response = await http_get_with_retry(
        client, "https://ton.example/v2/blockchain/accounts/x/transactions", max_retries=3,
        retry_delay=1.0, backoff_factor=2.0, max_delay=30.0,
    )
    assert response.status_code == 200
    assert client.calls == 3
    assert sleeps == [7.0, 7.0]


async def test_http_retry_429_caps_retry_after(monkeypatch) -> None:
    """Retry-After на час не замораживает минуточный цикл: потолок max_delay."""
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _ThrottledClient(throttled=1, retry_after="3600")

    await http_get_with_retry(
        client, "https://ton.example/v2/ping", max_retries=2,
        retry_delay=1.0, backoff_factor=2.0, max_delay=30.0,
    )
    assert sleeps == [30.0]


# ---------- /blockchain: аудит-отчёт ----------


async def test_blockchain_diagnostics_reports_pipeline(monkeypatch) -> None:
    """Отчёт виден целиком: курсор, stuck, очередь, сверка, баланс."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    now_iso = "2024-05-01T10:00:00+00:00"
    cursor = int(time.time()) - 300

    async def fake_state():
        return 5_000_000_000, "active", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_state)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_HISTORY_OK", True)

    async with SessionLocal() as session:
        for key, value in (
            (BEAT_KEY, now_iso),
            (SOURCE_KEY, "tonapi"),
            (CURSOR_KEY, str(cursor)),
        ):
            session.add(WatcherState(key=key, value=value))
        session.add(WatcherState(key=STUCK_TX_KEY, value=json.dumps({"abc123": {"fails": 2}})))
        await session.commit()

    try:
        text = await ton_pay.blockchain_diagnostics()
        assert "Блокчейн-контур" in text
        assert "курсор" in text
        assert "Stuck-входящих: 1" in text
        assert "Очередь выплат" in text
        assert "Кошельков verified" in text
        assert "5.0000 Gram" in text
        assert "Сверка истории" in text
        assert "доступна" in text
    finally:
        async with SessionLocal() as session:
            for key in (BEAT_KEY, SOURCE_KEY, CURSOR_KEY, STUCK_TX_KEY):
                row = await session.get(WatcherState, key)
                if row is not None:
                    await session.delete(row)
            await session.commit()


async def test_blockchain_diagnostics_flags_history_down(monkeypatch) -> None:
    """История молчит → отчёт прямо говорит, что повторы выплат заморожены."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def silent():
        return None, None, "none"

    monkeypatch.setattr(ton_pay, "fetch_account_state", silent)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_HISTORY_OK", False)

    text = await ton_pay.blockchain_diagnostics()
    assert "НЕДОСТУПНА" in text
    assert "повторы выплат заморожены" in text
    assert "Баланс казначея: недоступен" in text
