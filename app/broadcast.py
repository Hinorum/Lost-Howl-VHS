"""Рассылка анонсов дней и итогов в чаты, где бот состоит администратором."""

from __future__ import annotations

import asyncio
import html
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY
from app.db import SessionLocal
from app.models import Chat, Round, RoundStatus, StatusPost, WatcherState
from app.style import day_mark
from app.tally import format_results

logger = logging.getLogger(__name__)

POSITIONS = ("I", "II", "III")
_MAX_TEXT_LEN = 3900
_TITLE_CLAMP = 80
_DILEMMA_CLAMP = 400  # == FIELD_LIMITS["dilemma"]: лимит схемы равен показу
_BUTTON_TEXT_MAX = 64  # текст inline-кнопки Telegram — до 64 знаков
_FORGET_MARKS = ("forbidden", "not found", "kicked", "deactivated", "migrated")


async def _notify_admins_for_empty_audience(bot: Bot | None, *, day_index: int, reason: str) -> None:
    """Админ-алерт, если день не увидел никто: в чаты и личку не ушло ничего.

    Это не замена нормальной рассылке, а диагностический сигнал: анонс может
    уйти в пустоту, если бот не добавлен ни в один активный чат и никто не
    сделал /start. В таком случае персональная рассылка в права не поможет,
    поэтому админов оповещаем напрямую и даём подсказку, что делать дальше.
    """
    if bot is None or not settings.admin_id_set:
        return
    text = (
        f"⚠️ Анонс дня {day_index} не дошёл ни до кого: {reason}.\n"
        "Подключи активный чат командой /bind в нём или попроси игроков сделать /start."
    )
    for admin_id in sorted(settings.admin_id_set):
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Админ %s не получил предупреждение о пустом анонсе дня %s", admin_id, day_index, exc_info=True)


def cards_keyboard(
    round_id: int,
    cards,
    remember: bool = False,
    day_index: int | None = None,
) -> InlineKeyboardMarkup:
