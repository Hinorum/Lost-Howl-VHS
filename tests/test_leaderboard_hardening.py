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
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    _claim_times,
    _load_claim_window,
    _notify_tied_players,
    _open_claim_window,
    _prize_tied_groups,
    _resolve_claim_window,
    _top_correct_voters,
    _weighted_amounts,
    is_last_day_of_month,
    is_last_day_of_week,
    mark_leaderboards_for_finished,
    mark_month_leaderboard_ready,
    mark_week_leaderboard_ready,
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


# --- Слепые ветки: утилиты, окно Claim, метки готовности, месячные края -------


async def test_notify_tied_players_covers_kinds_and_failures() -> None:
    """DM tied-игрокам: оба kind-а, кнопка Claim, сбой доставки не валит (369-394)."""
    await _notify_tied_players(None, "week", "2026-W41", [1])  # 369-370: bot None
    bot = SimpleNamespace(send_message=AsyncMock())
    await _notify_tied_players(bot, "week", "2026-W41", [])  # 369-370: пусто
    assert bot.send_message.await_count == 0

    await _notify_tied_players(bot, "week", "2026-W41", [101, 102])
    assert bot.send_message.await_count == 2
    call = bot.send_message.await_args_list[0]
    assert call.args[0] == 101
    assert "недели 2026-W41" in call.args[1]  # 372, 376-382
    kb = call.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "claim:week"  # 387-389

    broken = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("сеть лежит")))
    await _notify_tied_players(broken, "month", "2026-10", [201])  # 390-394
    assert broken.send_message.await_count == 1
    month_call = broken.send_message.await_args_list[0]
    assert "месяца 2026-10" in month_call.args[1]  # 374
    month_kb = month_call.kwargs["reply_markup"]
    assert month_kb.inline_keyboard[0][0].text == "🗓 Заявить приз месяца"  # 385


async def test_claim_window_overwrites_and_garbage_is_none(session: AsyncSession) -> None:
    """Повторное открытие перезаписывает окно (329); битый JSON — окна нет (342-343)."""
    await _open_claim_window(session, "week", "2025-W01", [1, 2])
    await session.commit()
    await _open_claim_window(session, "week", "2025-W02", [3])  # строка 329
    await session.commit()
    window = await _load_claim_window(session, "week")
    assert window is not None
    assert window["period"] == "2025-W02" and window["players"] == [3]

    row = await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY)
    assert row is not None
    row.value = "это не json"
    await session.commit()
    assert await _load_claim_window(session, "week") is None  # 342-343


