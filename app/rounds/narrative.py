from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Round

logger = logging.getLogger(__name__)


async def write_epilogue(session: AsyncSession, round_row: Round) -> str:
    """Эпилог дня: канон (уцелевший consequence) печатается в итогах.

    Основной путь уже записал `epilogue_text` в момент закрытия раунда
    (```lifecycle.finish_tally```) — на тот момент пост итогов ещё впереди.
    Здесь — идемпотентный бэкафилл: если поле пусто, а победитель известен,
    дочитываем consequence победившей карты. Возвращает эпилог (пусто — нет).
    """
    if not round_row.epilogue_text and round_row.winner_card is not None:
        cards = {card.position: card for card in round_row.cards}
        winning_card = cards.get(round_row.winner_card)
        if winning_card is not None and winning_card.consequence:
            round_row.epilogue_text = winning_card.consequence[:700]
            await session.commit()
    return round_row.epilogue_text or ""