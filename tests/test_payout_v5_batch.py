"""Пачка выплат v5r1: N переводов одним внешним сообщением.

Контракт казначея v5r1 пакует действия в out_list одной транзакции: один
seqno на всю пачку, гонки «второй внешний месседж сражается за место» внутри
пачки нет. Здесь — транспортный слой (dispatch/send + http_channel): сборка
списка внутренних сообщений, инвариант _batch_seqno, уход в HTTP-канал при
мёртвых лайтсерверах. Решения диспетчера (анти-дубль, фолбэк на поштучный
путь, удержание строк после таймаута) — в тестах ниже по файлу.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pytoniq_core.crypto.keys import (
    mnemonic_new,
    mnemonic_to_private_key,
    private_key_to_public_key,
)

from app import ton_pay
from app.config import settings
from app.ton_utils import to_nano


class _Resp:
    def __init__(self, *, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        if self._body is not None:
            return self._body
        raise ValueError("no json")


def _enable_testnet_treasury(monkeypatch: pytest.MonkeyPatch) -> str:
    """Казначей testnet: мнемоника + производный v5r1-адрес (сеть не трогается)."""
    words = list(mnemonic_new(24))
    _, private_key = mnemonic_to_private_key(words)
    pub = private_key_to_public_key(private_key)
    address = ton_pay._wallet_address("v5r1", pub, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "v5r1")
    return " ".join(words)


def _v5_v4_wallets() -> tuple[object, object]:
    """Реальные оффлайн-кошельки v5r1 и v4r2 одной мнемоники (без провайдера)."""
    words = list(mnemonic_new(24))
    _, private_key = mnemonic_to_private_key(words)
    pub = private_key_to_public_key(private_key)
    mnemonic = " ".join(words)
    v5_wallet, v5_version = ton_pay.build_offline_wallet(
        mnemonic, ton_pay._wallet_address("v5r1", pub, -3), -3, forced_version="v5r1"
    )
    v4_wallet, v4_version = ton_pay.build_offline_wallet(
        mnemonic, ton_pay._wallet_address("v4r2", pub, -3), -3, forced_version="v4r2"
    )
    assert v5_version == "v5r1" and v4_version == "v4r2"
    return v5_wallet, v4_wallet


def _transfers(count: int = 2) -> list[tuple[str, int, str]]:
    return [
        ("0:" + f"{index + 1:02x}" * 32, to_nano(0.01 * (index + 1)), f"way:7:prize#{index + 1}")
        for index in range(count)
    ]


class _FakeChainWallet:
    """Минимальный контракт подписанта: всё, что уходит в «сеть», — в журнал."""

    private_key = b"\x01" * 32
    wallet_id = 0x7FFFFFFF

    def __init__(self, send_result: int = 1, send_error: Exception | None = None):
        self.send_result = send_result
        self.send_error = send_error
        self.raw_calls: list[tuple[int, int]] = []  # (seqno, сколько сообщений)
        self.internal_calls: list[tuple[object, int]] = []
        self.send_external_count = 0

    def create_wallet_internal_message(self, *, destination, value, body=None):
        self.internal_calls.append((destination, value))
        return {"dest": destination, "value": value}

    def raw_create_transfer_msg(self, *, private_key, seqno, wallet_id, messages):
        self.raw_calls.append((seqno, len(messages)))
        return b"transfer-out-list"

    async def send_external(self, *, body):
        self.send_external_count += 1
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    async def get_seqno(self):
        return 777


def test_wallet_supports_batch_true_for_v5_false_for_v4() -> None:
    """Источник истины о пачке — класс кошелька: v5r1 умеет out_list, v4r2 нет.

    Проверка по версии из настроек была бы косвенной: детект версии — догадка
    о контракте, а подписываем мы конкретным классом pytoniq. v4r2 не должен
    случайно попасть на пачечный путь: его raw-формат пакует максимум 4
    перевода, очередь цикла бывает длиннее.
    """
    v5_wallet, v4_wallet = _v5_v4_wallets()
    assert ton_pay.wallet_supports_batch(v5_wallet) is True
    assert ton_pay.wallet_supports_batch(v4_wallet) is False
    assert ton_pay.wallet_supports_batch(object()) is False


async def test_send_batch_uses_local_seqno_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одна пачка = один seqno + один инкремент батч-счётчика.

    Внутри пачки нет гонки за место в блоке: все действия в одной транзакции
    с одним seqno. Локальный инкремент — только при успехе вещания.
    """
    _enable_testnet_treasury(monkeypatch)
    wallet = _FakeChainWallet()
    transfers = _transfers(2)
    calls: list[str] = []

    async def fake_wallet():
        calls.append("wallet")
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 5)
    monkeypatch.setattr(ton_pay.state, "_http_channel_engaged_at", datetime.now(UTC))

    marker = await ton_pay.send_ton_transfers_batch(transfers)

    assert marker and marker.startswith("bcast:")
    assert wallet.raw_calls == [(5, 2)], "пачка обязана уйти одним внешним сообщением"
    assert [value for _dest, value in wallet.internal_calls] == [
        to_nano(0.01),
        to_nano(0.02),
    ]
    assert wallet.send_external_count == 1
    assert ton_pay.state._batch_seqno == 6
    # Лайтсерверы приняли вещание — флаг HTTP-канала снимается (симметрично
    # одиночному пути), иначе алерт «лайтсерверы недоступны» пережил бы возврат.
    assert ton_pay.state._http_channel_engaged_at is None