async def test_resolve_claim_window_disabled_clears_window(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claim_enabled=False: старое окно снимается, ничья не блокирует (418-419)."""
    monkeypatch.setattr(settings, "leaderboard_claim_enabled", False)
    session.add(
        WatcherState(
            key=WEEK_CLAIM_WINDOW_KEY,
            value=json.dumps(
                {"period": "2025-W01", "players": [1], "opened_at": datetime.now(UTC).isoformat()}
            ),
        )
    )
    await session.commit()
    placed = [(1, 5, to_nano(1), "w-1"), (2, 5, to_nano(1), "w-2")]
    ok = await _resolve_claim_window(
        session, None, "week", "2025-W02", placed, top_k=3, claims={}, now=datetime.now(UTC)
    )
    assert ok is True
    assert await _load_claim_window(session, "week") is None  # 418: окно очищено


async def test_resolve_claim_window_opens_and_notifies_on_tie(session: AsyncSession) -> None:
    """Ничья без окна: окно открывается, tied получают DM (440-446, включая 444)."""
    bot = SimpleNamespace(send_message=AsyncMock())
    placed = [(1, 5, to_nano(1), "w-1"), (2, 5, to_nano(1), "w-2")]
    ok = await _resolve_claim_window(
        session, bot, "week", "2026-W41", placed, top_k=3, claims={}, now=datetime.now(UTC)
    )
    assert ok is False
    window = await _load_claim_window(session, "week")
    assert window is not None and window["players"] == [1, 2]
    assert bot.send_message.await_count == 2  # 444: DM ушли


async def test_resolve_claim_window_repairs_opened_at(session: AsyncSession) -> None:
    """Наивная дата приводится к UTC (452); битая = now — окно ждёт (453-454)."""
    now = datetime.now(UTC)
    placed = [(1, 5, to_nano(1), "w-1"), (2, 5, to_nano(1), "w-2")]
    session.add(
        WatcherState(
            key=WEEK_CLAIM_WINDOW_KEY,
            value=json.dumps(
                {
                    "period": "2026-W41",
                    "players": [1, 2],
                    "opened_at": now.replace(tzinfo=None).isoformat(),
                }
            ),
        )
    )
    await session.commit()
    ok = await _resolve_claim_window(
        session, None, "week", "2026-W41", placed, top_k=3, claims={}, now=now
    )
    assert ok is False  # дедлайн не прошёл — ждём Claim (459-461)

    row = await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY)
    assert row is not None
    row.value = json.dumps(
        {"period": "2026-W41", "players": [1, 2], "opened_at": "позавчера-примерно"}
    )
    await session.commit()
    ok = await _resolve_claim_window(
        session, None, "week", "2026-W41", placed, top_k=3, claims={}, now=now
    )
    assert ok is False  # opened_at=now → окно ещё живо (453-454)


async def test_mark_ready_overwrites_and_skips_broken_finished(
    session: AsyncSession,
) -> None:
    """Повторная метка перезаписывается (540, 555); finished без opens_at — выход (567)."""
    await mark_week_leaderboard_ready(session, "2025-W01")
    await mark_week_leaderboard_ready(session, "2025-W02")  # ветка else (555)
    row = await session.get(WatcherState, WEEK_READY_KEY)
    assert row is not None and row.value == "2025-W02"

    await mark_month_leaderboard_ready(session, "2025-01")
    await mark_month_leaderboard_ready(session, "2025-02")  # ветка else (540)
    row = await session.get(WatcherState, MONTH_READY_KEY)
    assert row is not None and row.value == "2025-02"

    await mark_leaderboards_for_finished(session, None)
    await mark_leaderboards_for_finished(session, SimpleNamespace(opens_at=None, day_index=1))


async def test_rank_helpers_on_empty_inputs(session: AsyncSession) -> None:
    """Пустые окна/списки: без ошибок отдают пустые результаты (161, 251, 282)."""
    past = (datetime(2001, 1, 1, tzinfo=UTC), datetime(2001, 1, 3, tzinfo=UTC))
    assert await _top_correct_voters(session, *past) == []  # 161
    assert await _claim_times(session, "week", []) == {}  # 251
    assert _prize_tied_groups([], top_k=3) == []  # 282


def test_weighted_amounts_without_weights_is_empty() -> None:
    """Пустой список весов — сумм к распределению нет (860)."""
    assert _weighted_amounts(to_nano(1), [9001], []) == []


async def _cleanup_month_scene(
    session: AsyncSession, rounds: list[Round], pids: list[int]
) -> None:
    """Сносит все следы месячной сцены (общий finally для краёв)."""
    await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
    await session.execute(
        WatcherState.__table__.delete().where(
            WatcherState.key.in_([MARKER_KEY, MONTH_READY_KEY, MONTH_CLAIM_WINDOW_KEY])
        )
    )
    await session.execute(LeaderboardPot.__table__.delete())
    for round_row in rounds:
        await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
        await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
        await session.delete(round_row)
    for pid in pids:
        player = await session.get(Player, pid)
        if player is not None:
            await session.delete(player)
    await session.commit()


async def test_month_settle_waits_for_unfinished_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Незакрытый день месяца: метка не двигается (650-651)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = 830_000
    async with SessionLocal() as session:
        rounds, _prev = await _seed_month_scene(session, base, {base: 5}, 0.5)
        try:
            rounds[0].status = RoundStatus.OPEN
            await session.commit()
            assert await settle_month_if_due(bot=None) is False
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await _cleanup_month_scene(session, rounds, [base])


async def test_month_candidates_without_wallet_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ни у одного лидера нет кошелька: копилка ждёт, метка стоит (693-698)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = 840_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.5)
        try:
            for pid in (base, base + 1):
                player = await session.get(Player, pid)
                assert player is not None
                player.wallet_address = None
            await session.commit()
            assert await settle_month_if_due(bot=None) is False
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
            pot = (
                await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == prev_key)
                )
            ).scalar_one()
            assert pot.nanotons == to_nano(0.5)
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])


async def test_month_bad_weights_hold_pot(monkeypatch: pytest.MonkeyPatch) -> None:
    """monthly_prize_weights без чисел: ошибка конфига, горш не тронут (706-714)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_weights", "не-числа")
    base = 850_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.5)
        try:
            assert await settle_month_if_due(bot=None) is False
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
            pot = (
                await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == prev_key)
                )
            ).scalar_one()
            assert pot.nanotons == to_nano(0.5)
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])


async def test_month_classic_mode_without_payable_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Классика top_k=1: платить некому — метка не двигается (741-749)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 1)
    base = 860_000
    async with SessionLocal() as session:
        rounds, _prev = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.5)
        try:
            for pid in (base, base + 1):
                player = await session.get(Player, pid)
                assert player is not None
                player.wallet_address = None
            await session.commit()
            assert await settle_month_if_due(bot=None) is False
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])


async def test_month_empty_payments_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Страховка пустых выплат не двигает метку (756-763)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 1)
    monkeypatch.setattr("app.leaderboard.split_equal", lambda _total, _ids: {})
    base = 870_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.5)
        try:
            assert await settle_month_if_due(bot=None) is False
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is None or marker.value == ""
            pot = (
                await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == prev_key)
                )
            ).scalar_one()
            assert pot.nanotons == to_nano(0.5)
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])


async def test_month_dust_recarries_into_existing_current_pot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пыль прибавляется к УЖЕ существующей копилке текущего месяца (781-791)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 2)
    monkeypatch.setattr(settings, "monthly_prize_weights", "70,30")
    base = 880_000
    current_month = datetime.now(UTC).strftime("%Y-%m")
    async with SessionLocal() as session:
        rounds, _prev = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.05)
        session.add(LeaderboardPot(month=current_month, nanotons=to_nano(0.3)))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            pot = (
                await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == current_month)
                )
            ).scalar_one()
            recarry = to_nano(0.05) - to_nano(0.05) * 70 // 100
            assert pot.nanotons == to_nano(0.3) + recarry
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])


async def test_month_admin_alert_survives_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой отправки алерта админу не валит месячную выплату (824-827)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 1)
    monkeypatch.setattr(settings, "admin_ids", "7777")
    base = 890_000
    async with SessionLocal() as session:
        rounds, prev_key = await _seed_month_scene(session, base, {base: 5, base + 1: 3}, 0.5)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Telegram лёг")))
        try:
            assert await settle_month_if_due(bot=bot) is True
            assert bot.send_message.await_count >= 1  # дошли до send (825)
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is not None and marker.value == prev_key
            payouts = (
                await session.execute(select(Payout).where(Payout.kind == "leaderboard"))
            ).scalars().all()
            assert len(payouts) == 1
        finally:
            await _cleanup_month_scene(session, rounds, [base, base + 1])
