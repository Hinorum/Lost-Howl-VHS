"""Единое оформление публичного лица бота: обо мне, приветствие, меню команд.

Накатывается через Bot API при каждом старте (apply_profile) — BotFather
не нужен. Те же тексты продублированы в README для ручных полей (аватар,
картинка описания), которых у API нет.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from app.config import settings

# «О боте» в профиле — не больше 120 символов. Первая фраза сразу снимает
# вопрос «это официально?»: архив неофициальный и фанатский.
BOT_ABOUT = (
    "Неофициальный фанатский интерактивный VHS-архив по мотивам Lost Dogs. "
    "Каждый день Стая решает, что останется на плёнке."
)

# Приветственный экран пустого чата («Что умеет этот бот?»), лимит 512.
def _bot_description() -> str:
    # Неофициальность вшита в первое предложение, а не вынесена дисклеймером
    # вниз: «видеомагнитофон, собранный из хлама» — и так самодельная вещь,
    # а «Lost Dogs» остаётся мотивом, а не источником.
    base = (
        "Это не мир, а видеомагнитофон LOST HOWL, собранный из хлама:\n"
        "неофициальный фанатский VHS-архив по мотивам «Lost Dogs: The Way».\n"
        "Раз в месяц хранитель ставит в лоток кассету: стая псов ищет дом\n"
        "в разбитом городе.\n"
        "Каждый день Стая решает, что останется на плёнке: один кадр в трёх\n"
        "вариантах, голосом или ставкой Gram. Жребий дня (большинство, меньшинство,\n"
        "середина) решает, какой кадр уцелеет;\n"
    )
    hints = []
    if settings.revote_enabled:
        hints.append("Передумал — перемотай кадр (/change).")
    if settings.ton_enabled:
        hints.append("Веришь в сценарий — поставь Gram на кадр (/stake).")
    tail = " ".join(hints)
    if tail:
        base += tail + "\n"
    base += "Нажми START: PLAY."
    return base


BOT_DESCRIPTION = _bot_description()


def _build_commands() -> tuple[list[BotCommand], list[BotCommand]]:
    """Меню отражает включённые механики: без ставок — без кошелька и /top.

    Порядок логичный: онбординг, ежедневная игра, счёт, экономика, стая,
    справка. В группе день живёт через /today; полная памятка — в /help.
    """
    private = [
        BotCommand(command="start", description="Как играть: PLAY кассеты"),
        BotCommand(command="today", description="Карты дня"),
        BotCommand(command="score", description="Карточка Стаи: титул и серия"),
    ]
    if settings.revote_enabled:
        private.append(
            BotCommand(
                command="change",
                description=(
                    "Перемотать кадр (⭐ или Gram)"
                    if settings.ton_enabled
                    else f"Перемотать кадр (⭐ {settings.revote_stars})"
                ),
            )
        )
    if settings.ton_enabled:
        private += [
            BotCommand(command="wallet", description="Привязать кошелёк Gram"),
            BotCommand(command="stake", description="Как поставить Gram"),
            BotCommand(command="top", description="Копилки и лидеры"),
            BotCommand(command="fund", description="Фонд Стаи: баланс и журнал"),
        ]
    private += [
        BotCommand(command="invite", description="Позвать в стаю"),
        BotCommand(command="help", description="Памятка команд"),
    ]
    group = [
        BotCommand(command="today", description="Карты дня"),
        BotCommand(command="help", description="Памятка команд"),
    ]
    return private, group


# Личный чат: полный список. /advance хранителя сюда не попадает.
PRIVATE_COMMANDS, GROUP_COMMANDS = _build_commands()


async def apply_profile(bot: Bot) -> None:
    """Идемпотентно оформляет бота; сбои сети не мешают запуску."""
    log = logging.getLogger(__name__)
    try:
        await bot.set_my_short_description(BOT_ABOUT)
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_commands(
            PRIVATE_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            GROUP_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        log.info("Профиль бота обновлён: описание, обо мне, команды")
    except Exception as exc:
        log.warning("Не удалось обновить профиль бота: %s", exc)
