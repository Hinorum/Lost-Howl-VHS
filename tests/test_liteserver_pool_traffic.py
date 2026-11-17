"""Трафик Render: лайтсерверный пул не должен жить постоянно.

Инцидент 2026-11: бот на free-плане Render съел 15 ГБ трафика за 12 суток при
лимите 5 ГБ/мес. Причина — не в боте, а в pytoniq: каждый LiteClient держит
фоновый block_updater и на КАЖДОМ мастерчейн-блоке (~5 с) тянет getAllShardsInfo
(конфиг шардов сети с доказательством). В mainnet-конфиге 18 лайтсерверов, то
есть 18 клиентов × ~12 блоков/мин — постоянная выкачка сотен мегабайт в сутки
у бота, который лайтсерверы использует для отправки выплат раз в день.

Эти тесты фиксируют контракт: пул поднимается по требованию и закрывается, а
режим off вообще обходится без лайтсерверов (выплаты идут по HTTP-каналу).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pytoniq_core.crypto.keys import mnemonic_new

from app import ton_pay
from app.config import settings
from app.ton_utils import to_nano


class _FakeProvider:
    """Пул лайтсерверов: считает закрытия."""

    def __init__(self) -> None:
        self.closed = 0

    async def close_all(self) -> None:
        self.closed += 1


def _reset_pool(monkeypatch: pytest.MonkeyPatch) -> _FakeProvider:
    """Чистое состояние пула в памяти процесса."""
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "mainnet")
    monkeypatch.setattr(ton_pay.state, "_provider_last_used_at", datetime.now(UTC))
    return provider


# ---------- Режим off: лайтсерверы не поднимаются вообще ----------


async def test_off_mode_refuses_to_build_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """off: кошелёк с лайтсерверами не собирается — и никакого пула не встаёт."""
    monkeypatch.setattr(settings, "ton_liteserver_mode", "off")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    monkeypatch.setattr(ton_pay.state, "_provider", None)
    monkeypatch.setattr(ton_pay.state, "_wallet", None)

    with pytest.raises(ton_pay.LiteserverChannelDisabled):
        await ton_pay._get_wallet()
    assert ton_pay.state._provider is None
    assert ton_pay.state._wallet is None


async def test_off_mode_send_goes_straight_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """off: выплата уходит по HTTP, а лайтсерверный кошелёк вообще не трогается."""
    monkeypatch.setattr(settings, "ton_liteserver_mode", "off")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    called = {}

    async def boom():
        raise AssertionError("off: лайтсерверный кошелёк не должен подниматься")

    async def http_send(dest, amount, comment):
        called["dest"] = dest
        called["amount"] = amount
        return "bcast:1700000000"

    monkeypatch.setattr(ton_pay, "_get_wallet", boom)
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_send)

    dest = "0:" + "55" * 32
    marker = await ton_pay.send_ton_transfer(dest, to_nano(0.42), comment="way:9:prize#5")
    assert marker == "bcast:1700000000"
    assert called == {"dest": dest, "amount": to_nano(0.42)}


async def test_off_mode_batch_goes_straight_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """off: пачка уходит по HTTP одним BoC, лайтсерверы не участвуют."""
    monkeypatch.setattr(settings, "ton_liteserver_mode", "off")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    seen: list[list] = []

    async def boom():
        raise AssertionError("off: лайтсерверный кошелёк не должен подниматься")

    async def http_batch(transfers):
        seen.append(list(transfers))
        return "bcast:1700000001"

    monkeypatch.setattr(ton_pay, "_get_wallet", boom)
    monkeypatch.setattr(ton_pay, "_send_ton_transfers_http", http_batch)

    transfers = [("0:" + "66" * 32, to_nano(0.1), "way:9:prize#6")]
    marker = await ton_pay.send_ton_transfers_batch(transfers)
    assert marker == "bcast:1700000001"
    assert seen == [transfers]


def test_disabled_channel_is_classified_as_channel_problem() -> None:
    """Выключенный канал — это «канала нет», а не «сбой выплаты».

    Без этой классификации диспетчер записал бы в last_error ложь и застрял
    на ретраях вместо штатного ухода в HTTP-канал.
    """
    assert ton_pay._is_liteserver_down(ton_pay.LiteserverChannelDisabled("off"))


async def test_off_mode_cold_start_never_builds_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """off: сверка холодного старта идёт по HTTP и не поднимает пул."""
    monkeypatch.setattr(settings, "ton_liteserver_mode", "off")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(mnemonic_new(24)))
    monkeypatch.setattr(ton_pay.state, "_cold_start_checked", False)
    monkeypatch.setattr(ton_pay.state, "_cold_start_next_attempt_at", None)

    async def boom():
        raise AssertionError("off: пул лайтсерверов не должен подниматься")

    async def fake_seqno(wallet):
        return 42

    saved: dict = {}

    async def fake_save(payload):
        saved["payload"] = payload

    monkeypatch.setattr(ton_pay.wallet, "get_wallet", boom)
    monkeypatch.setattr(ton_pay.http_channel, "http_get_wallet_seqno", fake_seqno)
    monkeypatch.setattr(ton_pay.wallet, "_save_cold_start_stamp", fake_save)
    monkeypatch.setattr(
        ton_pay.wallet, "build_offline_treasury_wallet", lambda: (object(), "v5r1")
    )

    await ton_pay.ensure_cold_start_check()
    assert ton_pay.state._cold_start_checked is True
    assert '"http": 42' in saved["payload"]


# ---------- Режим on_demand: пул живёт только ради выплаты ----------


async def test_cold_start_closes_pool_after_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_demand: пул подняли ради сверки — сразу закрыли.

    Иначе пул живёт до конца процесса и молча качает getAllShardsInfo.
    """
    monkeypatch.setattr(settings, "ton_liteserver_mode", "on_demand")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(ton_pay.state, "_cold_start_checked", False)
    monkeypatch.setattr(ton_pay.state, "_cold_start_next_attempt_at", None)
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "mainnet")

    async def fake_get_wallet():
        return object()

    monkeypatch.setattr(ton_pay.wallet, "get_wallet", fake_get_wallet)

    await ton_pay.ensure_cold_start_check()
    assert provider.closed == 1
    assert ton_pay.state._provider is None
    assert ton_pay.state._wallet is None


