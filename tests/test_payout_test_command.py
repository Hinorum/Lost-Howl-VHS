"""Холостой перевод `/payout test`: предполёт без движения денег, отправка только по confirm.

Пункт чек-листа «HTTP-фолбэк на mainnet» / «холостой перевод» закрывается этой
командой, поэтому здесь проверяется именно её контракт: деньги не уходят без
явного confirm, назначение — только сам кошелёк казны, отправка идёт под локом
диспетчера, а смерть на любом шаге превращается в понятный ответ хранителю, а
не в тихую неудачу.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ton_pay
from app.config import settings
from app.handlers.payout import _parse_test_args, cmd_payout
from app.ton_utils import to_nano

ADMIN = 424242
TREASURY = "0:" + "33" * 32


def make_message(text: str, uid: int = ADMIN) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text=text,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )


def said(message: SimpleNamespace) -> str:
    return message.answer.call_args.args[0]


def _patch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сеть из предполёта: оффлайн-кошелёк, баланс, seqno — без реальных запросов."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(ton_pay, "build_offline_treasury_wallet", lambda: (object(), "v5r1"))

    async def account_state():
        return 1_500_000_000, "active", "tonapi"

    async def seqno(wallet):
        return 42

    monkeypatch.setattr(ton_pay, "fetch_account_state", account_state)
    monkeypatch.setattr(ton_pay, "http_get_wallet_seqno", seqno)


def _patch_history(monkeypatch: pytest.MonkeyPatch, tx: str | None) -> None:
    """История исходящих: tx — хеш, который найдётся по memo (None — не найдётся)."""

    async def tx_map(targets):
        if tx is None or not targets:
            return {}
        return {next(iter(targets)): tx}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", tx_map)
    monkeypatch.setattr("app.handlers.payout._TEST_POLL_SECONDS", 0)


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["abc"],
        ["0"],
        ["-1"],
        ["nan"],
        ["inf"],
        ["0.06"],
        ["0.0000000001"],
        ["0.001", "nope"],
    ],
)
def test_parse_refuses_anything_suspicious(tokens: list[str]) -> None:
    """Любой непонятный аргумент — отказ: команда шлёт реальные деньги, и
    опечатка в confirm обязана остановить до отправки, а не после."""
    with pytest.raises(ValueError):
        _parse_test_args(tokens)


def test_parse_accepts_case_insensitive_flags() -> None:
    assert _parse_test_args(["0.001"]) == (Decimal("0.001"), False, False)
    assert _parse_test_args(["0.001", "confirm", "http"]) == (Decimal("0.001"), True, True)
    assert _parse_test_args(["0,001", "HTTP", "Confirm"]) == (Decimal("0.001"), True, True)


async def test_outsider_cannot_reach_test_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """До любого сетевого шага: чужой не получает ни предполёта, ни отправки.

    ADMIN_IDS задаём сами: CI ставит ADMIN_IDS=1, и на нём id=1 был бы
    хранителем — тест обязан не зависеть от окружения (тот же приём, что в
    tests/test_treasury_adjust.py).
    """
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))

    def boom():
        raise AssertionError("сеть не должна трогаться для чужого")

    monkeypatch.setattr(ton_pay, "build_offline_treasury_wallet", boom)
    message = make_message("/payout test 0.001 confirm", uid=1)
    await cmd_payout(message)
    assert "только для хранителя" in said(message)


@pytest.mark.parametrize(
    "text",
    ["/payout test", "/payout test 1", "/payout test 0.06", "/payout test 0.001 nope"],
)
async def test_bad_invocation_shows_usage_without_network(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Ошибка разбора не доходит до предполёта: ни один сетевой шаг не зовётся."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))

    def boom():
        raise AssertionError("сеть не должна трогаться при ошибке разбора")

    monkeypatch.setattr(ton_pay, "build_offline_treasury_wallet", boom)
    message = make_message(text)
    await cmd_payout(message)
    assert "Формат" in said(message)
    assert "/payout test" in said(message)


async def test_preflight_reports_everything_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без confirm — только предполёт: пара мнемоник/адрес, баланс, seqno и
    явное «ничего не отправлено»."""
    _patch_preflight(monkeypatch)
    sent = False

    async def send(dest, nano, comment):
        nonlocal sent
        sent = True
        return "bcast:1"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    message = make_message("/payout test 0.001")
    await cmd_payout(message)

    text = said(message)
    assert not sent, "без confirm деньги не должны двигаться"
    assert "Пара мнемоник/адрес: v5r1 ✓" in text
    assert "Баланс: 1.5000 Gram" in text
    assert "seqno (runGetMethod по HTTP): 42 ✓" in text
    assert "сам кошелёк казны" in text
    assert "Ничего не отправлено" in text
    assert "confirm" in text


