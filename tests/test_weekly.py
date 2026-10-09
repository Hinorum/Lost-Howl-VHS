"""Тесты недельного лидерборда: ISO-недели, доли мест, выплата топ-3 игроков.

Ничья по верным путям решается большим вкладом Gram в неделю, затем — кто
раньше нажал Claim в /start; только потом меньшим player_id. Места — топ-3
ИГРОКА (не ступеней счёта) с долями 50/30/20.
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
from app.core.registry import RUN_START_KEY, WEEK_CLAIM_WINDOW_KEY
from app.db import SessionLocal
from app.leaderboard import (
    WEEK_READY_KEY,
    WEEKLY_MARKER_KEY,
    _rank_window,
    _week_prize_amounts,
    settle_week_if_due,
)
from app.models import (
    LeaderboardClaim,
    MemoryHit,
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
from app.rounds.anchor import default_anchor
from app.ton_utils import to_nano
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds


@pytest.fixture(autouse=True)
def _week_prize_contract():
    """Доли мест тестируются как контракт 50/30/20 независимо от env-переопределения."""
    prev = settings.weekly_prize_pcts
    settings.weekly_prize_pcts = "50,30,20"
    yield
    settings.weekly_prize_pcts = prev


async def _set_week_ready(session: AsyncSession, week_key: str) -> None:
    """Ставит флаг готовности недельного лидерборда (эпилог последнего дня недели записан)."""
    session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
    await session.commit()


async def _seed_expired_week_window(session: AsyncSession, week_key: str, players: list[int]) -> None:
    """Ставит окно Claim, дедлайн которого давно прошёл: выплата может идти сразу."""
    opened_at = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    session.add(
        WatcherState(
            key=WEEK_CLAIM_WINDOW_KEY,
            value=json.dumps({"period": week_key, "players": players, "opened_at": opened_at}),
        )
    )
    await session.commit()


def test_iso_week_key_and_bounds() -> None:
    moment = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)  # понедельник
    assert iso_week_key(moment) == "2026-W35"
    assert iso_week_key(moment.replace(tzinfo=None)) == "2026-W35"
    start, end = week_bounds("2026-W35")
    assert start == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 31, 0, 0, tzinfo=UTC)


def test_previous_week_key_across_year_boundary() -> None:
    assert previous_week_key(datetime(2026, 8, 26, tzinfo=UTC)) == "2026-W34"
    assert previous_week_key(datetime(2026, 1, 1, tzinfo=UTC)) == "2025-W52"


def test_parse_prize_pcts_filters_garbage_and_caps_at_three() -> None:
    assert parse_prize_pcts("20,30,50") == [20, 30, 50]
    assert parse_prize_pcts(" 10 , x , -5 , 40 ") == [10, 40]
    assert parse_prize_pcts("10,20,30,40") == [10, 20, 30]
    assert parse_prize_pcts("") == []


def test_week_prize_amounts_dust_and_rollover() -> None:
    amounts, rolled = _week_prize_amounts(to_nano(10), 3)
    assert amounts == [
        to_nano(10) * 50 // 100,
        to_nano(10) * 30 // 100,
        to_nano(10) * 20 // 100,
    ]
    assert rolled == 0
    # Достойных только двое: их места платим, незаполненное третье — в перенос.
    amounts, rolled = _week_prize_amounts(to_nano(10), 2)
    assert amounts == [to_nano(10) * 50 // 100, to_nano(10) * 30 // 100]
    assert sum(amounts) + rolled == to_nano(10)
    # Пыль не теряется и не уезжает в перенос.
    amounts, rolled = _week_prize_amounts(999, 3)
    assert sum(amounts) + rolled == 999


async def _seed_closed_round(session: AsyncSession, day_index: int, opens_at: datetime, winner_card: int = 0) -> Round:
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=opens_at,
        voting_ends_at=opens_at + timedelta(hours=23),
        tally_ends_at=opens_at + timedelta(hours=24),
        winner_card=winner_card,
        vote_counts_json="{}",
        payouts_finalized=True,
    )
    session.add(round_row)
    await session.flush()
    return round_row


def _set_stake(session: AsyncSession, round_row: Round, pid: int, status: str = "confirmed") -> None:
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=pid,
            amount_nanotons=to_nano(1),
            tx_hash="tx_" + os.urandom(16).hex(),
            status=status,
        )
    )


async def test_settle_week_pays_top3_by_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сильнейшие игроки получают 50%/30%; без кошелька или дней — мимо, доля переносится."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 850_000
    pid_best, pid_second, pid_nowallet, pid_lazy = (base + i for i in range(4))
    wallets = {
        pid_best: "0:" + os.urandom(32).hex(),
        pid_second: "0:" + os.urandom(32).hex(),
        pid_lazy: "0:" + os.urandom(32).hex(),
    }
    prev_start, prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_best, username="best", wallet_address=wallets[pid_best], wallet_verified=True),
                Player(id=pid_second, username="second", wallet_address=wallets[pid_second], wallet_verified=True),
                Player(id=pid_nowallet, username="nowallet"),  # лидер без кошелька — пропуск
                Player(id=pid_lazy, username="lazy", wallet_address=wallets[pid_lazy], wallet_verified=True),  # 2 дня — мало
            ]
        )
        rounds: list[Round] = []
        # best: 5 верных за неделю; second: 4 верных; lazy: 2 верных; nowallet: 7 верных.
        plan = {pid_best: 5, pid_second: 4, pid_nowallet: 7, pid_lazy: 2}
        day = 800_000
        for offset in range(7):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            # Ставили в течение недели только достойные (best и second) — один день.
            if offset == 0:
                for pid in (pid_best, pid_second):
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "weekly").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            # Места только у достойных: best (место 1, 50%) и second (место 2, 30%).
            assert [(p.player_id, p.amount_nanotons) for p in rows] == [
                (pid_best, pot_total * 50 // 100),
                (pid_second, pot_total * 30 // 100),
            ]
            assert all(p.dest_address == wallets[p.player_id] for p in rows)
            # Незаполненное третье место (20%) переносится в копилку текущей недели.
            current_pot = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert current_pot.nanotons == pot_total - pot_total * 50 // 100 - pot_total * 30 // 100
            # Выплаченная неделя закрыта, метка переведена.
            assert (await session.scalar(select(WeeklyPot.nanotons).where(WeeklyPot.week == week_key))) is None
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is not None and marker.value == previous_week_key()
            # Идемпотентность.
            assert await settle_week_if_due(bot=None) is False
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_best, pid_second, pid_nowallet, pid_lazy):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_pays_verified_wallet_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неподтверждённый кошелёк не получает копилку: платит только bv:-доказанный адрес.

    Топ по верным путям с привязанным, но неподтверждённым кошельком — мимо,
    как и без кошелька: приз не должен уйти на адрес, чьё владение не доказано.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 860_000
    pid_unverified, pid_verified = base, base + 1
    wallet_proven = "0:" + os.urandom(32).hex()
    prev_start, _prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                # Лидер недели, но кошелёк не подтверждён (bv: не пройден).
                Player(id=pid_unverified, username="shadow", wallet_address="0:" + os.urandom(32).hex()),
                Player(id=pid_verified, username="proven", wallet_address=wallet_proven, wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        # shadow — 7 верных, proven — 5: без верификации лидер всё равно мимо.
        plan = {pid_unverified: 7, pid_verified: 5}
        day = 800_100
        for offset in range(7):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in (pid_unverified, pid_verified):
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "weekly").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [p.player_id for p in rows] == [pid_verified]
            assert rows[0].dest_address == wallet_proven and rows[0].amount_nanotons > 0
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_unverified, pid_verified):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_pays_top_three_individuals_not_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Места — топ-3 ИГРОКА по верным путям, а не ступени счёта: 7-7-5 платятся целиком.

    Двое с 7 верными и одинаковым вкладом Gram не делят ступень: меньший
    player_id забирает первое место (50%), второй — второе (30%), третьим идёт
    одиночка со счётом 5 (20%). Игрок со счётом 6, но без кошелька, не виден;
    счёт 3 — четвёртый, призов не имеет.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 940_000
    pid_a7, pid_b7, pid_c5, pid_d3, pid_e6_lazy = (base + i for i in range(5))
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_a7, pid_b7, pid_c5, pid_d3)}
    prev_start, _prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a7, username="alpha", wallet_address=wallets[pid_a7], wallet_verified=True),
                Player(id=pid_b7, username="beta", wallet_address=wallets[pid_b7], wallet_verified=True),
                Player(id=pid_c5, username="gamma", wallet_address=wallets[pid_c5], wallet_verified=True),
                Player(id=pid_d3, username="delta", wallet_address=wallets[pid_d3], wallet_verified=True),
                Player(id=pid_e6_lazy, username="lazy"),
            ]
        )
        rounds: list[Round] = []
        plan = {pid_a7: 7, pid_b7: 7, pid_c5: 5, pid_d3: 3, pid_e6_lazy: 6}
        day = 880_000
        for offset in range(7):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            # delta: 4 дня участия при трёх верных — четвёртый день промахом.
            if offset == 3:
                session.add(Vote(round_id=round_row.id, player_id=pid_d3, card_position=1))
            # Ставили в течение недели все с кошельком (кроме lazy).
            if offset == 0:
                for pid in (pid_a7, pid_b7, pid_c5, pid_d3):
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            # a7 и b7 абсолютно равны (верность и Gram) — ничья блокирует выплату
            # до дедлайна Claim; в тесте окно уже протухло, платим по тайбрейку id.
            await _seed_expired_week_window(session, week_key, [pid_a7, pid_b7])
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "weekly").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            first = pot_total * 50 // 100
            second = pot_total * 30 // 100
            third = pot_total * 20 // 100
            amounts = {(p.player_id): p.amount_nanotons for p in rows}
            # Места — игроки: a7 и b7 равны (верность и Gram) — меньший id первым.
            assert amounts[pid_a7] == first
            assert amounts[pid_b7] == second
            assert amounts[pid_c5] == third
            assert pid_d3 not in amounts  # четвёртый по верным путям — без приза
            assert pid_e6_lazy not in amounts  # без кошелька и стажа — вне радара
            assert len(rows) == 3
            assert all(p.dest_address == wallets[p.player_id] for p in rows)
            # Копилка недели выплачена до нанотона.
            assert sum(amounts.values()) == pot_total
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_a7, pid_b7, pid_c5, pid_d3, pid_e6_lazy):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_two_tied_roll_third_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Двое абсолютно равных (верность и Gram): первое и второе места, третье — в перенос."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 950_000
    pids = [base, base + 1]
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    prev_start, _prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
Player(id=pids[0], username="one", wallet_address=wallets[pids[0]], wallet_verified=True),
                    Player(id=pids[1], username="two", wallet_address=wallets[pids[1]], wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        day = 890_000
        for offset in range(4):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in pids:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in pids:
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            # Двое абсолютно равных — окно Claim открыто и его дедлайн прошёл:
            # без заявок место решает меньший player_id.
            await _seed_expired_week_window(session, week_key, pids)
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (await session.execute(select(Payout).where(Payout.kind == "weekly")))
                .scalars()
                .all()
            )
            first = pot_total * 50 // 100
            second = pot_total * 30 // 100
            by_pid = {p.player_id: p.amount_nanotons for p in rows}
            assert by_pid[pids[0]] == first  # ничья по Gram — меньший player_id выше
            assert by_pid[pids[1]] == second
            current_pot = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert current_pot.nanotons == pot_total - first - second  # 20% — в перенос
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
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


async def test_settle_week_waits_without_eligible_players(monkeypatch: pytest.MonkeyPatch) -> None:
    """Достойных нет — метка не двигается, горш ждёт следующего цикла."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 860_000
    pid = base
    prev_start, _prev_end = week_bounds(previous_week_key())
    async with SessionLocal() as session:
        session.add(Player(id=pid, username="lonely"))  # без кошелька
        round_row = await _seed_closed_round(session, 810_001, prev_start + timedelta(hours=11))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(3)))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            assert await settle_week_if_due(bot=None) is False
            pot_left = await session.scalar(select(WeeklyPot.nanotons).where(WeeklyPot.week == week_key))
            assert pot_left == to_nano(3)
            assert await session.get(WatcherState, WEEKLY_MARKER_KEY) is None
        finally:
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
            await session.delete(round_row)
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_settle_week_postponed_until_last_day_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Последний день недели ещё не финализирован — выплату откладываем целиком."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = 870_000
    async with SessionLocal() as session:
        prev_start, prev_end = week_bounds(previous_week_key())
        # Незакрытый день в прошлой неделе (последний день закрывается уже в понедельник).
        unfinished = Round(
            day_index=820_001,
            status=RoundStatus.TALLYING,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="text",

            opens_at=prev_end - timedelta(hours=13),
            voting_ends_at=prev_end + timedelta(hours=10),
            tally_ends_at=prev_end + timedelta(hours=11),
            winner_card=None,
            vote_counts_json="{}",
        )
        session.add(unfinished)
        session.add(Player(id=base, username="u", wallet_address="0:" + os.urandom(32).hex(), wallet_verified=True))
        done = await _seed_closed_round(session, 820_002, prev_start + timedelta(hours=11))
        session.add(Vote(round_id=done.id, player_id=base, card_position=0))
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(1)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            assert await session.get(WatcherState, WEEKLY_MARKER_KEY) is None
        finally:
            await session.execute(WeeklyPot.__table__.delete())
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.delete(unfinished)
            await session.execute(Vote.__table__.delete().where(Vote.round_id == done.id))
            await session.delete(done)
            player = await session.get(Player, base)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_rank_window_orders_ties_by_gram_then_id(session: AsyncSession) -> None:
    """Равные по верным путям: больший вклад Gram выше; при равном Gram — меньший id.

    Дни участия больше не влияют на порядок — они лишь порог для стажа.
    """
    now = datetime.now(UTC)
    steady, lucky = 900_001, 900_002
    session.add_all([Player(id=steady, username="steady"), Player(id=lucky, username="lucky")])
    # steady: 3 дня участия, из них 2 верных (в последний день промахнулся);
    # lucky: ровно те же 2 верных, но всего за 2 дня. Ставок нет — вклад Gram 0.
    rounds = []
    for offset in range(3):
        round_row = await _seed_closed_round(session, 830_000 + offset, now - timedelta(days=offset + 1))
        rounds.append(round_row)
        session.add(Vote(round_id=round_row.id, player_id=steady, card_position=0 if offset < 2 else 1))
        if offset < 2:
            session.add(Vote(round_id=round_row.id, player_id=lucky, card_position=0))
    await session.commit()

    rows = await _rank_window(session, now - timedelta(days=10), now + timedelta(minutes=1), by="opens_at")
    assert rows[0] == (steady, 2, 3, 0)
    assert rows[1] == (lucky, 2, 2, 0)

    await session.execute(Vote.__table__.delete())
    for round_row in rounds:
        await session.delete(round_row)