async def test_dispatch_cycle_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_demand: после цикла выплат пул закрыт — между выплатами он не живёт."""
    from app.ton_pay import dispatch

    monkeypatch.setattr(settings, "ton_liteserver_mode", "on_demand")
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "mainnet")

    await dispatch._close_liteserver_pool()
    assert provider.closed == 1
    assert ton_pay.state._provider is None


async def test_dispatch_cycle_keeps_pool_in_always_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """always: пул намеренно живёт весь процесс — цикл его не трогает."""
    from app.ton_pay import dispatch

    monkeypatch.setattr(settings, "ton_liteserver_mode", "always")
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())

    await dispatch._close_liteserver_pool()
    assert provider.closed == 0
    assert ton_pay.state._provider is provider


# ---------- TTL: страховка от «забыли закрыть» ----------


def test_pool_expires_by_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пул, к которому давно не обращались, считается протухшим."""
    from app.ton_pay import wallet as wallet_pkg

    monkeypatch.setattr(settings, "ton_liteserver_pool_ttl_seconds", 300)
    monkeypatch.setattr(
        ton_pay.state, "_provider_last_used_at", datetime.now(UTC) - timedelta(seconds=301)
    )
    assert wallet_pkg._pool_expired() is True

    monkeypatch.setattr(
        ton_pay.state, "_provider_last_used_at", datetime.now(UTC) - timedelta(seconds=10)
    )
    assert wallet_pkg._pool_expired() is False


