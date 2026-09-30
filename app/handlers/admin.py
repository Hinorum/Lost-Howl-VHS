# Хранитель: жизненный цикл дня (/advance, /resetgame), диалог разбирательства,
# сверка фонда (/adjust) и стоп-кран (/pause, /resume).
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.broadcast import (
    announce_new_day,
    cards_keyboard,
    results_message,
    status_text,
)
from app.config import settings
from app.db import SessionLocal
from app.models import Chat, Payout, Round, RoundStatus, Stake, Vote, WatcherState
from app.rounds import (
    _now,
    claim_announcement,
    close_voting,
    create_next_round_detailed,
    ensure_current_round,
    finish_tally,
    get_active_round,
    reset_game,
    utc_aware,
    write_epilogue,
)
from app.style import money_mark, ok_mark, warn_mark
from app.tally import award_points
from app.ton_pay import pending_payout_count
from app.ton_utils import from_nano, to_nano
from app.voting import upsert_player

from .common import _set_paused_and_broadcast, router

logger = logging.getLogger(__name__)


_ACTIVE_STATUSES = {"member", "administrator", "creator"}


@router.my_chat_member()
async def track_chat(event: ChatMemberUpdated) -> None:
    """Запоминаем чаты, где бот состоит (в идеале — администратором)."""
    chat = event.chat
    if chat.type == ChatType.PRIVATE:
        return
    status = event.new_chat_member.status
    active = status in _ACTIVE_STATUSES
    async with SessionLocal() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            session.add(
                Chat(
                    id=chat.id,
                    title=chat.title or chat.username,
                    type=chat.type,
                    active=active,
                )
            )
        else:
            row.title = chat.title or chat.username or row.title
            row.active = active
        await session.commit()
    logger.info("Чат %s (%s): статус бота %s, active=%s", chat.id, chat.type, status, active)


@router.message(Command("advance"), F.chat.type == ChatType.PRIVATE)
async def cmd_advance(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    # Стоп-кран не спорит с хранителем: явный /advance снимает паузу сам
    # (инцидент: отказ «сначала /resume» выглядел как поломка кнопок).
    changed, _delivered = await _set_paused_and_broadcast(message.bot, False)
    if changed:
        await message.answer(f"{ok_mark('go')} Пауза снята автоматически: выполняю /advance.")
    closed_here = False
    claimed = False
    async with SessionLocal() as session:
        # Сначала дочитываем дни, застрявшие позади актуального (инцидент:
        # сбой анонса оставлял день в TALLYING навсегда).
        try:
            from app.rounds import heal_stale_rounds

            await heal_stale_rounds(session)
        except Exception:
            logger.warning("Лечение застрявших дней перед /advance не удалось", exc_info=True)
        round_row = await get_active_round(session)
        if round_row is None:
            round_row = await ensure_current_round(session)
            if await claim_announcement(session, round_row):
                await announce_new_day(message.bot, round_row)
                await message.answer(f"Открыт день {round_row.day_index}.")
            else:
                await message.answer(f"День {round_row.day_index} уже объявлен.")
            return
        if utc_aware(round_row.voting_ends_at) > _now():
            # Инцидент: тик уже закрыл N и открыл N+1, а /advance брал свежий
            # день как «актуальный» и закрывал его с нулём голосов и жребием,
            # плодя затем N+2 по latest+1. Свежеоткрытый день не трогаем: если
            # день действительно застрял, его окно голосования уже истекло.
            ends = utc_aware(round_row.voting_ends_at).strftime("%H:%M")
            await message.answer(
                f"День {round_row.day_index} ещё голосуется (до {ends}). "
                "Свежий день не закрываю — /advance нужен застрявшим дням."
            )
            return
        if round_row.status.value == "open":
            await close_voting(session, round_row)
            round_row, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, round_row)
                from app.stakes import finalize_day_payouts
                await finalize_day_payouts(session, round_row)
                await write_epilogue(session, round_row)
                from app.leaderboard import mark_leaderboards_for_finished
                await mark_leaderboards_for_finished(session, round_row)
            nxt, created = await create_next_round_detailed(
                session, base_day_index=round_row.day_index
            )
        elif round_row.status.value == "tallying":
            round_row, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, round_row)
                from app.stakes import finalize_day_payouts
                await finalize_day_payouts(session, round_row)
                await write_epilogue(session, round_row)
                from app.leaderboard import mark_leaderboards_for_finished
                await mark_leaderboards_for_finished(session, round_row)
            nxt, created = await create_next_round_detailed(
                session, base_day_index=round_row.day_index
            )
        else:
            return
        if created:
            claimed = await claim_announcement(session, nxt)
    if not created or not claimed:
        # День уже создан/объявлен планировщиком — второй пост не нужен.
        await message.answer(f"День {nxt.day_index} уже объявлен.")
        return
    delivered = await announce_new_day(message.bot, nxt, round_row if closed_here else None)
    if delivered:
        await message.answer(f"День {nxt.day_index} объявлен в {len(delivered)} чат(ах).")
    else:
        # Ни одного подписанного чата — покажем всё прямо здесь.
        await message.answer(await results_message(round_row), parse_mode=ParseMode.HTML)
        await message.answer(
            await status_text(nxt, show_title=True),
            parse_mode=ParseMode.HTML,
            reply_markup=cards_keyboard(nxt.id, remember=False, day_index=nxt.day_index),
        )


