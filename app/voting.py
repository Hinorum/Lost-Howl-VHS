from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, RevoteGrant, Round, RoundStatus, Vote


async def upsert_player(session: AsyncSession, user) -> Player:
    player = await session.get(Player, user.id)
    if player is None:
        player = Player(id=user.id, username=user.username, first_name=user.first_name)
        session.add(player)
        await session.commit()
        return player
    if player.username != user.username or player.first_name != user.first_name:
        player.username = user.username
        player.first_name = user.first_name
        await session.commit()
    return player


async def get_vote(session: AsyncSession, round_id: int, player_id: int) -> Vote | None:
    result = await session.execute(
        select(Vote).where(Vote.round_id == round_id, Vote.player_id == player_id)
    )
    return result.scalar_one_or_none()


async def cast_vote(session: AsyncSession, round_row: Round, player_id: int, position: int) -> str:
    if position not in (0, 1, 2):
        return "invalid"
    if round_row.status != RoundStatus.OPEN:
        return "closed"
    existing = await get_vote(session, round_row.id, player_id)
    if existing is not None:
        return "already"
    session.add(Vote(round_id=round_row.id, player_id=player_id, card_position=position))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "already"
    return "ok"


async def change_vote(session: AsyncSession, round_row: Round, player_id: int, position: int) -> str:
    """Платная смена выбора: списывает один оплаченный грант и переписывает голос.

    Возвращает ok / invalid / closed / no_vote / same / no_grant.
    """
    if position not in (0, 1, 2):
        return "invalid"
    if round_row.status != RoundStatus.OPEN:
        return "closed"
    vote = await get_vote(session, round_row.id, player_id)
    if vote is None:
        return "no_vote"
    if vote.card_position == position:
        return "same"
    # Атомарный захват старейшего гранта: условный UPDATE не даёт списать один
    # грант дважды при гонке параллельных вызовов.
    claim_result = await session.execute(
        update(RevoteGrant)
        .where(
            RevoteGrant.id
            == (
                select(RevoteGrant.id)
                .where(
                    RevoteGrant.round_id == round_row.id,
                    RevoteGrant.player_id == player_id,
                    RevoteGrant.status == "granted",
                )
                .order_by(RevoteGrant.id.asc())
                .limit(1)
            ).scalar_subquery()
        )
        .values(status="used", used_at=datetime.now(UTC))
    )
    if claim_result.rowcount != 1:
        # Гранта нет (или его перехватил параллельный вызов). Пустой commit
        # закрывает транзакцию, не истекая объекты сессии (expire_on_commit=False).
        await session.commit()
        return "no_grant"
    vote.card_position = position
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "no_grant"
    return "ok"