async def test_rank_window_gram_breaks_tie(session: AsyncSession) -> None:
    """Тот же счёт верных: игрок с бОльшим вкладом Gram в неделе стоит выше."""
    now = datetime.now(UTC)
    light, heavy = 900_101, 900_102  # heavy выше по id, но побеждать должен по Gram
    session.add_all([Player(id=light, username="light"), Player(id=heavy, username="heavy")])
    round_row = await _seed_closed_round(session, 830_500, now - timedelta(hours=5))
    session.add(Vote(round_id=round_row.id, player_id=light, card_position=0))
    session.add(Vote(round_id=round_row.id, player_id=heavy, card_position=0))
    session.add(Stake(round_id=round_row.id, player_id=light, amount_nanotons=to_nano(1), tx_hash="tx_light", status="confirmed"))
    session.add(Stake(round_id=round_row.id, player_id=heavy, amount_nanotons=to_nano(3), tx_hash="tx_heavy", status="confirmed"))
    await session.commit()

    rows = await _rank_window(session, now - timedelta(days=1), now + timedelta(minutes=1), by="opens_at")
    assert rows[0] == (heavy, 1, 1, to_nano(3))
    assert rows[1] == (light, 1, 1, to_nano(1))

    await session.execute(Vote.__table__.delete())
    await session.execute(Stake.__table__.delete())
    await session.delete(round_row)


