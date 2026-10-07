"""Защита /refinalize от задвоения реальных выплат.

Перефинализация дня с уже ушедшими в блокчейн выплатами (status=sent)
пересоздала бы их ПОВТОРНО — игроку пришла бы вторая выплата той же суммы.
Повтор невозможен только пока ни одна строка раунда не ушла в сеть.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import ensure_player
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.handlers.admin import cmd_refinalize
from app.models import Payout, Round, RoundStatus, WinRule


def make_message(uid: int, day: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text=f"/refinalize {day}",
        bot=SimpleNamespace(),
        answer=AsyncMock(),
    )


async def _seed_closed_round(session, day_index: int, *, payout_status: str = "pending") -> int:
    # Родитель выплаты — иначе Postgres не примет INSERT payouts (SQLite FK
    # не проверяет и исторически позволял игроков «из воздуха»).
    await ensure_player(7)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="Эхо",
        chapter_text="т",

        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC),
        tally_ends_at=datetime.now(UTC),
        payouts_finalized=True,
    )
    session.add(round_row)
    await session.flush()
    session.add(
        Payout(
            round_id=round_row.id,
            player_id=7,
            kind="prize",
            amount_nanotons=1_000_000_000,
            dest_address="0:" + "11" * 32,
            status=payout_status,
        )
    )
    await session.commit()
    return round_row.id


async def test_refinalize_refuses_when_anything_sent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        round_id = await _seed_closed_round(session, 420, payout_status="sent")

    msg = make_message(4242, 420)
    await cmd_refinalize(msg)

    body = msg.answer.call_args.args[0]
    assert "отменена" in body and "sent" in body
    async with SessionLocal() as session:
        row = await session.get(Round, round_id)
        payout_q = await session.execute(select(Payout).where(Payout.round_id == round_id))
        payouts = list(payout_q.scalars().all())
    # Флаг не сброшен, строка не пересоздана и не dismissed.
    assert row.payouts_finalized is True
    assert [p.status for p in payouts] == ["sent"]
    async with SessionLocal() as session:
        # Ребёнок (выплата) стирается раньше родителя — иначе Postgres
        # отвергнет DELETE по rounds (SQLite порядок не проверяет).
        await session.execute(delete(Payout).where(Payout.round_id == round_id))
        await session.execute(delete(Round).where(Round.id == round_id))
        await session.commit()


async def test_refinalize_proceeds_when_nothing_sent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        round_id = await _seed_closed_round(session, 421, payout_status="pending")

    msg = make_message(4242, 421)
    await cmd_refinalize(msg)

    bodies = [call.args[0] for call in msg.answer.call_args_list]
    assert all("отменена" not in b for b in bodies)
    assert any("finalized сброшен" in b for b in bodies)
    async with SessionLocal() as session:
        row = await session.get(Round, round_id)
        payout_q = await session.execute(select(Payout).where(Payout.round_id == round_id))
        payouts = list(payout_q.scalars().all())
    # Не-sent строки dismissed, затем finalize_day_payouts снова забирает claim
    # (атомарный UPDATE ставит флаг True в начале повторной финализации).
    assert row.payouts_finalized is True
    assert [p.status for p in payouts] == ["dismissed"]
    async with SessionLocal() as session:
        # Ребёнок (выплата) стирается раньше родителя — иначе Postgres
        # отвергнет DELETE по rounds (SQLite порядок не проверяет).
        await session.execute(delete(Payout).where(Payout.round_id == round_id))
        await session.execute(delete(Round).where(Round.id == round_id))
        await session.commit()


@pytest.mark.parametrize("payout_status", ["sending"])
async def test_refinalize_refuses_when_any_payout_moved(monkeypatch, payout_status) -> None:
    """sending (вещание ушло, коммит ещё нет) — деньги уже двинулись:
    перефинализация должна отказываться, иначе — вторая выплата той же суммы
    (другой payout.id, анти-дубль по memo слеп)."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        round_id = await _seed_closed_round(session, 422, payout_status=payout_status)

    msg = make_message(4242, 422)
    await cmd_refinalize(msg)

    body = msg.answer.call_args.args[0]
    assert "отменена" in body
    async with SessionLocal() as session:
        row = await session.get(Round, round_id)
        payout = (
            await session.execute(select(Payout).where(Payout.round_id == round_id))
        ).scalar_one()
    assert row.payouts_finalized is True  # флаг не сброшен
    assert payout.status == payout_status  # строка не пересоздана и не dismissed
    async with SessionLocal() as session:
        # Ребёнок (выплата) стирается раньше родителя — иначе Postgres
        # отвергнет DELETE по rounds (SQLite порядок не проверяет).
        await session.execute(delete(Payout).where(Payout.round_id == round_id))
        await session.execute(delete(Round).where(Round.id == round_id))
        await session.commit()