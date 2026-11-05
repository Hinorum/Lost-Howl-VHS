"""Правило выплат: получатель получает ЧИСТЫМИ.

Комиссию сети платит та копилка, из которой платят, а не остаток казны.
Раньше копилка обещала игроку одну сумму, а из казны уходил ещё и газ:
сумма выплат переставала сходиться с копилкой, и разницу тихо покрывал
хранитель (в проде так ушли 0.0576 Грама за неделю — плюс газ сверху).

Теперь газ вычитается из САМОЙ выплаты (из пула — до деления), и получатель
получает ровно обещанное — чистыми. Здесь проверяются все шесть денежных
путей: дневной пул, рейк казны, реферальная награда, недельный лидерборд,
месячный лидерборд и авто-возврат чужого перевода.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.core.fees import net_payout_amount, net_payout_pool
from app.core.registry import MONTH_READY_KEY, WEEK_READY_KEY
from app.db import SessionLocal
from app.leaderboard import previous_month_key, settle_month_if_due, settle_week_if_due
from app.models import (
    LeaderboardPot,
    Payout,
    Player,
    ReferralPot,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WeeklyPot,
    WinRule,
)
from app.ton_utils import to_nano
from app.ton_watch import Transfer, _stash_refund
from app.weeks import previous_week_key, week_bounds


@pytest.fixture(autouse=True)
def _ton_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    monkeypatch.setattr(settings, "payout_fee_gram", 0.005)
    monkeypatch.setattr(settings, "min_payout_gram", 0.02)
    monkeypatch.setattr(settings, "weekly_prize_pcts", "50,30,20")
    monkeypatch.setattr(settings, "monthly_prize_weights", "50,30,20")
    monkeypatch.setattr(settings, "monthly_prize_top_k", 3)
    monkeypatch.setattr(settings, "weekly_min_days", 1)


def _fee() -> int:
    return to_nano(settings.payout_fee_gram)


async def _make_closed_round(session: AsyncSession, winner_card: int = 0) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=901,
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


async def _seed_week_scene(session: AsyncSession, base: int, votes: dict[int, int]) -> list[Round]:
    """Прошлая неделя: игроки с верными голосами, ставками и кошельками."""
    week_key = previous_week_key()
    prev_start, _ = week_bounds(week_key)
    pids = list(votes)
    session.add_all(
        [Player(id=pid, wallet_address=f"w-{pid}", wallet_verified=True) for pid in pids]
    )
    rounds: list[Round] = []
    for i in range(max(votes.values())):
        opens_at = prev_start + timedelta(days=i, hours=1)
        round_row = Round(
            day_index=base + i,
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
        for pid, count in votes.items():
            if i < count:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        if i == 0:
            for pid in pids:
                session.add(
                    Stake(
                        round_id=round_row.id,
                        player_id=pid,
                        amount_nanotons=to_nano(1),
                        tx_hash="tx_" + os.urandom(16).hex(),
                        status="confirmed",
                    )
                )
        rounds.append(round_row)
    return rounds


async def _seed_month_scene(session: AsyncSession, base: int, votes: dict[int, int]) -> list[Round]:
    """Прошлый месяц: игроки с верными голосами, ставками и кошельками."""
    prev_key = previous_month_key()
    prev_start = datetime(*map(int, prev_key.split("-")), 1, tzinfo=UTC)
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pids = list(votes)
    session.add_all(
        [Player(id=pid, wallet_address=f"w-{pid}", wallet_verified=True) for pid in pids]
    )
    rounds: list[Round] = []
    for i in range(max(votes.values())):
        round_row = Round(
            day_index=base + i,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="text",
            opens_at=prev_start + timedelta(days=i + 1),
            voting_ends_at=prev_start + timedelta(days=i + 1, hours=23),
            tally_ends_at=prev_start + timedelta(days=i + 1, hours=24),
            winner_card=0,
            vote_counts_json="{}",
            payouts_finalized=True,
        )
        assert round_row.tally_ends_at < month_start
        session.add(round_row)
        await session.flush()
        for pid, count in votes.items():
            if i < count:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        if i == 0:
            for pid in pids:
                session.add(
                    Stake(
                        round_id=round_row.id,
                        player_id=pid,
                        amount_nanotons=to_nano(1),
                        tx_hash="tx_" + os.urandom(16).hex(),
                        status="confirmed",
                    )
                )
        rounds.append(round_row)
    return rounds


# --------------------------------------------------------------------------
# Помощники: одно правило в двух формулах
# --------------------------------------------------------------------------


def test_net_payout_pool_withholds_gas_before_split() -> None:
    """Пул делится ПОСЛЕ вычета газа за каждый перевод."""
    assert net_payout_pool(to_nano(10), 2, to_nano(0.005)) == to_nano(10) - to_nano(0.01)
    # Газ дороже пула — раздавать нечего, но и в минус не уходим.
    assert net_payout_pool(to_nano(0.001), 1, to_nano(0.005)) == 0
    assert net_payout_pool(0, 1, to_nano(0.005)) == 0
    # Нет выплат — газ не удерживается.
    assert net_payout_pool(to_nano(10), 0, to_nano(0.005)) == to_nano(10)


def test_net_payout_amount_withholds_gas_of_single_payout() -> None:
    """Каждая выплата платит свою комиссию из своей суммы."""
    assert net_payout_amount(to_nano(1), to_nano(0.005)) == to_nano(1) - to_nano(0.005)
    # Комиссия не больше суммы — выплаты нет (ноль перевода не отправляется).
    assert net_payout_amount(to_nano(0.005), to_nano(0.005)) == 0
    assert net_payout_amount(to_nano(0.001), to_nano(0.005)) == 0
    assert net_payout_amount(to_nano(1), 0) == to_nano(1)


# --------------------------------------------------------------------------
# Путь 1: дневной призовой пул
# --------------------------------------------------------------------------


async def test_day_pool_pays_winners_net_of_gas(session: AsyncSession) -> None:
    """Газ вычтен из пула до деления: победители получают доли чистыми."""
    for pid in (1, 2):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await _make_closed_round(session)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(6), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(4), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    pot = to_nano(10)
    cuts = (
        pot * 50 // 10_000  # рейк казны
        + pot * 50 // 10_000  # копилка месяца
        + pot * 200 // 10_000  # копилка недели
        + pot * round(settings.pack_fund_pct * 100) // 10_000  # Фонд Стаи
    )
    prize_pool = pot - cuts
    net_pool = net_payout_pool(prize_pool, 2, _fee())

    rows = {
        row.player_id: row.amount_nanotons
        for row in (await session.execute(select(Payout).where(Payout.kind == "prize"))).scalars()
    }
    assert set(rows) == {1, 2}
    # В пуле gas уже нет: получатели делят чистый пул по ставкам.
    assert sum(rows.values()) == net_pool
    assert rows[1] > rows[2]
    assert abs(rows[1] - rows[2] * 3 // 2) <= 2


# --------------------------------------------------------------------------
# Путь 2: рейк казны (доход хранителя)
# --------------------------------------------------------------------------


async def test_rake_to_keeper_is_net_of_gas(session: AsyncSession) -> None:
    """Рейк казны — тоже выплата: хранитель получает чистыми."""
    session.add(Player(id=1, wallet_address="wallet-1", wallet_verified=True))
    round_row = await _make_closed_round(session)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(10), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    pot = to_nano(10)
    house_cut = pot * round(settings.owner_rake_pct * 100) // 10_000
    rake = (
        await session.execute(select(Payout).where(Payout.kind == "rake"))
    ).scalar_one()
    assert rake.player_id is None
    assert rake.dest_address == "keeper"
    # Хранитель получает рейк за вычетом комиссии сети за его перевод.
    assert rake.amount_nanotons == house_cut - _fee()


async def test_rake_smaller_than_gas_goes_to_week_pot(
    session: AsyncSession, monkeypatch
) -> None:
    """Рейк меньше комиссии: платить нечего — он целиком идёт в копилку недели."""
    monkeypatch.setattr(settings, "payout_fee_gram", 0.01)
    session.add(Player(id=1, wallet_address="wallet-1", wallet_verified=True))
    round_row = await _make_closed_round(session)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(1), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    pot = to_nano(1)
    house_cut = pot * round(settings.owner_rake_pct * 100) // 10_000
    board_cut = pot * 50 // 10_000
    weekly_cut = pot * round(settings.weekly_pot_pct * 100) // 10_000
    fund_cut = pot * round(settings.pack_fund_pct * 100) // 10_000
    assert house_cut < _fee()
    # Рейка нет: перевода не создано.
    assert (
        await session.execute(select(Payout).where(Payout.kind == "rake"))
    ).scalars().all() == []
    # Рейк целиком в копилке недели — копилка не теряет копейки.
    week = (await session.execute(select(stakes_mod.WeeklyPot))).scalar_one()
    assert week.nanotons == weekly_cut + house_cut
    # Приз при этом платится чистыми: газ из пула.
    prize_pool = pot - board_cut - (weekly_cut + house_cut) - fund_cut
    prize = (
        await session.execute(select(Payout).where(Payout.kind == "prize"))
    ).scalar_one()
    assert prize.amount_nanotons == prize_pool - _fee()


# --------------------------------------------------------------------------
# Путь 3: реферальная награда
# --------------------------------------------------------------------------


async def test_referral_reward_is_net_of_gas(session: AsyncSession, monkeypatch) -> None:
    """Реферальная награда — выплата: пригласивший получает чистыми."""
    monkeypatch.setattr(settings, "referral_min_payout_gram", 0.0)
    session.add(Player(id=10, wallet_address="wallet-10", wallet_verified=True))
    session.add(ReferralPot(referrer_id=10, nanotons=to_nano(1)))
    await session.commit()

    created = await stakes_mod._settle_referral_pots(session)

    assert created == 1
    payout = (
        await session.execute(select(Payout).where(Payout.kind == "referral"))
    ).scalar_one()
    assert payout.player_id == 10
    assert payout.amount_nanotons == to_nano(1) - _fee()
    # Копилка обнулена: газ ушёл вместе с накоплением, а не из остатка казны.
    pot = (await session.execute(select(ReferralPot))).scalar_one()
    assert pot.nanotons == 0


# --------------------------------------------------------------------------
# Путь 4: недельный лидерборд
# --------------------------------------------------------------------------


async def test_weekly_prize_is_net_of_gas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Доли мест недели уходят лидерам чистыми: газ платит копилка недели."""
    base = 910_000
    week_key = previous_week_key()
    async with SessionLocal() as session:
        rounds = await _seed_week_scene(session, base, {base: 2, base + 1: 1})
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            # Места 50% и 30% — получателям чистыми.
            assert by_pid == {
                base: to_nano(10) * 50 // 100 - _fee(),
                base + 1: to_nano(10) * 30 // 100 - _fee(),
            }
            # Третье место без игрока — его доля целиком в новой неделе.
            rolled = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert rolled.nanotons == to_nano(10) * 20 // 100
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete())
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (base, base + 1):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