async def test_settle_week_excludes_player_without_stake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одинаково достойные, но не поставивший игрок не получает приз недели."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 960_000
    pid_staked, pid_no_stake = base, base + 1
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_staked, pid_no_stake)}
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_staked, username="staked", wallet_address=wallets[pid_staked], wallet_verified=True),
                Player(id=pid_no_stake, username="nostake", wallet_address=wallets[pid_no_stake], wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        day = 840_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_staked, pid_no_stake):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                _set_stake(session, round_row, pid_staked)
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(10)))
        await _set_week_ready(session, previous_week_key())
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            paid = {
                p.player_id
                for p in (
                    await session.execute(select(Payout).where(Payout.kind == "weekly"))
                ).scalars()
            }
            assert pid_staked in paid
            assert pid_no_stake not in paid
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_staked, pid_no_stake):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_refunded_stake_counts_rejected_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Возврат ставки (refunded) и нарушитель (rejected) НЕ считаются для лидерборда."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 970_000
    pid_refunded, pid_rejected, pid_confirmed = base, base + 1, base + 2
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_refunded, pid_rejected, pid_confirmed)}
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_refunded, username="refunded", wallet_address=wallets[pid_refunded], wallet_verified=True),
                Player(id=pid_rejected, username="rejected", wallet_address=wallets[pid_rejected], wallet_verified=True),
                Player(id=pid_confirmed, username="confirmed", wallet_address=wallets[pid_confirmed], wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        day = 845_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_refunded, pid_rejected, pid_confirmed):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                _set_stake(session, round_row, pid_refunded, status="refunded")
                _set_stake(session, round_row, pid_rejected, status="rejected")
                _set_stake(session, round_row, pid_confirmed, status="confirmed")
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(10)))
        await _set_week_ready(session, previous_week_key())
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            paid = {
                p.player_id
                for p in (
                    await session.execute(select(Payout).where(Payout.kind == "weekly"))
                ).scalars()
            }
            assert pid_confirmed in paid
            assert pid_refunded not in paid
            assert pid_rejected not in paid
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_refunded, pid_rejected, pid_confirmed):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_rank_window_honors_correct_cap(monkeypatch) -> None:
    """Анти-гринд: верные пути сверх потолка не надувают счёт недели."""
    base = 970_200
    pid_a, pid_b = base, base + 1
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a, username="grinder"),
                Player(id=pid_b, username="steady"),
            ]
        )
        rounds: list[Round] = []
        for offset in range(5):
            round_row = await _seed_closed_round(
                session, 846_000 + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            # grinder верно каждый день, steady — лишь дважды.
            session.add(Vote(round_id=round_row.id, player_id=pid_a, card_position=0))
            if offset < 2:
                session.add(Vote(round_id=round_row.id, player_id=pid_b, card_position=0))
        await session.commit()
        try:
            top = await _rank_window(session, prev_start, week_bounds(previous_week_key())[1], by="opens_at", limit=10)
            scored = {pid: correct for pid, correct, _days, _gram in top}
            assert scored[pid_a] == 5  # без потолка — все пять верных
            monkeypatch.setattr(settings, "leaderboard_correct_cap", 2)
            try:
                capped = await _rank_window(session, prev_start, week_bounds(previous_week_key())[1], by="opens_at", limit=10)
                assert {pid: c for pid, c, _d, _g in capped}[pid_a] == 2  # срезано до потолка
            finally:
                monkeypatch.undo()
        finally:
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_a, pid_b):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_claim_breaks_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Равные по верным путям и вкладу Gram: кто раньше нажал Claim — выше."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 980_000
    pid_early, pid_late = base, base + 1
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_early, pid_late)}
    prev_start, _ = week_bounds(previous_week_key())
    week_key = previous_week_key()

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_early, username="early", wallet_address=wallets[pid_early], wallet_verified=True),
                Player(id=pid_late, username="late", wallet_address=wallets[pid_late], wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        day = 847_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_early, pid_late):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in (pid_early, pid_late):
                    _set_stake(session, round_row, pid)
        session.add(
            LeaderboardClaim(
                player_id=pid_early, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=1, hours=1),
            )
        )
        session.add(
            LeaderboardClaim(
                player_id=pid_late, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=5, hours=1),
            )
        )
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            # Ранний Claim перевешивает даже равенство по id-порядку не нарушая 50/30.
            assert by_pid == {
                pid_early: to_nano(10) * 50 // 100,
                pid_late: to_nano(10) * 30 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY)
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_early, pid_late):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_claimer_beats_silent_rival(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заявивший Claim опережает равного, который кнопку не жал, даже при бОльшем id."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 981_000
    pid_quiet, pid_claimer = base, base + 1  # claimer выше по id
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_quiet, pid_claimer)}
    prev_start, _ = week_bounds(previous_week_key())
    week_key = previous_week_key()

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_quiet, username="quiet", wallet_address=wallets[pid_quiet], wallet_verified=True),
                Player(id=pid_claimer, username="claimer", wallet_address=wallets[pid_claimer], wallet_verified=True),
            ]
        )
        rounds: list[Round] = []
        day = 848_000
        for offset in range(2):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_quiet, pid_claimer):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in (pid_quiet, pid_claimer):
                    _set_stake(session, round_row, pid)
        session.add(
            LeaderboardClaim(
                player_id=pid_claimer, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=1, hours=1),
            )
        )
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        # Тихий соперник так и не нажал Claim: дедлайн окна истёк — заявивший
        # опережает молчуна при бОльшем id.
        await _seed_expired_week_window(session, week_key, [pid_quiet, pid_claimer])
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            assert by_pid == {
                pid_claimer: to_nano(10) * 50 // 100,
                pid_quiet: to_nano(10) * 30 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY)
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_quiet, pid_claimer):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


