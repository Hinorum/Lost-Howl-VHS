# Пульт хранителя /panel: сводка дня, очереди выплат и кнопки действий.
from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus, WatcherState
from app.story import store as story_store
from app.story.bay import (
    LibraryEntry,
    clear_edit_intent,
    default_cassettes_dir,
    get_edit_intent,
    get_next_cassette,
    list_cassettes,
    set_edit_intent,
    set_next_cassette,
    today_road,
)
from app.story.editor import (
    apply_cassette_file,
    day_view_text,
    day_yaml,
    restore_backup,
    scenario_yaml,
)
from app.story.narrative_lint import lint_groups
from app.story.schema import Cassette, validate_file
from app.ton_utils import from_nano

from .admin import (
    _ADJ_CONFIRM_WINDOW,
    _adjust_keyboard,
    _adjust_menu_text,
    cmd_advance,
)
from .common import _set_paused_and_broadcast, router
from .payout import (
    _payouts_text,
    _refunds_panel_text,
    _revenue_text,
    _stakes_panel_text,
)

logger = logging.getLogger(__name__)


async def _panel_confirm(key: str) -> bool:
    """Промежуточное подтверждение кнопок пульта (пауза/режим/завершение дня).

    Первый тап сохраняет отметку и возвращает False (хранитель увидит
    предупреждение), повторный в течение окна — True. Отметка живёт в
    watcher_state, как у корректировок казны: общая для процессов и переживает
    рестарт — на полпути между «первым и вторым тапом» день не потеряется.
    """
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        row = await session.get(WatcherState, key)
        pending = json.loads(row.value) if row is not None else None
        if pending is not None:
            try:
                created = datetime.fromisoformat(pending["created_at"])
            except (KeyError, TypeError, ValueError):
                created = None
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if (now - created).total_seconds() <= _ADJ_CONFIRM_WINDOW:
                    await session.delete(row)
                    await session.commit()
                    return True
        payload = json.dumps({"created_at": now.isoformat()}, ensure_ascii=False)
        if row is None:
            session.add(WatcherState(key=key, value=payload))
        else:
            row.value = payload
        await session.commit()
        return False


_PANEL_FOOTER = (
    "\n\n🕹 <b>Управление</b> (в личке):\n"
    "/advance — закрыть день досрочно и открыть следующий\n"
    "/today — превью поста игрока\n"
    "/incoming — журнал входящих переводов казначея\n"
    "/stakes — ставки дня · /payouts — очередь выплат (причина у каждой строки)\n"
    "/payout &lt;id&gt; retry|spam — ручной разбор долга\n"
    "/return &lt;id&gt; — ручной возврат ставки\n"
    "/treasury — казначей: баланс и пара ключей\n"
    "/blockchain — аудит блокчейн-контура (watcher, очередь, stuck, сверка)\n"
    "/adjust — сверка казны: ручной вывод или пропажа средств ⚖️\n"
    "/fundout &lt;Gram&gt; &lt;причина&gt; — раздача Фонда Стаи\n"
    "/disputes — список открытых споров\n"
    "/dispute — жалоба на итог / разбор спора\n"
    "/finalize — ручная финализация застрявших дней\n"
    "/refinalize — принудительная перефинализация\n"
    "/pause … /resume — стоп-кран игры (техработы) ⏸\n"
    "/revenue — касса (Stars/Gram)\n"
    "/resetgame confirm [keepstory] — полный сброс ⚠️\n"
    "/cassette — кассеты: библиотека и назначение «следующей» 📼\n"
)


async def _admin_panel_text(session=None) -> str:
    """Сводка состояния игры + подсказки по командам, одним сообщением."""
    _own_session = session is None
    if _own_session:
        session = SessionLocal()
        await session.__aenter__()
    try:
        return await _build_panel_text(session)
    finally:
        if _own_session:
            await session.__aexit__(None, None, None)