async def test_confirm_sends_self_transfer_and_reports_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm → перевод самому себе ровно на запрошенную сумму, memo из
    истории превращается в хеш транзакции."""
    _patch_preflight(monkeypatch)
    _patch_history(monkeypatch, "0xdeadbeef")
    sent: dict = {}

    async def send(dest, nano, comment):
        sent.update(dest=dest, nano=nano, comment=comment)
        return "bcast:1700000000"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    message = make_message("/payout test 0.001 confirm")
    await cmd_payout(message)

    text = said(message)
    assert sent["dest"] == TREASURY, "назначение — только сам кошелёк казны"
    assert sent["nano"] == to_nano(0.001)
    assert sent["comment"].startswith("payout-test ")
    assert "0xdeadbeef" in text
    assert "self-переводы" in text


async def test_send_happens_under_dispatch_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отправка вне лока диспетчера могла бы взять тот же seqno, что и пачка
    выплат, — второй перевод молча потерялся бы."""
    _patch_preflight(monkeypatch)
    _patch_history(monkeypatch, "0x1")
    state = {"locked": False}

    @asynccontextmanager
    async def fake_lock():
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    async def send(dest, nano, comment):
        assert state["locked"], "отправка должна идти под локом диспетчера"
        return "bcast:1"

    monkeypatch.setattr(ton_pay, "dispatch_lock", fake_lock)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    await cmd_payout(make_message("/payout test 0.001 confirm"))
    assert not state["locked"], "лок должен быть отпущен"


async def test_forced_http_channel_and_flag_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`http` — принудительная репетиция фолбэка: шлёт HTTP-каналом, обычный
    путь не трогает, а флаг «канал задействован» возвращается как был —
    репетиция не должна выглядеть диспетчеру как деградация."""
    _patch_preflight(monkeypatch)
    _patch_history(monkeypatch, "0x2")
    called = {"http": 0, "auto": 0}
    before = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    async def http_send(dest, nano, comment):
        called["http"] += 1
        return "bcast:2"

    async def auto_send(dest, nano, comment):
        called["auto"] += 1
        return "bcast:3"

    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_send)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", auto_send)
    monkeypatch.setattr(ton_pay.state, "_http_channel_engaged_at", before)

    await cmd_payout(make_message("/payout test 0.001 confirm http"))

    assert called == {"http": 1, "auto": 0}
    assert ton_pay.state._http_channel_engaged_at is before, (
        "флаг должен вернуться к исходному значению: принудительная репетиция — "
        "не переключение канала"
    )

    preflight = make_message("/payout test 0.001 http")
    await cmd_payout(preflight)
    assert "HTTP принудительно" in said(preflight)


async def test_memo_not_found_is_reported_as_confirmation_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memo не появился в истории — это риск sent vs confirmed, и хранитель
    должен увидеть его словами, а не пустым ответом."""
    _patch_preflight(monkeypatch)
    _patch_history(monkeypatch, None)

    async def send(dest, nano, comment):
        return "bcast:4"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    message = make_message("/payout test 0.001 confirm")
    await cmd_payout(message)

    text = said(message)
    assert "не нашёлся" in text
    assert "sent vs confirmed" in text


async def test_send_failure_reaches_the_keeper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реальная ошибка отправки не тонет в логах: причина в ответе."""
    _patch_preflight(monkeypatch)

    async def send(dest, nano, comment):
        raise RuntimeError("Лайтсерверы не приняли перевод (результат -1)")

    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    message = make_message("/payout test 0.001 confirm")
    await cmd_payout(message)

    text = said(message)
    assert "Отправка не удалась" in text
    assert "результат -1" in text


async def test_ton_disabled_is_reported_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_ton_transfer вернул None (TON выключен / нет мнемоники) — хранитель
    должен узнать причину, а не увидеть «успех» без отправки."""
    _patch_preflight(monkeypatch)

    async def send(dest, nano, comment):
        return None

    monkeypatch.setattr(ton_pay, "send_ton_transfer", send)
    message = make_message("/payout test 0.001 confirm")
    await cmd_payout(message)

    assert "Не отправлено" in said(message)