def test_weighted_amounts_distributes_full_pot(monkeypatch) -> None:
    """Сглаживание месячной копилки: веса нормируются по получателям."""
    from app.leaderboard import _weighted_amounts

    payable = [(1, 9, "w1"), (2, 7, "w2"), (3, 5, "w3")]
    weights = [60, 30, 10]
    amounts = _weighted_amounts(1000, payable, weights)
    assert sum(amounts) == 1000
    assert amounts == [600, 300, 100]

    # Весов меньше, чем получателей — последний вес повторяется, горш делится.
    amounts = _weighted_amounts(1000, payable[:2], [100])
    assert sum(amounts) == 1000
    assert amounts[0] == 500 and amounts[1] == 500

    assert _weighted_amounts(0, payable, weights) == []
    assert _weighted_amounts(1000, [], weights) == []


async def _seed_week_tie_scene(
    session: AsyncSession,
    base: int,
    n_days: int,
) -> tuple[int, int, list[Round]]:
    """Два абсолютно равных игрока за неделю: (pid_one, pid_two, rounds)."""
    pids = [base, base + 1]
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    session.add_all(
        [
            Player(id=pids[0], username="tie1", wallet_address=wallets[pids[0]], wallet_verified=True),
            Player(id=pids[1], username="tie2", wallet_address=wallets[pids[1]], wallet_verified=True),
        ]
    )
    prev_start, _ = week_bounds(previous_week_key())
    rounds: list[Round] = []
    for offset in range(n_days):
        round_row = await _seed_closed_round(
            session, 848_500 + offset, prev_start + timedelta(days=offset, hours=11)
        )
        rounds.append(round_row)
        for pid in pids:
            session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        if offset == 0:
            for pid in pids:
                _set_stake(session, round_row, pid)
    session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(10)))
    await session.commit()
    return pids[0], pids[1], rounds