def test_pool_ttl_zero_never_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL=0 — прежнее поведение: пул живёт, пока его не закроют руками."""
    from app.ton_pay import wallet as wallet_pkg

    monkeypatch.setattr(settings, "ton_liteserver_pool_ttl_seconds", 0)
    monkeypatch.setattr(
        ton_pay.state, "_provider_last_used_at", datetime.now(UTC) - timedelta(days=7)
    )
    assert wallet_pkg._pool_expired() is False


async def test_expired_pool_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Протухший пул закрывается и поднимается заново, а не переиспользуется."""
    from app.ton_pay import wallet as wallet_pkg

    monkeypatch.setattr(settings, "ton_liteserver_mode", "on_demand")
    monkeypatch.setattr(settings, "ton_liteserver_pool_ttl_seconds", 300)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    monkeypatch.setattr(settings, "treasury_address", "0:" + "77" * 32)
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())
    monkeypatch.setattr(ton_pay.state, "_wallet_network", "mainnet")
    monkeypatch.setattr(
        ton_pay.state, "_provider_last_used_at", datetime.now(UTC) - timedelta(seconds=600)
    )
    monkeypatch.setattr(ton_pay.state, "_cold_start_checked", True)

    fresh = object()
    started: list[str] = []

    async def fake_start(state, network, version, private_key, network_global_id):
        started.append(network)
        return fresh

    monkeypatch.setattr(wallet_pkg, "_start_treasury_provider", fake_start)
    monkeypatch.setattr(
        wallet_pkg, "_wallet_mnemonic_parts", lambda: (b"k" * 32, b"p" * 32, -239)
    )
    monkeypatch.setattr(wallet_pkg, "_resolve_treasury_version", lambda *a: "v5r1")

    result = await ton_pay._get_wallet()
    assert provider.closed == 1
    assert started == ["mainnet"]
    assert result is fresh


async def test_pool_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторное закрытие пула не падает и не мешает следующим выплатам."""
    provider = _FakeProvider()
    monkeypatch.setattr(ton_pay.state, "_provider", provider)
    monkeypatch.setattr(ton_pay.state, "_wallet", object())

    await ton_pay.close_treasury_provider()
    await ton_pay.close_treasury_provider()
    assert provider.closed == 1
    assert ton_pay.state._provider is None


async def test_close_does_not_raise_on_broken_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пул, который не закрылся, — не повод ронять цикл выплат."""

    class _Broken:
        async def close_all(self):
            raise RuntimeError("уже мёртв")

    monkeypatch.setattr(ton_pay.state, "_provider", _Broken())
    monkeypatch.setattr(ton_pay.state, "_wallet", object())

    await ton_pay.close_treasury_provider()
    assert ton_pay.state._provider is None
    assert ton_pay.state._wallet is None


async def test_off_mode_pool_is_never_built_by_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Даже если кто-то вызовет сверку при выключенном канале — пула не будет."""
    monkeypatch.setattr(settings, "ton_liteserver_mode", "off")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(ton_pay.state, "_cold_start_checked", False)
    monkeypatch.setattr(ton_pay.state, "_cold_start_next_attempt_at", None)
    monkeypatch.setattr(ton_pay.state, "_provider", None)

    async def fake_seqno(wallet):
        return 5

    async def fake_save(payload):
        return None

    monkeypatch.setattr(ton_pay.http_channel, "http_get_wallet_seqno", fake_seqno)
    monkeypatch.setattr(ton_pay.wallet, "_save_cold_start_stamp", fake_save)
    monkeypatch.setattr(
        ton_pay.wallet,
        "build_offline_treasury_wallet",
        lambda: (object(), "v5r1"),
    )

    await ton_pay.ensure_cold_start_check()
    assert ton_pay.state._provider is None


def test_mode_values_are_constrained() -> None:
    """Режим — из закрытого списка: опечатка в env не должна молча ломать выплаты."""
    from app.config import Settings

    field = Settings.model_fields["ton_liteserver_mode"]
    assert set(field.annotation.__args__) == {"off", "on_demand", "always"}
    assert field.default == "on_demand"


async def test_cancelled_transfer_is_not_classified_as_channel_down() -> None:
    """CancelledError — не «канал мёртв»: его нельзя превращать в HTTP-повтор."""
    assert not ton_pay._is_liteserver_down(asyncio.CancelledError())
