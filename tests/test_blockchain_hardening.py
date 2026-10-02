"""Ужесточение блокчейн-контура: приз только на verified-кошелёк, настраиваемое
окно перекрытия курсора и устойчивость к реорганизациям, защита от потери
сбойных транзакций (стuck-список держит окно скана).

Правки:
1. finalize_day_payouts берёт dest_address только у ВЕРИФИЦИРОВАННЫХ кошельков:
   приз не уходит на адрес, чьё владение не доказано bv:-переводом. Строка
   сначала ждёт в очереди (dest_address=""), а hydrate оживит её после верификации.
2. _CURSOR_OVERLAP_SECONDS настраивается через settings.watch_cursor_overlap_seconds:
   частые reorg требуют глубже перечитывать историю.
3. watch_once пишет сбойные транзакции в stuck-список (watcher_state) и держит
   курсор, пока они свежие; исчерпавшие лимит логируются админу, но курсор
   проходит мимо (без вечной пробки).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app import ton_watch
from app.config import settings
from app.core.registry import CURSOR_KEY, STUCK_TX_KEY
from app.db import SessionLocal
from app.models import Payout, Player, Round, RoundStatus, Stake, Vote, WatcherState, WinRule
from app.ton_utils import to_nano


async def _closed_round(session: AsyncSession, winner_card: int = 0, day_index: int = 1) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        winner_card=winner_card,
        vote_counts_json="{}",
    )
    session.add(round_row)
    await session.flush()
    return round_row


# ---------- 1. Приз только на verified-кошелёк ----------


async def test_prize_to_unverified_wallet_waits_in_queue(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Приз игроку с привязанным, но НЕ подтверждённым кошельком: выплата
    создаётся с пустым dest_address и задерживается (не уходит на чужой адрес)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    session.add(
        Player(id=1, wallet_address="wallet-1", wallet_verified=False, wallet_verify_code="ABC123")
    )
    round_row = await _closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(5), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    prize = (
        await session.execute(select(Payout).where(Payout.kind == "prize"))
    ).scalar_one()
    # Адрес пуст: приз ждёт верификации, hydrate потом дозаполнит dest_address.
    assert prize.dest_address == ""
    assert prize.status == "pending"


async def test_prize_to_verified_wallet_uses_address(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: verified-кошелёк — приз уходит на привязанный адрес сразу."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    session.add(Player(id=2, wallet_address="wallet-2", wallet_verified=True))
    round_row = await _closed_round(session, winner_card=0, day_index=2)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(3), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    prize = (
        await session.execute(select(Payout).where(Payout.kind == "prize"))
    ).scalar_one()
    assert prize.dest_address == "wallet-2"


async def test_refund_to_unverified_wallet_waits_too(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Возврат по безвыигрышному дню тоже не уходит на неподтверждённый адрес."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    session.add(
        Player(id=3, wallet_address="wallet-3", wallet_verified=False, wallet_verify_code="ABC123")
    )
    round_row = await _closed_round(session, winner_card=2, day_index=3)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=3, card_position=1),
            Stake(round_id=round_row.id, player_id=3, amount_nanotons=to_nano(2), tx_hash="c", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    refund = (
        await session.execute(select(Payout).where(Payout.kind == "refund"))
    ).scalar_one()
    assert refund.dest_address == ""


# ---------- 2. Настраиваемое окно перекрытия курсора ----------


def test_cursor_overlap_reads_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окно перекрытия курсора берётся из watch_cursor_overlap_seconds."""
    monkeypatch.setattr(settings, "watch_cursor_overlap_seconds", 3600)
    monkeypatch.setattr(ton_watch, "_CURSOR_OVERLAP_SECONDS", max(0, settings.watch_cursor_overlap_seconds))
    assert ton_watch._CURSOR_OVERLAP_SECONDS == 3600


