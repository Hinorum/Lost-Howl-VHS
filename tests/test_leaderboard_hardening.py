"""Жёсткие края лидерборда: ничья на границе призового среза и min_payout_gram.

Гарантии:
- ничья №3/№4 (равны верность и вклад Gram) — та же ничья, что внутри призов:
  открывает окно Claim, и раньше заявившийся игрок №4 может занять место №3;
- доля места недели ниже min_payout_gram не создаёт дохлый перевод: капает в
  копилку новой недели;
- месячная копилка с долями ниже порога не платится (ждёт роста), а частично
  неоплаченная пыль возвращается в копилку ТЕКУЩЕГО месяца.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.registry import (
    MARKER_KEY,
    MONTH_CLAIM_WINDOW_KEY,
    MONTH_READY_KEY,
    WEEK_CLAIM_WINDOW_KEY,
    WEEK_READY_KEY,
    WEEKLY_MARKER_KEY,
)
from app.db import SessionLocal
from app.leaderboard import (
    is_last_day_of_month,
    is_last_day_of_week,
    mark_leaderboards_for_finished,
    previous_month_key,
    settle_month_if_due,
    settle_week_if_due,
)
from app.models import (
    LeaderboardClaim,
    LeaderboardPot,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WeeklyPot,
    WinRule,
)
from app.ton_utils import to_nano
from app.weeks import iso_week_key, previous_week_key, week_bounds


def _sunday_last_of_month() -> datetime:
    """Ближайшее воскресенье, которое одновременно последний день месяца."""
    cursor = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    for _ in range(370):
        if is_last_day_of_week(cursor) and is_last_day_of_month(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise AssertionError("нет воскресенья в конце месяца в течение года")


async def test_leaderboard_ready_flags_persist_without_caller_commit(session: AsyncSession) -> None:
    """mark_leaderboards_for_finished коммитит СЕБЯ: флаги недели/месяца не теряются,
    когда вызывающая сессия закрывается без коммита (_finalize_new_day_job закрывает
    SessionLocal; /advance на раннем выходе «день уже создан»). Без этого копилка
    недели/месяца (2% банка) оставалась невыплачиваемой навсегда и немо."""
    from types import SimpleNamespace

    moment = _sunday_last_of_month()
    finished = SimpleNamespace(opens_at=moment, day_index=4242)
    await mark_leaderboards_for_finished(session, finished)
    # Осознанно НЕ коммитим вызвавший сеанс — ровно как _finalize_new_day_job.
    flags = {
        row.key: row.value
        for row in (await session.execute(select(WatcherState))).scalars().all()
    }
    assert flags[WEEK_READY_KEY] == iso_week_key(moment)
    assert flags[MONTH_READY_KEY] == moment.strftime("%Y-%m")


@pytest.fixture(autouse=True)
def _week_prize_contract():
    prev = settings.weekly_prize_pcts
    settings.weekly_prize_pcts = "50,30,20"
    yield
    settings.weekly_prize_pcts = prev


async def _set_week_ready(session: AsyncSession, week_key: str) -> None:
    session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
    await session.commit()


async def _seed_expired_week_window(session: AsyncSession, week_key: str, players: list[int]) -> None:
    opened_at = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    session.add(
        WatcherState(
            key=WEEK_CLAIM_WINDOW_KEY,
            value=json.dumps({"period": week_key, "players": players, "opened_at": opened_at}),
        )
    )


async def _seed_closed_round(session: AsyncSession, day_index: int, opens_at: datetime) -> Round:
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=opens_at,
        voting_ends_at=opens_at + timedelta(hours=23),
        tally_ends_at=opens_at + timedelta(hours=24),
        winner_card=0,
        vote_counts_json="{}",
        payouts_finalized=True,
    )
    session.add(round_row)
    await session.flush()
    return round_row


def _set_stake(session: AsyncSession, round_row: Round, pid: int, amount: float = 1.0) -> None:
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=pid,
            amount_nanotons=to_nano(amount),
            tx_hash="tx_" + os.urandom(16).hex(),
            status="confirmed",
        )
    )


async def _seed_week_boundary_tie(
    session: AsyncSession,
    base: int,
) -> tuple[dict[int, str], list[Round]]:
    """Сцена: A=6, B=5 верных; C,D=4 верных (ничья РЕЖУЩАЯ срез 3-го/4-го места)."""
    pids = [base, base + 1, base + 2, base + 3]
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    plan = {base: 6, base + 1: 5, base + 2: 4, base + 3: 4}
    session.add_all(
        [
            Player(id=pid, username=f"p{pid}", wallet_address=wallets[pid], wallet_verified=True)
            for pid in pids
        ]
    )
    prev_start, _ = week_bounds(previous_week_key())
    rounds: list[Round] = []
    day = 700_000
    for offset in range(7):
        round_row = await _seed_closed_round(
            session, day + offset, prev_start + timedelta(days=offset, hours=11)
        )
        rounds.append(round_row)
        for pid, count in plan.items():
            if offset < count:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        if offset == 0:
            for pid in pids:
                _set_stake(session, round_row, pid)
    return wallets, rounds


async def test_week_boundary_tie_opens_claim_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """№3 и №4 абсолютно равны — окно Claim открывается, выплата ждёт (не молчит)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    async with SessionLocal() as session:
        wallets, rounds = await _seed_week_boundary_tie(session, 720_000)
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            assert (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars().all() == []
            window_row = await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY)
            assert window_row is not None
            window = json.loads(window_row.value)
            assert window["period"] == week_key
            # Именно пограничная ничья №3/№4 — окно открыто, а не претензии внутри топа.
            assert set(window["players"]) == {720_002, 720_003}
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY, WEEK_READY_KEY])))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in wallets:
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_week_boundary_tied_fourth_promoted_by_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раньше заявившийся игрок №4, абсолютно равный №3, занимает третье место."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    prev_start, _ = week_bounds(previous_week_key())
    async with SessionLocal() as session:
        wallets, rounds = await _seed_week_boundary_tie(session, 760_000)
        # Игрок №4 заявился раньше молчащего №3; дедлайн окна прошёл.
        session.add(
            LeaderboardClaim(
                player_id=760_003, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=2, hours=1),
            )
        )
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _seed_expired_week_window(session, week_key, [760_002, 760_003])
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            # A(6), B(5) держат 50/30; третье место — заявившийся №4 (4 верных).
            assert by_pid == {
                760_000: to_nano(10) * 50 // 100,
                760_001: to_nano(10) * 30 // 100,
                760_003: to_nano(10) * 20 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY, WEEK_READY_KEY])))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in wallets:
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_week_dust_place_rolls_to_next_week_pot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Доля < min_payout_gram не создаёт перевод: уходит в копилку новой недели."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    base = 780_000
    pids = [base, base + 1, base + 2]  # 5 / 4 / 3 верных — без ничьих
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    plan = {base: 5, base + 1: 4, base + 2: 3}
    prev_start, _ = week_bounds(previous_week_key())
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid, username=f"p{pid}", wallet_address=wallets[pid], wallet_verified=True)
                for pid in pids
            ]
        )
        rounds: list[Round] = []
        day = 790_000
        for offset in range(5):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in pids:
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(0.099)  # 3-е место = 19.8M нанотонов < min_payout (0.02 Gram)
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            place_3 = pot_total * 20 // 100
            assert place_3 < to_nano(settings.min_payout_gram)
            assert by_pid == {
                base: pot_total * 50 // 100,
                base + 1: pot_total * 30 // 100,
            }
            current_week = iso_week_key(datetime.now(UTC))
            pot_row = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week == current_week))
            ).scalar_one_or_none()
            assert pot_row is not None
            assert pot_row.nanotons == place_3
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY, WEEK_READY_KEY])))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in pids:
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def _seed_month_scene(
    session: AsyncSession,
    base: int,
    weights: dict[int, int],
    pot_grams: float,
) -> tuple[list[Round], str]:
    """Месячный сценарий: игроки с верными голосами и ставками, горш прошлого месяца.

    weights: {pid: число верных путей}. Возвращает (rounds, prev_key).
    """
    prev_key = previous_month_key()
    prev_start = datetime(
        *map(int, prev_key.split("-")), 1, tzinfo=UTC
    )
    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    pids = list(weights)
    session.add_all(
        [
            Player(id=pid, wallet_address=f"w-{pid}", wallet_verified=True)
            for pid in pids
        ]
    )
    rounds: list[Round] = []
    day = 800_000
    for i in range(5):
        round_row = await _seed_closed_round(
            session, day + i, prev_start + timedelta(days=i + 1)
        )
        # tally_ends_at должен лежать в прошлом месяце: day+1 + 24ч <= месяц.
        round_row.opens_at = prev_start + timedelta(days=i + 1)
        round_row.tally_ends_at = prev_start + timedelta(days=i + 1, hours=24)
        assert round_row.tally_ends_at < month_start
        rounds.append(round_row)
        for pid, count in weights.items():
            if i < count:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        if i == 0:
            for pid in pids:
                _set_stake(session, round_row, pid)
    session.add(LeaderboardPot(month=prev_key, nanotons=to_nano(pot_grams)))
    session.add(WatcherState(key=MONTH_READY_KEY, value=prev_key))
    await session.commit()
    return rounds, prev_key