async def _build_panel_text(session) -> str:
    """Внутренняя логика сборки текста пульта."""
    from app.ops import is_game_paused as _paused_flag
    from app.ops import paused_reason as _pause_reason
    from app.ops import snapshot

    snap = await snapshot()
    lines = ["🎛 <b>ПУЛЬТ ХРАНИТЕЛЯ</b>"]
    try:
        if await _paused_flag(session):
            lines.append(
                f"⏸ ИГРА НА ПАУЗЕ ({await _pause_reason(session) or 'техработы'}) "
                "— снять: /resume. Входящие переводы возвращаются автоматически."
            )
    except Exception:
        # Молчание здесь врало бы: пульт без строки «ИГРА НА ПАУЗЕ» выглядит
        # как работающая игра. Строка не показана — говорим в лог.
        logger.warning("Пульт: не удалось прочитать паузу — строка не показана", exc_info=True)
    try:
        from app.ops import money_mode_enabled

        money_on = await money_mode_enabled(session)
        lines.append(
            "💰 Версия: <b>со ставками</b> и платной сменой выбора."
            if money_on
            else "🔰 Версия: <b>без ставок</b> (игра бесплатна, смена выбора закрыта)."
        )
    except Exception:
        logger.warning("Пульт: не удалось прочитать режим ставок", exc_info=True)
    rnd = snap.get("round") or {}
    closing = str(rnd.get("voting_ends_at", ""))[11:16]
    lines.append(
        f"День {rnd.get('day_index')} · {rnd.get('status')} · закрытие {closing} UTC"
    )
    if settings.ton_enabled:
        from app.rounds import round_pot

        nano, bets = await round_pot(session, int(rnd.get("id") or 0))
        lines.append(f"💰 Банк дня: {nano / 1e9:.2f} Gram · ставок {bets}")
        # Фонд Стаи: накопление хранителя, раздача вручную.
        try:
            from app.models import PackFund as _Fund

            fund_nano = (
                await session.execute(
                    select(func.coalesce(func.sum(_Fund.nanotons), 0))
                )
            ).scalar_one()
            lines.append(f"🐾 Фонд Стаи: {fund_nano / 1e9:.2f} Gram")
        except Exception:
            logger.warning("Строка Фонда Стаи в панели не собралась", exc_info=True)
        # Метрики суток: явка вчера, всплывшие эха, оставшиеся заглушки.
        try:
            from app.models import Vote as _Vote

            last_closed = (
                await session.execute(
                    select(Round.id)
                    .where(Round.status == RoundStatus.CLOSED)
                    .order_by(Round.day_index.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            votes_yesterday = 0
            if last_closed is not None:
                votes_yesterday = (
                    await session.execute(
                        select(func.count())
                        .select_from(_Vote)
                        .where(_Vote.round_id == last_closed)
                    )
                ).scalar_one()
            lines.append(f"📈 Вчера голосов: {votes_yesterday}")
        except Exception:
            logger.warning("Метрики суток для пульта не собраны", exc_info=True)
    queue = snap.get("payout_queue")
    oldest = snap.get("oldest_payout_age")
    dead = snap.get("dead_letter_payouts")
    oldest_note = f", старейшая {int(oldest // 60)} мин" if oldest else ""
    lines.append(f"💸 Выплаты: в очереди {queue}{oldest_note} · failed {dead}")
    # Разбивка по типу: сколько игроков ждут приз, сколько — возврат ставки.
    pending_by_kind = snap.get("payout_pending_by_kind") or {}
    dead_by_kind = snap.get("payout_dead_by_kind") or {}
    p_refund = int(pending_by_kind.get("refund", 0))
    p_prize = int(pending_by_kind.get("prize", 0))
    p_other = max(0, int(queue) - p_refund - p_prize)
    d_refund = int(dead_by_kind.get("refund", 0))
    parts = []
    if p_refund:
        parts.append(f"ждёт возвратов {p_refund}")
    if p_prize:
        parts.append(f"ждёт призов {p_prize}")
    if p_other:
        parts.append(f"долей {p_other}")
    if dead and d_refund:
        parts.append(f"failed-возвратов {d_refund}")
    if parts:
        lines.append(f"  · {', '.join(parts)}")
    # Призы, ждущие кошелька игрока: финализация не знала адреса, диспетчер
    # оживит строку сам, как только игрок привяжет /wallet (retry не нужен).
    try:
        no_wallet_count = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.dest_address == "",
                    Payout.kind.in_(["prize", "refund"]),
                    Payout.player_id.isnot(None),
                    Payout.status.notin_(["sent", "dismissed"]),
                )
            )
        ).scalar_one()
        if no_wallet_count:
            lines.append(
                f"🪙 Призов без кошелька: {no_wallet_count} — уйдут сами, "
                "когда игрок привяжет адрес. Разбор: /payouts."
            )
    except Exception:
        # Счётчик исчез бы из пульта вместе с предупреждением о призах, которые
        # не уйдут: молчащий пульт в этом месте опаснее шумного.
        logger.warning("Пульт: не удалось посчитать призы без кошелька", exc_info=True)
    pending_stakes = snap.get("pending_stakes") or 0
    if settings.ton_enabled:
        stakes_note = f"⏳ Переводов не обработано: {pending_stakes}"
        if not pending_stakes:
            stakes_note += " · всё обработано"
        lines.append(stakes_note)
    if settings.ton_enabled:
        lines.append(
            f"👀 Watcher: {snap.get('watcher_source') or '—'}, "
            f"пульс {int(snap.get('watcher_beat_age') or 0)} с"
        )
    # Топ неотправленного — прямо сюда, чтобы не ходить в /payouts за мелочами.
    try:
        rows = (
            await session.execute(
                select(Payout)
                .where(Payout.status.notin_(["sent", "dismissed"]))
                .order_by(Payout.id.asc())
                .limit(3)
            )
        ).scalars().all() if session is not None else []
        for row in rows:
            reason = f" — {row.last_error[:60]}" if getattr(row, "last_error", None) else ""
            lines.append(
                f"  #{row.id} {row.kind} {from_nano(row.amount_nanotons):.2f} G "
                f"{row.status}{reason}"
            )
    except Exception:
        # Застрявшие выплаты должны быть видны и когда запрос к ним сорвался:
        # «пульт ничего не показал» здесь читается как «всё разослано».
        logger.warning("Пульт: не удалось показать зависшие выплаты", exc_info=True)
    tick_age = snap.get("last_tick_age")
    if tick_age is not None and tick_age > 120:
        lines.append(f"⚠️ Тик отстаёт: {int(tick_age)} с — проверь логи.")
    # Падения тика и живые тревоги берутся из того же снимка, что и /health,
    # но владелец смотрит на пульт, а не на /health: без этих строк он видел
    # «пульт в порядке» при падающем тике и не знал, что именно тревожит.
    tick_fails = snap.get("tick_failures") or 0
    if tick_fails:
        lines.append(f"⚠️ Тик падал {tick_fails} раз подряд — последний цикл упал.")
    problems = snap.get("problems") or []
    if problems:
        # Список бывает длинным, а сообщение Telegram ограничено: берём голову
        # и честно говорим, сколько не показано, иначе выглядит так, будто
        # проблем ровно столько.
        shown = problems[:3]
        lines.append("🚨 Тревоги: " + " · ".join(shown))
        if len(problems) > len(shown):
            lines.append(f"…и ещё {len(problems) - len(shown)} — полный список в /ops.")
    return "\n".join(lines) + _PANEL_FOOTER


