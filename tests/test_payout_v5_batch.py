"""Пачка выплат v5r1: N переводов одним внешним сообщением.

Контракт казначея v5r1 пакует действия в out_list одной транзакции: один
seqno на всю пачку, гонки «второй внешний месседж сражается за место» внутри
пачки нет. Здесь — транспортный слой (dispatch/send + http_channel): сборка
списка внутренних сообщений, инвариант _batch_seqno, уход в HTTP-канал при
мёртвых лайтсерверах. Решения диспетчера (анти-дубль, фолбэк на поштучный
путь, удержание строк после таймаута) — в тестах ниже по файлу.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import ensure_player, ensure_round
from pytoniq_core.crypto.keys import (
    mnemonic_new,
    mnemonic_to_private_key,
    private_key_to_public_key,
)

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout
from app.ton_pay.dispatch.send import BatchRejected
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


# ---------- Диспетчер: пачечная ветка за флагом PAYOUT_V5_BATCH ----------


async def _seed_payouts(count: int, attempts: int = 0) -> list[int]:
    """Несколько выплат подряд — чтобы проверить поведение ПАЧКИ цикла."""
    ids: list[int] = []
    await ensure_round(7)
    for index in range(count):
        await ensure_player(200 + index)
        async with SessionLocal() as session:
            payout = Payout(
                round_id=7,
                player_id=200 + index,
                kind="prize",
                amount_nanotons=500_000_000,
                dest_address="0:" + os.urandom(32).hex(),
                # Сеть строки должна совпадать с сетью цикла (дефолт модели —
                # mainnet, а тестовый цикл гоняем на testnet).
                network="testnet" if settings.is_testnet else "mainnet",
            )
            session.add(payout)
            await session.flush()
            payout.attempts = attempts
            payout.status = "pending"
            await session.commit()
            ids.append(payout.id)
    return ids


async def _drop_payouts(ids: list[int]) -> None:
    async with SessionLocal() as session:
        for payout_id in ids:
            row = await session.get(Payout, payout_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


async def _payout_rows(ids: list[int]) -> list[Payout]:
    async with SessionLocal() as session:
        return [await session.get(Payout, payout_id) for payout_id in ids]


def _setup_batch_cycle(monkeypatch: pytest.MonkeyPatch, *, capable: bool = True) -> None:
    """Казна v5r1 и сеть диспетчера без живых лайтсерверов/индексаторов."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "word " * 24)
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + "33" * 32)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 0)

    async def account_state():
        return 50_000_000_000, "active", "tonapi"

    async def empty_markers() -> set[str]:
        return set()

    class _FakeWallet:
        async def get_seqno(self):
            return 4242

    async def fake_wallet():
        return _FakeWallet()

    monkeypatch.setattr(ton_pay, "fetch_account_state", account_state)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay, "wallet_supports_batch", lambda wallet: capable)


async def test_dispatch_batch_sends_all_rows_in_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Флаг включён, казна v5r1: весь цикл — одним внешним сообщением.

    Строки фиксируются sent сразу: пачка подписана одним seqno, ждать memo
    «предыдущего перевода» внутри неё нечего, а поштучный путь не зовётся
    вовсе — иначе смысл батча теряется.
    """
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(3)
    batch = AsyncMock(return_value="bcast:7")
    transfer = AsyncMock(return_value="bcast:old")
    wait = AsyncMock(return_value=True)
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", wait)
    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)

        assert sent == 3
        batch.assert_awaited_once()
        payload = batch.await_args.args[0]
        assert len(payload) == 3
        assert {comment for _dest, _nano, comment in payload} == {
            f"way:7:prize#{payout_id}" for payout_id in ids
        }
        transfer.assert_not_awaited()
        wait.assert_not_awaited()
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["sent"] * 3
        assert all(row.tx_hash == "bcast:7" and row.attempts == 0 for row in rows)
        assert all(row.sent_at is not None and row.last_error is None for row in rows)
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_flag_off_uses_per_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг выключен (боевое состояние) — прежний поштучный путь без изменений."""
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", False)
    ids = await _seed_payouts(2)
    batch = AsyncMock(return_value="bcast:7")
    transfer = AsyncMock(return_value="bcast:old")
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(
        "app.ton_pay.dispatch._wait_for_broadcast_memo", AsyncMock(return_value=True)
    )
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        batch.assert_not_awaited()
        assert transfer.await_count == 2
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["sent", "sent"]
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_skipped_for_v4_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """v4r2 не получает пачечный путь даже с включённым флагом.

    Его raw-формат пакует максимум 4 перевода, а очередь длиннее — источник
    истины о возможностях контракта, а не строка настроек.
    """
    _setup_batch_cycle(monkeypatch, capable=False)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(1)
    batch = AsyncMock(return_value="bcast:7")
    transfer = AsyncMock(return_value="bcast:old")
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        batch.assert_not_awaited()
        assert transfer.await_count == 1
        rows = await _payout_rows(ids)
        assert rows[0].status == "sent"
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_rejected_falls_back_to_row_by_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Явный отказ пачки (BatchRejected) — строки уходят поштучно в этом цикле.

    «Результат != 1» и ошибки сборки доказывают «в сеть не ушло»: фолбэк
    безопасен, и деньги не ждут следующего такта из-за одной плохой строки
    (атомарность v5r1: одно плохое действие отменяет всю пачку).
    """
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(2)
    batch = AsyncMock(
        side_effect=BatchRejected("Лайтсерверы не приняли пачку (результат 0)")
    )
    transfer = AsyncMock(return_value="bcast:old")
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(
        "app.ton_pay.dispatch._wait_for_broadcast_memo", AsyncMock(return_value=True)
    )
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)

        batch.assert_awaited_once()
        assert transfer.await_count == 2
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["sent", "sent"]
        assert all(row.tx_hash == "bcast:old" for row in rows)
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_timeout_marks_found_rows_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймаут вещания пачки, но memo нашлись — sent без повтора, как одиночный путь."""
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(3)
    batch = AsyncMock(side_effect=asyncio.TimeoutError)
    transfer = AsyncMock(return_value="bcast:old")

    async def tx_map(targets):
        return {target: f"tx:{target}" for target in targets}

    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", tx_map)
    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)

        assert sent == 3
        batch.assert_awaited_once()
        transfer.assert_not_awaited()
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["sent"] * 3
        assert all(row.tx_hash is None and row.attempts == 0 for row in rows)
        assert all(row.last_error is None for row in rows)
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_timeout_holds_rows_until_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймаут без memo: строки pending, в цикле НЕ повторяются; маркеры
    следующего цикла фиксируют sent без повтора (анти-дубль)."""
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(2)
    batch = AsyncMock(side_effect=asyncio.TimeoutError)
    transfer = AsyncMock(return_value="bcast:old")

    async def no_history(targets):
        return {}

    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", no_history)
    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)

        assert sent == 0
        batch.assert_awaited_once()
        transfer.assert_not_awaited()
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["pending", "pending"]
        assert all(row.attempts == 1 for row in rows)
        assert all("неясн" in (row.last_error or "") for row in rows)

        # Цепочка догнала: memo появились — второй цикл фиксирует sent сам.
        async def markers_ok() -> set[str]:
            return {f"way:7:prize#{payout_id}" for payout_id in ids}

        monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers_ok)
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert batch.await_count == 1, "пачка не должна перевешиваться"
        transfer.assert_not_awaited()
        rows = await _payout_rows(ids)
        assert [row.status for row in rows] == ["sent", "sent"]
        assert all(row.tx_hash is None for row in rows)
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_unexpected_error_also_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неклассифицированный сбой пачечного канала — консервативно «неясно».

    BatchRejected — единственный сигнал «точно не ушло». Всё прочее (сеть
    оборвалась вне классификации) не даёт права на повтор в цикле: строки
    удерживаются и решаются по memo.
    """
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    ids = await _seed_payouts(1)
    batch = AsyncMock(side_effect=RuntimeError("странный обрыв send_external"))
    transfer = AsyncMock(return_value="bcast:old")

    async def no_history(targets):
        return {}

    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", no_history)
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)

        batch.assert_awaited_once()
        transfer.assert_not_awaited()
        rows = await _payout_rows(ids)
        assert rows[0].status == "pending"
        assert "неясн" in (rows[0].last_error or "")
        assert rows[0].attempts == 1
    finally:
        await _drop_payouts(ids)