async def test_month_all_dust_waits_and_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все доли ниже порога: выплата НЕ идёт, горш и метка ждут роста."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 1)
    base = 810_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.01)
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (await session.execute(select(Payout).where(Payout.kind == "leaderboard"))).scalars().all() == []
            pot = (
                await session.execute(select(LeaderboardPot).where(LeaderboardPot.month == prev_key))
            ).scalar_one()
            assert pot.nanotons == to_nano(0.01)
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([MARKER_KEY, MONTH_READY_KEY])))
            await session.execute(LeaderboardPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (base, base + 1):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_month_dust_recarries_to_current_pot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Частичная пыль месяца не теряется: возвращается в копилку ТЕКУЩЕГО месяца."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 2)
    monkeypatch.setattr(settings, "monthly_prize_weights", "70,30")
    base = 820_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.05)
        try:
            assert await settle_month_if_due(bot=None) is True
            # Второе место (30% от 0.05 Грама = 0.015 < 0.02) — пыль: не платится.
            payouts = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "leaderboard"))).scalars()
            }
            assert payouts == {base: to_nano(0.05) * 70 // 100}
            current_month = datetime.now(UTC).strftime("%Y-%m")
            current_pot = (
                await session.execute(select(LeaderboardPot).where(LeaderboardPot.month == current_month))
            ).scalar_one_or_none()
            assert current_pot is not None
            assert current_pot.nanotons == to_nano(0.05) - to_nano(0.05) * 70 // 100
            # Старый горш удалён, метка переведена.
            assert (
                await session.execute(select(LeaderboardPot).where(LeaderboardPot.month == prev_key))
            ).scalar_one_or_none() is None
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is not None and marker.value == prev_key
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([MARKER_KEY, MONTH_READY_KEY])))
            await session.execute(LeaderboardPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (base, base + 1):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_month_boundary_tie_opens_claim_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Месяц: №top_k и №top_k+1 абсолютно равны (верность, вклад) — окно Claim открывается."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 3)
    monkeypatch.setattr(settings, "monthly_prize_weights", "50,30,20")
    base = 830_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(
            session, base, {base: 3, base + 1: 2, base + 2: 1, base + 3: 1}, 10.0
        )
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (
                await session.execute(select(Payout).where(Payout.kind == "leaderboard"))
            ).scalars().all() == []
            window_row = await session.get(WatcherState, MONTH_CLAIM_WINDOW_KEY)
            assert window_row is not None
            window = json.loads(window_row.value)
            assert window["period"] == prev_key
            # Пограничная ничья №3/#4, а не претензии внутри топа.
            assert set(window["players"]) == {base + 2, base + 3}
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([MARKER_KEY, MONTH_READY_KEY, MONTH_CLAIM_WINDOW_KEY])))
            await session.execute(LeaderboardPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (base, base + 1, base + 2, base + 3):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_month_boundary_tied_fourth_promoted_by_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Месяц: заявившийся №top_k+1, абсолютно равный №top_k, занимает последнее призовое место."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 3)
    monkeypatch.setattr(settings, "monthly_prize_weights", "50,30,20")
    base = 840_000
    prev_key = previous_month_key()
    prev_start = datetime(*map(int, prev_key.split("-")), 1, tzinfo=UTC)
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(
            session, base, {base: 3, base + 1: 2, base + 2: 1, base + 3: 1}, 10.0
        )
        # №4 заявился раньше молчащего №3; дедлайн окна прошёл.
        session.add(
            LeaderboardClaim(
                player_id=base + 3, kind="month", period=prev_key,
                claimed_at=prev_start + timedelta(days=1),
            )
        )
        opened_at = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
        session.add(
            WatcherState(
                key=MONTH_CLAIM_WINDOW_KEY,
                value=json.dumps(
                    {"period": prev_key, "players": [base + 2, base + 3], "opened_at": opened_at}
                ),
            )
        )
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "leaderboard"))).scalars()
            }
            # A(3), B(2) держат 50/30; третье место — заявившийся №4 (1 верный).
            assert by_pid == {
                base: to_nano(10) * 50 // 100,
                base + 1: to_nano(10) * 30 // 100,
                base + 3: to_nano(10) * 20 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key.in_([MARKER_KEY, MONTH_READY_KEY, MONTH_CLAIM_WINDOW_KEY])))
            await session.execute(LeaderboardPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (base, base + 1, base + 2, base + 3):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()
