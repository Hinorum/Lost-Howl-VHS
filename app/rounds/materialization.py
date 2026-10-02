from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Round, RoundStatus, WinRule
from app.ops import money_mode_enabled

from .time import _day_window, _now, utc_aware

logger = logging.getLogger(__name__)


def _payload_cards(payload: dict) -> list[dict]:
    cards = [dict(card) for card in payload.get("cards") or []]
    for position, card in enumerate(cards):
        card.setdefault("position", position)
        card.setdefault("tag", "care")
    return cards


async def _materialize_round(
    session: AsyncSession, payload: dict, latest: Round | None
) -> Round:
    """Быстрая половина: раунд и карты из готового payload. Только БД."""
    day_index = int(payload["day_index"])
    now = _now()
    opens_at = (
        now
        if latest is None or latest.tally_ends_at is None
        else max(now, utc_aware(latest.tally_ends_at))
    )
    voting_ends_at, tally_ends_at = _day_window(opens_at)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule(payload["rule"]),
        rule_entropy=payload.get("rule_entropy") or None,
        chapter_title=payload["chapter_title"],
        chapter_text=payload["chapter_text"],
        opens_at=opens_at,
        voting_ends_at=voting_ends_at,
        tally_ends_at=tally_ends_at,
    )
    for card in _payload_cards(payload):
        round_row.cards.append(Card(**card))
    session.add(round_row)
    await session.flush()
    round_row.money_mode = bool(await money_mode_enabled(session))
    return round_row


async def _stamp_day_money_mode(session: AsyncSession, round_row: Round) -> None:
    """Снимок режима ставок на открытие дня (money_mode поле Round).

    Логика см. комментарий к _materialize_round: рубильник пишется при открытии.
    """
    round_row.money_mode = bool(await money_mode_enabled(session))