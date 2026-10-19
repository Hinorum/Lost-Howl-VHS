"""Пересборка пула лайтсерверов по серии сбоев (TON_PROVIDER_REBUILD).

Больной узел в pytoniq остаётся «живым» и приоритетным для выбора (высокий
кэш masterchain seqno), а execute_method на ошибку-класс 651 пиры не
перебирает: без пересборки ретраи снова попадают в ту же ноду, и «больное
окно» длится часами (наблюдение mainnet 2026-10-17). Проверяем: счётчик
серии, порог, кулдаун, флаг-выключатель (без него — полный no-op), точки
вызова в отправке (одиночный путь, пачка, чтение seqno цикла) и фактическое
исполнение пересборки в get_wallet.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

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
from app.ton_utils import to_nano

# Класс сбоя, который классификатор считает лайтсерверным (нет пиров):
# ретраи в тот же пул бессмысленны — ровно случай для пересборки.
_DOWN_EXC = RuntimeError("LiteServerError: have no alive peers")


def _reset_provider_state(monkeypatch: pytest.MonkeyPatch, *, rebuild: bool) -> None:
    monkeypatch.setattr(ton_pay.state, "_liteserver_down_streak", 0)
    monkeypatch.setattr(ton_pay.state, "_provider_reset_requested", False)
    monkeypatch.setattr(ton_pay.state, "_last_provider_reset_at", None)
    monkeypatch.setattr(settings, "ton_provider_rebuild", rebuild)


# ---------- Счётчик серии, порог, кулдаун, флаг ----------


def test_flag_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг выключен (боевое состояние): полный no-op — поведение деплоя
    не меняется, пока хранитель не включит флаг осознанно."""
    _reset_provider_state(monkeypatch, rebuild=False)
    for _ in range(5):
        ton_pay.notify_liteserver_down(_DOWN_EXC)
    assert ton_pay.state._liteserver_down_streak == 0
    assert ton_pay.state._provider_reset_requested is False
    assert ton_pay.state._last_provider_reset_at is None


def test_streak_below_threshold_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одиночные сбои — норма больного канала, пул из-за них не трогаем."""
    _reset_provider_state(monkeypatch, rebuild=True)
    ton_pay.notify_liteserver_down(_DOWN_EXC)
    ton_pay.notify_liteserver_down(_DOWN_EXC)
    assert ton_pay.state._liteserver_down_streak == 2
    assert ton_pay.state._provider_reset_requested is False


def test_threshold_requests_rebuild_and_resets_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Порог подряд идущих сбоев: запрос пересборки, серия обнулена, время
    последней пересборки записано (кулдаун для следующих серий)."""
    _reset_provider_state(monkeypatch, rebuild=True)
    for _ in range(ton_pay.state._LITESERVER_REBUILD_AFTER):
        ton_pay.notify_liteserver_down(_DOWN_EXC)
    assert ton_pay.state._provider_reset_requested is True
    assert ton_pay.state._liteserver_down_streak == 0
    assert ton_pay.state._last_provider_reset_at is not None


def test_cooldown_blocks_second_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """При больном на уровне окружения канале пересборка не чаще кулдауна,
    но серия при этом НЕ сбрасывается: как только кулдаун истёк, первый же
    сбой снова просит пересборку, а не ждёт порог заново."""
    _reset_provider_state(monkeypatch, rebuild=True)
    for _ in range(ton_pay.state._LITESERVER_REBUILD_AFTER):
        ton_pay.notify_liteserver_down(_DOWN_EXC)
    # Пересборку «исполнил» get_wallet — запрос снят.
    monkeypatch.setattr(ton_pay.state, "_provider_reset_requested", False)
    monkeypatch.setattr(
        ton_pay.state, "_last_provider_reset_at", datetime.now(UTC) - timedelta(minutes=1)
    )
    for _ in range(ton_pay.state._LITESERVER_REBUILD_AFTER):
        ton_pay.notify_liteserver_down(_DOWN_EXC)
    assert ton_pay.state._provider_reset_requested is False
    monkeypatch.setattr(
        ton_pay.state, "_last_provider_reset_at", datetime.now(UTC) - timedelta(minutes=30)
    )
    ton_pay.notify_liteserver_down(_DOWN_EXC)
    assert ton_pay.state._provider_reset_requested is True


