# Диалог привязки кошелька: следующее сообщение игрока — это адрес.
#
# Catch-all живёт в ОТДЕЛЬНОМ роутере, который подключается последним
# (app/handlers/bootstrap.py: build_dispatcher). Раньше он регистрировался
# декоратором в общем роутере, и его позиция зависела от порядка импортов
# модулей: isort поднимал fallback выше player/wallet, и первый подходящий
# хендлер выигрывал — /start глотался молча. Отдельный роутер + явный
# include в конце снимают зависимость от порядка импортов целиком.
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.style import hint_mark, ok_mark

from .common import _dialog_close, _dialog_open
from .wallet import _bind_wallet

logger = logging.getLogger(__name__)

# Роутер catch-all'а: подключается к диспетчеру последним.
fallback_router = Router()


async def on_private_fallback(message: Message) -> None:
    """Диалог привязки кошелька: следующее сообщение игрока — это адрес.

    Живёт в отдельном роутере, который подключается после команд, поэтому
    команды перехватываются своими обработчиками раньше. Для всех остальных
    сообщений молчит.
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


# Регистрация на своём роутере — при импорте модуля. Позиция хендлера
# определяется порядком ПОДКЛЮЧЕНИЯ роутеров (build_dispatcher), а не
# порядком импортов модулей: isort умеет переставлять импорты, и такая
# перестановка однажды съела бы все команды.
fallback_router.message.register(
    on_private_fallback, F.chat.type == ChatType.PRIVATE
)
