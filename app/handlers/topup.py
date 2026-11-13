# Платная смена голоса: перевод Gram на адрес казначея и инструкция по оплате.
from __future__ import annotations

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.broadcast import POSITIONS
from app.config import settings
from app.db import SessionLocal
from app.models import RoundStatus
from app.payments import revote_memo
from app.rounds import get_active_round
from app.style import hint_mark, money_mark, path_mark, warn_mark
from app.voting import get_vote, upsert_player

from .common import (
    _active_round_money_mode,
    _game_paused_now,
    _personal_keyboard,
    router,
)


def _revote_gram_ceiling() -> float:
    """Верх вилки Gram-оплаты смены пути: строго ниже минимальной ставки.

    Приём revote-переводов идёт по вилке [revote_ton, stake_min_ton) — «до
    X» в тексте обязано быть ровно на один доцентный шаг ниже минимума,
    иначе при смене настроек название врёт проверке конфига.
    """
    return round(settings.stake_min_ton - 0.01, 4)


def _revote_keyboard(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💎 {settings.revote_ton:g} Gram",
                    callback_data=f"payton:{round_id}",
                )
            ]
        ]
    )


async def _revote_status(user) -> tuple[str, int | None]:
    """(текст, round_id). round_id не None только если день открыт И путь выбран."""
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return f"{warn_mark('revote-closed')} Кадр уже записан — перемотать нельзя.", None
        vote = await get_vote(session, round_row.id, player.id)
        if vote is None:
            return f"{hint_mark('revote-free')} Ты ещё не сделал выбор дня — первая запись бесплатная.", None
        # Название сцены из кассеты (card.title по позиции), не римская цифра.
        titles = {card.position: (card.title or "").strip() for card in round_row.cards}
        label = titles.get(vote.card_position) or POSITIONS[vote.card_position]
        return (
            f"{path_mark('care', str(player.id))} Сегодня твой выбор: {label}. "
            "Оплати перемотку кадра и нажми другой вариант. Грант действует до конца дня.",
            round_row.id,
        )


@router.message(Command("change"))
async def cmd_change(message: Message) -> None:
    if not settings.revote_enabled:
        await message.answer(f"{warn_mark('revote-off')} Перемотка кадра сейчас недоступна.")
        return
    # Кулдаун ДО любой работы: команда читает игрока, активный раунд и строит
    # клавиатуру, а вызывается на каждый вызов. Без ограничения скрипт-клиент
    # (MTProto, а не обычный клиент — у того нет лимита на нажатия) устраивает
    # постоянный поток запросов в БД и в Telegram.
    if message.from_user is not None and message.from_user.id not in settings.admin_id_set:
        from app.handlers.wallet import _wallet_throttled

        async with SessionLocal() as session:
            if await _wallet_throttled(session, message.from_user.id, "change_cd"):
                await message.answer(
                    f"{hint_mark('wallet-throttle')} Не так часто — попробуй ещё раз через полминуты."
                )
                return
    # Бесплатная версия (TON выключен): перемотки нет — платить нечем и незачем.
    if not settings.ton_enabled:
        await message.answer(
            f"{warn_mark('revote-off')} Перемотка кадра недоступна в бесплатной версии: "
            "игра идёт без ставок и платных действий."
        )
        return
    # Без адреса казначея оплату принять некуда: не обещаем кнопку, которая
    # на следующем шаге откажет.
    if not settings.active_treasury_address:
        await message.answer(
            f"{warn_mark('revote-off')} Приём Gram ещё не включён — "
            "перемотка кадра временно недоступна."
        )
        return
    # Версия без ставок: перемотка за валюту выключена целиком.
    if await _active_round_money_mode() is False:
        await message.answer(
            f"{warn_mark('revote-off')} Игра идёт в версии без ставок: "
            "перемотка кадра недоступна — первая запись и есть твой кадр."
        )
        return
    status, round_id = await _revote_status(message.from_user)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(status)
        if round_id is not None:
            await message.answer(
                "Оплата перемотки — <b>Gram</b>: перевод казначею от "
                f"{settings.revote_ton:g} до {_revote_gram_ceiling():g} Gram "
                f"(строго меньше минимума ставки {settings.stake_min_ton:g} Gram). "
                "Мемо не обязателен — сумма в нужном диапазоне зачтётся автоматически.",
                parse_mode=ParseMode.HTML,
                reply_markup=_revote_keyboard(round_id),
            )
        return
    # В группе деталей не даём: только приватная кнопка — как у /score и /wallet.
    await message.answer(
        "Перемотка кадра видна только тебе — нажми кнопку.",
        reply_markup=_personal_keyboard("change:view", "⏪ Перемотать кадр"),
    )