def test_ok_resets_streak_but_keeps_queued_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успех лайтсерверов обрывает серию; уже поставленный запрос пересборки
    удачный сбой не отменяет — он осознанный и дешёвый."""
    _reset_provider_state(monkeypatch, rebuild=True)
    ton_pay.notify_liteserver_down(_DOWN_EXC)
    ton_pay.state._provider_reset_requested = True
    ton_pay.notify_liteserver_ok()
    assert ton_pay.state._liteserver_down_streak == 0
    assert ton_pay.state._provider_reset_requested is True


# ---------- Точки вызова: одиночная отправка ----------


def _setup_treasury(monkeypatch: pytest.MonkeyPatch) -> str:
    """Валидная пара мнемоника/адрес v5r1 testnet + чистое состояние каналов."""
    mnemonic = mnemonic_new(24)
    words = list(mnemonic)
    _, private_key = mnemonic_to_private_key(words)
    pub = private_key_to_public_key(private_key)
    address = ton_pay._wallet_address("v5r1", pub, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "v5r1")
    # URL конфига лайтсерверов из .env разработчика не должен подменять
    # ветку from_testnet_config: тест про пересборку пула, а не про URL.
    monkeypatch.setattr(settings, "liteserver_config_url", "")
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)
    monkeypatch.setattr(ton_pay.state, "_http_channel_engaged_at", None)
    return address


class _SingleWallet:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    async def transfer(self, destination, amount, body=None, state_init=None):
        self.calls.append((destination, amount))
        return 1


async def test_send_transfer_down_notifies_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой чтения/вещания по лайтсерверам: серия учтена, перевод ушёл
    HTTP-каналом (фолбэк не сломан вставкой счётчика)."""
    _setup_treasury(monkeypatch)
    notified = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_down", notified)
    monkeypatch.setattr(ton_pay, "notify_liteserver_ok", MagicMock())

    async def failing_get():
        raise _DOWN_EXC

    monkeypatch.setattr(ton_pay, "_get_wallet", failing_get)
    http = AsyncMock(return_value="bcast:http-1")
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http)

    marker = await ton_pay.send_ton_transfer("0:" + "11" * 32, to_nano(0.1), comment="way:1:prize#1")
    assert marker == "bcast:http-1"
    notified.assert_called_once_with(_DOWN_EXC)
    http.assert_awaited_once()


async def test_send_transfer_success_notifies_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успех лайтсерверов: серия сбоев обрывается (счётчик сбрасывается)."""
    _setup_treasury(monkeypatch)
    ok = MagicMock()
    down = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_ok", ok)
    monkeypatch.setattr(ton_pay, "notify_liteserver_down", down)
    wallet = _SingleWallet()

    async def fake_get():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    marker = await ton_pay.send_ton_transfer("0:" + "22" * 32, to_nano(0.2), comment="way:1:prize#2")
    assert marker and marker.startswith("bcast:")
    ok.assert_called_once_with()
    down.assert_not_called()


# ---------- Точки вызова: пачка ----------


class _BatchWallet:
    """Интерфейс, нужный _send_raw_batch_with_seqno: одна подпись, один
    send_external на всю пачку."""

    def __init__(self):
        self.private_key = b"k" * 32
        self.wallet_id = 698983191
        self.seqnos: list[int] = []

    async def get_seqno(self) -> int:
        return 5

    def create_wallet_internal_message(self, destination, value=0, body=None):
        return ("internal", destination, value)

    def raw_create_transfer_msg(self, private_key, seqno, wallet_id, messages):
        self.seqnos.append(seqno)
        return ("transfer", seqno)

    async def send_external(self, body=None):
        return 1


async def test_send_batch_success_notifies_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_treasury(monkeypatch)
    ok = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_ok", ok)
    wallet = _BatchWallet()

    async def fake_get():
        return wallet

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    transfers = [("0:" + "33" * 32, to_nano(0.1), "way:1:prize#1")]
    marker = await ton_pay.send_ton_transfers_batch(transfers)
    assert marker and marker.startswith("bcast:")
    ok.assert_called_once_with()


async def test_send_batch_down_notifies_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой лайтсерверов на пачке: серия учтена, пачка ушла HTTP-каналом."""
    _setup_treasury(monkeypatch)
    notified = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_down", notified)
    monkeypatch.setattr(ton_pay, "notify_liteserver_ok", MagicMock())

    async def failing_get():
        raise _DOWN_EXC

    monkeypatch.setattr(ton_pay, "_get_wallet", failing_get)
    http = AsyncMock(return_value="bcast:http-batch")
    monkeypatch.setattr(ton_pay, "_send_ton_transfers_http", http)

    transfers = [("0:" + "44" * 32, to_nano(0.1), "way:1:prize#1")]
    marker = await ton_pay.send_ton_transfers_batch(transfers)
    assert marker == "bcast:http-batch"
    notified.assert_called_once_with(_DOWN_EXC)
    http.assert_awaited_once()


