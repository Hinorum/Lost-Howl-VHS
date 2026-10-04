# Пополнения и платная смена голоса: звёзды Telegram, банковский перевод на
# адрес фонда и проверка подтверждений перед зачислением.
from __future__ import annotations

import logging
from datetime import UTC

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select

from app.broadcast import POSITIONS
from app.config import settings
from app.db import SessionLocal
from app.models import Income, RevoteGrant, RoundStatus
from app.payments import build_revote_payload, parse_revote_payload, revote_memo
from app.rounds import get_active_round, get_round
from app.style import hint_mark, money_mark, ok_mark, path_mark, warn_mark
from app.voting import get_vote, upsert_player

from .common import (
    _active_round_money_mode,
    _game_paused_now,
    _personal_keyboard,
    router,
)

logger = logging.getLogger(__name__)


def _revote_gram_ceiling() -> float:
    """Верх вилки Gram-оплаты смены пути: строго ниже минимальной ставки.

    Приём revote-переводов идёт по вилке [revote_ton, stake_min_ton) — «до
    X» в тексте обязано быть ровно на один доцентный шаг ниже минимума,
    иначе при смене настроек название врёт проверке конфига.
    """
    return round(settings.stake_min_ton - 0.01, 4)


def _revote_keyboard(round_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ {settings.revote_stars} Stars", callback_data=f"paystars:{round_id}")]
    ]
    if settings.ton_enabled:
        rows.append(
            [InlineKeyboardButton(text=f"💎 {settings.revote_ton:g} Gram", callback_data=f"payton:{round_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        return (
            f"{path_mark('care', str(player.id))} Сегодня твой выбор: {POSITIONS[vote.card_position]}. "
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
    # Версия без ставок: перемотка за валюту/звёзды выключена целиком.
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
                "Выбери способ оплаты перемотки:\n\n"
                "⭐ <b>Stars</b> — надёжно и мгновенно (кнопка оплаты в Telegram).\n"
                "💎 <b>Gram</b> — перевод казначею: от "
                f"{settings.revote_ton:g} до {_revote_gram_ceiling():g} Gram (строго меньше минимума ставки "
                f"{settings.stake_min_ton:g} Gram). Мемо не обязателен: сумма из вилки "
                "зачтётся сама как оплата перемотки кадра. Если приложишь комментарий — "
                "бот найдёт `rv:день` в любой части текста, даже с подписью кошелька.",
                parse_mode=ParseMode.HTML,
                reply_markup=_revote_keyboard(round_id),
            )
        return
    # В группе деталей не даём: только приватная кнопка — как у /score и /wallet.
    await message.answer(
        "Перемотка кадра видна только тебе — нажми кнопку.",
        reply_markup=_personal_keyboard("change:view", "Перемотать кадр"),
    )


@router.callback_query(F.data == "change:view")
async def on_change_view(callback: CallbackQuery) -> None:
    status, _round_id = await _revote_status(callback.from_user)
    await callback.answer(f"{status} Открой бота в личке: /change"[:200], show_alert=True)


@router.callback_query(F.data.startswith("paystars:"))
async def on_paystars(callback: CallbackQuery) -> None:
    raw = callback.data.split(":")[1]
    if not raw.isdigit() or callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Оплата — только в личке у бота (/change).", show_alert=True)
        return
    # Стоп-кран: во время техработ платные механики не продаются,
    # чтобы игрок не платил за смену пути в замороженной игре.
    if await _game_paused_now():
        await callback.answer(
            "⏸ Идут технические работы — оплата временно недоступна. Попробуй позже.",
            show_alert=True,
        )
        return
    # Версия без ставок: платная смена выбора не продаётся вовсе.
    if await _active_round_money_mode() is False:
        await callback.answer(
            "Игра идёт в версии без ставок: перемотка кадра недоступна.",
            show_alert=True,
        )
        return
    status, active_id = await _revote_status(callback.from_user)
    round_id = int(raw)
    if active_id != round_id:
        await callback.answer(status[:200], show_alert=True)
        return
    # Кулдаун на выставление счёта. send_invoice — это вызов Bot API: без
    # ограничения кнопка «Оплатить» превращается в способ жечь квоту бота и
    # ловить flood-ожидание, и токен бота страдает от чужих нажатий.
    if callback.from_user.id not in settings.admin_id_set:
        from app.handlers.wallet import _wallet_throttled

        async with SessionLocal() as session:
            if await _wallet_throttled(session, callback.from_user.id, "paystars_cd"):
                await callback.answer(
                    f"{hint_mark('wallet-throttle')} Счёт только что выставлен — подожди полминуты.",
                    show_alert=True,
                )
                return
    await callback.bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="Перемотка кадра",
        description="Разовое право переписать выбор в сегодняшнем кадре. Действует до конца дня.",
        payload=build_revote_payload(round_id),
        currency="XTR",
        prices=[LabeledPrice(label="Перемотка кадра", amount=settings.revote_stars)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    if parse_revote_payload(query.invoice_payload) is None:
        await query.answer(ok=False, error_message="Счёт устарел. Вызови /change заново.")
        return
    # Второй рубеж стоп-крана: счёт мог быть выставлен до паузы —
    # отклоняем оплату ДО списания Stars.
    if await _game_paused_now():
        await query.answer(
            ok=False,
            error_message="⏸ Идут технические работы — оплата приостановлена. Попробуй позже.",
        )
        return
    # Версия без ставок: даже выставленный до переключения счёт не списываем.
    if await _active_round_money_mode() is False:
        await query.answer(
            ok=False,
            error_message="Игра перешла в версию без ставок — перемотка кадра отключена.",
        )
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    round_id = parse_revote_payload(payment.invoice_payload)
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        duplicate = await session.execute(
            select(RevoteGrant.id).where(RevoteGrant.unit_ref == payment.telegram_payment_charge_id)
        )
        if duplicate.scalar_one_or_none() is not None:
            await message.answer("Эта оплата уже учтена.")
            return
        valid = False
        if round_id is not None:
            round_row = await get_round(session, round_id)
            valid = round_row is not None and round_row.status == RoundStatus.OPEN
        session.add(
            RevoteGrant(
                round_id=round_id if valid else None,
                player_id=player.id,
                source="stars",
                unit_ref=payment.telegram_payment_charge_id,
            )
        )
        # Ledger доходов: звёзды оседают на балансе бота — фиксируем сумму.
        session.add(
            Income(
                kind="stars",
                amount_stars=payment.total_amount,
                round_id=round_id if valid else None,
                player_id=player.id,
                unit_ref=payment.telegram_payment_charge_id,
                note="revote",
            )
        )
        await session.commit()
    if valid:
        await message.answer(f"{ok_mark(str(round_id))} Оплачено ⭐ Нажми другой вариант — кадр перемотан.")
    else:
        await message.answer(
            f"{warn_mark('late-pay')} Оплата прошла, но день уже закрылся — грант сохранён. "
            "Напиши хранителю игры для возврата."
        )


@router.message(F.refunded_payment)
async def on_refunded_payment(message: Message) -> None:
    """Возврат Stars через Telegram: грант отзывается, ledger помечается.

    Если грант ещё не потрачен — он больше не даст сменить путь; если уже
    потрачен, смена пути остаётся, а хранитель получает алерт о расхождении.
    """
    from datetime import datetime

    from app.ops import notify_admins

    payment = message.refunded_payment
    if payment is None:
        return
    charge_id = payment.telegram_payment_charge_id
    spent_at_refund = False
    player_ref = message.from_user.id if message.from_user else 0
    async with SessionLocal() as session:
        grant = (
            await session.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id))
        ).scalar_one_or_none()
        if grant is not None:
            if grant.status == "granted":
                grant.status = "refunded"
            else:
                spent_at_refund = True
        income = (
            await session.execute(select(Income).where(Income.unit_ref == charge_id))
        ).scalar_one_or_none()
        if income is not None:
            stamp = int(datetime.now(UTC).timestamp())
            tail = f"refunded:{stamp}"
            income.note = (f"{income.note} | {tail}" if income.note else tail)[:200]
        await session.commit()
    try:
        await message.answer(
            "↩️ Возврат звёзд проведён. Грант на перемотку кадра отозван."
            if not spent_at_refund
            else "↩️ Возврат проведён, но перемотка кадра уже была использована — напишу хранителю."
        )
    except Exception as exc:
        # Деньги уже возвращены и хранитель получит уведомление строкой ниже;
        # не доставившийся ответ — не повод ронять обработчик. Но и не повод
        # замолчать: причина может быть не блокировкой бота, а сбоем сети.
        logger.debug("Возврат звёзд: ответ игроку не доставлен: %s", exc)
    bot = getattr(message, "bot", None)
    await notify_admins(
        bot,
        f"↩️ Stars refund: charge `{charge_id}` от игрока {player_ref}"
        + (" — грант был УЖЕ потрачен." if spent_at_refund else "."),
    )


@router.callback_query(F.data.startswith("payton:"))
async def on_payton(callback: CallbackQuery) -> None:
    raw = callback.data.split(":")[1]
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Оплата — только в личке у бота (/change).", show_alert=True)
        return
    if not settings.ton_enabled or not settings.active_treasury_address:
        await callback.answer("Приём Gram ещё не включён. Используй Stars.", show_alert=True)
        return
    if not raw.isdigit():
        await callback.answer("Некорректный счёт.", show_alert=True)
        return
    if callback.from_user.id not in settings.admin_id_set:
        from app.handlers.wallet import _wallet_throttled

        async with SessionLocal() as session:
            if await _wallet_throttled(session, callback.from_user.id, "payton_cd"):
                await callback.answer(
                    f"{hint_mark('wallet-throttle')} Инструкция только что отправлена — "
                    "подожди полминуты.",
                    show_alert=True,
                )
                return
    address = settings.active_treasury_address
    await callback.message.answer(
        f"{money_mark(raw)} Переведи от {settings.revote_ton:g} до {_revote_gram_ceiling():g} Gram "
        f"(строго меньше минимума ставки {settings.stake_min_ton:g} Gram) на адрес казначея:\n"
        f"<code>{address}</code>\n\n"
        f"Комментарий (memo) не обязателен — сумма из вилки зачтётся автоматически.\n"
        f"Можно приложить (бот распознает `rv:день` в любой части текста):\n<code>{revote_memo(int(raw))}</code>\n\n"
        "Эту сумму ставкой быть не может — потолок ниже минимума ставки. "
        "Ровно 0.5 Gram не подойдёт: это уже минимальная ставка, а не оплата перемотки кадра "
        "(бот примет её как ставку дня). Если кошелёк приложит мемо — оплата привяжется "
        "мгновенно и любой суммой из вилки. Без мемо сумма из той же вилки зачтётся "
        "автоматически. Грант на перемотку придёт в течение минуты.\n\n"
        "💡 Надёжнее и без кошелька — Stars: кнопка оплаты прямо в Telegram.\n"
        "Кошелёк должен быть привязан: /wallet. Неиспользованный до конца дня грант сгорает.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()
