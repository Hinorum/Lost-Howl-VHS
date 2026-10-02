"""Газ сети: пропорциональный вычет из призового пула, микродоли и вырождение.

Гарантии:
- пул делится ПОСЛЕ вычета комиссии за каждый перевод — приз приходит «чистыми»;
- доля меньше min_payout_gram не создаёт дохлый перевод, а капает в копилку недели;
- если комиссии съели весь пул (экзотика) — он целиком уходит в копилку недели,
  и итоги не врут про «возврат ставок»;
- возвраты (никто не угадал / отклонённые ставки) остаются полными.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Payout, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.tally import day_economics
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
def _ton_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    monkeypatch.setattr(settings, "payout_fee_gram", 0.005)
    monkeypatch.setattr(settings, "min_payout_gram", 0.02)


async def test_fee_is_deducted_proportionally(session: AsyncSession) -> None:
    """Два победителя с разными ставками: газ делится пропорционально, а не плоско."""
    for pid in (1, 2, 3):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(4), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(2), tx_hash="b", status="confirmed"),
            Vote(round_id=round_row.id, player_id=3, card_position=1),
            Stake(round_id=round_row.id, player_id=3, amount_nanotons=to_nano(4), tx_hash="c", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    pot = to_nano(10)
    cuts = (
        pot * 50 // 10_000
        + pot * 50 // 10_000
        + pot * 200 // 10_000
        + pot * round(settings.pack_fund_pct * 100) // 10_000
    )
    prize_pool = pot - cuts
    net_pool = prize_pool - to_nano(settings.payout_fee_gram) * 2

    rows = {
        row.player_id: row.amount_nanotons
        for row in (
            await session.execute(select(Payout).where(Payout.kind == "prize"))
        ).scalars()
    }
    assert set(rows) == {1, 2}
    # Доли сохранили пропорцию ставок 2:1 уже после вычета газа.
    assert rows[1] > rows[2]
    # Нанотонная пыль: split_pot кидает остаток целиком крупнейшей ставке,
    # поэтому допуск — несколько нанотонов (по числу победителей-минимайзеров).
    assert abs(rows[1] - rows[2] * 2) <= 2
    assert sum(rows.values()) == net_pool
    # Никто не получил меньше минимума: дохлых переводов нет.
    assert all(amount >= to_nano(settings.min_payout_gram) for amount in rows.values())


async def test_dust_shares_roll_to_weekly_pot(session: AsyncSession, monkeypatch) -> None:
    """Микродоля ниже min_payout_gram не идёт в очередь — капает в копилку недели."""
    monkeypatch.setattr(settings, "min_payout_gram", 6.0)  # обе доли (≈5.8 и ≈3.9) ниже порога
    for pid in (11, 12):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=41)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=11, card_position=0),
            Vote(round_id=round_row.id, player_id=12, card_position=0),
            Stake(round_id=round_row.id, player_id=11, amount_nanotons=to_nano(6), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=12, amount_nanotons=to_nano(4), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    payouts = list((await session.execute(select(Payout))).scalars())
    # Только рейк казны. Копилка месяца — отдельный учёт в LeaderboardPot, не Payout.
    assert created == 1
    assert {row.kind for row in payouts} == {"rake"}

    pot = to_nano(10)
    weekly_cut = pot * 200 // 10_000
    prize_pool = (
        pot
        - pot * 50 // 10_000
        - pot * 50 // 10_000
        - weekly_cut
        - pot * round(settings.pack_fund_pct * 100) // 10_000
    )
    week = (
        await session.execute(select(stakes_mod.WeeklyPot).limit(1))
    ).scalar_one()
    # Неделя получила свою долю плюс всю призовую пыль (пул минус газ).
    assert week.nanotons == weekly_cut + prize_pool - to_nano(settings.payout_fee_gram) * 2
    assert round_row.weekly_nanotons == week.nanotons
    # Фонд Стаи собрал свой процент отдельной строкой учёта.
    fund = (await session.execute(select(stakes_mod.PackFund))).scalar_one()
    assert fund.nanotons == pot * round(settings.pack_fund_pct * 100) // 10_000


async def test_gas_eaten_pool_goes_to_week_and_is_not_refund(
    session: AsyncSession, monkeypatch
) -> None:
    """Комиссии съели пул целиком: деньги в копилке недели, без строки возврата."""
    monkeypatch.setattr(settings, "payout_fee_gram", 100.0)  # газ больше любого пула
    session.add(Player(id=21, wallet_address="wallet-21", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=42)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=21, card_position=0),
            Stake(round_id=round_row.id, player_id=21, amount_nanotons=to_nano(1), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    kinds = {
        row.kind
        for row in (await session.execute(select(Payout))).scalars()
    }
    assert "prize" not in kinds
    assert "refund" not in kinds  # ставка победителя не «возвращена» — она разыграна
    assert created == 1  # только рейк казны (копилка месяца — в LeaderboardPot)

    pot = to_nano(1)
    weekly_cut = pot * 200 // 10_000
    prize_pool = pot - pot * 50 // 10_000 * 2 - weekly_cut - pot * round(settings.pack_fund_pct * 100) // 10_000
    week = (
        await session.execute(select(stakes_mod.WeeklyPot).limit(1))
    ).scalar_one()
    assert week.nanotons == weekly_cut + prize_pool

    # Итоги не называют этот день «возвратом ставок»: коэффициента нет,
    # но и ложного «все ставки возвращены» тоже нет.
    stats = await day_economics(session, round_row)
    assert stats["refunded"] is False
    assert stats["multiplier"] is None


async def test_refunds_deduct_gas(session: AsyncSession) -> None:
    """Возвраты (никто не поставил на верный путь) тоже платят газ сети."""
    session.add(Player(id=31, wallet_address="wallet-31", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=43)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=31, card_position=1),
            Stake(round_id=round_row.id, player_id=31, amount_nanotons=to_nano(0.7), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    refunds = (
        (await session.execute(select(Payout).where(Payout.kind == "refund")))
        .scalars()
        .all()
    )
    # Сумма возврата = ставка минус газ сети на перевод.
    assert [row.amount_nanotons for row in refunds] == [
        to_nano(0.7) - to_nano(settings.payout_fee_gram)
    ]


async def test_refunds_proportional_ratio(monkeypatch, session: AsyncSession) -> None:
    """refund_fee_ratio: комиссия на возврат пропорциональна ставке, а не плоская."""
    monkeypatch.setattr(settings, "refund_fee_ratio", 0.01)  # 1%
    session.add(Player(id=41, wallet_address="wallet-41", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=53)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=41, card_position=1),
            Stake(round_id=round_row.id, player_id=41, amount_nanotons=to_nano(0.7), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    refunds = (
        (await session.execute(select(Payout).where(Payout.kind == "refund")))
        .scalars()
        .all()
    )
    # Возврат = ставка × (1 − 1%) = пропорционально, без плоской потери.
    assert refunds[0].amount_nanotons == int(to_nano(0.7) * 0.99)
    monkeypatch.undo()


async def test_auto_refund_marks_stake_refunded(session: AsyncSession) -> None:
    """Застрявшая ставка (rejected/pending) с авто-возвратом помечается refunded,
    а не висит вечным «переводов не обработано» в панели и часовом алерте."""
    session.add(Player(id=51, wallet_address="wallet-51", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=63)
    session.add_all(
        [
            Stake(round_id=round_row.id, player_id=51, amount_nanotons=to_nano(0.7), tx_hash="c", status="rejected"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, round_row)
    stake = (
        await session.execute(select(Stake).where(Stake.player_id == 51))
    ).scalar_one()
    refunds = (
        (await session.execute(select(Payout).where(Payout.kind == "refund")))
        .scalars()
        .all()
    )
    assert len(refunds) == 1
    assert stake.status == "refunded"
