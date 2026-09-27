# Сборка диспетчера и обработчики глобальных сбоев (всё, что вне роутинга).
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import ErrorEvent, Message

from app.config import settings

from .common import _dialog_close, router

logger = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    @router.message.outer_middleware()
    async def _close_wallet_dialog_on_command(handler, event: Message, data: dict) -> None:
        """Любая команда, кроме /wallet, закрывает открытый диалог привязки.

        Специфичные хендлеры команд перехватывают апдейт раньше, чем
        on_private_fallback, поэтому там закрыть диалог невозможно — делаем
        это на уровне маршрутизатора: прошёл команду — значит ожидание адреса
        прервано. /wallet не трогаем: он сам продолжает/открывает привязку.
        """
        if isinstance(event, Message) and event.chat.type == ChatType.PRIVATE:
            text = (event.text or "").strip()
            if text.startswith("/") and not text.startswith("/wallet"):
                uid = event.from_user.id if event.from_user else 0
                try:
                    await _dialog_close(uid)
                except Exception:
                    logger.exception("Не удалось закрыть диалог кошелька (uid=%s)", uid)
        return await handler(event, data)

    _register_error_handler(dispatcher)
    return dispatcher


def _register_error_handler(dispatcher: Dispatcher) -> None:
    """Глобальный обработчик сбоев: без него aiogram глотает исключение и
    отвечает вебхуку 200 — Telegram не перезашлёт апдейт, и действие игрока
    (голос, оплата) теряется молча. Логируем стек и говорим игроку честное
    «не получилось»: повторный клик обычно проходит. Кнопке снимаем спиннер
    (иначе он висит до клиентского таймаута), а причину сбоя хранитель
    получает в личку (троттлинг раз в час) — диагноз не требует логов."""

    @dispatcher.error()
    async def on_error(event: ErrorEvent) -> bool:
        await handle_update_error(event.bot, event)
        return True


_LAST_UPDATE_ERROR_ALERT: dict[str, float] = {}


_UPDATE_ERROR_ALERT_COOLDOWN = 3600.0


_PLAYER_ERROR_TEXT = (
    "⚠️ Плёнка заело — шаг не засчитан. Перемотай и попробуй ещё; "
    "если повторится, напиши хранителю."
)


def _describe_update(event) -> tuple[str, str]:
    """Кто и что именно сломалось: (kind, описание для лога и тревоги).

    kind («callback»/«message»/«update») держит троттлинг тревоги: при одном
    счётчике на все сбои падение кнопки на час затыкало бы тревогу о падении
    сообщения, и наоборот — второй инцидент выглядел бы как тишина.

    Описание одно и то же в строке лога и в тексте тревоги: хранитель ищет в
    логах ровно то, что ему показали. Раньше в лог уходила строка «Ошибка
    обработки апдейта» без единого идентификатора, а тревога обещала найти по
    ней нужный стек — при десяти одинаковых строках это было невозможно.
    """
    update = event.update
    callback = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)
    if callback is not None:
        kind = "callback"
    elif message is not None:
        kind = "message"
    else:
        kind = "update"
    parts = [f"kind={kind}"]
    user = getattr(update, "from_user", None)
    if user is not None:
        parts.append(f"uid={user.id}")
    chat = getattr(message, "chat", None) if message is not None else None
    if chat is None and callback is not None:
        chat = getattr(getattr(callback, "message", None), "chat", None)
    if chat is None:
        parts.append("chat=?")
    else:
        parts.append(f"chat={chat.id} ({getattr(chat, 'type', '?')})")
    parts.append(f"update_id={getattr(update, 'update_id', '?')}")
    if callback is not None:
        # Обрезаем: в callback_data может быть пользовательский текст, а нужна
        # лишь кнопка, на которой что-то отвалилось.
        parts.append(f"data={str(getattr(callback, 'data', '') or '')[:40]!r}")
    return kind, " ".join(parts)


async def handle_update_error(bot: Bot | None, event) -> None:
    """Единая реакция на упавший апдейт: игроку, кнопке и хранителю."""
    import time as _time

    kind, detail = _describe_update(event)
    logger.error("Ошибка обработки апдейта: %s", detail, exc_info=event.exception)
    update = event.update
    callback = update.callback_query
    chat_id = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif callback is not None and getattr(callback, "message", None) is not None:
        chat_id = callback.message.chat.id
    # Кнопка не должна крутиться до клиентского таймаута.
    if callback is not None:
        try:
            await callback.answer("Плёнка заело — перемотай и попробуй ещё.", show_alert=True)
        except Exception:
            logger.debug("Спиннер на кнопке снять не вышло: %s", detail, exc_info=True)
    if chat_id is not None and bot is not None:
        try:
            await bot.send_message(chat_id, _PLAYER_ERROR_TEXT)
        except Exception:
            logger.debug("Игроку %s не сообщили о сбое: %s", chat_id, detail, exc_info=True)
    now = _time.time()
    last = _LAST_UPDATE_ERROR_ALERT.get(kind, 0.0)
    if (
        bot is not None
        and settings.admin_id_set
        and now - last >= _UPDATE_ERROR_ALERT_COOLDOWN
    ):
        _LAST_UPDATE_ERROR_ALERT[kind] = now
        summary = f"{type(event.exception).__name__}: {event.exception}"[:350]
        from app.ops import notify_admins

        try:
            await notify_admins(
                bot,
                f"⚠️ Сбой обработки апдейта ({kind}): {summary}\n"
                f"Идентификаторы: {detail}\n"
                "Полный стек — в логах сервиса по строке с этими идентификаторами.",
            )
        except Exception:
            # Тревога о сбое, потерянная молча, — это инцидент без следа.
            logger.exception("Тревога о сбое апдейта не доставлена (%s)", detail)


async def create_bot() -> Bot:
    if not settings.bot_token or settings.bot_token.endswith("replace-me"):
        raise RuntimeError("Впиши BOT_TOKEN в .env или переменные окружения @BotFather.")
    return Bot(settings.bot_token)