async def test_settle_week_tie_opens_claim_window_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ничья без заявок: выплата блокируется, окно Claim открывается с tied-игроками."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    async with SessionLocal() as session:
        pid_one, pid_two, rounds = await _seed_week_tie_scene(session, 982_000, 3)
        try:
            await _set_week_ready(session, week_key)
            assert await settle_week_if_due(bot=None) is False
            paid = (
                (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars().all()
            )
            assert paid == []
            # Метка не сдвинута (может существовать пустой ряд-замок).
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is None or marker.value == ""
            window_row = await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY)
            assert window_row is not None
            window = json.loads(window_row.value)
            assert window["period"] == week_key
            assert set(window["players"]) == {pid_one, pid_two}
            # Повторный вызов (дедлайн не прошёл) — снова ждём.
            assert await settle_week_if_due(bot=None) is False
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key.in_(
                    [WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY]
                ))
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_one, pid_two):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_tie_pays_after_all_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все tied подали Claim — ничья разрешена временем заявок, выплата идёт."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    base_t = datetime.now(UTC)
    async with SessionLocal() as session:
        pid_one, pid_two, rounds = await _seed_week_tie_scene(session, 983_000, 2)
        try:
            await _set_week_ready(session, week_key)
            # Первый заход открывает окно и ждёт.
            assert await settle_week_if_due(bot=None) is False
            session.add(
                LeaderboardClaim(
                    player_id=pid_one, kind="week", period=week_key, claimed_at=base_t + timedelta(hours=1)
                )
            )
            session.add(
                LeaderboardClaim(
                    player_id=pid_two, kind="week", period=week_key, claimed_at=base_t + timedelta(hours=2)
                )
            )
            await session.commit()
            # Все tied заявились — платим: раньше заявивший выше.
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            assert by_pid == {
                pid_one: to_nano(10) * 50 // 100,
                pid_two: to_nano(10) * 30 // 100,
            }
            # Окно закрыто после выплаты.
            assert await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY) is None
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key.in_(
                    [WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY]
                ))
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_one, pid_two):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_tie_reopens_stale_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окно от прошлого периода не наследуется: под новый период открывается свежее."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    week_key = previous_week_key()
    async with SessionLocal() as session:
        pid_one, pid_two, rounds = await _seed_week_tie_scene(session, 984_000, 2)
        try:
            await _seed_expired_week_window(session, "2025-W01", [pid_one, pid_two])
            await _set_week_ready(session, week_key)
            # Окно мертво (другой период): ничья открывает новое и ждёт.
            assert await settle_week_if_due(bot=None) is False
            window_row = await session.get(WatcherState, WEEK_CLAIM_WINDOW_KEY)
            assert window_row is not None
            window = json.loads(window_row.value)
            assert window["period"] == week_key
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key.in_(
                    [WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY]
                ))
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_one, pid_two):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