async def _panel_keyboard() -> InlineKeyboardMarkup:
    """Кнопочный пульт хранителя: обновление и безопасные действия.

    Кнопка стоп-крана живёт здесь же: подпись зависит от текущего
    состояния (пауза/работа), поэтому клавиатура пересобирается на каждый показ.
    """
    from app.ops import is_game_paused, money_mode_enabled

    async with SessionLocal() as session:
        paused = await is_game_paused(session)
        money_on = await money_mode_enabled(session)
    pause_button = (
        InlineKeyboardButton(text="▶️ Возобновить игру", callback_data="panel:resume")
        if paused
        else InlineKeyboardButton(text="⏸ Пауза игры", callback_data="panel:pause")
    )
    version_button = InlineKeyboardButton(
        text="🔰 Версия без ставок" if money_on else "💰 Версия со ставками",
        callback_data="panel:now" if money_on else "panel:money",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="panel:view"),
                InlineKeyboardButton(text="💸 Выплаты", callback_data="panel:payouts"),
                InlineKeyboardButton(text="🎲 Ставки", callback_data="panel:stakes"),
            ],
            [
                InlineKeyboardButton(text="↩️ Вернуть ставку", callback_data="panel:refunds"),
                InlineKeyboardButton(text="🏛 Казначей", callback_data="panel:treasury"),
                InlineKeyboardButton(text="💰 Касса", callback_data="panel:revenue"),
            ],
            [
                InlineKeyboardButton(text="⚖️ Сверка казны", callback_data="panel:adjust"),
                pause_button,
                InlineKeyboardButton(text="📼 Кассеты", callback_data="panel:cassettes"),
            ],
            [
                version_button,
                InlineKeyboardButton(text="⏩ Завершить день", callback_data="panel:advance"),
            ],
        ]
    )


@router.message(Command("panel"), F.chat.type == ChatType.PRIVATE)
async def cmd_panel(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Пульт только для хранителя игры.")
        return
    async with SessionLocal() as session:
        text = await _admin_panel_text(session)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=await _panel_keyboard())