@router.message(Command("resetgame"), F.chat.type == ChatType.PRIVATE)
async def cmd_resetgame(message: Message) -> None:
    """Сброс игры — только для хранителя. Два режима:
    /resetgame confirm — всё с нуля, включая канон истории;
    /resetgame confirm keepstory — счёты чисты, но мир помнит прошлое."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    # Явный confirm снимает паузу сам: сброс под стоп-краном — осознанное
    # действие хранителя, а не ошибка, которую надо блокировать.
    changed, _delivered = await _set_paused_and_broadcast(message.bot, False)
    if changed:
        await message.answer(f"{ok_mark('go')} Пауза снята автоматически: выполняю сброс.")
    words = (message.text or "").lower().split()
    if "confirm" not in words:
        await message.answer(
            f"{warn_mark('reset')} Это сотрёт все дни, голоса, ставки, выплаты и очки игроков.\n"
            "Кошельки, чаты и копилка месяца останутся.\n"
            "<code>/resetgame confirm</code> — полный сброс вместе с каноном истории.\n"
            "<code>/resetgame confirm keepstory</code> — сброс счётов, "
            "но мир и эхо прошлого сохраняются.",
            parse_mode=ParseMode.HTML,
        )
        return
    keep_story = "keepstory" in words
    async with SessionLocal() as session:
        owed = await pending_payout_count(session)
        if owed:
            await message.answer(
                f"{warn_mark('queue')} Сброс отложен: в очереди {owed} неотправленных переводов.\n"
                "Сначала дай выплатам уйти (автоцикл) или разбери зависшие вручную —\n"
                "обнуление стёрло бы чужие деньги."
            )
            return
        # Вторая линия защиты (инцидент: ставки зависшего дня стёрлись сбросом,
        # а монеты остались в казнее): дни без финализации со ставками —
        # неразыгранные обязательства. Сначала дай им доиграть.
        stuck_rows = (
            await session.execute(
                select(Round.day_index)
                .join(Stake, Stake.round_id == Round.id)
                .where(Round.payouts_finalized.is_(False))
                .distinct()
            )
        ).scalars().all()
        if stuck_days := sorted(stuck_rows):
            days_list = ", ".join(str(day) for day in stuck_days[:10])
            await message.answer(
                f"{warn_mark('queue')} Сброс отложен: у дня(ей) {days_list} есть "
                "неразыгранные ставки — деньги игроков ещё в казнее.\n"
                "Дай дням доиграть (/advance или автоцикл): подсчёт сам создаст "
                "возвраты и призы, после отправки сброс пройдёт."
            )
            return
        # Страховка перед необратимым сбросом: снимок БД до стирания, чтобы
        # случайный /resetgame не лишил возможности откатить решения.
        try:
            from app.backups import backup_now
            await backup_now()
        except Exception:
            logger.warning("Бэкап перед /resetgame не удался", exc_info=True)
            await message.answer(f"{warn_mark('queue')} Бэкап перед сбросом не удался, " "продолжаю без снимка.")
        new_round = await reset_game(session, keep_story=keep_story)
        first = await claim_announcement(session, new_round)
    if not first:
        await message.answer(f"День {new_round.day_index} только что объявил другой процесс бота.")
        return
    await announce_new_day(message.bot, new_round)
    mode = "Канон истории сохранён." if keep_story else "История стёрта полностью."
    await message.answer(
        f"{ok_mark('reset')} Игра обнулена. День {new_round.day_index} объявлен в чатах. "
        f"{mode} Голосование до {new_round.voting_ends_at:%H:%M} UTC."
    )


async def _resolve_player_arg(session, raw: str) -> int | None:
    """Игровой id или @ник → player_id (None, если не нашли)."""
    raw = raw.strip().lstrip("@")
    from app.models import Player as _Player

    if raw.isdigit():
        return int(raw)
    row = (
        await session.execute(select(_Player).where(_Player.username == raw).limit(1))
    ).scalar_one_or_none()
    return row.id if row is not None else None


@router.message(Command("dispute"), F.chat.type == ChatType.PRIVATE)
async def cmd_dispute(message: Message) -> None:
    """Жалоба на итог дня. Хранителю доступны open/resolve/reject/compensate."""
    from app import disputes as dispute_mod

    parts = message.text.split()
    verb = parts[1].lower() if len(parts) > 1 else ""
    if verb in ("resolve", "reject", "compensate", "open"):
        if message.from_user is None or message.from_user.id not in settings.admin_id_set:
            await message.answer("Разрешение и компенсация споров — только для хранителя.")
            return
        async with SessionLocal() as session:
            if verb == "open":
                if len(parts) < 4:
                    await message.answer("Формат: /dispute open <день> <id или @ник> <причина>")
                    return
                try:
                    round_id = int(parts[2])
                except ValueError:
                    await message.answer("Номер дня должен быть целым числом.")
                    return
                pid = await _resolve_player_arg(session, parts[3])
                if pid is None:
                    await message.answer("Игрок не найден (id или @ник).")
                    return
                reason = " ".join(parts[4:]) if len(parts) > 4 else ""
                reply = await dispute_mod.open_dispute(session, round_id, pid, reason)
            elif verb == "resolve" or verb == "reject":
                if len(parts) < 2:
                    await message.answer("Формат: /dispute resolve <id> [примечание]")
                    return
                try:
                    did = int(parts[2])
                except ValueError:
                    await message.answer("Номер спора должен быть целым числом.")
                    return
                note = " ".join(parts[3:]) if len(parts) > 3 else ""
                fn = dispute_mod.resolve_dispute if verb == "resolve" else dispute_mod.reject_dispute
                reply = await fn(session, did, note)
            else:  # compensate
                if len(parts) < 4:
                    await message.answer("Формат: /dispute compensate <id> <Gram> [примечание]")
                    return
                try:
                    did = int(parts[2])
                except ValueError:
                    await message.answer("Номер спора должен быть целым числом.")
                    return
                note = " ".join(parts[4:]) if len(parts) > 4 else ""
                reply = await dispute_mod.compensate_dispute(session, did, parts[3], note)
        await message.answer(reply)
        return
    # Публичная само-подача: жалоба игрока на его последний сыгранный день.
    if message.from_user is None:
        return
    from app.models import Round as _Round
    from app.models import Vote as _Vote

    reason = message.text.split(maxsplit=1)[1] if " " in message.text else ""
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        latest = (
            await session.execute(
                select(_Round.id)
                .join(_Vote, _Vote.round_id == _Round.id)
                .where(_Vote.player_id == player.id)
                .order_by(_Round.day_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        reply = await dispute_mod.open_dispute(session, latest, player.id, reason)
    await message.answer(reply)


@router.message(Command("disputes"), F.chat.type == ChatType.PRIVATE)
async def cmd_disputes(message: Message) -> None:
    """Список открытых споров. Только для хранителя."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Только для хранителя игры.")
        return
    from app import disputes as dispute_mod

    async with SessionLocal() as session:
        rows = await dispute_mod.open_disputes(session)
    if not rows:
        await message.answer("Открытых споров нет.")
        return
    lines = ["⚖ Споры в рассмотрении:"]
    for d in rows:
        lines.append(
            f"  #{d.id} · день {d.round_id if d.round_id is not None else '—'} · "
            f"игрок {d.player_id} · {d.reason[:90]}"
        )
    lines.append("")
    lines.append("Разбор: /dispute resolve <id> [заметка] · отказ: /dispute reject <id> · компенсация: /dispute compensate <id> <Gram>")
    await message.answer("\n".join(lines))


