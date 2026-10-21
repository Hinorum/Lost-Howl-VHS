"""Адрес должен быть из той же сети, что и игра.

У v5-кошельков сеть зашита в data cell, то есть тестнетовый и mainnet-овый
адреса — РАЗНЫЕ аккаунты. Адрес чужой сети, привязанный в базе, вёл бы себя
тихо: переводы с него в сети бота не видны (кошелёк не подтверждается, ставки
не засчитаются), а приз ждал бы в очереди выплат.

Разбор сети — по тегу user-friendly адреса: бит 0x80 даёт префиксы 0Q/kQ
(тестнет), остальные — EQ/UQ (mainnet). Raw-форма (wc:hex) сеть не несёт,
поэтому для неё сеть неизвестна и проверка её не отвергает.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.db import SessionLocal, init_db
from app.handlers import cmd_wallet, on_private_fallback
from app.models import WalletDialog
from app.ton_utils import address_network, friendly_address

# Валидные user-friendly адреса (CRC16 верный) — сгенерированы
# friendly_address() из raw-форм.
MAINNET_ADDRESS = friendly_address("0:" + "ab" * 32)
TESTNET_ADDRESS = friendly_address("0:" + "ab" * 32, testnet=True)
# Другой аккаунт владельца — чтобы не срабатывала проверка «владелец = казна».
OWNER_ADDRESS = friendly_address("0:" + "cd" * 32)

_uid = 860_000


def next_uid() -> int:
    global _uid
    _uid += 1
    return _uid


def make_message(uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid, username=f"u{uid}", first_name="Тест"),
        text=text,
        answer=AsyncMock(),
        answer_media_group=AsyncMock(),
    )


async def _dialog_active(uid: int) -> bool:
    async with SessionLocal() as session:
        return await session.get(WalletDialog, uid) is not None


@pytest.fixture(scope="module", autouse=True)
async def _handlers_db():
    await init_db()
    yield


@pytest.fixture(autouse=True)
async def _wipe_dialogs_after():
    yield
    async with SessionLocal() as session:
        await session.execute(WalletDialog.__table__.delete())
        await session.commit()


def test_address_network_reads_tag() -> None:
    """Сеть читается из тега: 0Q/kQ — тестнет, EQ/UQ — mainnet."""
    assert address_network(MAINNET_ADDRESS) == "mainnet"
    assert address_network(TESTNET_ADDRESS) == "testnet"
    assert address_network("EQ" + "A" * 46) == "mainnet"
    assert address_network("UQ" + "A" * 46) == "mainnet"
    assert address_network("kQ" + "A" * 46) == "testnet"
    assert address_network("0Q" + "A" * 46) == "testnet"


def test_address_network_unknown_for_raw_form() -> None:
    """Raw-форма сеть не несёт — по ней сеть не определить (и не отвергать)."""
    assert address_network("0:" + "ab" * 32) is None
    assert address_network("") is None
    assert address_network("мусор") is None


async def test_bind_rejects_address_of_another_network(monkeypatch) -> None:
    """Адрес чужой сети не привязывается, а игроку объясняют почему."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    uid = next_uid()
    await cmd_wallet(make_message(uid, "/wallet"))
    message = make_message(uid, TESTNET_ADDRESS)
    await on_private_fallback(message)

    text = message.answer.call_args.args[0]
    assert "testnet" in text
    assert "mainnet" in text
    # Диалог остаётся открытым: игрок может прислать адрес правильной сети.
    assert await _dialog_active(uid)


async def test_bind_accepts_address_of_current_network(monkeypatch) -> None:
    """Адрес своей сети привязывается как обычно."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    uid = next_uid()
    await cmd_wallet(make_message(uid, "/wallet"))
    message = make_message(uid, MAINNET_ADDRESS)
    await on_private_fallback(message)

    assert "привязан" in message.answer.call_args.args[0]
    assert not await _dialog_active(uid)


async def test_bind_on_testnet_rejects_mainnet_address(monkeypatch) -> None:
    """Правило симметрично: на тестнете адрес mainnet тоже чужой."""
    monkeypatch.setattr(settings, "ton_network", "testnet")
    uid = next_uid()
    await cmd_wallet(make_message(uid, "/wallet"))
    message = make_message(uid, MAINNET_ADDRESS)
    await on_private_fallback(message)

    text = message.answer.call_args.args[0]
    assert "mainnet" in text
    assert "testnet" in text
    assert await _dialog_active(uid)


def test_startup_flags_own_address_of_another_network(monkeypatch) -> None:
    """Адрес казны или кошелька владельца чужой сети — проблема на старте.

    Молча он вёл бы к тому, что выплаты уходят в никуда, а переводы с адреса
    не видны в сети бота: диагностировать это по логам почти невозможно.
    """
    from app.main import validate_config

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", TESTNET_ADDRESS)
    # Другой аккаунт, иначе сработала бы не связанная с сетью проверка
    # «кошелёк владельца совпадает с казной».
    monkeypatch.setattr(settings, "owner_wallet_address", OWNER_ADDRESS)

    problems = validate_config()
    assert any("TREASURY_ADDRESS" in problem for problem in problems), problems
    assert not any("OWNER_WALLET_ADDRESS" in problem for problem in problems), problems
