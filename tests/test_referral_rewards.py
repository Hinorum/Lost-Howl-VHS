"""Реферальные награды: накопление и пороговые выплаты.

Гарантии:
- с ПОДТВЕРЖДЁННОЙ ставки приведённого игрока доля referral_pct уходит в
  копилку пригласившего, а НЕ в пул победителей (день без приведённых ставок
  делится как раньше — 96%);
- день возвратов (никто не угадал) реферальной награды не приносит;
- накопление ждёт referral_min_payout_gram: микропереводы не плодятся,
  пыль копится к следующему дню;
- выплата только при ПОДТВЕРЖДЁННОМ кошельке пригласившего, через обычную
  очередь Payout kind="referral".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Payout, Player, Referral, ReferralPot, Round, RoundStatus, Stake, Vote, WinRule
from app.ton_utils import to_nano


async def make_closed_round(session: AsyncSession, winner_card: int, day_index: int = 1) -> Round:
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


@pytest.fixture(autouse=True)
def _referral_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    monkeypatch.setattr(settings, "payout_fee_gram", 0.005)
    monkeypatch.setattr(settings, "min_payout_gram", 0.02)
    monkeypatch.setattr(settings, "referral_pct", 1.0)
    monkeypatch.setattr(settings, "referral_min_payout_gram", 0.5)


async def _referral_pot_total(session: AsyncSession) -> dict[int, int]:
    rows = (await session.execute(select(ReferralPot))).scalars().all()
    return {pot.referrer_id: pot.nanotons for pot in rows}


async def test_referred_stake_feeds_pot_and_shrinks_prize_pool(session: AsyncSession) -> None:
    """1% от подтверждённой ставки приведённого — в копилку, победители делят меньше."""
    referrer, referred, other = 1000, 1001, 1002
    for pid in (referrer, referred, other):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=0),
            Vote(round_id=round_row.id, player_id=other, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(2), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=other, amount_nanotons=to_nano(2), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    pot = to_nano(4)
    referral_cut = to_nano(2) * 100 // 10_000  # 1% от 2 Gram ставки приведённого
    assert await _referral_pot_total(session) == {referrer: referral_cut}

    cuts = sum(
        pot * bp // 10_000
        for bp in (round(settings.owner_rake_pct * 100), round(settings.leaderboard_rake_pct * 100),
                   round(settings.weekly_pot_pct * 100), round(settings.pack_fund_pct * 100))
    )
    prize_pool = pot - cuts - referral_cut
    net_pool = prize_pool - to_nano(settings.payout_fee_gram) * 2
    rows = {
        row.player_id: row.amount_nanotons
        for row in (await session.execute(select(Payout).where(Payout.kind == "prize"))).scalars()
    }
    assert set(rows) == {referred, other}
    # Пул ужался на реферальную долю: оба победителя делят 96%−(1%)−комиссии.
    assert sum(rows.values()) == net_pool
    assert abs(rows[referred] - rows[other]) <= 1  # равные ставки — равные доли


async def test_no_referrers_keeps_old_split(session: AsyncSession) -> None:
    """День без приведённых ставок не откладывает реферальной доли: пул 96% как раньше."""
    for pid in (2001, 2002):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=2001, card_position=0),
            Vote(round_id=round_row.id, player_id=2002, card_position=0),
            Stake(round_id=round_row.id, player_id=2001, amount_nanotons=to_nano(2), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=2002, amount_nanotons=to_nano(2), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    assert await _referral_pot_total(session) == {}
    pot = to_nano(4)
    cuts = sum(
        pot * bp // 10_000
        for bp in (round(settings.owner_rake_pct * 100), round(settings.leaderboard_rake_pct * 100),
                   round(settings.weekly_pot_pct * 100), round(settings.pack_fund_pct * 100))
    )
    net_pool = (pot - cuts) - to_nano(settings.payout_fee_gram) * 2
    rows = {
        row.player_id: row.amount_nanotons
        for row in (await session.execute(select(Payout).where(Payout.kind == "prize"))).scalars()
    }
    assert sum(rows.values()) == net_pool


async def test_refund_day_credits_no_referral(session: AsyncSession) -> None:
    """Никто не угадал (возврат ставок) — реферальной награды нет."""
    referrer, referred = 3000, 3001
    for pid in (referrer, referred):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=1),  # мимо верного пути
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(2), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    assert await _referral_pot_total(session) == {}


async def test_rejected_referred_stake_does_not_feed_pot(session: AsyncSession) -> None:
    """Нарушинская (rejected) ставка приведённого реферальной доли не даёт."""
    referrer, referred, winner = 4000, 4001, 4002
    for pid in (referrer, referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=0),
            Vote(round_id=round_row.id, player_id=winner, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(2), tx_hash="a", status="rejected"),
            Stake(round_id=round_row.id, player_id=winner, amount_nanotons=to_nano(2), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    assert await _referral_pot_total(session) == {}


async def test_referral_pays_only_above_threshold_with_verified_wallet(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Накопление >= referral_min_payout_gram при подтверждённом кошельке — выплата."""
    referrer, referred, winner = 5000, 5001, 5002
    for pid in (referrer, referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=0),
            Vote(round_id=round_row.id, player_id=winner, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(100), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=winner, amount_nanotons=to_nano(1), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    referral_cut = to_nano(100) * 100 // 10_000  # 1 Gram >= порог 0.5
    assert await _referral_pot_total(session) == {referrer: 0}
    payout = (
        await session.execute(select(Payout).where(Payout.kind == "referral"))
    ).scalar_one()
    assert payout.player_id == referrer
    assert payout.amount_nanotons == referral_cut
    assert payout.dest_address == "wallet-5000"
    assert payout.round_id is None


async def test_referral_below_threshold_accumulates_then_pays(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пыль (0.4 Gram < 0.5) не создаёт перевода: копится, выплачивается на третьем дне."""
    referrer, referred, winner = 6000, 6001, 6002
    for pid in (referrer, referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    # День 1: 1% от 40 Gram = 0.4 Gram — ниже порога.
    round_day1 = await make_closed_round(session, winner_card=0, day_index=1)
    session.add_all(
        [
            Vote(round_id=round_day1.id, player_id=referred, card_position=0),
            Vote(round_id=round_day1.id, player_id=winner, card_position=0),
            Stake(round_id=round_day1.id, player_id=referred, amount_nanotons=to_nano(40), tx_hash="a", status="confirmed"),
            Stake(round_id=round_day1.id, player_id=winner, amount_nanotons=to_nano(1), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()
    await stakes_mod.finalize_day_payouts(session, round_day1)
    assert await _referral_pot_total(session) == {referrer: to_nano(40) * 100 // 10_000}

    # День 2: ещё 0.4 — накопление 0.8 >= 0.5: выплата, копилка в ноль.
    round_day2 = await make_closed_round(session, winner_card=0, day_index=2)
    session.add_all(
        [
            Vote(round_id=round_day2.id, player_id=referred, card_position=0),
            Vote(round_id=round_day2.id, player_id=winner, card_position=0),
            Stake(round_id=round_day2.id, player_id=referred, amount_nanotons=to_nano(40), tx_hash="c", status="confirmed"),
            Stake(round_id=round_day2.id, player_id=winner, amount_nanotons=to_nano(1), tx_hash="d", status="confirmed"),
        ]
    )
    await session.commit()
    await stakes_mod.finalize_day_payouts(session, round_day2)
    assert await _referral_pot_total(session) == {referrer: 0}
    payout = (
        await session.execute(select(Payout).where(Payout.kind == "referral"))
    ).scalar_one()
    assert payout.amount_nanotons == to_nano(40) * 100 // 10_000 * 2


async def test_referral_without_verified_wallet_waits(session: AsyncSession) -> None:
    """У пригласившего нет подтверждённого кошелька — накопление ждёт привязки."""
    referrer, referred, winner = 7000, 7001, 7002
    session.add(Player(id=referrer, wallet_address="wallet-7000", wallet_verified=False))  # не подтверждён
    for pid in (referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=0),
            Vote(round_id=round_row.id, player_id=winner, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(100), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=winner, amount_nanotons=to_nano(1), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    assert await _referral_pot_total(session) == {referrer: to_nano(100) * 100 // 10_000}
    assert (await session.execute(select(Payout).where(Payout.kind == "referral"))).scalar_one_or_none() is None


async def test_losing_referred_stake_still_pays_share(session: AsyncSession) -> None:
    """Проигравший приведённый (день выиграл другой игрок) всё равно платит 1%."""
    referrer, referred, winner = 8000, 8001, 8002
    for pid in (referrer, referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=1),  # НЕ верный путь
            Vote(round_id=round_row.id, player_id=winner, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=to_nano(2), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=winner, amount_nanotons=to_nano(2), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    # Доля снята со ставки проигравшего приведённого (winning_stakes = только winner).
    assert await _referral_pot_total(session) == {referrer: to_nano(2) * 100 // 10_000}
    prize_rows = (
        await session.execute(select(Payout).where(Payout.kind == "prize"))
    ).scalars().all()
    assert [p.player_id for p in prize_rows] == [winner]


async def test_gas_day_accrues_referral_despite_dust_pool(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Комиссии съели призовой пул целиком (пыль → копилка недели), но доля ДО
    вычета газа всё равно начисляется: она снята с фонда раньше комиссий."""
    monkeypatch.setattr(settings, "payout_fee_gram", 0.5)
    referrer, referred, winner = 8100, 8101, 8102
    for pid in (referrer, referred, winner):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    session.add(Referral(referrer_id=referrer, referred_id=referred))
    round_row = await make_closed_round(session, winner_card=0)
    stake_nano = to_nano(0.06)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=referred, card_position=1),  # проиграл
            Vote(round_id=round_row.id, player_id=winner, card_position=0),
            Stake(round_id=round_row.id, player_id=referred, amount_nanotons=stake_nano, tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=winner, amount_nanotons=stake_nano, tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)

    pot = stake_nano * 2
    cuts = sum(
        pot * bp // 10_000
        for bp in (round(settings.owner_rake_pct * 100), round(settings.leaderboard_rake_pct * 100),
                   round(settings.weekly_pot_pct * 100), round(settings.pack_fund_pct * 100))
    )
    referral_cut = stake_nano * 100 // 10_000
    prize_pool = pot - cuts - referral_cut
    # Газ (0.5 Грама) больше пула → призов нет, весь пул ушёл в копилку недели…
    assert (await session.execute(select(Payout).where(Payout.kind == "prize"))).scalars().all() == []
    weekly_cut = pot * round(settings.weekly_pot_pct * 100) // 10_000
    assert round_row.weekly_nanotons == weekly_cut + prize_pool
    # …а реферальная доля начислена (снята с фонда ДО вычета газа).
    assert round_row.referral_nanotons == referral_cut
    assert await _referral_pot_total(session) == {referrer: referral_cut}


async def test_settle_referral_pots_concurrency_guard(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Между чтением копилки и атомарным UPDATE её обнулила другая реплика —
    повторная выплата не создаётся (ветка rowcount == 0)."""
    from sqlalchemy import update

    referrer, winner = 8200, 8201
    session.add(Player(id=referrer, wallet_address="wallet-8200", wallet_verified=True))
    session.add(Player(id=winner, wallet_address="wallet-8201"))
    session.add(ReferralPot(referrer_id=referrer, nanotons=to_nano(1.0)))
    await session.commit()

    real_execute = session.execute
    zeroed = {"seen": False}

    async def spy(statement, *args, **kwargs):
        # НАШ атомарный UPDATE по копилке — но «другой процесс» успел обнулить её первым.
        if (
            statement.__class__.__name__ == "Update"
            and getattr(getattr(statement, "table", None), "name", None) == "referral_pots"
        ):
            await real_execute(
                update(ReferralPot).where(ReferralPot.referrer_id == referrer).values(nanotons=0)
            )
            zeroed["seen"] = True
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", spy)
    created = await stakes_mod._settle_referral_pots(session)

    assert zeroed["seen"] is True
    assert created == 0
    assert await _referral_pot_total(session) == {referrer: 0}
    assert (await session.execute(select(Payout).where(Payout.kind == "referral"))).scalar_one_or_none() is None