async def _adjust_menu_text() -> str:
    """Меню сверки: что видно по расхождению и какие есть кнопки."""
    from app.ops import is_game_paused, paused_reason, treasury_expected_state

    lines = ["⚖️ <b>Сверка казны с БД</b>"]
    async with SessionLocal() as session:
        state = await treasury_expected_state(session)
        paused = await is_game_paused(session)
        reason = await paused_reason(session)
    if state is None:
        lines.append("Баланс казначея недоступен (оба индексатора молчат) — повтори позже.")
        return "\n".join(lines)
    drift = state.drift_nanotons
    lines.append(
        f"Баланс цепочки: {state.balance_nanotons / 1e9:.4f} Gram\n"
        f"Ожидания БД: ~{state.expected_nanotons / 1e9:.4f} Gram "
        f"(допуск ±{state.tolerance_nanotons / 1e9:.4f})"
    )
    if not state.beyond_tolerance:
        lines.append(f"\n{ok_mark('clean')} Всё сходится в допуске — корректировка не нужна.")
    elif drift > 0:
        lines.append(
            f"\n⚠️ На цепи меньше ожиданий на <b>{drift / 1e9:.4f} Gram</b>. Что это было?\n"
            "✋ <b>Ручной вывод</b> — ты сам выводил/переводил эти деньги: сумма уйдёт "
            "в леджер, алерт сверки замолчит.\n"
            "🕳 <b>Пропажа средств</b> — то же самое плюс стоп-кран: игра встанет на паузу, "
            "а все входящие переводы будут автоматически возвращаться отправителям "
            "с комментарием о техработах."
        )
    else:
        lines.append(
            f"\n⚠️ На цепи больше ожиданий на <b>{-drift / 1e9:.4f} Gram</b> — "
            "похоже, было пополнение мимо учёта. Кнопка «Пополнение» запомнит его."
        )
    if paused:
        lines.append(
            f"\n⏸ Игра сейчас на паузе ({reason or 'техработы'}). Снять: /resume или кнопка пульта."
        )
    lines.append("\nТочная сумма: <code>/adjust &lt;сумма&gt; out|in|loss [комментарий]</code>")
    return "\n".join(lines)