async def test_send_batch_outside_cycle_reads_network_seqno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вне цикла (смоук /payout test) свежий seqno читается один раз на пачку.

    Локальный счётчик цикла смоук не подменяет: у него нет батч-контекста.
    """
    _enable_testnet_treasury(monkeypatch)
    wallet = _FakeChainWallet()
    transfers = _transfers(2)

    async def fake_wallet():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)

    marker = await ton_pay.send_ton_transfers_batch(transfers)

    assert marker and marker.startswith("bcast:")
    assert wallet.raw_calls == [(777, 2)]
    assert ton_pay.state._batch_seqno is None


async def test_send_batch_rejected_raises_and_drops_seqno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result != 1 — явный отказ: исключение наружу, протухший seqno не остаётся.

    Диспетчер по этому исключению уйдёт на поштучный фолбэк — пачка не
    помечается sent.
    """
    _enable_testnet_treasury(monkeypatch)
    wallet = _FakeChainWallet(send_result=0)

    async def fake_wallet():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 5)

    with pytest.raises(RuntimeError, match="не приняли пачку"):
        await ton_pay.send_ton_transfers_batch(_transfers(2))
    assert ton_pay.state._batch_seqno is None


async def test_send_batch_no_peers_falls_back_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Мёртвые лайтсерверы — пачка уходит целиком через HTTP-канал."""
    _enable_testnet_treasury(monkeypatch)
    wallet = _FakeChainWallet(send_error=RuntimeError("no alive peers"))
    transfers = _transfers(3)
    http_batch = AsyncMock(return_value="bcast:http")

    async def fake_wallet():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay, "_send_ton_transfers_http", http_batch)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 5)

    result = await ton_pay.send_ton_transfers_batch(transfers)

    assert result == "bcast:http"
    http_batch.assert_awaited_once_with(transfers)
    assert ton_pay.state._batch_seqno is None  # в HTTP-путь счётчик не тащим


async def test_send_batch_non_liteserver_error_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка сборки/пары мнемоника-адрес — не повод для HTTP: текст как есть."""
    _enable_testnet_treasury(monkeypatch)
    wallet = _FakeChainWallet(send_error=ValueError("Address не собирается"))
    http_batch = AsyncMock(return_value="bcast:http")

    async def fake_wallet():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay, "_send_ton_transfers_http", http_batch)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)

    with pytest.raises(ValueError, match="Address"):
        await ton_pay.send_ton_transfers_batch(_transfers(2))
    http_batch.assert_not_awaited()