@router.callback_query(F.data.startswith("panel:"))
async def on_panel_action(callback: CallbackQuery) -> None:
    """Единая точка кнопок пульта: гейт хранителя + маршрутизация действий."""
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Пульт только для хранителя.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    try:
        if action in {"view", "refresh"}:
            async with SessionLocal() as session:
                text = await _admin_panel_text(session)
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=await _panel_keyboard()
                    )
                except TelegramBadRequest as exc:
                    # Двойной тап «Обновить» — нормальный жест, а не ошибка:
                    # Telegram отклоняет правку без изменений содержимого.
                    if "message is not modified" in str(exc).lower():
                        await callback.answer("Без изменений.")
                        return
                    raise
            await callback.answer("Обновлено.")
            return
        if action == "payouts":
            await callback.message.answer(await _payouts_text())
            await callback.answer("Список ниже.")
            return
        if action == "stakes":
            await callback.message.answer(await _stakes_panel_text())
            await callback.answer("Ставки ниже.")
            return
        if action == "refunds":
            await callback.message.answer(await _refunds_panel_text())
            await callback.answer("Возвраты ниже.")
            return
        if action == "adjust":
            await callback.message.answer(
                await _adjust_menu_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=_adjust_keyboard(),
            )
            await callback.answer()
            return
        if action in {"money", "now"}:
            # Версия игры: со ставками / без. Вступает со СЛЕДУЮЩЕГО дня —
            # текущий день живёт по своему снимку Round.money_mode.
            from app.ops import set_money_mode

            want_money = action == "money"
            confirm = (
                (
                    "Переключить в версию БЕЗ ставок? Ставки и платная смена "
                    "выбора закроются со следующего дня; входящие переводы уйдут "
                    "обратно. Нажми кнопку ещё раз для подтверждения."
                )
                if not want_money
                else (
                    "Вернуть версию СО СТАВКАМИ?\n"
                    "— ставки и платная смена выбора снова доступны со следующего дня."
                    "\n\nНажми кнопку ещё раз для подтверждения."
                )
            )
            if not await _panel_confirm(
                f"panel_confirm:{callback.from_user.id}:{action}"
            ):
                await callback.answer(confirm, show_alert=True)
                return
            async with SessionLocal() as session:
                changed = await set_money_mode(session, want_money)
            if not changed:
                await callback.answer(
                    "Версия уже установлена." if not want_money else "Версия уже активна.",
                    show_alert=True,
                )
                return
            state_name = "со ставками и платной сменой выбора" if want_money else "без ставок"
            if callback.message is not None:
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        f"💰 Версия «{state_name}» включена со СЛЕДУЮЩЕГО дня.\n"
                        "Текущий день живёт по своему режиму.\n\n"
                        + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        # Повторное нажатие привело к тому же тексту: Telegram
                        # отвечает ошибкой, а менять нечего. Молчание уместно.
                        pass
                    else:
                        raise
            await callback.answer("Готово.")
            return
        if action in {"pause", "resume"}:
            # Стоп-кран с последствиями: первый тап предупреждает,
            # повторный тап той же кнопки в течение двух минут — делает.
            want_paused = action == "pause"
            if not await _panel_confirm(
                f"panel_confirm:{callback.from_user.id}:{action}"
            ):
                confirm = (
                    "Остановить игру: дни замрут, входящие переводы пойдут обратно "
                    "с пометкой о техработах. Нажми кнопку ещё раз для подтверждения."
                    if want_paused
                    else "Возобновить игру? Новый день откроется сам в ближайший тик. "
                    "Нажми кнопку ещё раз для подтверждения."
                )
                await callback.answer(confirm, show_alert=True)
                return
            changed, delivered = await _set_paused_and_broadcast(
                callback.bot, want_paused, "технические работы"
            )
            if not changed:
                await callback.answer(
                    "Игра уже на паузе." if want_paused else "Игра и так идёт.",
                    show_alert=True,
                )
                return
            chats = f" Анонс в {delivered} чат(ах)." if delivered else ""
            status_msg = "⏸ Игра остановлена." if want_paused else "▶️ Игра возобновляется."
            if callback.message is not None:
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        status_msg + chats + "\n\n" + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        # Повторное нажатие привело к тому же тексту: Telegram
                        # отвечает ошибкой, а менять нечего. Молчание уместно.
                        pass
                    else:
                        raise
            await callback.answer("Готово.")
            return
        if action == "treasury":
            from app.ton_pay import treasury_diagnostics

            await callback.message.answer(
                await treasury_diagnostics(), parse_mode=ParseMode.HTML
            )
            await callback.answer()
            return
        if action == "revenue":
            await callback.message.answer(await _revenue_text())
            await callback.answer()
            return
        if action == "cassettes":
            async with SessionLocal() as session:
                text = await _cassette_menu_text(session)
            await callback.message.answer(
                text, parse_mode=ParseMode.HTML, reply_markup=await _cassette_keyboard()
            )
            await callback.answer()
            return
        if action in {"advance", "advance:go"}:
            if action != "advance:go":
                # Досрочное закрытие — действие с последствиями. Кнопка всегда
                # шлёт один и тот же callback_data, поэтому «нажми ещё раз»
                # фиксируется в памяти: второй тап в окне подтверждает.
                # (Раньше ветка ":go" была недостижима из UI — кнопка не могла
                # завершить день никогда, только просила «ещё раз» вечно.)
                if not await _panel_confirm(
                    f"panel_confirm:{callback.from_user.id}:advance"
                ):
                    await callback.answer(
                        "Закрыть голосование досрочно и открыть следующий день? "
                        "Нажми кнопку ещё раз для подтверждения.",
                        show_alert=True,
                    )
                    return
            _answers: list[str] = []

            class _ShimMessage:
                """Лёгкий двойник Message: переиспользуем логику /advance."""

                chat = SimpleNamespace(type=ChatType.PRIVATE)
                text = "/advance"
                bot = callback.bot
                from_user = callback.from_user

                async def answer(self, text, *args, **kwargs):
                    _answers.append(str(text))

            await cmd_advance(_ShimMessage())
            summary = "\n".join(_answers)[:3500] or "Готово."
            if callback.message is not None:
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        f"⏩ {summary}\n\n" + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        # Повторное нажатие привело к тому же тексту: Telegram
                        # отвечает ошибкой, а менять нечего. Молчание уместно.
                        pass
                    else:
                        raise
            await callback.answer("День переключён.")
            return
        await callback.answer("Неизвестное действие.", show_alert=True)
    except Exception as exc:
        logger.exception("Действие пульта %s не удалось", action)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)