def _adjust_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✋ Это был ручной вывод", callback_data="adj:out")],
            [InlineKeyboardButton(text="🕳 Пропажа средств (пауза игры)", callback_data="adj:loss")],
            [InlineKeyboardButton(text="💰 Ручное пополнение казны", callback_data="adj:in")],
        ]
    )


def _adjust_confirm_text(action: str) -> str:
    return {
        "out": (
            "Записать текущее расхождение как ручной вывод? Ожидания БД скорректируются, "
            "алерт замолчит. Нажми кнопку ещё раз для подтверждения."
        ),
        "in": (
            "Записать излишек как ручное пополнение казны? "
            "Нажми кнопку ещё раз для подтверждения."
        ),
        "loss": (
            "⚠️ Пропажа средств: расхождение уйдёт в леджер, а игра ВСТАНЕТ НА ПАУЗУ — "
            "входящие переводы будут возвращаться с пометкой о техработах. "
            "Нажми кнопку ещё раз для подтверждения."
        ),
    }.get(action, "")


_ADJ_CONFIRM_WINDOW = 120.0


async def _apply_adjustment(bot, direction: str, amount_nanotons: int | None, note: str = "") -> str:
    """Записывает корректировку казны; «пропажа» дополнительно гасит игру."""
    from app.ops import (
        MANUAL_IN_KIND,
        MANUAL_OUT_KIND,
        record_manual_adjustment,
        treasury_expected_state,
    )

    kind_by_direction = {
        "out": MANUAL_OUT_KIND,
        "loss": MANUAL_OUT_KIND,
        "in": MANUAL_IN_KIND,
    }
    if direction not in kind_by_direction:
        return "Неизвестное действие сверки."
    async with SessionLocal() as session:
        if amount_nanotons is None:
            state = await treasury_expected_state(session)
            if state is None:
                return "Баланс казначея недоступен (индексаторы молчат) — попробуй позже."
            drift = state.drift_nanotons
            if abs(drift) <= state.tolerance_nanotons:
                return (
                    f"{ok_mark('clean')} Расхождений нет (в допуске "
                    f"±{state.tolerance_nanotons / 1e9:.4f} Gram) — корректировка не нужна."
                )
            amount_nanotons = abs(int(drift))
        row = await record_manual_adjustment(
            session, kind_by_direction[direction], int(amount_nanotons), note
        )
    labels = {
        "out": "Ручной вывод",
        "in": "Ручное пополнение",
        "loss": "Пропажа средств",
    }
    result = (
        f"{money_mark('adj')} <b>{labels[direction]}</b>: "
        f"{from_nano(row.amount_nanotons):.4f} Gram записано в леджер казны. "
        "Ожидания БД скорректированы — алерт сверки замолчит."
    )
    if direction == "loss":
        changed, delivered = await _set_paused_and_broadcast(bot, True, note or "пропажа средств")
        if changed:
            chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
            result += (
                f"\n⏸ Игра остановлена.{chats} Входящие переводы теперь возвращаются "
                "отправителям с пометкой о техработах. Снять паузу: /resume"
            )
        else:
            result += "\n⏸ Игра уже стояла на паузе."
    return result