# --- Слепые ветки недельной выплаты: якорь, копилки, церемония ----------------


async def _cleanup_week_scene(
    session: AsyncSession, rounds: list[Round], pids: list[int]
) -> None:
    """Сносит все следы недельной сцены (общий finally для краёв)."""
    await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
    await session.execute(
        WatcherState.__table__.delete().where(
            WatcherState.key.in_(
                [WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY, WEEK_READY_KEY, RUN_START_KEY]
            )
        )
    )
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


async def _wipe_week_keys(session: AsyncSession) -> None:
    """Чистит ключи watcher'а и копилки перед постановкой своей сцены."""
    await session.execute(
        WatcherState.__table__.delete().where(
            WatcherState.key.in_(
                [WEEKLY_MARKER_KEY, WEEK_CLAIM_WINDOW_KEY, WEEK_READY_KEY, RUN_START_KEY]
            )
        )
    )
    await session.execute(WeeklyPot.__table__.delete())
    await session.commit()


async def test_settle_week_without_pots_advances_marker() -> None:
    """Копилок прошлых недель нет: метка всё равно двигается, False (947-950)."""
    week_key = previous_week_key()
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        await _set_week_ready(session, week_key)
        try:
            assert await settle_week_if_due(bot=None) is False
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is not None and marker.value == week_key
        finally:
            await _cleanup_week_scene(session, [], [])