async def test_send_batch_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """TON выключен — контракт функции как у одиночного пути: None без сети."""
    monkeypatch.setattr(settings, "ton_enabled", False)

    def boom():
        raise AssertionError("кошелёк не должен запрашиваться")

    monkeypatch.setattr(ton_pay, "_get_wallet", boom)
    assert await ton_pay.send_ton_transfers_batch(_transfers(2)) is None


async def test_send_batch_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая пачка — ошибка вызова, а не «ничего не ушло, ну и ладно»."""
    with pytest.raises(ValueError, match="пустая пачка"):
        await ton_pay.send_ton_transfers_batch([])


# ---------- HTTP-канал: пачечная ветка ----------


async def test_send_batch_http_uses_local_seqno_and_sends_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP-пачка: один Boc со ВСЕМИ сообщениями, локальный seqno, один инкремент."""
    _enable_testnet_treasury(monkeypatch)
    seen: dict = {}

    async def fake_broadcast(wallet, seqno, internal_msg):
        seen["seqno"] = seqno
        seen["messages"] = internal_msg

    monkeypatch.setattr(ton_pay, "_http_broadcast_external", fake_broadcast)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 9)
    monkeypatch.setattr(ton_pay.state, "_http_channel_engaged_at", None)

    transfers = _transfers(2)
    marker = await ton_pay._send_ton_transfers_http(transfers)

    assert marker and marker.startswith("bcast:")
    assert seen["seqno"] == 9
    assert isinstance(seen["messages"], list)
    assert len(seen["messages"]) == 2, "оба перевода обязаны ехать одним Boc"
    assert ton_pay.state._batch_seqno == 10
    assert ton_pay.state._http_channel_engaged_at is not None


async def test_send_batch_http_fetches_seqno_outside_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вне цикла seqno берётся из toncenter; после успеха счётчик наращивается."""
    _enable_testnet_treasury(monkeypatch)
    seen: dict = {}

    async def fake_get_seqno(wallet):
        return 7

    async def fake_broadcast(wallet, seqno, internal_msg):
        seen["seqno"] = seqno

    monkeypatch.setattr(ton_pay.http_channel, "http_get_wallet_seqno", fake_get_seqno)
    monkeypatch.setattr(ton_pay, "_http_broadcast_external", fake_broadcast)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)

    marker = await ton_pay._send_ton_transfers_http(_transfers(2))
    assert marker and marker.startswith("bcast:")
    assert seen["seqno"] == 7
    assert ton_pay.state._batch_seqno == 8


async def test_http_broadcast_external_accepts_message_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Список сообщений — тот же путь: один внешний Boc и один sendBoc.

    Проверка с РЕАЛЬНЫМ оффлайн-кошельком v5r1: pytoniq пакует два действия
    в out_list; ошибка формата вылезла бы здесь, а не в бою.
    """
    _enable_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()
    posted: list[dict] = []

    async def fake_post(client, url, *, json=None, headers=None, timeout=None, **kw):
        posted.append(json)
        return _Resp(body={"ok": True, "result": {"@type": "ok"}})

    monkeypatch.setattr(ton_pay, "http_post_with_retry", fake_post)
    messages = [
        wallet.create_wallet_internal_message(destination=wallet.address, value=to_nano(0.01)),
        wallet.create_wallet_internal_message(destination=wallet.address, value=to_nano(0.02)),
    ]
    await ton_pay._http_broadcast_external(wallet, seqno=1, internal_msg=messages)

    assert len(posted) == 1, "пачка — строго один sendBoc"
    assert posted[0]["method"] == "sendBoc"
    assert posted[0]["params"]["boc"]
