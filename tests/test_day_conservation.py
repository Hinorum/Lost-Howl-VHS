"""Инвариант консервации игрового дня: сумма ставок == сумма всех выходов.

«Вход» (pot + застрявшие ставки) должен ровно превращаться в «выход»:
строки Payout (приз/возврат/рейк) + копилки (неделя, месяц, фонд, рефералы)
+ газ сети (payout_fee_gram за каждый отправляемый перевод). Ничего не
теряется и не создаётся из воздуха — для победного дня, возвратного дня,
дня с пылью и дня, где газ съел весь пул.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Payout, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.ton_utils import to_nano

FEE = 0.005


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
    monkeypatch.setattr(settings, "payout_fee_gram", FEE)


async def _assert_day_balances(
    session: AsyncSession,
    round_row: Round,
    n_winning_stakes: int,
    n_refund_rows: int,
) -> None:
    """сумма всех ставок раунда == суммы Payout + копилки + газ за переводы.

    n_winning_stakes — сколько переводов приза «оплачено» газом (даже при
    пылевой доле перевод не создаётся, но газ вычтен заранее). n_refund_rows —
    сколько строк возврата создалось (за каждую тоже газ).

    Во «вход» входят все полученные казной суммы раунда: подтверждённые
    (пот) и застрявшие (rejected/pending), возвращаемые отдельной строкой.
    """
    fee = to_nano(FEE)
    stakes_in = sum(
        r.amount_nanotons
        for r in (await session.execute(select(Stake).where(Stake.round_id == round_row.id))).scalars().all()
    )
    rows = list((await session.execute(select(Payout).where(Payout.round_id == round_row.id))).scalars().all())
    week = sum(
        r.nanotons for r in (await session.execute(select(stakes_mod.WeeklyPot))).scalars().all()
    )
    month = sum(
        r.nanotons for r in (await session.execute(select(stakes_mod.LeaderboardPot))).scalars().all()
    )
    fund = sum(
        r.nanotons for r in (await session.execute(select(stakes_mod.PackFund))).scalars().all()
    )
    ref = sum(
        r.nanotons for r in (await session.execute(select(stakes_mod.ReferralPot))).scalars().all()
    )
    out = sum(r.amount_nanotons for r in rows) + week + month + fund + ref
    gas = fee * (n_winning_stakes + n_refund_rows)
    assert out + gas == stakes_in, (
        f"день {round_row.day_index}: in={stakes_in} out={out} gas={gas} pot={round_row.pot_nanotons}"
    )


async def test_conservation_single_winner_with_losers_and_stuck(session: AsyncSession) -> None:
    """Победный день: выигравший получает приз минус газ, проигравший без
    возврата (деньги в пуле), застрявшая ставка возвращается с газом."""
    for pid in (1, 2, 3, 4):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=1)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(6), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=3, amount_nanotons=to_nano(4), tx_hash="b", status="confirmed"),
            # Застрявшая ставка: не confirmed, финализация вернёт её с газом.
            Stake(round_id=round_row.id, player_id=4, amount_nanotons=to_nano(2), tx_hash="c", status="rejected"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    # prize(1) + rake(1) + refund stuck(1) — месяц в LeaderboardPot, неделя своя.
    assert created == 3
    await _assert_day_balances(session, round_row, n_winning_stakes=1, n_refund_rows=1)


async def test_conservation_two_winners_proportional(session: AsyncSession) -> None:
    """Два победителя: пул делится пропорционально ставкам после вычета
    газа за каждый перевод; газ платится за обоих."""
    for pid in (1, 2, 3):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0, day_index=2)
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
    await _assert_day_balances(session, round_row, n_winning_stakes=2, n_refund_rows=0)


async def test_conservation_dust_share_rolls_to_week(session: AsyncSession, monkeypatch) -> None:
    """Микродоля одного из победителей ниже min_payout_gram: перевод не
    создаётся, доля капает в копилку недели — деньги не теряются."""
    monkeypatch.setattr(settings, "min_payout_gram", 6.0)  # обе доли ниже порога
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
    # Только рейк казны: призовые доли целиком ушли в неделю. Газ вычтен за
    # обоих победителей как за потенциальные переводы.
    assert created == 1
    await _assert_day_balances(session, round_row, n_winning_stakes=2, n_refund_rows=0)


async def test_conservation_refund_day(session: AsyncSession) -> None:
    """Никто не поставил на верный путь: ВСЕ подтверждённые ставки
    возвращаются, с вычетом газа за каждый перевод. Рейков и копилок нет."""
    for pid in (1, 2):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=2, day_index=43)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(3), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(7), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    assert created == 2
    await _assert_day_balances(session, round_row, n_winning_stakes=0, n_refund_rows=2)


async def test_conservation_gas_eaten_pool(session: AsyncSession, monkeypatch) -> None:
    """Газ больше пула: приз целиком уходит в копилку недели (после вычета
    газа из пула), ничего не «пропадает» и не возвращается как ставка."""
    monkeypatch.setattr(settings, "payout_fee_gram", 100.0)
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
    assert created == 1  # только рейк казны
    # Переводы приза не создавались — казна газ не вычитала и не тратит.
    await _assert_day_balances(session, round_row, n_winning_stakes=0, n_refund_rows=0)