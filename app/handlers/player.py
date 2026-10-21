# Онбординг и ежедневная игра: старт, карты дня, счёт и голосование.
# Сюжетные команды (/lore, /calling, /best, нюх, квиз памяти) сняты вместе
# со слоем сюжета — ядро: голосование, ставки и выплаты.
from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.broadcast import CARD_POSITIONS, POSITIONS, cards_keyboard, scene_label, status_text
from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardClaim, RoundStatus
from app.rounds import get_active_round, get_latest_round
from app.style import (
    day_mark,
    hint_mark,
    ok_mark,
    path_mark,
    warn_mark,
)
from app.ton_utils import from_nano, to_nano
from app.voting import cast_vote, change_vote, get_vote, upsert_player
from app.weeks import human_claim_period

from .common import _DYOR_TEXT, _ensure_round, _personal_keyboard, router

logger = logging.getLogger(__name__)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start."""
    lines = [
        "<b>Команды Стаи</b>",
        "/start — вставить кассету: как играть и памятка",
        "/today — кадр дня: варианты и выбор",
        "/score — карточка Стаи: титул, серия и голоса за неделю и месяц",
        "/dispute — жалоба на итог дня: хранитель разберёт",
    ]
    if settings.revote_enabled and settings.ton_enabled:
        # В бесплатной версии /change безусловно отказывает (topup) — не рекламируем.
        lines.append("/change — перемотать кадр (⭐ или Gram)")
    if settings.ton_enabled:
        from app.style import pct_text as _pct_text

        lines.append("/wallet — кошелёк · /stake — поставить Gram на кадр")
        lines.append("/top — копилки и лидеры")
        lines.append("/fund — Фонд Стаи: баланс и журнал")
        pool_pct = round(
            100
            - settings.owner_rake_pct
            - settings.leaderboard_rake_pct
            - settings.weekly_pot_pct
            - settings.pack_fund_pct
            - settings.referral_pct,
            2,
        )
        lines.append(
            f"\n💰 Фонд дня: {_pct_text(pool_pct)}% — угадавшим кадр. Детали — /stake; "
            "копилки и награды за приглашённых — /top и /invite."
        )
    lines.append("/invite — позвать в стаю: ссылка и награда за приведённых")
    return lines


async def _invite_text(user, bot) -> str | None:
    """Текст «плёнки наград»: ссылка, счётчик, копилка. None — приглашения выключены."""
    from app.referrals import (
        invited_count,
        referral_link,
        referral_pot_balance,
        resolve_bot_username,
    )

    username = await resolve_bot_username(bot)
    link = referral_link(user.id, username)
    if not link:
        return None
    count = await invited_count(user.id)
    balance = await referral_pot_balance(user.id)
    threshold_nano = to_nano(settings.referral_min_payout_gram)
    if balance <= 0:
        status = "Пока копилка пуста: награда капает с подтверждённых ставок приведённых."
    elif balance >= threshold_nano:
        status = (
            "🎁 Порог выплаты пройден — придёт на подтверждённый кошелёк "
            "автоматически в ближайшей финализации дня."
        )
    else:
        need = from_nano(threshold_nano - balance)
        status = f"🌸 До выплаты не хватает {need:g} Gram — копилка докапает с новых ставок."
    earned = from_nano(balance)
    return (
        f"🐾 <b>Твоя плёнка наград</b>\n\n"
        f"Ссылка в стаю:\n{link}\n\n"
        "Кто придёт по ней — тот войдёт следом за тобой.\n"
        f"Приведено: {count}\n"
        f"В копилке: {earned:g} Gram "
        f"(автовыплата от {settings.referral_min_payout_gram:g} Gram)\n\n"
        f"{status}"
    )


@router.message(Command("invite", "referral"))
async def cmd_invite(message: Message) -> None:
    """Личная ссылка приглашения, счётчик приведённых и копилка наград.

    Команда /referral — тот же хендлер: обе вели на одну страницу, объединены.
    """
    if message.chat.type != ChatType.PRIVATE:
        return
    caller = message.from_user
    if caller is None:
        return
    text = await _invite_text(caller, getattr(message, "bot", None))
    if text is None:
        await message.answer("🧭 Приглашения в стаю пока не открыты — приходи чуть позже.")
        return
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        await _record_start_referral(session, message)
        keyboard = await _start_keyboard(session, player)
    uid = str(message.from_user.id) if message.from_user else "0"
    lines = [
        f"{day_mark(uid)} <b>{settings.world_name}</b>",
        "",
        "Перед тобой видеомагнитофон LOST HOWL: раз в месяц хранитель вставляет",
        "кассету — фанфик по «Lost Dogs: The Way» про стаю псов, что ищет дом.",
        "",
        "Каждый день — один кадр в трёх вариантах. Жребий дня решает,",
        "какой кадр уцелеет и поедет в сценарий дальше.",
    ]
    if settings.ton_enabled:
        lines += [
            "Выбор — голосом или ставкой Gram: день Большинства — за кадром",
            "с самой большой суммой Gram, Меньшинства — с самой малой, Середины —",
            "со средней.",
            "🐾 <b>Как поставить Gram — /stake.</b> Голос тоже ведёт: он строит "
            "лидерборд недели и месяца.",
        ]
    else:
        lines += [
            "Выбор — голосом: день Большинства — за кадром с наибольшим числом",
            "голосов, Меньшинства — с наименьшим, Середины — со средним.",
            "🐾 Кадр выбирают кнопкой под картой дня — до конца дня.",
        ]
    lines += [
        "",
        "Итог дня и новый кадр придут сразу после закрытия.",
    ]
    lines.extend(_commands_help())
    if settings.ton_enabled:
        lines.append(f"\n⚠️ {_DYOR_TEXT}")
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _record_start_referral(session, message: Message) -> None:
    """Фиксирует пришедшего по чужой ссылке один раз; всё побочное — молча.

    Порядок: upsert_player уже создал строку, так что приглашающий существует
    в таблице. Отказы (самоссылка, подделка, повтор) не должны мешать /start.
    """
    try:
        from app.referrals import parse_referral_arg, record_referral

        caller = message.from_user
        if caller is None or caller.id <= 0:
            return
        get_args = getattr(message, "get_args", None)
        if callable(get_args):
            arg = (get_args() or "").strip()
        else:
            parts = (message.text or "").split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            return
        referrer_id = parse_referral_arg(arg)
        if referrer_id is None:
            return
        await record_referral(session, referrer_id=referrer_id, referred_id=caller.id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Реферальный переход не записан")


# Быстрые действия — одни и те же на пульте и на карточке Стаи: одна марка
# на смысл, состав собирается из этого кортежа, а не дублируется по коду.
_QUICK_ACTIONS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("💰 Кошелёк", "menu:wallet"), ("💸 Ставка", "stake:view")),
    (("🏆 Копилки", "menu:top"), ("🏦 Фонд", "menu:fund")),
)


def _quick_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in _QUICK_ACTIONS
    ]


def _menu_keyboard() -> InlineKeyboardMarkup:
    """Пульт LOST HOWL: кнопки-действия вместо вызова команд слепым меню.

    Сами действия — уже существующие колбэки, где их хватает (карточка
    Стаи, ставка — с приватным окном в группе), или короткие menu:* сценарии.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="▶️ Сегодня", callback_data="menu:today"),
            InlineKeyboardButton(text="🏅 Карточка Стаи", callback_data="score:view"),
        ],
        *_quick_rows(),
        [
            InlineKeyboardButton(text="📨 Позвать в стаю", callback_data="menu:invite"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _start_keyboard(session, player) -> InlineKeyboardMarkup:
    """Личное меню /start: пульт + претензии на места лидерборда.

    Кнопки Claim появляются только у игроков, попавших в ничью за призовые
    места закрытого периода, пока окно заявок открыто (приз ещё не роздан).
    Остальным кнопка не видна: заявлять нечего.
    """
    markup = _menu_keyboard()
    buttons: list[InlineKeyboardButton] = []
    if settings.leaderboard_claim_enabled:
        from app.leaderboard import _claim_window_players

        for kind, text, data in (
            ("week", "🎟 Заявить приз недели", "claim:week"),
            ("month", "🎟 Заявить приз месяца", "claim:month"),
        ):
            tied_players, _period = await _claim_window_players(session, kind)
            if player.id in tied_players:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        if buttons:
            markup.inline_keyboard.append(buttons)
    return markup


@router.callback_query(F.data == "claim:week")
async def on_claim_week(callback: CallbackQuery) -> None:
    await _on_claim(callback, "week")


@router.callback_query(F.data == "claim:month")
async def on_claim_month(callback: CallbackQuery) -> None:
    await _on_claim(callback, "month")


async def _on_claim(callback: CallbackQuery, kind: str) -> None:
    """Претензия на место лидерборда: решает ничьи по времени Claim.

    Кнопка видна только tied-игрокам, пока окно Claim открыто (приз не
    распределён из-за ничьей). Дублирующий check здесь — защита от hand-craft
    callback: период берётся из окна, а не из текущей даты. После дедлайна
    claim_window_hours заявки закрыты — поздняя претензия не переставляет
    молчаливого соперника. Кошелёк обязателен,
    иначе приз физически некуда отправить. Идемпотентно:
    unique(player_id, kind, period) — повторный тап не заводит вторую запись.
    """
    if callback.from_user is None:
        await callback.answer()
        return
    from app.leaderboard import _load_claim_window

    async with SessionLocal() as session:
        window = await _load_claim_window(session, kind)
        tied_players = list(window.get("players") or []) if window else []
        period = (window.get("period") or "") if window else ""
        player = await upsert_player(session, callback.from_user)
        if not period or not tied_players or player.id not in tied_players:
            await callback.answer(
                "Сейчас нет открытых заявок: приз этого периода уже распределён "
                "или ничьей среди лидеров нет.",
                show_alert=True,
            )
            return
        # Дедлайн, как его считает settle: после opened_at + claim_window_hours
        # заявки закрыты — поздняя претензия не должна перестоять молчаливого
        # соперника. Битый opened_at окно чинит по-своему (settle перезапускает
        # таймер), поэтому здесь просто не считаем дедлайн прошедшим.
        try:
            opened_at = datetime.fromisoformat((window or {}).get("opened_at", ""))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            opened_at = None
        now = datetime.now(UTC)
        if (
            opened_at is not None
            and settings.claim_window_hours > 0
            and (now - opened_at).total_seconds() >= settings.claim_window_hours * 3600
        ):
            await callback.answer(
                f"Заявки закрыты: дедлайн {settings.claim_window_hours}ч прошёл — "
                "приз решится по заявкам, поданным вовремя.",
                show_alert=True,
            )
            return
        if not player.wallet_address:
            await callback.answer(
                "Сначала привяжи кошелёк — без него приз не уйдёт.", show_alert=True
            )
            return
        human = human_claim_period(kind, period)
        confirm = (
            f"Заявка на копилку недели ({human}) принята: при равенстве верных "
            "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
            if kind == "week"
            else f"Заявка на копилку месяца ({human}) принята: при равенстве верных "
            "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
        )
        existing = await session.scalar(
            select(LeaderboardClaim.id).where(
                LeaderboardClaim.player_id == player.id,
                LeaderboardClaim.kind == kind,
                LeaderboardClaim.period == period,
            )
        )
        if existing is not None:
            await callback.answer(
                "Место уже заявлено: твоя претензия учтена (раньше — выше).",
                show_alert=True,
            )
            return
        session.add(
            LeaderboardClaim(
                player_id=player.id,
                kind=kind,
                period=period,
                claimed_at=datetime.now(UTC),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await callback.answer(
                "Место уже заявлено: твоя претензия учтена (раньше — выше).",
                show_alert=True,
            )
            return
    await callback.answer(confirm, show_alert=True)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    round_row = await _ensure_round()
    await message.answer(
        await status_text(round_row, show_title=True),
        parse_mode=ParseMode.HTML,
        reply_markup=cards_keyboard(round_row.id, cards=round_row.cards),
    )


async def _score_text(user) -> str:
    """Единый экран Стаи: выбор дня, титул, серия, голоса и личный блок.

    Одна карточка для /score (и /rank): выбор дня, прогресс и голоса за
    неделю и месяц, затем личные данные (кошелёк, ставка дня, приведённые в стаю).
    В группе сюда не показываем — только приватный поп-ап _score_short.
    """
    from app.handlers.wallet import _today_stake_line
    from app.referrals import invited_count
    from app.streaks import TITLES, calc_rank, streak_lines, title_for_streak

    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session) or await get_latest_round(session)
        vote = await get_vote(session, round_row.id, player.id) if round_row else None
        rank = await calc_rank(session, player.id)
        stake_line = await _today_stake_line(session, player.id)

    invited = await invited_count(user.id)

    def _scene_label() -> str:
        # Название сцены — не наш текст: приходит из кассеты. Карточка Стаи
        # уходит parse_mode=HTML, поэтому сырой `<`/`&` из правки в /panel
        # Telegram отвергнет целиком. Экранируем здесь, у самой HTML-витрины
        # (scene_label экранирует так же, но эта локальная подпись добавляет
        # римскую позицию и раньше шла в обход общего хелпера).
        if vote is None or round_row is None:
            return "—"
        name = next((c.title for c in round_row.cards if c.position == vote.card_position), None)
        if name is None:
            return f"{POSITIONS[vote.card_position]}."
        escaped = html.escape(name, quote=False)
        return f"{POSITIONS[vote.card_position]}. «{escaped}»"

    if vote is None:
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не сделал выбор дня."
    elif round_row is not None and round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"{path_mark('care', str(user.id))} Твой выбор: {_scene_label()}"
    else:
        choice = f"Вчера твой выбор: {_scene_label()}"

    personal: list[str] = []
    if settings.ton_enabled:
        if player.wallet_address:
            state = "подтверждён" if player.wallet_verified else "ждёт подтверждения"
            personal.append(f"💰 Кошелёк: привязан ({state})")
        else:
            personal.append("💰 Кошелёк: не привязан — /wallet в личке")
        if stake_line:
            personal.append(f"💸 {stake_line}")
    if invited:
        personal.append(f"🐾 Приведено в стаю: {invited}")

    title = title_for_streak(player.current_streak)
    lines = [
        f"{title.emoji} <b>{title.name}</b> — титул Стаи: растёт за серию верных сцен",
        choice,
        "",
        *streak_lines(player),
        "",
        f"📅 Голосов на неделе: {rank['week_votes']} · верных: {rank['week_correct']}",
        f"🗓 Голосов в месяце: {rank['month_votes']} · верных: {rank['month_correct']}",
    ]
    if settings.ton_enabled:
        w_pcts = "/".join(
            part.strip() for part in settings.weekly_prize_pcts.split(",") if part.strip()
        )
        m_pcts = "/".join(
            part.strip() for part in settings.monthly_prize_weights.split(",") if part.strip()
        )
        lines.append(
            f"🏆 Топ-3 верных сцен недели и месяца делят копилки Gram "
            f"(доли недели {w_pcts}, месяца {m_pcts})"
        )
    lines.append("")
    lines.append("🐾 Лестница титулов (верных сцен подряд):")
    ladder = [f"{t.correct_needed} {t.emoji} {t.name}" for t in TITLES[1:]]
    for i in range(0, len(ladder), 3):
        lines.append(" · ".join(ladder[i : i + 3]))
    if personal:
        lines.append("")
        lines.extend(personal)
    return "\n".join(lines)


async def _score_short(user) -> str:
    """Компактная карточка для окна колбэка: лимит Telegram — 200 символов."""
    from app.streaks import calc_rank, title_for_streak

    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        rank = await calc_rank(session, player.id)

    title = title_for_streak(player.current_streak)
    return (
        f"{title.emoji} {title.name}\n"
        f"🔥 Серия: {player.current_streak} · Лучшая: {player.best_streak}\n"
        f"📅 Голосов на неделе: {rank['week_votes']} · верных: {rank['week_correct']}\n"
        f"🗓 Голосов в месяце: {rank['month_votes']} · верных: {rank['month_correct']}"
    )


def _score_keyboard() -> InlineKeyboardMarkup:
    """Быстрые действия с карточки Стаи — те же марки, что на пульте."""
    return InlineKeyboardMarkup(inline_keyboard=_quick_rows())


@router.message(Command("score", "rank"))
async def cmd_score(message: Message) -> None:
    """Карточка Стаи: титул, серия и голоса.

    Команда /rank — тот же хендлер: обе вели на одну карточку, объединены.
    """
    if message.chat.type != ChatType.PRIVATE:
        # В группе личные цифры не показываем: только кнопка с приватным окном.
        await message.answer(
            "Твой счёт увидишь только ты — нажми кнопку.",
            reply_markup=_personal_keyboard("score:view", "🏅 Мой счёт"),
        )
        return
    await message.answer(
        await _score_text(message.from_user),
        parse_mode=ParseMode.HTML,
        reply_markup=_score_keyboard(),
    )


@router.callback_query(F.data == "score:view")
async def on_score_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(
            await _score_text(callback.from_user),
            parse_mode=ParseMode.HTML,
            reply_markup=_score_keyboard(),
        )
        await callback.answer()
        return
    # Окно колбэка не рендерит HTML и держит лимит 200 символов — отдаём
    # заранее собранную компактную карточку без разметки.
    await callback.answer(await _score_short(callback.from_user), show_alert=True)


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery) -> None:
    """Пульт LOST HOWL: сценарии кнопок, не покрытые готовыми колбэками."""
    data = callback.data or ""
    action = data.split(":", 1)[1] if ":" in data else ""
    handler = {
        "today": _menu_today,
        "wallet": _menu_wallet,
        "top": _menu_top,
        "fund": _menu_fund,
        "invite": _menu_invite,
    }.get(action)
    if handler is None:
        await callback.answer()
        return
    try:
        await handler(callback)
    except Exception:
        logger.exception("Кнопка меню %s упала", action)
        await callback.answer("Что-то щёлкнуло — попробуй ещё раз.", show_alert=True)


async def _menu_today(callback: CallbackQuery) -> None:
    """▶️ Сегодня — повтор дневного поста с кнопками голосования (где угодно)."""
    if callback.message is None:
        await callback.answer()
        return
    round_row = await _ensure_round()
    await callback.message.answer(
        await status_text(round_row, show_title=True),
        parse_mode=ParseMode.HTML,
        reply_markup=cards_keyboard(round_row.id, cards=round_row.cards),
    )
    await callback.answer()


async def _menu_wallet(callback: CallbackQuery) -> None:
    """💰 Кошелёк: в личке открывает диалог привязки, в группе — направляет."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Кошелёк — личное: открой профиль бота, нажми Start — там кнопка в пульте.",
            show_alert=True,
        )
        return
    from app.handlers.wallet import _wallet_bind_prompt, _wallet_view_safe

    from .common import _dialog_start

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        if not player.wallet_address:
            await _dialog_start(callback.from_user.id)
            await callback.message.answer(
                _wallet_bind_prompt(), parse_mode=ParseMode.HTML
            )
            await callback.answer()
            return
    await callback.message.answer(
        await _wallet_view_safe(callback.from_user), parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def _menu_top(callback: CallbackQuery) -> None:
    """🏆 Копилки недели и месяца — публичный пост."""
    if callback.message is None:
        await callback.answer()
        return
    from app.handlers.wallet import _top_text

    await callback.message.answer(await _top_text())
    await callback.answer()


async def _menu_fund(callback: CallbackQuery) -> None:
    """🐾 Фонд Стаи — публичный пост с журналом."""
    if callback.message is None:
        await callback.answer()
        return
    from app.handlers.wallet import _fund_text

    await callback.message.answer(await _fund_text(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def _menu_invite(callback: CallbackQuery) -> None:
    """🐾 Позвать в стаю — карточка приглашения (та же, что /invite)."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Приглашения — личное: открой профиль бота и нажми Start — там кнопка в пульте.",
            show_alert=True,
        )
        return
    text = await _invite_text(callback.from_user, getattr(callback, "bot", None))
    if text is None:
        await callback.answer("🧭 Приглашения в стаю пока не открыты.", show_alert=True)
        return
    await callback.message.answer(text, parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    _, raw_round_id, raw_position = parts
    try:
        round_id = int(raw_round_id)
        position = int(raw_position)
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    if position not in CARD_POSITIONS:
        # POSITIONS[position] ниже бросил бы IndexError (а отрицательный индекс
        # молча взял бы чужую сцену) и оставил кнопку со спиннером. cast_vote и
        # change_vote такую позицию всё равно отвергают — отвечаем тем же.
        await callback.answer(
            f"{warn_mark('invalid')} Такого варианта нет в кадре дня.",
            show_alert=True,
        )
        return
    label = POSITIONS[position]
    async with SessionLocal() as session:
        # Кулдаун на нажатие. Голосование — основное действие игры, поэтому окно
        # короткое (3 с): передумать и вернуться игрок должен успеть, а поток
        # нажатий от скрипт-клиента (у него нет лимита на тапы, в отличие от
        # обычного клиента) — нет. Повтор по тому же пути и так безопасен:
        # cast_vote возвращает already, и второй голос не создаётся.
        if callback.from_user.id not in settings.admin_id_set:
            from app.handlers.wallet import _wallet_throttled

            if await _wallet_throttled(session, callback.from_user.id, "vote_cd"):
                player = await upsert_player(session, callback.from_user)
                round_row = await get_active_round(session)
                if round_row is None or round_row.id != round_id:
                    await callback.answer("Этот день уже закрыт.", show_alert=True)
                    return
                current = await get_vote(session, round_row.id, player.id)
                if current is None:
                    await callback.answer(
                        f"{hint_mark('wallet-throttle')} Голос ещё не принят — подожди немного.",
                        show_alert=True,
                    )
                    return
                # Отвечаем тем, что игрок уже выбрал: троттлинг не должен
                # выглядеть как «выбор потерялся».
                held = scene_label(
                    {card.position: card.title for card in round_row.cards},
                    current.card_position,
                )
                await callback.answer(
                    f"{hint_mark('wallet-throttle')} Твой выбор: {held}. "
                    "Подожди немного, чтобы сменить.",
                    show_alert=True,
                )
                return
        player = await upsert_player(session, callback.from_user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.id != round_id:
            await callback.answer("Этот день уже закрыт.", show_alert=True)
            return
        # Имя сцены выбора — чтобы игрок ВИДЕЛ, за что именно голосует
        # («Выбор I. «Вскрыть крышу»»), а не абстрактную римскую цифру.
        label = scene_label(
            {card.position: card.title for card in round_row.cards}, position
        )
        result = await cast_vote(session, round_row, player.id, position)
        outcome = ""
        if result == "already":
            vote = await get_vote(session, round_row.id, player.id)
            current_position: int | None = vote.card_position if vote else None
            if (
                settings.revote_enabled
                and round_row.status == RoundStatus.OPEN
                and current_position is not None
                and current_position != position
            ):
                # Есть оплаченный грант — списываем и меняем путь прямо здесь.
                outcome = await change_vote(session, round_row, player.id, position)
        elif result == "ok":
            # Подтверждение выбора в личку отдельным сообщением: по кнопке
            # (в группе или личке) алерт исчезает, а «мой выбор дня» должно
            # оставаться видимым до конца дня. Молчим, если игрок ещё не
            # открывал диалог с ботом — алерт был и так показан.
            if settings.player_dm:
                try:
                    from app.handlers.wallet import _post_vote_stake_markup

                    text = f"Твой выбор этого дня: {label}. Итоги — после закрытия сцены."
                    markup = await _post_vote_stake_markup(session, player, round_row)
                    if markup is not None:
                        text += (
                            "\n\n💳 Ставку на выбор вноси прямо здесь: кнопка кошелька "
                            "откроет перевод с подставленным адресом, а «Скопировать "
                            "адрес» пригодится для любого другого кошелька."
                        )
                    bot = callback.bot
                    if bot is None:
                        return
                    await bot.send_message(
                        callback.from_user.id,
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                except Exception as exc:
                    logger.debug("Личное подтверждение выбора игроку %s не доставлено: %s", callback.from_user.id, exc)
    if result == "already":
        if outcome == "ok":
            await callback.answer(
                f"Грант списан. Выбор изменён на {label}.",
                show_alert=True,
            )
            return
        if outcome == "no_grant":
            # Обещаем /change только когда он реально продаётся: в бесплатной
            # версии и в день без ставок команда безусловно отказывает.
            hint = "Твой выбор уже записан."
            if getattr(round_row, "money_mode", True) is False:
                hint += " Сегодня версия без ставок — кадр не перемотать."
            elif settings.ton_enabled:
                if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
                    hint = (
                        "Твой выбор уже записан. Перемотка кадра платная — "
                        "через личку бота: /change."
                    )
                else:
                    hint = (
                        f"Твой выбор уже записан. Перемотать кадр — "
                        f"⭐{settings.revote_stars}, команда /change."
                    )
            await callback.answer(hint[:200], show_alert=True)
            return
    texts = {
        "ok": f"{ok_mark(str(round_id))} Выбор {label} принят. Итоги скрыты до конца дня.",
        "already": f"{hint_mark('already')} Ты уже сделал выбор сегодня.",
        "closed": f"{warn_mark('closed')} День закрыт — кадр фиксируется, итоги скоро.",
        "invalid": f"{warn_mark('invalid')} Такого варианта нет в кадре дня.",
    }
    await callback.answer(texts.get(result, "Неизвестный ответ."), show_alert=True)