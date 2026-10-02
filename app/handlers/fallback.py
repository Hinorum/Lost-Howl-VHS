# Диалог привязки кошелька: следующее сообщение игрока — это адрес.
# Хендлер вешается явно через register_private_fallback() в конце
# app.handlers, после команд: isort иначе ставит этот модуль раньше player.
from __future__ import annotations

import logging

from aiogram import F
from aiogram.enums import ChatType
from aiogram.types import Message

from app.style import hint_mark, ok_mark

from .common import _dialog_close, _dialog_open, router
from .wallet import _bind_wallet

logger = logging.getLogger(__name__)

_FALLBACK_REGISTERED = False


def register_private_fallback() -> None:
    """Catch-all ЛС — строго после команд: иначе /help глотается молча.

    Декоратор @router.message в этом модуле нельзя: isort поднимает
    `from .fallback` раньше player/wallet, и первый подходящий хендлер
    выигрывает. Регистрируем явно из пакета, когда все команды уже на роутере.
    """
    global _FALLBACK_REGISTERED
    if _FALLBACK_REGISTERED:
        return
    router.message.register(on_private_fallback, F.chat.type == ChatType.PRIVATE)
    _FALLBACK_REGISTERED = True


async def on_private_fallback(message: Message) -> None:
    """Диалог привязки кошелька: следующее сообщение игрока — это адрес.

    Регистрируется последним, поэтому команды перехватываются своими
    обработчиками раньше. Для всех остальных сообщений молчит.
    """
    uid = message.from_user.id if message.from_user else 0
    if not await _dialog_open(uid):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(f"{hint_mark('retry')} Пришли адрес текстом (UQ…/EQ…) или напиши «отмена».")
        return
    if text.lower() in {"отмена", "cancel"}:
        await _dialog_close(uid)
        await message.answer(f"{ok_mark('cancel')} Отменено. Когда будешь готов: /wallet")
        return
    if text.startswith("/"):
        # Любая другая команда закрывает режим ожидания без лишнего шума.
        await _dialog_close(uid)
        return
    await _bind_wallet(message, text)
