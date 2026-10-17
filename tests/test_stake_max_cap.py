"""Потолок ставки STAKE_MAX_TON: перевод выше отклоняется и возвращается.

Отклонение идёт тем же контуром, что «пыль» ниже минимума: строка ставки
получает статус rejected и НЕ занимает слот дня — валидная ставка позже
записывается на её место, а отклонённая сумма возвращается (минус газ).
0 = потолка нет (прежнее поведение). Потолок ниже минимума ловит
validate_config при старте — здесь проверяем само отклонение.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Payout, Player, Round, RoundStatus, Stake, WinRule
from app.ton_utils import to_nano


async def _open_round(session: AsyncSession, day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )
    session.add(round_row)
    await session.commit()
    return round_row


async def test_over_cap_rejected_then_valid_replaces_and_refunds(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_max_ton", 5.0)
    round_row = await _open_round(session, 801)
    player = Player(id=801_001)
    session.add(player)
    await session.commit()

    assert (
        await stakes_mod.register_stake(session, round_row, player, to_nano(6), "cap-tx-1")
        == "too_big"
    )
    rejected = (
        await session.execute(select(Stake).where(Stake.tx_hash == "cap-tx-1"))
    ).scalar_one()
    assert rejected.status == "rejected"

    # Отклонённая строка не занимает слот: валидная ставка в пределах потолка
    # записывается на её место, а 6 Gram возвращаются тем же расчётом, что и
    # пыль (минус газ).
    assert (
        await stakes_mod.register_stake(session, round_row, player, to_nano(3), "cap-tx-2")
        == "ok"
    )
    current = (
        await session.execute(select(Stake).where(Stake.tx_hash == "cap-tx-2"))
    ).scalar_one()
    assert current.status == "pending" and current.amount_nanotons == to_nano(3)
    rows = (await session.execute(select(Stake))).scalars().all()
    assert len(rows) == 1  # слот один — строка заменена, а не задвоена

    refund = (
        await session.execute(
            select(Payout).where(Payout.kind == "refund", Payout.player_id == player.id)
        )
    ).scalar_one()
    assert refund.amount_nanotons == stakes_mod.refund_net_amount(to_nano(6))


async def test_cap_boundary_exact_amount_is_ok(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_max_ton", 5.0)
    round_row = await _open_round(session, 802)
    player = Player(id=802_001)
    session.add(player)
    await session.commit()

    assert (
        await stakes_mod.register_stake(session, round_row, player, to_nano(5), "cap-tx-3")
        == "ok"
    )


async def test_cap_zero_means_no_ceiling(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_max_ton", 0.0)
    round_row = await _open_round(session, 803)
    player = Player(id=803_001)
    session.add(player)
    await session.commit()

    assert (
        await stakes_mod.register_stake(session, round_row, player, to_nano(500), "cap-tx-4")
        == "ok"
    )


async def test_below_min_stays_too_small_with_cap_set(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_max_ton", 5.0)
    round_row = await _open_round(session, 804)
    player = Player(id=804_001)
    session.add(player)
    await session.commit()

    assert (
        await stakes_mod.register_stake(session, round_row, player, to_nano(0.01), "cap-tx-5")
        == "too_small"
    )