async def test_settle_week_waits_for_unfinished_day() -> None:
    """Незакрытый день недели: платить рано, метка не двигается (969-970)."""
    week_key = previous_week_key()
    prev_start, _ = week_bounds(week_key)
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        round_row = await _seed_closed_round(
            session, 890_000, prev_start + timedelta(days=1, hours=11)
        )
        round_row.status = RoundStatus.OPEN
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(5)))
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await _cleanup_week_scene(session, [round_row], [])


async def test_settle_week_short_start_relaxes_min_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сброс за 1-3 дня до понедельника: порог 4 дня снят (983-992)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    week_key = previous_week_key()
    prev_start, period_end = week_bounds(week_key)
    start = period_end - timedelta(days=2)  # суббота перед концом недели
    pid = 891_001
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        session.add(
            Player(
                id=pid,
                username=f"p{pid}",
                wallet_address="0:" + os.urandom(32).hex(),
                wallet_verified=True,
            )
        )
        round_row = await _seed_closed_round(
            session, 891_000, prev_start + timedelta(days=2, hours=11)
        )
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        _set_stake(session, round_row, pid)
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        session.add(
            WatcherState(key=RUN_START_KEY, value=json.dumps(default_anchor(start)))
        )
        await session.commit()
        try:
            # 1 день голосования < weekly_min_days=4 — спасает только якорь.
            assert await settle_week_if_due(bot=None) is True
            payouts = (
                await session.execute(select(Payout).where(Payout.kind == "weekly"))
            ).scalars().all()
            assert [p.player_id for p in payouts] == [pid]
        finally:
            await _cleanup_week_scene(session, [round_row], [pid])


async def test_settle_week_corrupt_anchor_keeps_strict_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Битый якорь не валит расчёт (993): порог остаётся строгим, метка стоит."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    week_key = previous_week_key()
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(5)))
        await _set_week_ready(session, week_key)
        session.add(WatcherState(key=RUN_START_KEY, value=json.dumps({"key": 12345, "dom": 5})))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is None or marker.value == ""
        finally:
            await _cleanup_week_scene(session, [], [])


