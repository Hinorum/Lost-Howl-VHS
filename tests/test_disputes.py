"""Тесты каркаса разрешения споров: подача, резолюция и компенсация-выплата."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import disputes as mod
from app.config import settings
from app.models import Dispute, Payout, Player
from app.ton_utils import to_nano


async def test_open_dispute_records_complaint(session: AsyncSession) -> None:
    res = await mod.open_dispute(session, round_id=5, player_id=7, reason="приз не пришёл")
    assert "открыт" in res
    d = (await session.execute(select(Dispute))).scalars().one()
    assert d.round_id == 5 and d.player_id == 7
    assert d.reason == "приз не пришёл"
    assert d.status == "open"


async def test_open_dispute_requires_player_and_reason(session: AsyncSession) -> None:
    assert "игрока" in await mod.open_dispute(session, 5, None, "x")
    assert "причину" in await mod.open_dispute(session, 5, 7, "  ")


async def test_resolve_and_reject_set_status(session: AsyncSession) -> None:
    await mod.open_dispute(session, 5, 7, "проблема")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "resolved" in await mod.resolve_dispute(session, d.id, "выплата отправлена повторно")
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "resolved" and d2.resolved_at is not None
    assert d2.keeper_note == "выплата отправлена повторно"
    # Повторная резолюция отклоняется.
    assert "уже закрыт" in await mod.resolve_dispute(session, d.id, "x")


async def test_reject_dispute(session: AsyncSession) -> None:
    await mod.open_dispute(session, 5, 7, "не согласен")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "rejected" in await mod.reject_dispute(session, d.id, "без оснований")
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "rejected"


async def test_compensate_creates_payout_for_wallet_player(session: AsyncSession) -> None:
    session.add(Player(id=7, wallet_address="EQw", wallet_verified=True))
    await session.commit()
    await mod.open_dispute(session, 5, 7, "недоплата")
    d = (await session.execute(select(Dispute))).scalars().one()
    res = await mod.compensate_dispute(session, d.id, "0.5", "возврат части приза")
    assert "компенсация" in res and "0.5" in res
    pay = (await session.execute(select(Payout))).scalars().one()
    assert pay.kind == "dispute"
    assert pay.amount_nanotons == to_nano(0.5)
    assert pay.player_id == 7
    assert pay.dest_address == "EQw"
    # Компенсация закрывает открытый спор.
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "resolved"


async def test_compensate_refuses_walletless_and_bad_amount(session: AsyncSession) -> None:
    session.add(Player(id=8))
    await session.commit()
    await mod.open_dispute(session, 5, 8, "x")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "кошелёк" in await mod.compensate_dispute(session, d.id, "0.5")
    # Сумма проверяется даже при наличии кошелька.
    session.add(Player(id=9, wallet_address="EQv", wallet_verified=True))
    await session.commit()
    await mod.open_dispute(session, 5, 9, "y")
    rows = list((await session.execute(select(Dispute))).scalars().all())
    d2 = next(r for r in rows if r.player_id == 9)
    assert "числом" in await mod.compensate_dispute(session, d2.id, "abc")


async def test_compensate_refuses_unverified_wallet(session: AsyncSession) -> None:
    """Привязанный, но не доказанный bv: адрес — не получатель. Компенсация
    ушла бы в необработанный bounce, а спор при этом закрылся бы."""
    session.add(Player(id=11, wallet_address="EQunverified"))
    await session.commit()
    await mod.open_dispute(session, 5, 11, "недоплата")
    d = (await session.execute(select(Dispute))).scalars().one()

    res = await mod.compensate_dispute(session, d.id, "0.5", "возврат части приза")

    assert "bv:" in res
    assert (await session.execute(select(Payout))).scalars().all() == []
    # Спор остаётся открытым: деньги не ушли, решение не принято.
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "open" and d2.resolved_at is None


async def test_disputes_flag_gates_open(session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "disputes_enabled", False)
    assert "выключено" in await mod.open_dispute(session, 5, 7, "x")


async def test_open_dispute_dedupes_same_round(session: AsyncSession) -> None:
    res = await mod.open_dispute(session, round_id=5, player_id=7, reason="приз не пришёл")
    assert "открыт" in res
    res2 = await mod.open_dispute(session, round_id=5, player_id=7, reason="дубль")
    assert "уже открыт" in res2
    rows = list((await session.execute(select(Dispute))).scalars().all())
    assert len(rows) == 1
    # Тот же игрок может открыть спор на ДРУГОЙ день.
    res3 = await mod.open_dispute(session, round_id=6, player_id=7, reason="другой день")
    assert "открыт" in res3
    assert (await session.execute(select(Dispute))).scalars().all().__len__() == 2
    # Закрытый спор не блокирует новую подачу на тот же день.
    d = (
        await session.execute(select(Dispute).where(Dispute.round_id == 5))
    ).scalars().one()
    await mod.reject_dispute(session, d.id, "мимо")
    res4 = await mod.open_dispute(session, round_id=5, player_id=7, reason="снова")
    assert "открыт" in res4


async def test_open_dispute_caps_active_per_player(session: AsyncSession) -> None:
    for day in range(1, 6):
        res = await mod.open_dispute(session, round_id=day, player_id=7, reason=f"день {day}")
        assert "открыт" in res
    over = await mod.open_dispute(session, round_id=6, player_id=7, reason="шестой")
    assert "пять открытых" in over
    rows = list((await session.execute(select(Dispute))).scalars().all())
    assert len(rows) == 5