async def _cassette_menu_text(session) -> str:
    """Список библиотеки кассет: валидные файлы, назначение, замечания."""
    today = datetime.now(UTC).date()
    next_name = await get_next_cassette(session)
    lines = ["📼 <b>КАССЕТЫ</b>"]
    lines.append(f"Следующая: <b>{next_name}</b>" if next_name else "Следующая: —")
    entries = list_cassettes()
    if not entries:
        lines.append("Библиотека пуста: валидных кассет в каталоге нет.")
        return "\n".join(lines)
    month_now = today.strftime("%Y-%m")
    for entry in entries:
        if entry.cassette is None:
            lines.append(
                f"❌ <b>{entry.file_name}</b> — невалидна: {'; '.join(entry.errors)}"
            )
            continue
        cassette = entry.cassette
        marks = []
        if entry.file_name == next_name:
            marks.append("🟢 назначена")
        elif cassette.month == month_now:
            marks.append("▶ играется")
        flags = " · " + " · ".join(marks) if marks else ""
        lines.append(
            f"📖 {entry.file_name} · {cassette.title} · {cassette.month} "
            f"({len(cassette.days)} дней){flags}"
        )
        if entry.warnings:
            lines.append(f"   ⚠️ {'; '.join(entry.warnings)}")
        # Повествовательный линт (ротация/дубли/эхо/штампы) раньше жил только
        # в CLI, и редакционные замечания кассеты существовали вне бота.
        # Панель даёт счёт по разрядам и по одному примеру из каждого —
        # полный хвост по-прежнему в cassette_tool lint.
        groups = [(label, group) for label, group in lint_groups(cassette) if group]
        total = sum(len(group) for _label, group in groups)
        if total:
            counts = " · ".join(f"{label} {len(group)}" for label, group in groups)
            lines.append(f"   🧾 повествовательный линт: {total} замечаний ({counts})")
            shown: list[str] = [group[0] for _label, group in groups[:3]]
            for warning in shown:
                lines.append(f"     • {warning[:110]}")
            if total > len(shown):
                lines.append(
                    f"     … и ещё {total - len(shown)} (см. cassette_tool lint)"
                )
    lines.append(
        "\nКассета активна, когда её месяц совпал с текущим; до этого движок "
        "играет шаблон «Путь I/II/III» (стоп на стыке месяцев)."
    )
    return "\n".join(lines)


