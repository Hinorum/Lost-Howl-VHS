"""Тесты казначея: детект версии контракта (v4r2/v5r1) и отправка переводов.

Реальный кейс: казначей создан в современном кошельке как v5r1, а бот шлёт
как v4r2 — внешние сообщения отвергаются контрактом, выплаты вечно ретраятся.
Детект по совпадению производного адреса ловит такую пару до выхода в сеть.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

from app import ton_pay
from app.config import settings
from app.ton_utils import to_nano


def _public_key() -> bytes:
    _, private_key = mnemonic_to_private_key(mnemonic_new(24))
    return private_key_to_public_key(private_key)


def _reset_wallet_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ton_pay, "_provider", None)
    monkeypatch.setattr(ton_pay, "_wallet", None)
    monkeypatch.setattr(ton_pay, "_wallet_network", None)


def test_versions_give_different_addresses() -> None:
    pub = _public_key()
    v4 = ton_pay._wallet_address("v4r2", pub, -239)
    v5_main = ton_pay._wallet_address("v5r1", pub, -239)
    v5_test = ton_pay._wallet_address("v5r1", pub, -3)
    # Версии контракта различают адрес; у v4 сеть не входит в data.
    assert v4 != v5_main != v5_test and v4 != v5_test
    # Детерминизм: тот же ключ — тот же адрес.
    assert ton_pay._wallet_address("v5r1", pub, -3) == v5_test


async def test_detects_v5_testnet_and_v4_mainnet() -> None:
    pub = _public_key()
    for version, ngid in (("v5r1", -3), ("v5r1", -239), ("v4r2", -239), ("v4r2", -3)):
        address = ton_pay._wallet_address(version, pub, ngid)
        found, candidates = ton_pay._detect_wallet_version(pub, address, ngid)
        assert found == version, (version, ngid)
        assert set(candidates) == {"v4r2", "v5r1"}


async def test_mismatch_reports_candidates_but_no_version() -> None:
    pub = _public_key()
    stranger = "0:" + os.urandom(32).hex()
    found, candidates = ton_pay._detect_wallet_version(pub, stranger, -3)
    assert found is None
    assert all(candidates[v] != stranger for v in candidates)


async def test_get_wallet_rejects_wrong_version_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Явно запрошенная версия с чужим адресом — падение до выхода в сеть."""
    _reset_wallet_singleton(monkeypatch)
    pub = _public_key()
    words = mnemonic_new(24)
    v4_address = ton_pay._wallet_address("v4r2", pub, -239)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_address", v4_address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "v5r1")
    with pytest.raises(ValueError, match="не совпадает"):
        await ton_pay._get_wallet()