async def test_settle_week_dust_rolls_into_existing_pot_surviving_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пыль катится в СУЩЕСТВУЮЩУЮ копилку (1073); сбой номинации (1079-1080)
    и рассылки церемонии (1107-1108) не валят выплату."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    monkeypatch.setattr(
        "app.leaderboard._memory_nomination",
        AsyncMock(side_effect=RuntimeError("память упала")),
    )
    monkeypatch.setattr(
        "app.broadcast.whisper_to_chats",
        AsyncMock(side_effect=RuntimeError("рассылка упала")),
    )
    week_key = previous_week_key()
    prev_start, _ = week_bounds(week_key)
    pid = 892_001
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        session.add(
            Player(
                id=pid,
                username=f"p{pid}",
                wallet_address="0:" + os.urandom(32).hex(),
                wallet_verified=True,
            )
        )
        round_row = await _seed_closed_round(
            session, 892_000, prev_start + timedelta(days=1, hours=11)
        )
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        _set_stake(session, round_row, pid)
        pot_total = to_nano(0.01)  # вся доля ниже min_payout (0.02 Gram)
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        current_week = iso_week_key(datetime.now(UTC))
        session.add(WeeklyPot(week=current_week, nanotons=to_nano(0.7)))
        await _set_week_ready(session, week_key)
        await session.commit()
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            assert await settle_week_if_due(bot=bot) is True
            current = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week == current_week))
            ).scalar_one()
            assert current.nanotons == to_nano(0.7) + pot_total  # 1073: +=
            payouts = (
                await session.execute(select(Payout).where(Payout.kind == "weekly"))
            ).scalars().all()
            assert payouts == []
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is not None and marker.value == week_key
        finally:
            await _cleanup_week_scene(session, [round_row], [pid])


async def test_settle_week_ceremony_carries_memory_nomination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Церемония несёт номинацию памяти (1097, «1 находка» — 902), шёпот в чаты
    уходит (1103-1106), сбой алерта админу — warning без падения (1110-1115)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    monkeypatch.setattr(settings, "admin_ids", "7777")
    whisper = AsyncMock()
    monkeypatch.setattr("app.broadcast.whisper_to_chats", whisper)
    week_key = previous_week_key()
    prev_start, _ = week_bounds(week_key)
    pid = 893_001
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        session.add(
            Player(
                id=pid,
                username=f"p{pid}",
                wallet_address="0:" + os.urandom(32).hex(),
                wallet_verified=True,
            )
        )
        round_row = await _seed_closed_round(
            session, 893_000, prev_start + timedelta(days=1, hours=11)
        )
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        session.add(MemoryHit(player_id=pid, round_id=round_row.id))
        _set_stake(session, round_row, pid)
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        await session.commit()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Telegram лёг")))
        try:
            assert await settle_week_if_due(bot=bot) is True
            assert whisper.await_count == 1
            ceremony = whisper.await_args.args[1]
            assert "🏆 Итоги недели Стаи" in ceremony
            assert "🧠 Самый памятливый пёс недели" in ceremony  # 1097
            assert "1 находка" in ceremony  # 902
            assert bot.send_message.await_count >= 1  # 1110-1115
        finally:
            await session.execute(
                MemoryHit.__table__.delete().where(MemoryHit.round_id == round_row.id)
            )
            await session.commit()
            await _cleanup_week_scene(session, [round_row], [pid])


async def test_memory_nomination_pluralizes_genitive_counts(
    session: AsyncSession,
) -> None:
    """Склонение рода для количества находок: 5 — «находок» (906)."""
    from app.leaderboard import _memory_nomination

    base = datetime(2020, 3, 2, tzinfo=UTC)
    pid = 894_500
    rounds: list[Round] = []
    for i in range(5):
        round_row = await _seed_closed_round(session, 894_000 + i, base + timedelta(days=i))
        session.add(MemoryHit(player_id=pid, round_id=round_row.id))
        rounds.append(round_row)
    await session.commit()

    nomination = await _memory_nomination(session, base, base + timedelta(days=10))
    assert nomination is not None
    assert "5 находок" in nomination  # ветка else (906)


async def test_settle_week_one_day_participant_qualifies_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Указ: порога 4 дней больше нет — дефолт min_days=1, одного дня хватает.

    Настройку НЕ пинаем: проверяем сам дефолт конфига; если порог поднимут
    снова, тест сломается первым.
    """
    assert settings.weekly_min_days == 1
    monkeypatch.setattr(settings, "ton_enabled", True)
    week_key = previous_week_key()
    prev_start, _ = week_bounds(week_key)
    pid = 895_001
    async with SessionLocal() as session:
        await _wipe_week_keys(session)
        session.add(
            Player(
                id=pid,
                username=f"p{pid}",
                wallet_address="0:" + os.urandom(32).hex(),
                wallet_verified=True,
            )
        )
        round_row = await _seed_closed_round(
            session, 895_000, prev_start + timedelta(days=3, hours=11)
        )
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        _set_stake(session, round_row, pid)
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await _set_week_ready(session, week_key)
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            payouts = (
                await session.execute(select(Payout).where(Payout.kind == "weekly"))
            ).scalars().all()
            assert [p.player_id for p in payouts] == [pid]
        finally:
            await _cleanup_week_scene(session, [round_row], [pid])


