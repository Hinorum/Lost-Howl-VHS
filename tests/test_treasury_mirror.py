"""Зеркало казны: парсинг движений, связка с БД, бутстрап и тождество баланса.

Цель — инцидент «казна расходится» стал невозможен по построению: баланс
цепочки равен Σ balance_delta зеркала от генезиса до головы, поэтому сверка
«в ноль» не имеет допуска на газ и смотрит ровно на разницу Σ vs живой
баланс. Тесты фиксируют чистые парсеры, батчевую классификацию, идемпотентный
бутстрап и проверку тождества — всё без сети (фикстуры индексаторов).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app import ops, treasury_mirror
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
from app.models import Income, Payout, Stake, TreasuryMove, WatcherState
from app.ton_utils import to_nano
from app.treasury_mirror import (
    MirrorMove,
    classify_incoming,
    classify_outgoing,
    mirror_balance,
    parse_tonapi_move,
    parse_toncenter_move,
    parse_way_memo,
    reset_treasury_mirror,
    resolve_kind,
    treasury_mirror_block,
)

NET = "testnet"
TREASURY = "0:" + "ab" * 32
PLAYER = "0:" + "cd" * 32


def _h64(seed: str) -> str:
    """Детерминированный 64-hex хеш (валидные входы норм-функции)."""
    return (seed * 80)[:64]


def _tonapi_item(seed: str, *, value: int = 1_000_000_000, fee: int = 5_000_000, delta: int | None = None,
                 lt: int = 1000, comment: str = "", outgoing: bool = False, source: str = PLAYER) -> dict:
    """Транзакция в формате TonAPI v2 (входящая или исходящая)."""
    if outgoing:
        in_msg = {"value": 0, "source": None, "msg_data": {}}
        out_msgs = [{"value": value, "destination": {"address": source},
                     "msg_data": {"decoded_comment": comment} if comment else {"raw_message": ""}}]
        balance_delta = delta if delta is not None else -(value + fee)
    else:
        in_msg = {"value": value, "source": {"address": source},
                  "msg_data": {"decoded_comment": comment} if comment else {"raw_message": ""}}
        out_msgs = []
        balance_delta = delta if delta is not None else (value - fee)
    return {
        "hash": _h64(seed),
        "utime": 1_700_000_000,
        "lt": lt,
        "total_fees": fee,
        "balance_delta": str(balance_delta),
        "success": True,
        "in_msg": in_msg,
        "out_msgs": out_msgs,
    }


def _move(*args, **kwargs) -> MirrorMove:
    return _to_move(_tonapi_item(*args, **kwargs))


def _to_move(item: dict) -> MirrorMove | None:
    return parse_tonapi_move(item, NET, TREASURY)


# ---------- Парсеры TonAPI / Toncenter ----------


def test_tonapi_parse_incoming_move() -> None:
    move = _move("in-1")
    assert move is not None
    assert move.direction == "in"
    assert move.value_nanotons == 1_000_000_000
    assert move.fee_nanotons == 5_000_000
    assert move.balance_delta_nanotons == 995_000_000
    assert move.counterparty == PLAYER
    assert move.provider == "tonapi"
    assert move.is_money_move


def test_tonapi_parse_outgoing_with_memo() -> None:
    move = _move("out-1", outgoing=True, comment="way:7:prize#42")
    assert move is not None and move.direction == "out"
    assert move.value_nanotons == 1_000_000_000
    assert move.balance_delta_nanotons == -(1_000_000_000 + 5_000_000)
    assert move.comment == "way:7:prize#42"


def test_tonapi_parse_outgoing_without_provider_delta_counts_forward_fee() -> None:
    """Исходящий перевод: fwd_fee списывается с казны, но не входит в total_fees."""
    item = _tonapi_item("out-fwd", outgoing=True, comment="way:7:prize#42")
    item.pop("balance_delta")
    item["total_fees"] = 5_000_000
    item["out_msgs"] = [{"value": 1_000_000_000, "fwd_fee": 44446,
                         "destination": {"address": PLAYER},
                         "msg_data": {"decoded_comment": "way:7:prize#42"}}]
    move = _to_move(item)
    assert move is not None and move.direction == "out"
    assert move.balance_delta_nanotons == -(1_000_000_000 + 5_000_000 + 44446)
    assert move.fee_nanotons == 5_000_000


def test_toncenter_parse_outgoing_counts_forward_fee() -> None:
    item = {
        "hash": _h64("tc-fwd"),
        "now": 1_700_000_002,
        "lt": 5002,
        "total_fees": 5_000_000,
        "in_msg": {"value": 0, "source": "0:" + "00" * 32,
                   "message_content": {"decoded": {"@type": "comment", "comment": ""}}},
        "out_msgs": [{"value": 300_000_000, "fwd_fee": 44446, "destination": PLAYER,
                      "message_content": {"decoded": {"@type": "comment", "comment": "way:3:refund#9"}}}],
    }
    move = parse_toncenter_move(item, NET, TREASURY)
    assert move is not None and move.direction == "out"
    assert move.balance_delta_nanotons == -(300_000_000 + 5_000_000 + 44446)


def test_tonapi_parse_self_transfer_becomes_self() -> None:
    move = _move("self-1", outgoing=True, source=TREASURY)
    assert move is not None and move.direction == "self"


def test_tonapi_parse_skips_void_tx() -> None:
    item = _tonapi_item("void-1")
    item["balance_delta"] = "0"
    item["in_msg"] = {"value": 0, "source": None, "msg_data": {}}
    assert _to_move(item) is None


def test_toncenter_parse_incoming_computes_delta() -> None:
    item = {
        "hash": _h64("tc-1"),
        "now": 1_700_000_000,
        "lt": 5000,
        "fee": "5000000",
        "success": True,
        # у Toncenter нет balance_delta — сальдо выводится из in/out/fee
        "in_msg": {"value": "2000000000", "source": PLAYER,
                   "message_content": {"decoded": {"@type": "comment", "comment": ""}}},
        "out_msgs": [],
    }
    move = parse_toncenter_move(item, NET, TREASURY)
    assert move is not None
    assert move.direction == "in"
    assert move.balance_delta_nanotons == 1_995_000_000
    assert move.provider == "toncenter"


def test_toncenter_parse_outgoing_with_fee() -> None:
    item = {
        "hash": _h64("tc-2"),
        "now": 1_700_000_001,
        "lt": 5001,
        "fee": 5_000_000,
        "in_msg": {"value": 0, "source": "0:" + "00" * 32,
                   "message_content": {"decoded": {"@type": "comment", "comment": ""}}},
        "out_msgs": [{"value": 300_000_000, "destination": PLAYER,
                      "message_content": {"decoded": {"@type": "comment", "comment": "way:3:refund#9"}}}],
    }
    move = parse_toncenter_move(item, NET, TREASURY)
    assert move is not None and move.direction == "out"
    assert move.balance_delta_nanotons == -(300_000_000 + 5_000_000)
    assert move.comment == "way:3:refund#9"


def test_hash_normalization_handles_base64url() -> None:
    raw = os.urandom(32)
    b64url = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    move = _to_move({**_tonapi_item("b64"), "hash": b64url})
    assert move is not None
    assert move.tx_hash == raw.hex()


# ---------- Классификация по мемо ----------


def test_parse_way_memo_forms() -> None:
    assert parse_way_memo("way:7:prize#42") == ("prize", 42)
    assert parse_way_memo("на время паузы | way:5:refund#11") == ("refund", 11)
    assert parse_way_memo("обычный текст") is None
    assert parse_way_memo("way:7:prize") is None  # без #id


def test_classify_incoming_by_memo() -> None:
    assert classify_incoming("rv:123") == "revote"
    assert classify_incoming("Куда-то bv:A1B2") == "walletverify"
    assert classify_incoming("bank: пополнение") == "bank"
    assert classify_incoming("bank") == "bank"
    assert classify_incoming("") == "stake"


def test_classify_outgoing_by_memo() -> None:
    assert classify_outgoing("way:7:refund#3") == "refund"
    assert classify_outgoing("way:7:prize#3") == "payout:prize"
    assert classify_outgoing("что-то служебное") == "unknown_out"


# ---------- Связка с БД ----------


async def test_resolve_kind_incoming_stake_linked(session) -> None:
    session.add(Stake(round_id=1, player_id=1, amount_nanotons=to_nano(1),
                      tx_hash=_h64("stk"), network=NET, status="confirmed"))
    move = _move("stk")
    kind, linked = await resolve_kind(session, move)
    assert kind == "stake" and linked is not None


async def test_resolve_kind_incoming_uses_income_note(session) -> None:
    session.add(Income(kind="ton", amount_nanotons=to_nano(1), network=NET,
                       unit_ref=_h64("rv5"), note="in:revote;src:player"))
    kind, linked = await resolve_kind(session, _move("rv5", comment="rv:5"))
    assert kind == "revote" and linked is not None


async def test_resolve_kind_unknown_inbound(session) -> None:
    kind, linked = await resolve_kind(session, _move("thief"))
    assert kind == "unknown_in" and linked is None


async def test_resolve_kind_outbound_by_memo(session) -> None:
    session.add(Payout(kind="prize", amount_nanotons=to_nano(0.9), dest_address=PLAYER, status="sent"))
    await session.flush()
    payout = (await session.execute(select(Payout))).scalar_one()
    kind, linked = await resolve_kind(session, _move("pay", outgoing=True, comment=f"way:2:prize#{payout.id}"))
    assert kind == "payout:prize" and linked == payout.id


async def test_resolve_kind_refund_by_memo(session) -> None:
    session.add(Payout(kind="refund", amount_nanotons=to_nano(0.9), dest_address=PLAYER, status="sent"))
    await session.flush()
    payout = (await session.execute(select(Payout))).scalar_one()
    kind, linked = await resolve_kind(session, _move("rf", outgoing=True, comment=f"way:2:refund#{payout.id}"))
    assert kind == "refund" and linked == payout.id


async def test_resolve_kind_outbound_by_tx_hash_fallback(session) -> None:
    session.add(Payout(kind="referral", amount_nanotons=to_nano(0.5), dest_address=PLAYER, status="sent",
                       tx_hash=_h64("legacy")))
    kind, linked = await resolve_kind(session, _move("legacy", outgoing=True, comment="старое мемо"))
    assert kind == "payout:referral" and linked is not None


async def test_resolve_kind_unknown_outbound(session) -> None:
    kind, linked = await resolve_kind(session, _move("leak", outgoing=True, comment=""))
    assert kind == "unknown_out" and linked is None


# ---------- Тождество зеркала ----------


def test_mirror_balance_invariant_matches_chain_sum() -> None:
    """Баланс казны = Σ balance_delta от генезиса до головы (чистая арифметика)."""
    moves = [
        _move("a", lt=1),
        _move("b", lt=2),
        _move("c", lt=3, value=500_000_000, fee=4_000_000, delta=496_000_000),
        _move("d", lt=4, outgoing=True, value=300_000_000),
        _move("e", lt=5, outgoing=True, value=200_000_000, fee=5_000_000),
    ]
    total = sum(m.balance_delta_nanotons for m in moves if m is not None)
    expected = (
        (1_000_000_000 - 5_000_000)
        + (1_000_000_000 - 5_000_000)
        + 496_000_000
        - (300_000_000 + 5_000_000)
        - (200_000_000 + 5_000_000)
    )
    assert total == expected


async def test_mirror_balance_sums_deltas(session) -> None:
    session.add_all([
        TreasuryMove(tx_hash=_h64("x1"), network=NET, utime=1, lt=1, direction="in", kind="stake",
                     value_nanotons=1_000_000_000, fee_nanotons=5_000_000, balance_delta_nanotons=995_000_000),
        TreasuryMove(tx_hash=_h64("x2"), network=NET, utime=2, lt=2, direction="out", kind="payout:prize",
                     value_nanotons=300_000_000, fee_nanotons=5_000_000, balance_delta_nanotons=-305_000_000),
        TreasuryMove(tx_hash=_h64("y1"), network="mainnet", utime=3, lt=3, direction="in", kind="stake",
                     value_nanotons=999_000_000, fee_nanotons=1_000_000, balance_delta_nanotons=998_000_000),
    ])
    await session.flush()
    assert await mirror_balance(session, NET) == 690_000_000
    assert await mirror_balance(session, "mainnet") == 998_000_000


# ---------- Синк: бутстрап и инкремент ----------

_MIRROR_STATE_KEYS = [
    TREASURY_MIRROR_BOOTSTRAP_KEY,
    TREASURY_MIRROR_BOTTOM_KEY,
    TREASURY_MIRROR_CURSOR_KEY,
    TREASURY_MIRROR_CHECK_KEY,
    TREASURY_MIRROR_BEAT_KEY,
    TREASURY_MIRROR_SOURCE_KEY,
]


async def _count_moves() -> int:
    async with SessionLocal() as db:
        return int((await db.execute(
            select(func.count()).select_from(TreasuryMove))).scalar_one())


async def _wipe_mirror() -> None:
    async with SessionLocal() as db:
        await db.execute(delete(TreasuryMove))
        await db.execute(delete(WatcherState).where(WatcherState.key.in_(_MIRROR_STATE_KEYS)))
        await db.commit()


def _fake_page_serving(ledger: list[dict], ton_api: bool = True):
    """Фабрика _fetch_page: страницы desc по 100, как у живого индексатора."""

    async def fetch(before_lt: int | None = None):
        items = sorted(
            (i for i in ledger if before_lt is None or int(i["lt"]) < before_lt),
            key=lambda i: -int(i["lt"]),
        )
        page = items[:100]
        parsed = []
        for item in page:
            parsed.append(
                parse_tonapi_move(item, NET, TREASURY)
                if ton_api
                else parse_toncenter_move(item, NET, TREASURY)
            )
        return [m for m in parsed if m is not None], "tonapi" if ton_api else "toncenter", True

    return fetch


def _fake_chain_balance(ledger: list[dict]) -> int:
    """«Живой баланс» из данных, которые должен увидеть зеркало."""
    return sum(
        m.balance_delta_nanotons
        for i in ledger
        if (m := parse_tonapi_move(i, NET, TREASURY)) is not None
    )


@pytest.fixture()
def ton_mirror(monkeypatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_address", "")


async def test_bootstrap_bounded_then_completes_then_idempotent(ton_mirror, monkeypatch) -> None:
    ledger = [_tonapi_item(f"b{i}", lt=10_000 + i * 7) for i in range(150)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    # Бутстрап ограничен: первый цикл добирает максимум погран-cтраницу.
    monkeypatch.setattr(settings, "treasury_mirror_max_pages_per_sync", 1)
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        r1 = await treasury_mirror.sync_treasury_mirror()
        assert r1["pages"] == 1 and r1["bootstrapped"] is False
        assert r1["added"] == 100

        r2 = await treasury_mirror.sync_treasury_mirror()
        assert r2["bootstrapped"] is False  # вторая страница добирает остаток
        assert r2["added"] == 50

        r3 = await treasury_mirror.sync_treasury_mirror()
        assert r3["bootstrapped"] is True  # пустая страница — дно достигнуто
        assert r3["added"] == 0 and r3["exact"] is True

        async with SessionLocal() as db:
            n = (await db.execute(select(func.count()).select_from(TreasuryMove))).scalar_one()
            boot = await db.get(WatcherState, TREASURY_MIRROR_BOOTSTRAP_KEY)
        assert n == 150
        assert boot is not None and boot.value == "1"
    finally:
        await _wipe_mirror()


async def test_incremental_adds_new_head_only(ton_mirror, monkeypatch) -> None:
    ledger = [_tonapi_item(f"i{i}", lt=20_000 + i * 3) for i in range(120)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        r1 = await treasury_mirror.sync_treasury_mirror()
        assert r1["bootstrapped"] is True and r1["added"] == 120

        # Новые транзакции поверх головы: инкремент добавляет только их.
        ledger.append(_tonapi_item("new1", lt=20_999, value=2_000_000_000, fee=6_000_000,
                                   delta=1_994_000_000))
        ledger.append(_tonapi_item("new2", lt=21_000, value=3_000_000_000, fee=6_000_000,
                                   delta=2_994_000_000))
        monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                            _fake_chain_balance_async(_fake_chain_balance(ledger)))
        r2 = await treasury_mirror.sync_treasury_mirror()
        assert r2["added"] == 2
        assert r2["exact"] is True

        async with SessionLocal() as db:
            head_raw = await db.get(WatcherState, TREASURY_MIRROR_CURSOR_KEY)
            head = int(head_raw.value) if head_raw else None
            assert head == 21_000
    finally:
        await _wipe_mirror()


async def test_bootstrap_against_toncenter_fallback(ton_mirror, monkeypatch) -> None:
    """Тот же бутстрап через Toncenter v3 (нет balance_delta — вычисляется)."""
    ledger = [_tonapi_item(f"t{i}", lt=30_000 + i * 2) for i in range(80)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(
        [_to_toncenter(i) for i in ledger], ton_api=False
    ))
    import app.ton_pay

    async def fake_state():
        return _fake_chain_balance(ledger), None, "toncenter"

    monkeypatch.setattr(app.ton_pay, "fetch_account_state", fake_state)
    try:
        result = await treasury_mirror.sync_treasury_mirror()
        assert result["bootstrapped"] is True and result["added"] == 80
        assert result["exact"] is True and result["source"] == "toncenter"
    finally:
        await _wipe_mirror()


async def test_identity_flags_chain_change(ton_mirror, monkeypatch) -> None:
    """Цепочка «может» измениться мимо зеркала — тождество обязано быть ложью."""
    ledger = [_tonapi_item(f"r{i}", lt=40_000 + i) for i in range(10)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger) + to_nano(1)))
    try:
        result = await treasury_mirror.sync_treasury_mirror()
        assert result["bootstrapped"] is True
        assert result["exact"] is False
        assert result["diff_nanotons"] == -to_nano(1)
        async with SessionLocal() as db:
            check_row = await db.get(WatcherState, TREASURY_MIRROR_CHECK_KEY)
            assert check_row.value and '"exact": false' in check_row.value
    finally:
        await _wipe_mirror()


def _to_toncenter(item: dict) -> dict:
    """Конвертация фикстуры TonAPI в формат Toncenter v3."""
    return {
        "hash": item["hash"],
        "now": item["utime"],
        "lt": item["lt"],
        "fee": item["total_fees"],
        "in_msg": {
            "value": item["in_msg"].get("value", 0),
            "source": (
                item["in_msg"].get("source", {}).get("address")
                if isinstance(item["in_msg"].get("source"), dict)
                else item["in_msg"].get("source") or "0:" + "00" * 32
            ),
        },
        "out_msgs": [
            {"value": m.get("value", 0), "destination": m.get("destination", {}).get("address") if isinstance(m.get("destination"), dict) else m.get("destination", "")}
            for m in item.get("out_msgs", [])
        ],
    }


def _fake_chain_balance_async(value: int):
    async def fake():
        return value, "active", "tonapi"
    return fake


# ---------- Отчёт и ежедневная автосверка ----------


async def test_treasury_mirror_block_renders_empty(ton_mirror) -> None:
    text = await treasury_mirror_block()
    assert "Зеркало казны (testnet):" in text
    assert "бутстрап" in text or "пуста" in text


async def test_mirror_anomaly_exact_is_clean(ton_mirror, monkeypatch) -> None:
    ledger = [_tonapi_item(f"an{i}", lt=50_000 + i) for i in range(5)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        await treasury_mirror.sync_treasury_mirror()
        async with SessionLocal() as session:
            assert await ops._treasury_mirror_anomaly(session) is None
    finally:
        await _wipe_mirror()


async def test_mirror_anomaly_flags_mismatch(ton_mirror, monkeypatch) -> None:
    ledger = [_tonapi_item(f"d{i}", lt=60_000 + i) for i in range(5)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger) + to_nano(0.5)))
    try:
        await treasury_mirror.sync_treasury_mirror()
        async with SessionLocal() as session:
            note = await ops._treasury_mirror_anomaly(session)
        assert note is not None and "расходится" in note
    finally:
        await _wipe_mirror()


async def test_mirror_anomaly_bootstrap_in_progress_is_not_alarm(ton_mirror, monkeypatch) -> None:
    """Бутстрап с живыми циклами — работа, а не тревога; замирание — тревога."""
    from datetime import timedelta

    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BEAT_KEY,
                                value=datetime.now(UTC).isoformat()))
            await db.commit()
        async with SessionLocal() as session:
            assert await ops._treasury_mirror_anomaly(session) is None
        async with SessionLocal() as db:
            beat = await db.get(WatcherState, TREASURY_MIRROR_BEAT_KEY)
            beat.value = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            await db.commit()
        async with SessionLocal() as session:
            note = await ops._treasury_mirror_anomaly(session)
        assert note is not None and "циклы не идут" in note
    finally:
        await _wipe_mirror()


async def test_mirror_anomaly_warns_when_bootstrapped_mirror_freezes(ton_mirror) -> None:
    """Выстроенное зеркало с протухшим «зелёным» CHECK обязано кричать:
    последняя сверка устарела, расхождение может расти без контроля."""
    from datetime import timedelta

    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=TREASURY_MIRROR_BOOTSTRAP_KEY, value="1"))
            db.add(WatcherState(key=TREASURY_MIRROR_BEAT_KEY,
                                value=datetime.now(UTC).isoformat()))
            db.add(WatcherState(key=TREASURY_MIRROR_CHECK_KEY,
                                value=json.dumps({"exact": True, "diff_nanotons": 0})))
            await db.commit()
        async with SessionLocal() as session:
            assert await ops._treasury_mirror_anomaly(session) is None  # свежий CHECK
        async with SessionLocal() as db:
            beat = await db.get(WatcherState, TREASURY_MIRROR_BEAT_KEY)
            beat.value = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            await db.commit()
        async with SessionLocal() as session:
            note = await ops._treasury_mirror_anomaly(session)
        assert note is not None and "не обновляется" in note
    finally:
        await _wipe_mirror()


# ---------- Реорганизация цепочки (reorg) ----------


async def test_reorg_rewrites_moved_tx_in_place(ton_mirror, monkeypatch) -> None:
    """Реорг переставил транзакцию (новый lt/utime/сальдо) — строка переписана,
    а не продублирована. Иначе одна транзакция учитывалась бы дважды и Σ
    balance_delta разошлась бы с цепочкой навсегда."""
    moved = _tonapi_item("org", lt=50_000, value=1_000_000_000, fee=5_000_000)
    other = _tonapi_item("keep", lt=49_990, value=500_000_000, fee=5_000_000)
    ledger = [moved, other]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        first = await treasury_mirror.sync_treasury_mirror()
        assert first["added"] == 2 and first["exact"] is True
        async with SessionLocal() as db:
            before = (await db.execute(
                select(TreasuryMove).where(TreasuryMove.tx_hash == _h64("org")))).scalar_one()
        assert before.lt == 50_000

        # Тот же хеш вернулся в цепочку с другой позицией и сальдо.
        ledger[0] = _tonapi_item("org", lt=50_500, value=1_200_000_000, fee=5_000_000)
        reorg_sum = _fake_chain_balance(ledger)
        monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                            _fake_chain_balance_async(reorg_sum))
        second = await treasury_mirror.sync_treasury_mirror()

        assert second["added"] == 0, "перезапись, не новая строка"
        assert second["updated"] == 1, "только что переехавшая транзакция"
        assert second["exact"] is True, f"зеркало обязано сойтись с новой цепочкой: {second}"
        assert await _count_moves() == 2
        async with SessionLocal() as db:
            after = (await db.execute(
                select(TreasuryMove).where(TreasuryMove.tx_hash == _h64("org")))).scalar_one()
        assert after.lt == 50_500
        assert after.balance_delta_nanotons == 1_195_000_000
    finally:
        await _wipe_mirror()


async def test_reorg_orphan_stays_phantom_and_breaks_identity(ton_mirror, monkeypatch) -> None:
    """Известное ограничение зафиксировано тестом, а не спрятано.

    Транзакция, которую реорг выкинул из цепочки, больше НЕ приходит ни в одну
    страницу — зеркало не умеет её удалять (см. docstring reset_treasury_mirror),
    и строка-фантом остаётся в Σ навсегда. Обязательное следствие: тождество
    обязано врать (exact=False, diff = сальдо фантома) и ежедневная автосверка
    поднять тревогу, а не рапортовать «сходится ±0» над мёртвой строкой.
    Ни /mirror reset, ни пересборка фантом не лечат — см. конец теста.
    """
    kept = _tonapi_item("alive", lt=60_000, value=1_000_000_000, fee=5_000_000)
    orphan = _tonapi_item("doomed", lt=59_000, value=400_000_000, fee=5_000_000)
    ledger = [kept, orphan]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        first = await treasury_mirror.sync_treasury_mirror()
        assert first["exact"] is True
        phantom_delta = _fake_chain_balance([orphan])

        # Реорг: orphan исчез из цепочки, живой баланс упал на его сальдо.
        ledger.remove(orphan)
        monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                            _fake_chain_balance_async(_fake_chain_balance(ledger)))
        second = await treasury_mirror.sync_treasury_mirror()

        assert second["exact"] is False, "фантом обязан ломать тождество"
        assert second["diff_nanotons"] == phantom_delta
        assert await _count_moves() == 2, "строка-фантом остаётся в зеркале"
        async with SessionLocal() as session:
            note = await ops._treasury_mirror_anomaly(session)
        assert note is not None and "расходится" in note

        # Известно и по docstring reset_treasury_mirror: сброс НЕ удаляет
        # строки, а выкинутой реоргом транзакции уже никто не принесёт — фантом
        # переживает пересборку. Это осознанный размен: лучше постоянная тревога
        # с внятной цифрой, чем тихо переписанная история; чинить вручную.
        await reset_treasury_mirror()
        third = await treasury_mirror.sync_treasury_mirror()
        assert third["added"] == 0 and third["updated"] == 0
        assert third["exact"] is False, "фантом остаётся после пересборки"
        assert third["diff_nanotons"] == phantom_delta
        assert await _count_moves() == 2
        async with SessionLocal() as session:
            assert await ops._treasury_mirror_anomaly(session) is not None
    finally:
        await _wipe_mirror()


# ---------- Re-bootstrap по команде хранителя (/mirror reset confirm) ----------


def _admin_message(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock(), text=text)


async def test_reset_mirror_keeps_rows_and_rebootstraps(ton_mirror, monkeypatch) -> None:
    """Сброс состояния НЕ трогает строки: следующий цикл перестраивает зеркало
    без дублей и заново доказывает тождество."""
    ledger = [_tonapi_item(f"rs{i}", lt=80_000 + i) for i in range(5)]
    monkeypatch.setattr(treasury_mirror, "_fetch_page", _fake_page_serving(ledger))
    import app.ton_pay

    monkeypatch.setattr(app.ton_pay, "fetch_account_state",
                        _fake_chain_balance_async(_fake_chain_balance(ledger)))
    try:
        first = await treasury_mirror.sync_treasury_mirror()
        assert first["bootstrapped"] is True and first["added"] == 5
        async with SessionLocal() as db:
            rows_before = (await db.execute(
                select(func.count()).select_from(TreasuryMove))).scalar_one()
        await reset_treasury_mirror()
        async with SessionLocal() as db:
            for key in _MIRROR_STATE_KEYS:
                assert await db.get(WatcherState, key) is None
        rows_after_reset = (await _count_moves())
        assert rows_after_reset == rows_before  # данные зеркала не удаляются

        rebuilt = await treasury_mirror.sync_treasury_mirror()
        assert rebuilt["bootstrapped"] is True
        assert rebuilt["added"] == 0  # идемпотентная перезапись без дублей
        assert rebuilt["exact"] is True
        async with SessionLocal() as db:
            rows_final = (await db.execute(
                select(func.count()).select_from(TreasuryMove))).scalar_one()
        assert rows_final == rows_before
    finally:
        await _wipe_mirror()


async def test_mirror_command_guards_nonadmin(monkeypatch) -> None:
    from app.handlers.payout import cmd_mirror

    monkeypatch.setattr(settings, "admin_ids", "42")
    message = _admin_message(777_777, "/mirror reset confirm")
    await cmd_mirror(message)
    assert "хранителя" in message.answer.await_args.args[0]


async def test_mirror_command_requires_confirm(monkeypatch) -> None:
    from app.handlers.payout import cmd_mirror

    monkeypatch.setattr(settings, "admin_ids", "42")
    message = _admin_message(42, "/mirror")
    await cmd_mirror(message)
    assert "reset confirm" in message.answer.await_args.args[0]


async def test_mirror_command_resets_state(monkeypatch) -> None:
    from app.handlers.payout import cmd_mirror

    monkeypatch.setattr(settings, "admin_ids", "42")
    message = _admin_message(42, "/mirror reset confirm")
    await cmd_mirror(message)
    assert "сброшено" in message.answer.await_args.args[0]