@router.callback_query(F.data == "change:view")
async def on_change_view(callback: CallbackQuery) -> None:
    # Кнопка «Перемотать кадр» живёт в сообщениях чата и переживает выключение
    # фичи: тап по старой кнопке не должен продавать то, что уже не продаётся
    # (/change в этом случае отказывает).
    if not settings.revote_enabled:
        await callback.answer("Перемотка кадра сейчас недоступна.", show_alert=True)
        return
    if not settings.ton_enabled:
        await callback.answer(
            "Перемотка кадра недоступна в бесплатной версии.", show_alert=True
        )
        return
    if not settings.active_treasury_address:
        await callback.answer(
            "Приём Gram ещё не включён — перемотка кадра временно недоступна.",
            show_alert=True,
        )
        return
    status, _round_id = await _revote_status(callback.from_user)
    await callback.answer(f"{status} Открой бота в личке: /change"[:200], show_alert=True)


def _payton_round_id(data: str) -> int | None:
    """Счёт из data:payton:<round_id>: цифры — id раунда, иначе None."""
    raw = data.split(":", 1)[1] if ":" in data else ""
    return int(raw) if raw.isdigit() else None


async def _payton_guard(callback: CallbackQuery) -> str | None:
    """Текущий статус кнопки оплаты перемотки: текст отказа или None, если кнопка жива.

    Проверки по порядку: личка → фича → стоп-кран → версия без ставок → казначей →
    валидность счёта → сверка счёт ↔ активный день → cooldown для неадмина.
    Любой отказ молча не открывает инструкцию: игрок не должен платить за
    несуществующий кадр.
    """
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        return "Оплата — только в личке у бота (/change)."
    if not settings.revote_enabled:
        return "Перемотка кадра сейчас недоступна."
    # Стоп-кран: во время техработ реквизиты не выдаём, чтобы игрок не
    # отправил деньги за смену пути в замороженной игре.
    if await _game_paused_now():
        return "⏸ Идут технические работы — оплата временно недоступна. Попробуй позже."
    # Версия без ставок: платная смена выбора не продаётся вовсе.
    if await _active_round_money_mode() is False:
        return "Игра идёт в версии без ставок: перемотка кадра недоступна."
    if not settings.ton_enabled or not settings.active_treasury_address:
        return "Приём Gram ещё не включён — оплата перемотки временно недоступна."
    round_id = _payton_round_id(callback.data or "")
    if round_id is None:
        return "Некорректный счёт."
    # Счёт мог устареть: показываем реквизиты только для активного дня и
    # только тому, кто уже сделал первый выбор.
    status, active_id = await _revote_status(callback.from_user)
    if active_id != round_id:
        return status[:200]
    if callback.from_user.id not in settings.admin_id_set:
        from app.handlers.wallet import _wallet_throttled

        async with SessionLocal() as session:
            if await _wallet_throttled(session, callback.from_user.id, "payton_cd"):
                return (
                    f"{hint_mark('wallet-throttle')} Инструкция только что отправлена — "
                    "подожди полминуты."
                )
    return None


def _payton_payment_text(raw: str, address: str) -> str:
    """Инструкция по Gram: вилка, потолок ниже ставки, memo и предупреждение,
    что ровно минимальная ставка — это ставка дня, а не оплата перемотки."""
    return (
        f"{money_mark(raw)} Перемотка кадра — оплата Gram.\n"
        f"Переведи <b>{settings.revote_ton:g}–{_revote_gram_ceiling():g} Gram</b> "
        f"(строго меньше минимума ставки {settings.stake_min_ton:g} Gram) на казначея:\n"
        f"<code>{address}</code>\n"
        f"Memo не обязателен — сумма из вилки зачтётся автоматически. Можно приложить "
        f"<code>{revote_memo(int(raw))}</code> в любой части комментария.\n\n"
        "Грант придёт в течение минуты и живёт до конца дня. "
        f"Ровно {settings.stake_min_ton:g} Gram — уже минимальная ставка, не оплата перемотки.\n"
        "Для оплаты кошелёк должен быть привязан: /wallet."
    )


@router.callback_query(F.data.startswith("payton:"))
async def on_payton(callback: CallbackQuery) -> None:
    error = await _payton_guard(callback)
    if error is not None:
        await callback.answer(error, show_alert=True)
        return
    round_id = _payton_round_id(callback.data or "")
    message = callback.message
    if message is None:
        # Guard уже отвечает в личке; защита от None — для типов.
        return
    await message.answer(_payton_payment_text(str(round_id), settings.active_treasury_address))
    await callback.answer()