@router.message(Command("adjust"), F.chat.type == ChatType.PRIVATE)
async def cmd_adjust(message: Message) -> None:
    """Сверка казны: меню разбора расхождения баланса с ожиданиями БД.

    Кнопки закрывают расхождение целиком; точная сумма — аргументами:
    /adjust 1.3569 out «вывод на биржу».
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    parts = (message.text or "").split()
    if len(parts) >= 3 and parts[2].lower() in {"out", "in", "loss"}:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            await message.answer(
                "Сумма не разобралась. Формат: <code>/adjust &lt;сумма&gt; out|in|loss [комментарий]</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if amount <= 0:
            await message.answer("Сумма должна быть положительной.")
            return
        note = " ".join(parts[3:]) if len(parts) > 3 else ""
        result = await _apply_adjustment(message.bot, parts[2].lower(), to_nano(amount), note)
        await message.answer(result, parse_mode=ParseMode.HTML)
        return
    await message.answer(
        await _adjust_menu_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_adjust_keyboard(),
    )


@router.callback_query(F.data.startswith("adj:"))
async def on_adjust_action(callback: CallbackQuery) -> None:
    """Кнопки сверки казны: первый тап предупреждает, второй — делает.

    Промежуточное подтверждение живёт в watcher_state (ключ adj:<admin_id>),
    а не в памяти процесса: два инстанса бота видят один и тот же ожидающий
    тап, и второй тап подтверждает даже на другой реплике.
    """
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Сверка только для хранителя.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action not in {"out", "in", "loss"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    confirm_key = f"adj:{callback.from_user.id}"
    async with SessionLocal() as session:
        row = await session.get(WatcherState, confirm_key)
        pending = json.loads(row.value) if row is not None else None
        now = datetime.now(UTC)
        ready = False
        if pending is not None and pending.get("action") == action:
            try:
                created = datetime.fromisoformat(pending["created_at"])
            except (KeyError, TypeError, ValueError):
                created = None
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                ready = (now - created).total_seconds() <= _ADJ_CONFIRM_WINDOW
        if not ready:
            payload = json.dumps(
                {"action": action, "created_at": now.isoformat()}, ensure_ascii=False
            )
            if row is None:
                session.add(WatcherState(key=confirm_key, value=payload))
            else:
                row.value = payload
            await session.commit()
            await callback.answer(_adjust_confirm_text(action), show_alert=True)
            return
        await session.delete(row)
        await session.commit()
    try:
        result = await _apply_adjustment(callback.bot, action, None)
    except Exception as exc:
        logger.exception("Корректировка казны %s не удалась", action)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)
        return
    if callback.message is not None:
        await callback.message.answer(result, parse_mode=ParseMode.HTML, reply_markup=None)
    await callback.answer("Записано.")


@router.message(Command("finalize"), F.chat.type == ChatType.PRIVATE)
async def cmd_finalize(message: Message) -> None:
    """Ручная финализация ставок застрявших дней: /finalize или /finalize 40"""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.stakes import finalize_day_payouts
    from app.ton_pay import dispatch_pending_payouts

    words = (message.text or "").split()
    target_day = int(words[1]) if len(words) > 1 and words[1].isdigit() else None

    # Сначала покажем состояние раунда(ов) для диагностики.
    async with SessionLocal() as session:
        if target_day is not None:
            rnd = await session.execute(
                select(Round).where(Round.day_index == target_day).order_by(Round.id.desc()).limit(1)
            )
            row = rnd.scalar_one_or_none()
            if row is None:
                await message.answer(f"День {target_day} не найден.")
                return
            status = row.status.value if hasattr(row.status, 'value') else str(row.status)
            fin = row.payouts_finalized
            pot = row.pot_nanotons
            wc = row.winner_card

            # Ставки
            stakes_q = await session.execute(
                select(Stake).where(Stake.round_id == row.id)
            )
            stakes = list(stakes_q.scalars().all())
            stakes_by_player = {s.player_id: s for s in stakes}

            # Голоса
            votes_q = await session.execute(
                select(Vote).where(Vote.round_id == row.id)
            )
            votes = list(votes_q.scalars().all())

            # Существующие выплаты
            payouts_q = await session.execute(
                select(Payout).where(Payout.round_id == row.id)
            )
            payouts = list(payouts_q.scalars().all())

            # Таблица голосов + ставок
            lines = [f"День {target_day} (Round#{row.id}): status={status}, finalized={fin}"]
            lines.append(f"Пот: {pot} нанотонов | Победившая карта: {wc}")
            lines.append("")
            lines.append("Голоса:")
            for v in votes:
                stake = stakes_by_player.get(v.player_id)
                stake_str = f"{stake.amount_nanotons}n ({stake.status})" if stake else "нет"
                winner_mark = " ✅ПОБЕДА" if v.card_position == wc else ""
                lines.append(f"  P{v.player_id}: карта {v.card_position}{winner_mark} | ставка: {stake_str}")

            # Анализ: кто выиграл
            winner_ids = {v.player_id for v in votes if v.card_position == wc}
            winning_stakes = [s for s in stakes if s.player_id in winner_ids and s.status == "confirmed"]
            lines.append("")
            if winning_stakes:
                total_prize = sum(s.amount_nanotons for s in winning_stakes)
                lines.append(f"Победители со ставкой: {len(winning_stakes)}, сумма ставок: {total_prize}n")
            else:
                lines.append("Победителей со ставкой: 0 — все ставки будут возвращены")
                if winner_ids:
                    lines.append(f"  (Угадали: {', '.join(f'P{pid}' for pid in winner_ids)}, но ставки не сделали)")

            # Выплаты
            if payouts:
                lines.append("")
                lines.append(f"Выплаты ({len(payouts)}):")
                for p in payouts:
                    lines.append(f"  id={p.id}: {p.kind} {p.status} {p.amount_nanotons}n → P{p.player_id}")

            await message.answer("\n".join(lines))
        # Теперь финализация
        q = select(Round).where(
            Round.status == RoundStatus.CLOSED,
            Round.payouts_finalized.is_(False),
        )
        if target_day is not None:
            q = q.where(Round.day_index == target_day)
        rounds = list((await session.execute(q)).scalars().all())
    if not rounds:
        await message.answer(f"{ok_mark('ok')} Незавершённых дней нет" + (f" (день {target_day} не найден или уже finalized)" if target_day else ""))
        return
    results = []
    for rnd in rounds:
        try:
            async with SessionLocal() as session:
                row = await session.get(Round, rnd.id)
                if row is None:
                    results.append(f"Round#{rnd.id}: не найден")
                    continue
                created = await finalize_day_payouts(session, row)
                results.append(f"День {row.day_index} (Round#{row.id}): создано выплат {created}")
        except Exception as exc:
            results.append(f"День {rnd.day_index} (Round#{rnd.id}): ОШИБКА — {exc!r}")
    # Отправляем
    try:
        sent = await dispatch_pending_payouts(bot=message.bot)
        results.append(f"Отправлено: {sent}")
    except Exception as exc:
        results.append(f"Ошибка отправки: {exc!r}")
    await message.answer("\n".join(results))


@router.message(Command("refinalize"), F.chat.type == ChatType.PRIVATE)
async def cmd_refinalize(message: Message) -> None:
    """Принудительная перефинализация: сбрасывает finalized, помечает старые
    невыполненные выплаты dismissed, пересоздаёт всё заново. /refinalize 1"""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return

    from app.stakes import finalize_day_payouts
    from app.ton_pay import dispatch_lock, dispatch_pending_payouts

    words = (message.text or "").split()
    if len(words) < 2 or not words[1].isdigit():
        await message.answer("Использование: /refinalize <день_номер>")
        return
    target_day = int(words[1])

    async with SessionLocal() as session:
        rnd = await session.execute(
            select(Round).where(Round.day_index == target_day).order_by(Round.id.desc()).limit(1)
        )
        row = rnd.scalar_one_or_none()
        if row is None:
            await message.answer(f"День {target_day} не найден.")
            return
        if row.status != RoundStatus.CLOSED:
            status = row.status.value if hasattr(row.status, 'value') else str(row.status)
            await message.answer(f"День {target_day}: статус={status}, нужен CLOSED.")
            return

        async with dispatch_lock():
            # Перефинализация может задвоить реальные деньги: строки sent уже
            # ушли в блокчейн (или в пути), и пересоздание создаст ИХ ПОВТОРНО.
            # Отказ внятным сообщением: пусть хранитель сам разберётся с уже
            # ушедшим (сверка /treasury, /adjust), а не плодит вторую выплату.
            sent_q = await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.round_id == row.id,
                    Payout.status == "sent",
                )
            )
            sent_count = int(sent_q.scalar_one())
            if sent_count:
                await message.answer(
                    f"Round#{row.id} (день {target_day}): перефинализация отменена — "
                    f"уже выполнено {sent_count} выплат (статус sent). Повторное "
                    "создание задвоило бы реальные переводы в блокчейне. "
                    "Разберись с ушедшим через /treasury или /adjust."
                )
                return
            stale_q = await session.execute(
                select(Payout).where(
                    Payout.round_id == row.id,
                    Payout.status.notin_(["sent"]),
                )
            )
            stale = list(stale_q.scalars().all())
            for p in stale:
                p.status = "dismissed"

            row.payouts_finalized = False
            await session.commit()
            dismissed = len(stale)

        await message.answer(
            f"Round#{row.id} (день {target_day}): finalized сброшен, "
            f"dismissed {dismissed} старых выплат. Запускаю финализацию..."
        )

    try:
        async with SessionLocal() as session:
            row = await session.get(Round, row.id)
            if row is None:
                await message.answer("Round не найден после сброса.")
                return
            created = await finalize_day_payouts(session, row)
            await message.answer(f"Создано выплат: {created}")
    except Exception as exc:
        await message.answer(f"Ошибка финализации: {exc!r}")
        return

    try:
        sent = await dispatch_pending_payouts(bot=message.bot)
        await message.answer(f"Отправлено выплат: {sent}")
    except Exception as exc:
        await message.answer(f"Ошибка отправки: {exc!r}")


@router.message(Command("pause"), F.chat.type == ChatType.PRIVATE)
async def cmd_pause(message: Message) -> None:
    """Стоп-кран: дни замирают, входящие переводы автоматически возвращаются."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    words = (message.text or "").split(maxsplit=1)
    reason = words[1].strip()[:200] if len(words) > 1 else "идут технические работы"
    changed, delivered = await _set_paused_and_broadcast(message.bot, True, reason)
    if not changed:
        await message.answer(f"{warn_mark('pause')} Игра уже на паузе. Снять: /resume")
        return
    chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
    await message.answer(
        f"{ok_mark('pause')} Игра остановлена: {reason}.{chats}\n"
        "Дни не открываются, watcher каждый входящий перевод возвращает отправителю "
        "(в комментарии — «техработы»). Очередь выплат продолжает разгребаться.\n"
        "Снять паузу: /resume"
    )


@router.message(Command("resume"), F.chat.type == ChatType.PRIVATE)
async def cmd_resume(message: Message) -> None:
    """Снимает стоп-кран: следующий тик откроет новый день сам."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    changed, delivered = await _set_paused_and_broadcast(message.bot, False)
    if not changed:
        await message.answer("Игра и так идёт.")
        return
    chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
    await message.answer(
        f"{ok_mark('go')} Пауза снята.{chats} Новый день откроется в ближайший тик (до минуты)."
    )
