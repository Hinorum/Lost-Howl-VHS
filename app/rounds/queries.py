from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Round, RoundStatus


@dataclass(frozen=True, slots=True)
class RoundMeta:
    """Лёгкая проекция дня для горячего пути планировщика.

    chapter_text, dilemma, счётчики голосов и карты сюда не входят: они нужны
    только когда день реально закрывается или анонсируется, а тик читает мету
    каждые 15 секунд. Полная строка с текстом главы — заметная доля трафика
    между сервисом и внешней БД.
    """

    id: int
    day_index: int
    status: RoundStatus
    voting_ends_at: datetime
    tally_ends_at: datetime


_META_COLUMNS = (
    Round.id,
    Round.day_index,
    Round.status,
    Round.voting_ends_at,
    Round.tally_ends_at,
)


async def get_active_round(session: AsyncSession) -> Round | None:
    result = await session.execute(
        select(Round)
        .options(selectinload(Round.cards))
        .where(Round.status.in_([RoundStatus.OPEN, RoundStatus.TALLYING]))
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_round(session: AsyncSession, round_id: int) -> Round | None:
    result = await session.execute(
        select(Round).options(selectinload(Round.cards)).where(Round.id == round_id)
    )
    return result.scalar_one_or_none()


async def get_latest_round(session: AsyncSession) -> Round | None:
    result = await session.execute(
        select(Round).options(selectinload(Round.cards)).order_by(Round.day_index.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_active_round_meta(session: AsyncSession) -> RoundMeta | None:
    """Активный день (OPEN/TALLYING) без тяжёлых полей и карт."""
    row = (
        await session.execute(
            select(*_META_COLUMNS)
            .where(Round.status.in_([RoundStatus.OPEN, RoundStatus.TALLYING]))
            .order_by(Round.day_index.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else RoundMeta(*row)


async def get_latest_round_meta(session: AsyncSession) -> RoundMeta | None:
    """Последний день игры без chapter_text/карт — для горячего тика."""
    row = (
        await session.execute(
            select(*_META_COLUMNS).order_by(Round.day_index.desc()).limit(1)
        )
    ).first()
    return None if row is None else RoundMeta(*row)