async def _cassette_keyboard() -> InlineKeyboardMarkup:
    """Кнопки назначения «следующей» кассеты и входа в её «Редактор плёнки»."""
    async with SessionLocal() as session:
        next_name = await get_next_cassette(session)
    rows = []
    for entry in list_cassettes():
        if entry.cassette is None:
            continue
        mark = "🟢 " if entry.file_name == next_name else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{entry.cassette.title}",
                    callback_data=f"cassette:set:{entry.file_name}",
                ),
                InlineKeyboardButton(
                    text="🎞",
                    callback_data=f"cassette:scene:{entry.file_name}",
                ),
            ]
        )
    if next_name:
        rows.append(
            [InlineKeyboardButton(text="❌ Снять выбор", callback_data="cassette:clear")]
        )
    rows.append([_cb("➕ Новый месяц", "cassette:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _scene_text(entry: LibraryEntry, status: str = "") -> str:
    """Шапка «Редактора плёнки»: что за кассета, контекст и как вернуть правки."""
    cassette = entry.cassette
    assert cassette is not None
    roads = " · ".join(["main"] + [fork.to for fork in cassette.switch])
    lines = [
        f"🎞 <b>ПЛЁНКА: {cassette.title}</b>",
        f"Файл: {entry.file_name} · {cassette.month} · {len(cassette.days)} дней",
        f"Дороги: {roads}",
    ]
    if status:
        lines.append(status)
    lines += [
        "",
        "Правки — через файл: скачай YAML, измени в любом текстовом редакторе "
        "и пришли документом. <b>Вернуть месяц</b> принимает целый сценарий, "
        "<b>Вернуть день</b> — один день (фрагмент); <b>Проверить</b> — только "
        "валидация без записи. Неисправный файл кассету не трогает.",
    ]
    return "\n".join(lines)


async def _scene_status(entry: LibraryEntry) -> str:
    """Контекст плёнки: назначена ли «следующей», играется ли, сегодняшняя дорога."""
    if entry.cassette is None:
        return ""
    today = datetime.now(UTC).date()
    lines: list[str] = []
    async with SessionLocal() as session:
        next_name = await get_next_cassette(session)
        if entry.file_name == next_name:
            lines.append("🟢 назначена «следующей».")
        if entry.cassette.month == today.strftime("%Y-%m"):
            try:
                road, _dates = await today_road(session, entry.cassette, today)
            except Exception:
                road = "main"
            lines.append(f"▶ играется · сегодня день {today.day} · дорога {road}.")
    return "\n".join(lines)


def _scene_keyboard(
    file_name: str, awaiting: bool, has_backup: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки редактора: скачать/вернуть/проверить месяц или день, минус отмена."""
    rows = [
        [_cb("📥 Скачать месяц (.yaml)", f"cassette:month:{file_name}")],
        [
            _cb("📥 Скачать день", f"cassette:pick:{file_name}:main:dl"),
            _cb("📄 Прочитать день", f"cassette:pick:{file_name}:main:view"),
        ],
        [
            _cb("📤 Вернуть месяц", f"cassette:edit:{file_name}:month"),
            _cb("🔎 Проверить месяц", f"cassette:check:{file_name}:month"),
        ],
        [
            _cb("📤 Вернуть день", f"cassette:edit:{file_name}:day"),
            _cb("🔎 Проверить день", f"cassette:check:{file_name}:day"),
        ],
    ]
    if awaiting:
        rows.append([_cb("⏹ Отменить загрузку", f"cassette:stop:{file_name}")])
    if has_backup:
        rows.append([_cb("🗄 Вернуть бэкап", f"cassette:restore:{file_name}")])
    rows.append([_cb("🔙 К библиотеке", "cassette:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _new_keyboard() -> InlineKeyboardMarkup:
    """Кнопки экрана «Новая кассета»: отмена загрузки или назад в библиотеку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_cb("⏹ Отменить", "cassette:stop")],
            [_cb("🔙 К библиотеке", "cassette:back")],
        ]
    )


def _report_keyboard(scene_file: str | None, has_backup: bool) -> InlineKeyboardMarkup:
    """Кнопки под отчётом правки: в плёнку, вернуть бэкап, к списку кассет."""
    rows: list[list[InlineKeyboardButton]] = []
    if scene_file:
        if has_backup:
            rows.append([_cb("🗄 Вернуть бэкап", f"cassette:restore:{scene_file}")])
        rows.append([_cb("🎞 Плёнка", f"cassette:scene:{scene_file}")])
    rows.append([_cb("📼 К кассетам", "cassette:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _report_text(head: str, lines: list[str]) -> str:
    """Отчёт для Telegram: вердикт и финальный статус не теряются при обрезке.

    Жёсткий потолок поста 4096 (тут 4000). Замечания могут быть длинными —
    свёрнутым режется ТОЛЬКО их край, а не хвост с «Проверено — файл не изменён».
    """
    text = head + "\n".join(lines)
    if len(text) <= 4000:
        return text
    status = lines[-1]
    prefix = head + "\n".join(lines[:-1])
    budget = 4000 - len(status) - 1
    if len(prefix) > budget:
        prefix = prefix[: budget - 1].rstrip() + "…"
    return prefix + "\n" + status


def _safe_cassette_name(file_name: str) -> str | None:
    """Имя файла кассеты без выхода за пределы библиотеки, иначе None.

    file_name приходит из callback_data, то есть от клиента. Раньше он
    подставлялся в путь как есть: `cassette:scene:../../../../etc/passwd` даёт
    чтение любого файла контейнера, а `cassette:restore:<путь>` — запись в
    любой путь, где нашлось одноимённое `.bak`. Telegram ограничивает
    callback_data 64 байтами, то есть такой путь в него помещается.

    Сама проверка живёт в story.editor: там же строятся пути при записи, и
    дубль правил в двух модулях разошёлся бы при первом же изменении.
    """
    from app.story.editor import is_safe_cassette_name

    return file_name if is_safe_cassette_name(file_name) else None


def _has_backup(file_name: str) -> bool:
    safe = _safe_cassette_name(file_name)
    if safe is None:
        return False
    return (default_cassettes_dir() / (safe + ".bak")).is_file()


def _scene_badge(awaiting: bool, hint: str = "") -> str:
    """Пометка о том, что бот ждёт документ правки."""
    if not awaiting:
        return ""
    return "\n⏳ Жду документ" + (f": {hint}" if hint else "") + "."


def _library_entry(file_name: str) -> LibraryEntry | None:
    """Живая запись библиотеки по имени файла (из списка /cassette)."""
    safe = _safe_cassette_name(file_name)
    if safe is None:
        return None
    path = default_cassettes_dir() / safe
    if not path.is_file():
        return None
    result = validate_file(path)
    return LibraryEntry(
        file_name=safe,
        cassette=result.cassette,
        errors=result.errors,
        warnings=result.warnings,
    )


def _day_grid_keyboard(
    cassette: Cassette, file_name: str, road: str, action: str
) -> InlineKeyboardMarkup:
    """Сетка дней дороги: скачать фрагмент (dl) или прочитать кадр (view)."""
    rows: list[list[InlineKeyboardButton]] = []
    if cassette.switch:
        road_buttons = []
        for candidate in ["main"] + [fork.to for fork in cassette.switch]:
            mark = "• " if candidate == road else ""
            road_buttons.append(
                _cb(mark + candidate, f"cassette:pick:{file_name}:{candidate}:{action}")
            )
        rows.append(road_buttons)
    if road == "main":
        day_numbers = [day.day_index for day in cassette.days]
    else:
        day_numbers = [
            day.day_index
            for fork in cassette.switch
            if fork.to == road
            for day in fork.days
        ]
    for i in range(0, len(day_numbers), 6):
        rows.append(
            [
                _cb(str(n), f"cassette:day:{file_name}:{road}:{action}:{n}")
                for n in day_numbers[i : i + 6]
            ]
        )
    rows.append(
        [
            _cb("⬅️ К плёнке", f"cassette:scene:{file_name}"),
            _cb("🔙 К библиотеке", "cassette:back"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _day_doc_name(file_name: str, road: str, day: int) -> str:
    stem = Path(file_name).stem
    if road == "main":
        return f"{stem}--day-{day}.yaml"
    return f"{stem}--day-{day}-{road}.yaml"


_MAX_TEXT_CHUNK = 3900


def _chunk_message(text: str) -> list[str]:
    """Делит длинный кадр дня на сообщения (лимит 4096 знаков Telegraph)."""
    if len(text) <= _MAX_TEXT_CHUNK:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > _MAX_TEXT_CHUNK:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


@router.message(Command("cassette"), F.chat.type == ChatType.PRIVATE)
async def cmd_cassette(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Пульт только для хранителя игры.")
        return
    async with SessionLocal() as session:
        text = await _cassette_menu_text(session)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=await _cassette_keyboard())


@router.callback_query(F.data.startswith("cassette:"))
async def on_cassette_action(callback: CallbackQuery) -> None:
    """Назначение «следующей», вход в «Редактор плёнки», скачивание и правки."""
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Пульт только для хранителя.", show_alert=True)
        return
    parts = callback.data.split(":")
    op = parts[1] if len(parts) > 1 else ""
    file_name = parts[2] if len(parts) > 2 else ""
    # Всё, что дальше, работает с именем файла как с путём. Отсекаем выход за
    # пределы библиотеки ДО ветвления: иначе «неизвестное действие» и
    # «кассеты нет» выглядели бы одинаково, а проверка по op сползала бы на
    # действия, где имя не нужно (clear/stop/back), а имя всё равно в данных.
    if file_name and _safe_cassette_name(file_name) is None:
        await callback.answer("Некорректное имя кассеты.", show_alert=True)
        return
    try:
        if op == "set":
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            async with SessionLocal() as session:
                await set_next_cassette(session, file_name)
        elif op == "clear":
            async with SessionLocal() as session:
                await set_next_cassette(session, None)
        elif op == "scene":
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            async with SessionLocal() as session:
                edit_file, _unused = await get_edit_intent(session)
            status = await _scene_status(entry)
            await callback.message.edit_text(
                f"{_scene_text(entry, status)}{_scene_badge(edit_file == file_name)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_scene_keyboard(
                    file_name,
                    awaiting=(edit_file == file_name),
                    has_backup=_has_backup(file_name),
                ),
            )
        elif op in ("month",):
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            payload = scenario_yaml(entry.cassette).encode("utf-8")
            await callback.message.answer_document(
                BufferedInputFile(payload, filename=Path(file_name).with_suffix(".yaml").name)
            )
        elif op in ("pick",):
            road = parts[3] if len(parts) > 3 else "main"
            action = parts[4] if len(parts) > 4 else "dl"
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            await callback.message.edit_text(
                (
                    f"🎞 {entry.cassette.title}"
                    f"{' · дорога ' + road if road != 'main' else ''} — день?"
                ),
                reply_markup=_day_grid_keyboard(entry.cassette, file_name, road, action),
            )
        elif op in ("day",):
            road = parts[3] if len(parts) > 3 else "main"
            action = parts[4] if len(parts) > 4 else "dl"
            try:
                day_index = int(parts[5])
            except (IndexError, ValueError):
                await callback.answer("Нажми день в сетке.", show_alert=True)
                return
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            item = entry.cassette.day_for(day_index, road)
            if item is None:
                await callback.answer("Такого дня на дороге нет.", show_alert=True)
                return
            if action == "dl":
                payload = day_yaml(item, road=road).encode("utf-8")
                await callback.message.answer_document(
                    BufferedInputFile(
                        payload, filename=_day_doc_name(file_name, road, day_index)
                    )
                )
            else:
                for chunk in _chunk_message(day_view_text(entry.cassette, day_index, road)):
                    await callback.message.answer(chunk)
        elif op == "edit":
            mode = parts[3] if len(parts) > 3 else "month"
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            async with SessionLocal() as session:
                await set_edit_intent(session, file_name, mode)
            meaning = {
                "month": "целый сценарий .yaml (скачай, измени, пришли файлом)",
                "day": "фрагмент одного дня .yaml (скачай, измени, пришли файлом)",
            }.get(mode, "")
            status = await _scene_status(entry)
            await callback.message.edit_text(
                f"{_scene_text(entry, status)}{_scene_badge(True, meaning)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_scene_keyboard(
                    file_name, awaiting=True, has_backup=_has_backup(file_name)
                ),
            )
        elif op == "check":
            mode = parts[3] if len(parts) > 3 else "month"
            entry = _library_entry(file_name)
            if entry is None or entry.cassette is None:
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            async with SessionLocal() as session:
                await set_edit_intent(session, file_name, f"{mode}-check")
            meaning = {
                "month": "проверю целый сценарий без записи",
                "day": "проверю фрагмент дня без записи",
            }.get(mode, "проверю без записи")
            status = await _scene_status(entry)
            await callback.message.edit_text(
                f"{_scene_text(entry, status)}{_scene_badge(True, meaning)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_scene_keyboard(
                    file_name, awaiting=True, has_backup=_has_backup(file_name)
                ),
            )
        elif op == "new":
            async with SessionLocal() as session:
                await set_edit_intent(session, "<new>", "new")
            await callback.message.edit_text(
                "➕ <b>НОВАЯ КАССЕТА</b>\n\n"
                "Пришли документом YAML-сценарий нового месяца (контракт §1). "
                "Шаблон — «Скачать месяц» любой существующей плёнки. Имя файла "
                "выведется из cassette_id и месяца; перезаписать существующие "
                "нельзя.\n\n"
                "⏳ Жду документ.",
                parse_mode=ParseMode.HTML,
                reply_markup=_new_keyboard(),
            )
        elif op == "restore":
            safe = _safe_cassette_name(file_name)
            path = default_cassettes_dir() / safe if safe else None
            if path is None or not path.is_file():
                await callback.answer("Такой кассеты нет в библиотеке.", show_alert=True)
                return
            ok, lines = restore_backup(safe, default_cassettes_dir())
            if not ok:
                await callback.answer("\n".join(lines)[:200], show_alert=True)
                return
            # Откат — тоже правка: файл на диске уже прежняя версия, переносим
            # её в базу, иначе деплой вернёт состояние до восстановления.
            persisted = await story_store.persist(safe, default_cassettes_dir())
            async with SessionLocal() as session:
                text = await _cassette_menu_text(session)
            await callback.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=await _cassette_keyboard()
            )
            await callback.answer(
                "Кассета восстановлена из бэкапа."
                if persisted
                else "Кассета восстановлена в кэш, но не в базу — правка "
                "пропадёт при рестарте."
            )
            return
        elif op == "stop":
            async with SessionLocal() as session:
                await clear_edit_intent(session)
            await callback.message.edit_text(
                "Загрузка отменена. Кассета не изменена: правки возвращались "
                "документом.",
                reply_markup=await _cassette_keyboard(),
            )
        elif op == "back":
            async with SessionLocal() as session:
                text = await _cassette_menu_text(session)
            await callback.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=await _cassette_keyboard()
            )
        else:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
        await callback.answer("Готово.")
    except Exception as exc:
        logger.exception("Действие кассеты %s не удалось", callback.data)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)


@router.message(F.document)
async def on_cassette_document(message: Message) -> None:
    """Приём документа: правка месяца/дня, проверка без записи, новая кассета."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        return
    if message.document is None:
        return
    # Потолок размера: Telegram и так ограничивает документ 20 МБ, но файл
    # целиком тянется в память и распаковывается. Кассета — текст в JSON на
    # единицы килобайт; мегабайтный «документ» может быть только ошибкой или
    # попыткой положить память контейнера под нож.
    max_bytes = 2 * 1024 * 1024
    if (getattr(message.document, "file_size", None) or 0) > max_bytes:
        await message.reply(
            f"❌ Документ больше {max_bytes // (1024 * 1024)} МБ — кассета столько "
            "не весит. Пришли файл поменьше или разбей его.",
        )
        return
    async with SessionLocal() as session:
        edit_file, edit_mode = await get_edit_intent(session)
    if edit_file is None:
        return
    # Правка всегда идёт по уже известному файлу из библиотеки, но проверяем
    # имя ещё раз: edit_file лежит в БД, а значит в принципе мог прийти из
    # callback_data в прежней версии кода. Ссылка на <new> — это создание
    # новой кассеты, там имя задаёт сам файл.
    if edit_file != "<new>" and _safe_cassette_name(edit_file) is None:
        await message.reply("❌ Имя кассеты некорректно — правка отклонена.")
        return
    mode = edit_mode.split("-", 1)[0]
    dry_run = edit_mode.endswith("-check")
    try:
        buffer = io.BytesIO()
        await message.bot.download(file=message.document.file_id, destination=buffer)
        ok, lines, final_name = apply_cassette_file(
            buffer.getvalue(),
            edit_file,
            mode,
            default_cassettes_dir(),
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("Загрузка правки кассеты %s не удалась", edit_file)
        ok, lines, final_name = False, [f"Не получилось: {exc}"], None
    async with SessionLocal() as session:
        await clear_edit_intent(session)
    # Долговечность: файл уже на диске — переносим правку в базу (см.
    # app/story/store.py). Отказ не валит отчёт, но хранитель обязан знать,
    # что правка пока только в кэше и рестарт её снесёт.
    if ok and not dry_run and final_name:
        if not await story_store.persist(final_name, default_cassettes_dir()):
            lines = [
                *lines,
                "⚠️ База кассет не ответила: правка сохранилась ТОЛЬКО В КЭШЕ "
                "диска и пропадёт при рестарте — повтори сохранение позже.",
            ]
    scene_file = final_name or (edit_file if edit_file != "<new>" else None)
    has_backup = bool(ok and not dry_run and scene_file and _has_backup(scene_file))
    head = "✅ " if ok else "❌ "
    await message.reply(
        _report_text(head, lines),
        reply_markup=_report_keyboard(scene_file, has_backup),
    )