# --------------------------------------------------------------------------
# Путь 5: месячный лидерборд
# --------------------------------------------------------------------------


async def test_monthly_prize_is_net_of_gas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Доли мест месяца уходят лидерам чистыми: газ платит копилка месяца."""
    base = 920_000
    prev_key = previous_month_key()
    async with SessionLocal() as session:
        rounds = await _seed_month_scene(session, base, {base: 2, base + 1: 1})
        session.add(LeaderboardPot(month=prev_key, nanotons=to_nano(10)))
        session.add(WatcherState(key=MONTH_READY_KEY, value=prev_key))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "leaderboard"))).scalars()
            }
            # Вес 50 и 30 нормализованы на двоих из топ-3: 50/80 и 30/80 пула.
            assert by_pid == {
                base: to_nano(10) * 50 // 80 - _fee(),
                base + 1: to_nano(10) * 30 // 80 - _fee(),
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(WatcherState.__table__.delete())
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


# --------------------------------------------------------------------------
# Путь 6: авто-возврат чужого перевода
# --------------------------------------------------------------------------


async def test_auto_refund_is_net_of_gas(session: AsyncSession) -> None:
    """Возврат чужого перевода — тоже выплата: игрок получает чистыми."""
    transfer = Transfer(
        tx_hash="net-refund",
        source="0:" + "ab" * 32,
        value_nanotons=to_nano(1),
        comment="",
        utime=int(datetime.now(UTC).timestamp() - 60),
    )
    assert await _stash_refund(session, transfer, None) == "refund_queued"
    payout = (
        await session.execute(select(Payout).where(Payout.kind == "refund"))
    ).scalar_one()
    assert payout.amount_nanotons == to_nano(1) - _fee()
    assert payout.dest_address == transfer.source


async def test_auto_refund_below_gas_waits_manual_return(
    session: AsyncSession, monkeypatch
) -> None:
    """Перевод дешевле газа возврата: авто-возврата нет, ждёт ручного."""
    monkeypatch.setattr(settings, "refund_min_gram", 0.0)
    monkeypatch.setattr(settings, "payout_fee_gram", 1.0)
    transfer = Transfer(
        tx_hash="tiny-refund",
        source="0:" + "cd" * 32,
        value_nanotons=to_nano(0.1),
        comment="",
        utime=int(datetime.now(UTC).timestamp() - 60),
    )
    assert await _stash_refund(session, transfer, None) == "refund_fee"
    # Перевода не создано — деньги остаются в казне до ручного возврата.
    assert (await session.execute(select(Payout))).scalars().all() == []
