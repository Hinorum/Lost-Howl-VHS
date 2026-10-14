from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, RevoteGrant, Round, RoundStatus, Vote


async def upsert_player(session: AsyncSession, user) -> Player:
    """Поднять игрока по Telegram-пользователю: создать или освежить профиль.

    Аккаунт-бот НИКОГДА не подписывается на личную рассылку. Telegram
    запрещает боту писать боту (USER_BOT_TO_BOT_DISABLED), поэтому такая
    подписка заведомо недоставима: бот в группе может отправить команду,
    и его from_user (is_bot=True) создавал игрока с dm_subscribed по
    умолчанию — каждая рассылка спотыкалась об этом аккаунт, оставляя
    «0 из N» и почасовую тревогу (инцидент dm:vote-reminder).

    Существующему игроку-боту подписка снимается при следующем обращении;
    сама запись не удаляется — голосы и история должны остаться целыми.
    is_bot читаем через getattr: тестовые заглушки (SimpleNamespace)
    поля не имеют и по умолчанию не боты.
    """
    is_bot = bool(getattr(user, "is_bot", False))
    player = await session.get(Player, user.id)
    if player is None:
        player = Player(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            dm_subscribed=not is_bot,
        )
        session.add(player)
        await session.commit()
        return player
    changed = False
    if player.username != user.username or player.first_name != user.first_name:
        player.username = user.username
        player.first_name = user.first_name
        changed = True
    if is_bot and player.dm_subscribed:
        # Самолечение: игрок-бот, подписанный до этого фикса, отпишется
        # при первом же обращении.
        player.dm_subscribed = False
        changed = True
    if changed:
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