# ---------- Точка вызова: seqno цикла диспетчера ----------


async def _seed_one_payout() -> int:
    await ensure_round(77)
    await ensure_player(901)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=77,
            player_id=901,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            network="testnet" if settings.is_testnet else "mainnet",
        )
        session.add(payout)
        await session.flush()
        payout.attempts = 0
        payout.status = "pending"
        await session.commit()
        return payout.id


async def test_cycle_seqno_down_notifies_and_reads_via_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Цикл: seqno лайтсерверами не читается (больная нода не отдаёт стейт) —
    серия учтена, seqno прочитан по HTTP, пачка остаётся пачкой."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "word " * 24)
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + "33" * 32)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 0)
    monkeypatch.setattr(settings, "payout_v5_batch", True)

    async def account_state():
        return 50_000_000_000, "active", "tonapi"

    class _SeqnoDownWallet:
        async def get_seqno(self):
            raise _DOWN_EXC

    async def fake_wallet():
        return _SeqnoDownWallet()

    async def empty_markers() -> set[str]:
        return set()

    async def empty_tx_map() -> dict:
        return {}

    monkeypatch.setattr(ton_pay, "fetch_account_state", account_state)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", empty_tx_map)
    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay, "wallet_supports_batch", lambda wallet: True)
    monkeypatch.setattr(ton_pay, "_http_get_wallet_seqno", AsyncMock(return_value=4242))
    notified = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_down", notified)
    ok = MagicMock()
    monkeypatch.setattr(ton_pay, "notify_liteserver_ok", ok)
    batch = AsyncMock(return_value="bcast:7")
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", batch)
    monkeypatch.setattr(
        "app.ton_pay.dispatch._wait_for_broadcast_memo", AsyncMock(return_value=True)
    )

    payout_id = await _seed_one_payout()
    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        notified.assert_called_once()
        assert notified.call_args.args[0] is _DOWN_EXC
        # Успешного лайтсерверного чтения в этом цикле не было.
        ok.assert_not_called()
        batch.assert_awaited_once()
    finally:
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
            if row is not None:
                await session.delete(row)
            await session.commit()


# ---------- Исполнение пересборки в get_wallet ----------


class _OldProvider:
    def __init__(self):
        self.closed = 0

    async def close_all(self):
        self.closed += 1


class _NewProvider:
    def __init__(self):
        self.started = 0

    async def close_all(self):
        pass

    async def start_up(self):
        self.started += 1


async def test_get_wallet_rebuilds_pool_on_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрошенная пересборка исполнилась: старый пул закрыт, поднят свежий,
    флаг снят (иначе пересборка повторялась бы на каждом обращении)."""
    _setup_treasury(monkeypatch)
    import pytoniq
    from pytoniq.contract.wallets.wallet_v5 import WalletV5R1

    created: list[_NewProvider] = []

    class _Balancer:
        @classmethod
        def from_testnet_config(cls):
            provider = _NewProvider()
            created.append(provider)
            return provider

    new_wallet = object()

    async def fake_from_private_key(cls, provider, private_key, wc=0, network_global_id=None):
        assert isinstance(provider, _NewProvider)
        return new_wallet

    monkeypatch.setattr(pytoniq, "LiteBalancer", _Balancer)
    monkeypatch.setattr(WalletV5R1, "from_private_key", classmethod(fake_from_private_key))

    old = _OldProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", old)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "testnet")
    monkeypatch.setattr(ton_pay.state, "_provider_reset_requested", True)

    wallet = await ton_pay._get_wallet()
    assert wallet is new_wallet
    assert old.closed == 1
    assert len(created) == 1 and created[0].started == 1
    assert ton_pay.state._provider is created[0]
    assert ton_pay.state._wallet_network == "testnet"
    assert ton_pay.state._provider_reset_requested is False


async def test_get_wallet_without_request_returns_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без запроса живой кошелёк той же сети возвращается как есть — пул
    не трогается (поведение прежних деплоев)."""
    _setup_treasury(monkeypatch)
    cached = object()
    old = _OldProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", old)
    monkeypatch.setattr(ton_pay.state, "_wallet", cached)
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "testnet")
    monkeypatch.setattr(ton_pay.state, "_provider_reset_requested", False)

    assert await ton_pay._get_wallet() is cached
    assert old.closed == 0
