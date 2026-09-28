# Онбординг и ежедневная игра: старт, карты дня, счёт и голосование.
# Сюжетные команды (/lore, /calling, /best, нюх, квиз памяти) сняты вместе
# со слоем сюжета — ядро: голосование, ставки и выплаты.
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.broadcast import POSITIONS, cards_keyboard, scene_label, status_text
from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardClaim, Player, RoundStatus
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

from .common import _DYOR_TEXT, _ensure_round, _personal_keyboard, router

logger = logging.getLogger(__name__)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start и /help."""
    lines = [
        "<b>Команды Стаи</b>",
        "/start — вставить кассету: как играть и памятка",
        "/menu — пульт LOST HOWL: всё по кнопкам",
        "/today — кадр дня: варианты и выбор",
        "/score — карточка Стаи: титул, серия и голоса за неделю и месяц",
    ]
    if settings.revote_enabled:
        lines.append(
            "/change — перемотать кадр (⭐ или Gram)"
            if settings.ton_enabled
            else f"/change — перемотать кадр (⭐ {settings.revote_stars})"
        )
    if settings.ton_enabled:
        from app.handlers.wallet import _pct_text

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
            f"\n💰 Фонд дня: {_pct_text(pool_pct)}% — поставившим на верную сцену; остальное — "
            "Фонд Стаи, копилки недели и месяца (/top), хранителю и пригласившим "
            f"({_pct_text(settings.referral_pct)}%, см. /referral). Подробности: /stake."
        )
    lines += [
        "/invite — позвать в стаю по личной ссылке",
        "/referral — твоя награда за приведённых",
        "/help — эта памятка",
    ]
    return lines


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Памятка команд с пультом вместо слепого меню."""
    lines = [f"{day_mark(str(message.from_user.id))} <b>{settings.world_name}</b>", ""]
    lines.extend(_commands_help())
    label = (
        await _dm_toggle_label(message.from_user.id)
        if message.from_user is not None
        else "🔔 Итоги в личку: ВКЛ"
    )
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Пульт LOST HOWL: все действия дня по кнопкам."""
    uid = str(message.from_user.id) if message.from_user else "0"
    label = (
        await _dm_toggle_label(message.from_user.id)
        if message.from_user is not None
        else "🔔 Итоги в личку: ВКЛ"
    )
    await message.answer(
        f"{day_mark(uid)} <b>Пульт {settings.world_name}</b>\n\n"
        "Всё по кнопкам: кадр дня, твой счёт, кошелёк — одним нажатием. "
        "Вариант дня выбираешь кнопкой под кадром.",
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    """Личная ссылка приглашения ?start=ref_<id>_<токен> и счётчик приведённых."""
    if message.chat.type != ChatType.PRIVATE:
        return
    caller = message.from_user
    if caller is None:
        return
    from app.referrals import invited_count, referral_link, resolve_bot_username

    username = await resolve_bot_username(getattr(message, "bot", None))
    link = referral_link(caller.id, username)
    if not link:
        await message.answer("🧭 Приглашения в стаю пока не открыты — приходи чуть позже.")
        return
    count = await invited_count(caller.id)
    await message.answer(
        f"🐾 Твоя личная ссылка в стаю:\n{link}\n\n"
        "Кто придёт по ней — тот войдёт следом за тобой. "
        f"Приведено всего: {count}.\n"
        "📼 За каждую подтверждённую ставку приведённого награда пишется "
        f"на твою плёнку — баланс: /referral."
    )


@router.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    """Реферальная награда игрока: ссылка, приведённые и накопленный баланс."""
    if message.chat.type != ChatType.PRIVATE:
        return
    caller = message.from_user
    if caller is None:
        return
    from app.referrals import (
        invited_count,
        referral_link,
        referral_pot_balance,
        resolve_bot_username,
    )

    username = await resolve_bot_username(getattr(message, "bot", None))
    link = referral_link(caller.id, username)
    if not link:
        await message.answer("🧭 Приглашения в стаю пока не открыты — приходи чуть позже.")
        return
    count = await invited_count(caller.id)
    balance = await referral_pot_balance(caller.id)
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
    await message.answer(
        f"🎞 <b>Твоя плёнка наград</b>\n\n"
        f"Ссылка в стаю:\n{link}\n\n"
        f"Приведено: {count}\n"
        f"В копилке: {earned:g} Gram "
        f"(автовыплата от {settings.referral_min_payout_gram:g} Gram)\n\n"
        f"{status}",
        parse_mode=ParseMode.HTML,
    )


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
        "Перед тобой не мир, а видеомагнитофон LOST HOWL, собранный",
        "из хлама. Раз в месяц хранитель вставляет в лоток кассету —",
        "фанфик по «Lost Dogs: The Way»: стая псов ищет дом в разбитом городе.",
        "",
        "Каждый день — один кадр в трёх вариантах. Ты выбираешь его",
        "голосом или ставкой Gram. Жребий дня решает, какой кадр уцелеет:",
        "день Большинства — с самой большой суммой Gram, день Меньшинства — с самой малой,",
        "день Середины — со средней суммой Gram. Уцелевший кадр едет в сценарий дальше.",
    ]
    if settings.ton_enabled:
        lines.append(
            "🐾 Как поставить Gram на кадр — /stake. Голос без ставки тоже ведёт "
            "тебя: он строит лидерборд недели и месяца."
        )
    else:
        lines.append("🐾 Кадр выбирают кнопкой под картой дня — до конца дня.")
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


def _menu_keyboard(toggle_label: str) -> InlineKeyboardMarkup:
    """Пульт LOST HOWL: кнопки-действия вместо вызова команд слепым меню.

    Сами действия — уже существующие колбэки, где их хватает (карточка
    Стаи, ставка — с приватным окном в группе), или короткие menu:* сценарии.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="▶️ Сегодня", callback_data="menu:today"),
            InlineKeyboardButton(text="⭐ Карточка Стаи", callback_data="score:view"),
        ],
        [
            InlineKeyboardButton(text="💰 Кошелёк", callback_data="menu:wallet"),
            InlineKeyboardButton(text="💸 Ставка", callback_data="stake:view"),
        ],
        [
            InlineKeyboardButton(text="🏆 Копилки", callback_data="menu:top"),
            InlineKeyboardButton(text="🐾 Фонд", callback_data="menu:fund"),
        ],
        [
            InlineKeyboardButton(text=toggle_label, callback_data="dm:toggle"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _dm_toggle_label(uid: int) -> str:
    """Подпись кнопки личных рассылок по состоянию игрока."""
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
    subscribed = bool(getattr(player, "dm_subscribed", True))
    return (
        "🔔 Итоги в личку: ВКЛ"
        if subscribed
        else "🔕 Итоги в личку: ВЫКЛ"
    )


async def _start_keyboard(session, player) -> InlineKeyboardMarkup:
    """Личное меню /start: пульт + претензии на места лидерборда.

    Кнопки Claim появляются только у игроков, попавших в ничью за призовые
    места закрытого периода, пока окно заявок открыто (приз ещё не роздан).
    Остальным кнопка не видна: заявлять нечего.
    """
    subscribed = bool(getattr(player, "dm_subscribed", True))
    label = (
        "🔔 Итоги в личку: ВКЛ"
        if subscribed
        else "🔕 Итоги в личку: ВЫКЛ"
    )
    markup = _menu_keyboard(label)
    buttons: list[InlineKeyboardButton] = []
    if settings.leaderboard_claim_enabled:
        from app.leaderboard import _claim_window_players

        for kind, text, data in (
            ("week", "🗓 Заявить приз недели", "claim:week"),
            ("month", "🗓 Заявить приз месяца", "claim:month"),
        ):
            tied_players, _period = await _claim_window_players(session, kind)
            if player.id in tied_players:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        if buttons:
            markup.inline_keyboard.append(buttons)
    return markup


@router.callback_query(F.data == "dm:toggle")
async def on_dm_toggle(callback: CallbackQuery) -> None:
    """Тумблер личной рассылки: единственный параметр — флаг dm_subscribed."""
    if callback.from_user is None:
        await callback.answer()
        return
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        subscribed = not bool(getattr(player, "dm_subscribed", True))
        player.dm_subscribed = subscribed
        await session.commit()
        keyboard = await _start_keyboard(session, player)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    await callback.answer(
        "Итоги и анонсы снова приходят в личку." if subscribed
        else "Личные рассылки отключены — играем только в группе.",
        show_alert=True,
    )


@router.callback_query(F.data == "claim:week")
async def on_claim_week(callback: CallbackQuery) -> None:
    await _on_claim(callback, "week")


@router.callback_query(F.data == "claim:month")
async def on_claim_month(callback: CallbackQuery) -> None:
    await _on_claim(callback, "month")


_MONTH_NAMES_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _human_claim_period(kind: str, period: str) -> str:
    """Человекочитаемый период заявки вместо служебного «2026-W35» / «2026-08»."""
    if kind == "week":
        from app.weeks import week_bounds

        start, end = week_bounds(period)
        return f"с {start:%d.%m} по {(end - timedelta(days=1)):%d.%m}"
    year, month = map(int, period.split("-"))
    return f"{_MONTH_NAMES_RU[month - 1]} {year}"


async def _on_claim(callback: CallbackQuery, kind: str) -> None:
    """Претензия на место лидерборда: решает ничьи по времени Claim.

    Кнопка видна только tied-игрокам, пока окно Claim открыто (приз не
    распределён из-за ничьей). Дублирующий check здесь — защита от hand-craft
    callback: период берётся из окна, а не из текущей даты. Кошелёк обязателен,
    иначе приз физически некуда отправить. Идемпотентно:
    unique(player_id, kind, period) — повторный тап не заводит вторую запись.
    """
    if callback.from_user is None:
        await callback.answer()
        return
    from app.leaderboard import _claim_window_players

    async with SessionLocal() as session:
        tied_players, period = await _claim_window_players(session, kind)
        player = await upsert_player(session, callback.from_user)
        if not period or not tied_players or player.id not in tied_players:
            await callback.answer(
                "Сейчас нет открытых заявок: приз этого периода уже распределён "
                "или ничьей среди лидеров нет.",
                show_alert=True,
            )
            return
        if not player.wallet_address:
            await callback.answer(
                "Сначала привяжи кошелёк — без него приз не уйдёт.", show_alert=True
            )
            return
        human = _human_claim_period(kind, period)
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
        reply_markup=cards_keyboard(round_row.id, remember=False, day_index=round_row.day_index),
    )


async def _score_text(user) -> str:
    """Единый экран Стаи: выбор дня, титул, серия, голоса и личный блок.

    Одна карточка для /score и /rank: выбор дня, прогресс и голоса за
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
        name = next((c.title for c in round_row.cards if c.position == vote.card_position), None)
        if name is None:
            return f"{POSITIONS[vote.card_position]}."
        return f"{POSITIONS[vote.card_position]}. «{name}»"

    if vote is None:
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не сделал выбор дня."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
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
        lines.append("🏆 Топ-3 верных сцен недели и месяца делят копилки Gram (доли 50/30/20)")
    lines.append("")
    lines.append("🐾 Лестница титулов (верных сцен подряд):")
    ladder = [f"{t.correct_needed} {t.name}" for t in TITLES[1:]]
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
    """Быстрые действия с карточки Стаи: кошелёк, ставка, копилки и фонд."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Кошелёк", callback_data="menu:wallet"),
                InlineKeyboardButton(text="💸 Ставка", callback_data="stake:view"),
            ],
            [
                InlineKeyboardButton(text="🏆 Копилки", callback_data="menu:top"),
                InlineKeyboardButton(text="🐾 Фонд", callback_data="menu:fund"),
            ],
        ]
    )


