"""Диагностика казначея (/treasury): баланс, сверка пары, очередь.

Цель — инцидент «выплаты не уходят» разбирается одним сообщением бота
без раскрытия мнемоники и без логов.

Здесь же — риск-место чтения баланса: индексатор отдаёт 200 без поля
balance (тело ошибки, смена формы ответа). Раньше это превращалось в
честный ноль: /treasury писал «0.0000 Gram», /blockchain то же, а сверка
зеркала получала chain_balance=0 и поднимала тревогу на всю казну.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

from app import ton_pay
from app.config import settings
from app.handlers import cmd_treasury


def _derived_address(version: str, mnemonic: list[str], network_global_id: int) -> str:
    _, private_key = mnemonic_to_private_key(mnemonic)
    public_key = private_key_to_public_key(private_key)
    return ton_pay._wallet_address(version, public_key, network_global_id)


async def test_diagnostics_reports_balance_and_pair_match(monkeypatch) -> None:
    words = mnemonic_new(24)
    address = _derived_address("v4r2", words, -3)  # тестнет-глобаль
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "owner_wallet_address", "")

    async def fake_state():
        return 4_213_000_000, "active", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_state)
    text = await ton_pay.treasury_diagnostics()
    assert "Казначей (testnet)" in text
    assert "4.2130 Gram" in text
    assert "v4r2 ✓" in text  # пара мнемоника/адрес сходится
    assert "не задан" in text and "OWNER_WALLET_ADDRESS" in text


async def test_diagnostics_flags_pair_mismatch(monkeypatch) -> None:
    words = mnemonic_new(24)
    stranger = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", stranger)

    async def silent():
        return None, None, "none"

    monkeypatch.setattr(ton_pay, "fetch_account_state", silent)
    text = await ton_pay.treasury_diagnostics()
    assert "не дают настроенный адрес" in text
    assert "Баланс: недоступен" in text


async def test_diagnostics_warns_on_empty_balance(monkeypatch) -> None:
    words = mnemonic_new(24)
    address = _derived_address("v5r1", words, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "owner_wallet_address", address)

    async def empty_balance():
        return 0, "uninit", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", empty_balance)
    text = await ton_pay.treasury_diagnostics()
    assert "@testgiver_ton_bot" in text


class _Resp:
    """Ответ индексатора: 200 + произвольное тело (в т.ч. без balance)."""

    def __init__(self, body: dict | str | None, status_code: int = 200):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _indexers(monkeypatch, *, tonapi: _Resp, toncenter: _Resp) -> list[str]:
    """Подменяет HTTP-слой: тонкий ответ TonAPI + ответ Toncenter по URL."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + "ab" * 32)
    monkeypatch.setattr(settings, "treasury_address", "")
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: object())
    asked: list[str] = []

    async def fake_get(client, url, *, headers=None, params=None, **kwargs):
        asked.append(url)
        return toncenter if "toncenter" in url else tonapi

    monkeypatch.setattr(ton_pay, "http_get_with_retry", fake_get)
    return asked


# ---------- Риск-место: частичный ответ индексатора ----------


async def test_partial_tonapi_response_falls_back_to_toncenter(monkeypatch) -> None:
    """TonAPI ответил 200 без balance → идём к Toncenter, а не рисуем ноль.

    Тело ошибки с кодом 200 (прокси/лимитер/смена формы ответа) раньше
    превращалось в balance=0: /treasury писал «0.0000 Gram» и советовал
    пополнить казну, /blockchain — то же, а сверка зеркала получала
    chain_balance=0 и поднимала тревогу на всю сумму казны.
    """
    asked = _indexers(
        monkeypatch,
        tonapi=_Resp({"error": "Too many requests", "code": 429}),
        toncenter=_Resp({"balance": "4213000000"}),
    )
    balance, status, source = await ton_pay.fetch_account_state()
    assert (balance, status, source) == (4_213_000_000, None, "toncenter")
    assert len(asked) == 2, "Toncenter обязан быть опробован до отказа"


async def test_partial_tonapi_body_without_field_still_reports_none(monkeypatch) -> None:
    """Оба индексатора ответили без числового balance → «не знаю», не ноль."""
    _indexers(
        monkeypatch,
        tonapi=_Resp({"status": "active"}),  # поля balance нет вовсе
        toncenter=_Resp({"code": 500, "message": "internal"}),
    )
    balance, status, source = await ton_pay.fetch_account_state()
    assert (balance, status, source) == (None, None, "none")


async def test_real_zero_balance_is_not_mistaken_for_unavailable(monkeypatch) -> None:
    """Настоящий ноль приходит строкой "0" — это ноль, а не «не знаю».

    Различение обязано остаться: иначе пустая казна перестанет получать
    подсказку про @testgiver_ton_bot.
    """
    _indexers(
        monkeypatch,
        tonapi=_Resp({"balance": "0", "status": "uninit"}),
        toncenter=_Resp({"balance": "1"}),
    )
    balance, status, source = await ton_pay.fetch_account_state()
    assert (balance, status, source) == (0, "uninit", "tonapi")


async def test_unparsable_balance_is_treated_as_unavailable(monkeypatch) -> None:
    """balance = null/""/мусор → не число → фолбэк, а не 0."""
    for body in ({"balance": None}, {"balance": ""}, {"balance": "много"}):
        _indexers(monkeypatch, tonapi=_Resp(body), toncenter=_Resp({"balance": "777"}))
        assert await ton_pay.fetch_account_state() == (777, None, "toncenter"), body


async def test_diagnostics_says_unavailable_instead_of_zero(monkeypatch) -> None:
    """Частичный ответ индексатора не должен пугать хранителя нулём."""
    words = mnemonic_new(24)
    address = _derived_address("v4r2", words, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "owner_wallet_address", "")

    async def silent():
        return None, None, "none"

    monkeypatch.setattr(ton_pay, "fetch_account_state", silent)
    text = await ton_pay.treasury_diagnostics()
    assert "Баланс: недоступен" in text
    assert "0.0000 Gram" not in text
    assert "@testgiver_ton_bot" not in text


async def test_cmd_treasury_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")

    outsider = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await cmd_treasury(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]

    admin = SimpleNamespace(
        from_user=SimpleNamespace(id=4242),
        answer=AsyncMock(),
    )
    async def fake_diag() -> str:
        return "🏛 Казначей (testnet)"

    monkeypatch.setattr("app.ton_pay.treasury_diagnostics", fake_diag)
    await cmd_treasury(admin)
    admin.answer.assert_awaited_once()


def test_pair_check_detects_invalid_mnemonic(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "один два три")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    assert "неполная" in ton_pay.treasury_pair_check_text()
