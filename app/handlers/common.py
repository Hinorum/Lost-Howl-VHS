# Переиспользуемая инфраструктура хендлеров: роутер, защита диалога привязки,
# стоп-кран с объявлением, режим денег дня, флаг эха, общие клавиатуры.
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import SessionLocal
from app.models import RoundStatus, WalletDialog
from app.rounds import ensure_current_round, get_active_round

logger = logging.getLogger(__name__)

router = Router()

# Диалог привязки кошелька живёт 15 минут: начал — сразу пришли адрес.
# Дольше открытую «сессию» считаем забытой, чтобы случайное сообщение
# спустя день не трактовалось как попытка привязки.
_WALLET_DIALOG_TTL_SECONDS = 15 * 60


async def _ensure_round():
    async with SessionLocal() as session:
        return await ensure_current_round(session)


def _personal_keyboard(action: str, label: str) -> InlineKeyboardMarkup:
    """Кнопка личных данных: окно по нажатию видит только тот, кто нажал."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)]])


_DYOR_TEXT = (
    "Игра, а не вклад. Ты сам решаешь, на что ставить, и распоряжаешься своими ставками. DYOR."
)


async def _dialog_open(uid: int) -> bool:
    if uid <= 0:
        return False
    async with SessionLocal() as session:
        row = await session.get(WalletDialog, uid)
        if row is None:
            return False
        since = row.since
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        if since is not None and (
            datetime.now(UTC) - since
        ).total_seconds() > _WALLET_DIALOG_TTL_SECONDS:
            # Просроченный диалог закрываем и для других путей (бутстрап и т.п.).
            await session.delete(row)
            await session.commit()
            return False
        return True


async def _dialog_start(uid: int) -> None:
    if uid <= 0:
        return
    async with SessionLocal() as session:
        row = await session.get(WalletDialog, uid)
        if row is None:
            session.add(WalletDialog(player_id=uid))
        else:
            # Повторный старт продлевает окно ожидания адреса.
            row.since = datetime.now(UTC)
        await session.commit()


async def _dialog_close(uid: int) -> None:
    if uid <= 0:
        return
    async with SessionLocal() as session:
        row = await session.get(WalletDialog, uid)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _game_paused_now() -> bool:
    """Быстрая проверка стоп-крана без привязки к чужой сессии."""
    from app.ops import is_game_paused

    async with SessionLocal() as session:
        return await is_game_paused(session)


async def _active_round_money_mode() -> bool | None:
    """Режим открытого дня: True = со ставками/платной сменой, None = дня нет.

    День снимает режим на своё открытие (Round.money_mode), поэтому даже если
    хранитель переключил рубильник посреди дня — текущий день живёт по своему
    снимку (новый режим вступает со следующего дня).
    """
    async with SessionLocal() as session:
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return None
        return getattr(round_row, "money_mode", True) is not False


async def _set_paused_and_broadcast(bot, paused: bool, reason: str = "") -> tuple[bool, int]:
    """Стоп-кран игры + объявление в чатах. Возвращает (изменилось, чатов).

    Маркер announce:* (claim_once) страхует от двойного объявления при гонке
    двух процессов, когда оба успели прочитать старое состояние и оба
    подтвердили смену: рассылку в чаты делает только один из них.
    """
    from app.ops import claim_once, set_game_paused

    async with SessionLocal() as session:
        changed = await set_game_paused(session, paused, reason)
    if not changed:
        return False, 0
    from app.broadcast import whisper_to_chats

    text = (
        "⏸ Игра приостановлена: идут технические работы. Переводы на адрес фонда "
        "будут возвращены отправителям."
        if paused
        else "▶️ Технические работы завершены — игра возобновляется. Новый день откроется сам в течение минуты."
    )
    try:
        async with SessionLocal() as session:
            marker = f"announce:{'pause' if paused else 'resume'}:{int(time.time())}"
            if not await claim_once(session, marker):
                return True, 0
        delivered = await whisper_to_chats(bot, text)
    except Exception:
        logger.warning("Объявление о паузе/возобновлении не разослано", exc_info=True)
        delivered = 0
    return True, delivered