async def test_dispatch_batch_excludes_frozen_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор с недоступной историей в пачку не берётся — его разбирает заморозка."""
    _setup_batch_cycle(monkeypatch)
    monkeypatch.setattr(settings, "payout_v5_batch", True)
    monkeypatch.setattr(settings, "payout_max_attempts", 5)
    repeat_id = (await _seed_payouts(1, attempts=1))[0]
    fresh_id = (await _seed_payouts(1))[0]
    ids = [repeat_id, fresh_id]
    batch = AsyncMock(return_value="bcast:7")
    transfer = AsyncMock(return_value="bcast:old")

    async def markers_history_down() -> set[str]:
        # Так ведёт себя настоящий fetch_broadcast_markers, когда оба
        # провайдера не ответили: пусто + флаг истории недоступен.
        ton_pay.state._RECONCILE_HISTORY_OK = False
        return set()

    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers_history_down)
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)

        batch.assert_awaited_once()
        payload = batch.await_args.args[0]
        assert len(payload) == 1, "замороженный повтор не должен ехать в пачке"
        assert payload[0][2] == f"way:7:prize#{fresh_id}"
        rows = await _payout_rows(ids)
        assert rows[0].status == "pending", "повтор ждёт возвращения истории"
        assert rows[0].attempts == 1, "заморозка не тратит попытку клейма"
        assert "история" in (rows[0].last_error or "")
        assert rows[1].status == "sent"
    finally:
        await _drop_payouts(ids)


async def test_wait_for_batch_memos_is_state_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    """Целевой скан пачки не переписывает флаги полноты истории казначея.

    Скан с ранним стопом неполон по построению: его «не найдено» не имеет
    права отправить соседние строки в заморозку/повтор — флаги возвращаются
    как были, даже когда провайдер по ходу упал.
    """
    from app.ton_pay.dispatch import _wait_for_batch_memos

    class _Row:
        def __init__(self, payout_id: int) -> None:
            self.id = payout_id

    rows = [(_Row(1), ("way:7:prize#1",)), (_Row(2), ("way:7:prize#2",))]
    calls = {"n": 0}

    async def tx_map(targets):
        calls["n"] += 1
        ton_pay.state._RECONCILE_HISTORY_OK = False
        ton_pay.state._RECONCILE_WINDOW_COMPLETE = False
        return {target: "tx" for target in targets}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", tx_map)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_HISTORY_OK", True)
    monkeypatch.setattr(ton_pay.state, "_RECONCILE_WINDOW_COMPLETE", True)

    found = await _wait_for_batch_memos(rows, 0)

    assert found == {1, 2}
    assert calls["n"] == 1, "ранний стоп: все найдены — второй скан не нужен"
    assert ton_pay.state._RECONCILE_HISTORY_OK is True
    assert ton_pay.state._RECONCILE_WINDOW_COMPLETE is True