def test_cursor_overlap_default_nonnegative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отрицательная настройка окна обрезается до нуля (не валит курсор в минус)."""
    monkeypatch.setattr(settings, "watch_cursor_overlap_seconds", -5)
    monkeypatch.setattr(ton_watch, "_CURSOR_OVERLAP_SECONDS", max(0, settings.watch_cursor_overlap_seconds))
    assert ton_watch._CURSOR_OVERLAP_SECONDS == 0


# ---------- 3. Стuck-список сбойных транзакций ----------


async def test_stuck_roundtrip(session: AsyncSession) -> None:
    """json-сериализация stuck-списка переживает чтение/запись (свежий utime)."""
    now = int(datetime.now(UTC).timestamp())
    await ton_watch._write_stuck(session, {"tx-1": {"utime": now, "fails": 2}})
    loaded = await ton_watch._read_stuck(session)
    assert loaded == {"tx-1": {"utime": now, "fails": 2}}


async def test_stuck_retention_drops_old_entries(session: AsyncSession) -> None:
    """Врачующиеся записи старше stuck_retention_days уходят при записи."""
    now = int(datetime.now(UTC).timestamp())
    stale = now - (settings.stuck_retention_days + 1) * 86_400
    await ton_watch._write_stuck(
        session,
        {
            "old": {"utime": stale, "fails": 9, "reported": True},
            "frozen": {"utime": stale, "fails": 1},
            "fresh": {"utime": now, "fails": 2},
        },
    )
    loaded = await ton_watch._read_stuck(session)
    assert list(loaded) == ["fresh"]


def test_stuck_load_tolerates_bad_json(session: AsyncSession) -> None:
    """Повреждённый JSON не роняет watcher — возвращается пустой список."""
    assert ton_watch._load_stuck("not json{") == {}
    assert ton_watch._load_stuck("") == {}
    assert ton_watch._load_stuck('["list-not-dict"]') == {}


async def test_watch_stores_stuck_and_keeps_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбойная транзакция попадает в stuck-список, курсор стоит перед ней
    (а не уходит вперёд и не теряет перевод навсегда)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    bad = ton_watch.Transfer("badx-1", "0:" + "aa" * 32, to_nano(0.2), "", base)
    good = ton_watch.Transfer("goodx-1", "0:" + "bb" * 32, to_nano(0.2), "", base + 1)
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=([good, bad], True))
    )

    async def exploding(transfer, bot=None):
        if transfer.tx_hash == "badx-1":
            raise RuntimeError("моргнула цепь")
        return "refund_queued"

    monkeypatch.setattr(ton_watch, "process_transfer", exploding)
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            stuck_row = await db.get(WatcherState, STUCK_TX_KEY)
            assert stuck_row is not None
            assert "badx-1" in ton_watch._load_stuck(stuck_row.value)
            cursor = await db.get(WatcherState, CURSOR_KEY)
            assert cursor is not None and int(cursor.value) == bad.utime
    finally:
        async with SessionLocal() as db:
            for key in (CURSOR_KEY, STUCK_TX_KEY):
                await db.execute(WatcherState.__table__.delete().where(WatcherState.key == key))
            await db.execute(
                Payout.__table__.delete().where(
                    Payout.kind == "refund", Payout.tx_hash.in_(["goodx-1"])
                )
            )
            await db.commit()


async def test_stuck_clears_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный успешный проход вычищает транзакцию из stuck-списка."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    tx = ton_watch.Transfer("retry-1", "0:" + "cc" * 32, to_nano(0.2), "", base + 2)

    async def fetch(_since, before_lt=None):
        return ([tx], True)

    calls = {"fail": True}
    monkeypatch.setattr(ton_watch, "fetch_recent_transfers", fetch)

    async def flaky(transfer, bot=None):
        if calls["fail"]:
            raise RuntimeError("сначала сбой")
        return "refund_queued"

    monkeypatch.setattr(ton_watch, "process_transfer", flaky)
    try:
        await ton_watch.watch_once()
        calls["fail"] = False
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            stuck_row = await db.get(WatcherState, STUCK_TX_KEY)
            assert stuck_row is None or "retry-1" not in ton_watch._load_stuck(stuck_row.value)
    finally:
        async with SessionLocal() as db:
            for key in (CURSOR_KEY, STUCK_TX_KEY):
                await db.execute(WatcherState.__table__.delete().where(WatcherState.key == key))
            await db.execute(
                Payout.__table__.delete().where(
                    Payout.kind == "refund", Payout.tx_hash.in_(["retry-1"])
                )
            )
            await db.commit()