@router.message(Command("score"))
async def cmd_score(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        # В группе личные цифры не показываем: только кнопка с приватным окном.
        await message.answer(
            "Твой счёт увидишь только ты — нажми кнопку.",
            reply_markup=_personal_keyboard("score:view", "Мой счёт"),
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


@router.message(Command("rank"))
async def cmd_rank(message: Message) -> None:
    """Показывает тот же единый экран, что и /score: титул, серия и голоса."""
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            await _score_text(message.from_user),
            parse_mode=ParseMode.HTML,
            reply_markup=_score_keyboard(),
        )
    else:
        await message.answer(
            "Рейтинг — только в личке.",
            reply_markup=_personal_keyboard("rank:view", "Мой рейтинг"),
        )


@router.callback_query(F.data == "rank:view")
async def on_rank_view(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await callback.answer(await _score_short(callback.from_user), show_alert=True)


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery) -> None:
    """Пульт LOST HOWL: сценарии кнопок, не покрытые готовыми колбэками."""
    action = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    handler = {
        "today": _menu_today,
        "wallet": _menu_wallet,
        "top": _menu_top,
        "fund": _menu_fund,
        "help": _menu_help,
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
        reply_markup=cards_keyboard(
            round_row.id, remember=False, day_index=round_row.day_index
        ),
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


async def _menu_help(callback: CallbackQuery) -> None:
    """❓ Помощь — памятка с пультом."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    lines = [f"{day_mark(str(callback.from_user.id))} <b>{settings.world_name}</b>", ""]
    lines.extend(_commands_help())
    label = await _dm_toggle_label(callback.from_user.id)
    await callback.message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
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
    label = POSITIONS[position]
    async with SessionLocal() as session:
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
                    await callback.bot.send_message(
                        callback.from_user.id,
                        f"Твой выбор этого дня: {label}. Итоги — после закрытия сцены.",
                        parse_mode=ParseMode.HTML,
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
            hint = (
                f"Твой выбор уже записан. Перемотать кадр — ⭐{settings.revote_stars}, команда /change."
            )
            if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
                hint = "Твой выбор уже записан. Перемотка кадра платная — через личку бота: /change."
            await callback.answer(hint[:200], show_alert=True)
            return
    texts = {
        "ok": f"{ok_mark(str(round_id))} Выбор {label} принят. Итоги скрыты до конца дня.",
        "already": f"{hint_mark('already')} Ты уже сделал выбор сегодня.",
        "closed": f"{warn_mark('closed')} День закрыт — кадр фиксируется, итоги скоро.",
        "invalid": f"{warn_mark('invalid')} Такого варианта нет в кадре дня.",
    }
    await callback.answer(texts.get(result, "Неизвестный ответ."), show_alert=True)