async def test_get_wallet_auto_detect_requires_known_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto + адрес, не бьющийся ни с одной версией, — внятная ошибка."""
    _reset_wallet_singleton(monkeypatch)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(mnemonic_new(24)))
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    monkeypatch.setattr(settings, "treasury_wallet_version", "auto")
    with pytest.raises(ValueError, match="ни с одной поддерживаемой"):
        await ton_pay._get_wallet()


class _FakeWallet:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    async def transfer(self, destination, amount, body=None, state_init=None):
        self.calls.append((destination, amount))
        return 1


async def test_send_uses_wallet_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Путь send_ton_transfer: сумма и получатель доходят до кошелька."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    fake = _FakeWallet()

    async def fake_get():
        return fake

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    dest = "0:" + os.urandom(32).hex()
    marker = await ton_pay.send_ton_transfer(dest, to_nano(0.42), comment="way:7:prize#9")
    assert marker and marker.startswith("bcast:")
    assert fake.calls == [(dest, to_nano(0.42))]


async def test_send_without_mnemonic_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "")
    monkeypatch.setattr(settings, "ton_network", "testnet")
    called = False

    async def fail_get():
        nonlocal called
        called = True
        return _FakeWallet()

    monkeypatch.setattr(ton_pay, "_get_wallet", fail_get)
    result = await ton_pay.send_ton_transfer("0:" + os.urandom(32).hex(), to_nano(1), comment="x")
    assert result is None and called is False


# ---------- Регрессия: seqno-гонка при подряд идущих переводах ----------


class _BatchWallet(_FakeWallet):
    """Только интерфейс, нужный _send_raw_with_seqno: подписанное внешнее
    сообщение собирается из create_wallet_internal_message +
    raw_create_transfer_msg и уходит через send_external."""

    def __init__(self):
        super().__init__()
        self.private_key = b"k" * 32
        self.wallet_id = 698983191
        self.seqnos: list[int] = []

    def create_wallet_internal_message(self, destination, value=0, body=None):
        return ("internal", destination, value)

    def raw_create_transfer_msg(self, private_key, seqno, wallet_id, messages):
        self.seqnos.append(seqno)
        return ("transfer", seqno)

    async def send_external(self, body=None):
        return 1


async def test_send_uses_local_seqno_when_batch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Два перевода в одной пачке получают РАЗНЫЕ seqno (N, N+1), а не два
    одинаковых: без этого повторный get_seqno() у сети дал бы один и тот же
    seqno, и второй перевод молча бы потерялся в блокчейне."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    fake = _BatchWallet()

    async def fake_get():
        return fake

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 100)
    try:
        await ton_pay.send_ton_transfer("0:" + "11" * 32, to_nano(1), comment="a")
        await ton_pay.send_ton_transfer("0:" + "22" * 32, to_nano(2), comment="b")
    finally:
        monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)
    assert fake.seqnos == [100, 101]
    # Батч-путь не ходит в wallet.transfer: каждый перевод — отдельный
    # подписанный external со своим seqno, вещается напрямую.
    assert fake.calls == []


async def test_send_batch_aborts_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой вещания отменяет батч: следующий перевод получит свежий seqno,
    чтобы не вещать последовательность из уже потраченных номеров."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))

    class _BrokenBatchWallet(_BatchWallet):
        async def send_external(self, body=None):
            self.broken = True
            return 0

    fake = _BrokenBatchWallet()
    fake.broken = False

    async def fake_get():
        return fake

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 100)
    try:
        with pytest.raises(RuntimeError, match="не приняли"):
            await ton_pay.send_ton_transfer("0:" + "11" * 32, to_nano(1), comment="a")
        assert ton_pay.state._batch_seqno is None
    finally:
        monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)


async def test_send_batch_aborts_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.wait_for-таймаут диспетчера рвёт корутину вещания CancelledError
    (это НЕ Exception): батч-счётчик обязан сброситься и здесь, иначе следующий
    перевод пачки переиспользует уже разосланный seqno и молча потеряется."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))

    class _CancelledWallet(_BatchWallet):
        async def send_external(self, body=None):
            raise asyncio.CancelledError()

    fake = _CancelledWallet()

    async def fake_get():
        return fake

    monkeypatch.setattr(ton_pay, "_get_wallet", fake_get)
    monkeypatch.setattr(ton_pay.state, "_batch_seqno", 100)
    try:
        with pytest.raises(asyncio.CancelledError):
            await ton_pay.send_ton_transfer("0:" + "11" * 32, to_nano(1), comment="a")
        assert ton_pay.state._batch_seqno is None
    finally:
        monkeypatch.setattr(ton_pay.state, "_batch_seqno", None)


# ---------- Регрессия формата внешнего сообщения v5 ----------


def test_v5_external_body_matches_contract_spec() -> None:
    """Тело внешнего сообщения v5 обязано быть
    [op 'sign'|wallet_id|valid_until|seqno|флаги][подпись 512 бит] + ref на
    цепочку OutList — сверено с реальной транзакцией Keeper тестнета.
    Дрейф версий pytoniq-core ломал сборку молча; этот тест ловит такое.

    Контракт парсит ровно так: signature = последние 512 бит тела, подпись
    стоит над хешем всего префикса вместе со ссылками (wallet_v5.fc,
    process_signed_request).
    """
    from pytoniq.contract.contract import Contract
    from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
    from pytoniq_core import begin_cell
    from pytoniq_core.boc.address import Address
    from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key
    from pytoniq_core.crypto.signature import verify_sign
    from pytoniq_core.tlb.custom.wallet import WalletMessage

    _, private_key = mnemonic_to_private_key(mnemonic_new(24))
    public_key = private_key_to_public_key(private_key)
    wallet_message = WalletMessage(
        send_mode=3,  # +2 ignore errors обязателен для внешних сообщений
        message=Contract.create_internal_msg(
            dest=Address("0:" + "11" * 32), value=to_nano(0.5), body=None
        ),
    )
    dummy = object.__new__(WalletV5R1)  # raw_create_transfer_msg не требует state
    body = WalletV5R1.raw_create_transfer_msg(
        dummy,
        private_key=private_key,
        seqno=7,
        wallet_id=2147483645,  # тестнет: 0x80000000 ^ (-3)
        messages=[wallet_message],
        valid_until=1_900_000_000,
    )

    s = body.begin_parse()
    assert s.load_uint(32) == 0x7369676E  # op 'sign'
    assert s.load_uint(32) == 2147483645
    valid_until = s.load_uint(32)
    assert valid_until == 1_900_000_000
    assert s.load_uint(32) == 7  # msg_seqno
    assert (s.load_uint(1), s.load_uint(1)) == (1, 0)  # out_actions есть, extended нет
    signature = s.load_bytes(64)
    chain = s.load_ref()
    assert len(body.refs) == 1

    # Префикс до подписи — ровно то, хеш чего проверяет контракт.
    prefix = (
        begin_cell()
        .store_uint(0x7369676E, 32)
        .store_uint(2147483645, 32)
        .store_uint(valid_until, 32)
        .store_uint(7, 32)
        .store_uint(1, 1)
        .store_uint(0, 1)
        .store_ref(chain)
        .end_cell()
    )
    assert verify_sign(public_key, prefix.hash, signature)


def test_v5_wallet_id_testnet_packing() -> None:
    """wallet_id тестнета (Keeper-казначей): 0x7FFFFFFD = 2147483645."""
    from pytoniq.contract.wallets.wallet_v5 import WalletV5WalletID

    packed = WalletV5WalletID(workchain=0, network_global_id=-3).pack()
    assert packed == 2147483645
    unpacked = WalletV5WalletID.unpack(packed, -3)
    assert (unpacked.workchain, unpacked.version, unpacked.subwallet_number) == (0, 0, 0)
    # Мейннет: другая глобаль — другой адрес при той же мнемонике.
    assert WalletV5WalletID(workchain=0, network_global_id=-239).pack() != packed
