"""Рассылка анонсов дней и итогов в чаты, где бот состоит администратором."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.models import Chat, Round, RoundStatus, StatusPost
from app.style import day_mark
from app.tally import format_results

logger = logging.getLogger(__name__)

POSITIONS = ("I", "II", "III")
_MAX_TEXT_LEN = 3900
_TITLE_CLAMP = 80
_FORGET_MARKS = ("forbidden", "not found", "kicked", "deactivated", "migrated")


def cards_keyboard(round_id: int, remember: bool = False, day_index: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Сцена I", callback_data=f"vote:{round_id}:0"),
            InlineKeyboardButton(text="Сцена II", callback_data=f"vote:{round_id}:1"),
            InlineKeyboardButton(text="Сцена III", callback_data=f"vote:{round_id}:2"),
        ],
    ]
    if remember:
        # Кнопка памяти живёт только в дни, когда в главу реально всплыло эхо.
        # Кодируем и PK раунда (для отметки MemoryHit), и его day_index (для
        # поиска всплывших эхо) — после /resetgame id уже не равен day_index.
        if day_index is None:
            day_index = round_id
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧠 Я помню этот след",
                    callback_data=f"remember:{round_id}:{day_index}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clamp(text: str, limit: int) -> str:
    """Обрезка по словам, чтобы служебные строки не вытеснялись из поста."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    tail = cut.rsplit(" ", 1)
    if len(tail) == 2:
        cut = tail[0]
    return cut.rstrip(" ,.;:") + "…"


def _utc(value: datetime) -> datetime:
    return value if getattr(value, "tzinfo", None) else value.replace(tzinfo=UTC)


async def status_text(round_row: Round, *, show_title: bool = True) -> str:
    from app.models import RULE_PHRASES, VOTE_RULE_PHRASES

    if round_row.status == RoundStatus.OPEN:
        stake_mode = (
            settings.ton_enabled
            and getattr(round_row, "money_mode", True) is not False
        )
        if stake_mode:
            phase = (
                f"🎬 Сцена дня: {RULE_PHRASES[round_row.win_rule]}. "
                "Кадр дня решает счёт Gram — банки сцен скрыты до конца сцены. "
                "Голос без ставки ведёт только лидерборд."
            )
        else:
            phase = (
                f"🎬 Сцена дня: {VOTE_RULE_PHRASES[round_row.win_rule]}. "
                "Счёт сцен скрыт до конца сцены."
            )
    elif round_row.status == RoundStatus.TALLYING:
        phase = "⏳ Подсчёт: итоги через мгновение."
    else:
        phase = "🌙 День закрыт."
    # Пути голосования читаются словами: заголовок + суть каждого.
    # (Раньше описания жили в подписях трёх фото-карт — генерацию карт
    # убрали, и текст снова стал носителем смысла развилки.)
    # Компактный профиль: контракт кассеты просит карту не длиннее 260 знаков
    # (поле description, схема ≤260), а показ здесь даёт ровно этот задел —
    # текст развилки не режется многоточием.
    cards = "\n".join(
        f"{POSITIONS[card.position]}. {_clamp(card.title, 80)} — {_clamp(card.description, 260)}"
        for card in sorted(round_row.cards, key=lambda item: item.position)
    )
    bank_line = ""
    if settings.ton_enabled and getattr(round_row, "money_mode", True) is not False:
        from app.db import SessionLocal
        from app.rounds import round_pot

        async with SessionLocal() as db:
            nano, _bets = await round_pot(db, round_row.id)
        bank_line = f"\n💰 Банк дня: {nano / 1e9:.2f} Gram"
    # Бесшовные сутки: подсчёт мгновенный, оба времени совпадают — хватит
    # одного дедлайна. Легаси-раунды с зазором показывают обе строки.
    voting_at = _utc(round_row.voting_ends_at)
    tally_at = _utc(round_row.tally_ends_at)
    if tally_at - voting_at > timedelta(minutes=5):
        deadline = (
            f"🗳 Голосование до: {voting_at:%H:%M} UTC · "
            f"🏁 Итоги и новый день: {tally_at:%H:%M} UTC"
        )
    else:
        deadline = f"🗳 Голосование до {voting_at:%H:%M} UTC — итоги и новый день придут сразу после"
    head = ""
    if show_title:
        head += f"{day_mark(str(round_row.id))} {_clamp(round_row.chapter_title, _TITLE_CLAMP)}\n\n"
    # Глава кассеты живым текстом между заголовком и развилкой: сначала стая
    # слышит день, потом видит три сцены. Жёсткий потолок кассеты — 700 знаков
    # (schema.py), обрезка по словам ниже лишь страхует легаси-раунды без кассеты.
    story = (
        f"{_clamp(round_row.chapter_text, 1500)}\n\n"
        if getattr(round_row, "chapter_text", "")
        else ""
    )
    # Хвост поста (правило дня, банк, дедлайн) неприкосновенен: при упоре в
    # потолок режется «верх», а не обещание игроку сроков исхода голосования.
    tail = f"\n\n{phase}{bank_line}\n{deadline}"
    core = f"{head}{story}{cards}"
    budget = _MAX_TEXT_LEN - len(tail)
    if len(core) > budget:
        core = _clamp(core, budget)
    return core + tail


async def active_chat_ids() -> list[int]:
    async with SessionLocal() as session:
        rows = await session.execute(select(Chat.id).where(Chat.active.is_(True)))
        return [row[0] for row in rows.all()]


async def deactivate_chat(chat_id: int) -> None:
    async with SessionLocal() as session:
        row = await session.get(Chat, chat_id)
        if row is not None and row.active:
            row.active = False
            await session.commit()
            logger.info("Чат %s помечен неактивным", chat_id)


async def active_player_ids() -> list[int]:
    """Игроки, подписанные на личные дубликаты рассылок (/start → dm_subscribed)."""
    from app.models import Player

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Player.id).where(Player.dm_subscribed.is_(True))
        )
        return [row[0] for row in rows.all()]


async def _dm_send_all(bot: Bot, deliver, label: str) -> int:
    """Рассылка одного сообщения всем подписанным игрокам в личку.

    Промахи не критичны: бот не имеет права писать тем, кто его не начинал
    (forbidden) — их молча пропускаем, как в личном эхе. Возвращает число
    доставленных сообщений.
    """
    if bot is None or not settings.player_dm:
        return 0
    player_ids = await active_player_ids()
    if not player_ids:
        return 0
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(player_id: int) -> bool:
        async with semaphore:
            try:
                await deliver(player_id)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await deliver(player_id)
                    return True
                except Exception as exc2:
                    logger.warning("Игроку %s сообщение не доставлено (после ретрая): %s", player_id, exc2)
                    return False
            except Exception as exc:
                logger.warning("Игроку %s сообщение не доставлено: %s", player_id, exc)
                return False

    outcomes = await asyncio.gather(*(worker(pid) for pid in player_ids))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info("%s: доставлено %d из %d игроков", label, delivered, len(player_ids))
    return delivered


async def results_body(finished: Round, session=None) -> str:
    """Сухие итоги + экономика дня + плагины (БЕЗ нейро-эпилога).

    Быстрая, только БД — это то, что уходит пользователям СРАЗУ после вскрытия
    итогов, пока эпилог ещё пишется нейросетью. session можно передать готовую.
    """
    from app.tally import day_economics, format_economics, format_plugin_results

    # format_results читает .cards синхронно; асинхронная ленивая подгрузка вне
    # greenlet-контекста дала бы MissingGreenlet и сорвала бы весь пост итогов.
    # Грузим карты заранее (selectinload), из своей сессии, если её не передали.
    try:
        _stmt = select(Round).where(Round.id == finished.id).options(selectinload(Round.cards))
        if session is None:
            async with SessionLocal() as _own:
                _loaded = (await _own.execute(_stmt)).scalar_one_or_none()
        else:
            _loaded = (await session.execute(_stmt)).scalar_one_or_none()
        if _loaded is not None:
            finished = _loaded
    except Exception:
        logger.warning("Карты дня %s не подгружены для итогов", getattr(finished, "day_index", "?"), exc_info=True)

    # Ставки по путям и коэффициент считается один раз — и в реальной рассылке
    # (announce_new_day зовёт results_message БЕЗ сессии), поэтому открываем свою.
    # Без этого пути и коэффициент в итогах просто не появлялись.
    economics_stats: dict | None = None
    try:
        if session is not None:
            economics_stats = await day_economics(session, finished)
        else:
            economics_stats = await _economics_own_session(finished)
    except Exception:
        logger.warning("Экономика дня %s не собрана — пост без коэффициента", getattr(finished, "day_index", "?"), exc_info=True)

    path_stakes = (economics_stats or {}).get("path_stakes", {})
    multiplier = (economics_stats or {}).get("multiplier")

    text = format_results(finished, path_stakes, multiplier)
    if economics_stats is not None:
        try:
            economics = format_economics(economics_stats)
            if economics:
                text += f"\n\n{economics}"
        except Exception:
            logger.exception("Экономика дня %s не посчитана", getattr(finished, "day_index", "?"))
    # Запись дневника кассеты (ПОВ-контраст): читается из активной кассеты
    # месяца по дате и дню закрытого раунда. Нет кассеты / нет поля / сбой —
    # дневника нет, сухие итоги не зависят от сюжетного слоя (fail-open).
    try:
        from app.story import bay as story_bay

        if session is not None:
            diary = await story_bay.day_diary(session, finished)
        else:
            async with SessionLocal() as _diary_db:
                diary = await story_bay.day_diary(_diary_db, finished)
        if diary:
            text += f"\n\n📖 {html.escape(diary)}"
    except Exception:
        logger.debug("Дневник дня не добавлен в итоги", exc_info=True)
    # Плагиновые строки итогов (echoes, relations, bestiary и т.д.)
    try:
        plugin_text = await format_plugin_results(finished, session)
        if plugin_text:
            text += f"\n\n{plugin_text}"
    except Exception:
        logger.debug("Plugin results format не собран", exc_info=True)
    return text


async def results_message(finished: Round, session=None) -> str:
    """Полные итоги дня: сухой блок + экономика + эпилог (если готов).

    Эпилог — текст сюжетного слоя с разметкой от нейросети; в HTML-пост он
    попадает экранированным целиком (это простой текст, своих тегов нет).

    session можно передать готовую (тесты, вызовы внутри транзакции);
    иначе открывается своя краткоживущая сессия.
    """
    text = await results_body(finished, session)
    epilogue = getattr(finished, "epilogue_text", "") or ""
    if epilogue:
        text += f"\n\n{html.escape(epilogue)}"
    return text


async def _economics_own_session(row: Round) -> dict:
    from app.tally import day_economics

    async with SessionLocal() as own:
        return await day_economics(own, row)


_BROADCAST_PARALLELISM = 8


async def _deliver_day(
    bot: Bot,
    chat_id: int,
    round_row: Round,
    finished: Round | None,
    results_text: str | None = None,
    remember: bool = False,
    is_dm: bool = False,
) -> None:
    """Полный пакет дня в один чат. Итоги передаются готовым текстом:
    экономика дня считается один раз на рассылку, а не на каждый чат."""
    if finished is not None:
        if results_text is None:
            results_text = await results_message(finished)
        # Итоги дня — только текстом. Фото победившей ветки не постим: это был
        # дубль обложки нового дня, а вечерний костёр уже дал отдельный кадр.
        # HTML: строка правила дня несёт жирные блоки.
        if results_text:
            await bot.send_message(chat_id, results_text, parse_mode=ParseMode.HTML)
    # Медиа дня нет: build_day_post() всегда возвращал пустой список, поэтому
    # ветки send_photo/send_media_group были недостижимы. Картинки вернутся
    # вместе с реальной генерацией — тогда понадобится и send_media_group
    # (Telegram принимает его только от двух вложений, см. историю 2dfc1a).
    sent = await bot.send_message(
        chat_id,
        await status_text(round_row, show_title=True),
        parse_mode=ParseMode.HTML,
        reply_markup=cards_keyboard(round_row.id, remember=remember, day_index=round_row.day_index),
    )
    # Запоминаем, куда ушёл пост-статус дня: когда watcher подтвердит новые
    # ставки, refresh_day_bank отредактирует этот пост с актуальным банком —
    # без повторного /today. Точку доставки пишем защищённо: тесты и легаси
    # вызовы без реального сообщения/раунда не должны ронять рассылку дня.
    msg_id = getattr(sent, "message_id", 0)
    if msg_id and isinstance(getattr(round_row, "id", None), int):
        await remember_day_post(round_row.id, chat_id, msg_id, is_dm=is_dm)


async def remember_day_post(round_id: int, chat_id: int, message_id: int, *, is_dm: bool) -> None:
    """Записать/обновить точку доставки поста-статуса дня (upsert).

    Повторный анонс того же дня (ретрай после флуд-контроля) просто
    переписывает message_id; обнулённый last_pot_nanotons означает, что свежий
    пост ещё не сверен с банком и refresh правил его не пропустит.
    """
    try:
        async with SessionLocal() as session:
            # Посты прошлых дней заморожены (банк не меняется) — точка доставки
            # нового дня вытесняет устаревшие. От своих однораундовых строк не
            # избавляемся: их правит refresh по мере роста банка этого дня.
            from sqlalchemy import delete

            await session.execute(delete(StatusPost).where(StatusPost.round_id != round_id))
            row = (
                await session.execute(
                    select(StatusPost).where(
                        StatusPost.round_id == round_id,
                        StatusPost.chat_id == chat_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    StatusPost(
                        round_id=round_id,
                        chat_id=chat_id,
                        message_id=int(message_id),
                        is_dm=is_dm,
                    )
                )
            else:
                row.message_id = int(message_id)
                row.is_dm = is_dm
                row.last_pot_nanotons = None
            await session.commit()
    except Exception:
        logger.warning(
            "Точка доставки поста дня не записана (round=%s chat=%s):",
            round_id,
            chat_id,
            exc_info=True,
        )


async def refresh_day_bank(bot: Bot | None = None) -> None:
    """Актуализировать банк дня в УЖЕ отправленных постах-статусах.

    Пост дня уходит один раз при анонсе — с суммой подтверждённых ставок на тот
    момент. Дальше банк живёт своей жизнью (watcher подтверждает новые ставки),
    а текст поста замирает: игроки видят устаревший счёт, пока не позовут /today.
    Здесь правим только посты ОТКРЫТОГО раунда и только в тех чатах, где число
    подтверждённого банка МЕНЯЛОСЬ (last_pot_nanotons) — без правок-простыней
    на каждый тик. Точки доставки прошлых дней (закрытые/подсчёт) вычищаются:
    их банк заморожен, редактировать нечего.
    """
    if bot is None:
        return
    from app.rounds import get_active_round, round_pot

    async with SessionLocal() as session:
        current = await get_active_round(session)
        if current is None or current.status != RoundStatus.OPEN:
            return
        if not (settings.ton_enabled and getattr(current, "money_mode", True) is not False):
            return
        nano, _bets = await round_pot(session, current.id)
        rows = (
            (
                await session.execute(
                    select(StatusPost).where(
                        StatusPost.round_id == current.id,
                        (
                            (StatusPost.last_pot_nanotons.is_(None))
                            | (StatusPost.last_pot_nanotons != nano)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        loaded = (
            await session.execute(
                select(Round).where(Round.id == current.id).options(selectinload(Round.cards))
            )
        ).scalar_one_or_none()
        if loaded is None:
            return
        text = await status_text(loaded, show_title=True)
        keyboard = cards_keyboard(loaded.id, remember=False, day_index=loaded.day_index)

        for row in rows:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=row.chat_id,
                    message_id=row.message_id,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                row.last_pot_nanotons = nano
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=row.chat_id,
                        message_id=row.message_id,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                    row.last_pot_nanotons = nano
                except Exception as exc2:
                    logger.warning(
                        "Правка банка дня не удалась после ретрая (chat=%s): %s",
                        row.chat_id,
                        exc2,
                    )
                    await session.delete(row)
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if "not modified" not in lowered:
                    # Пост/чат исчезли (удалено, бот выгнан) — точка доставки мертва.
                    await session.delete(row)
                    continue
                # Текст совпал (пост уже с этим банком) — фиксируем счёт, правка не нужна.
                row.last_pot_nanotons = nano
            except TelegramForbiddenError:
                # Бот выгнан из чата: точка доставки мертва, дедуп не спасёт.
                await session.delete(row)
            except Exception as exc:
                logger.warning(
                    "Правка банка дня не удалась (round=%s chat=%s): %s",
                    current.id,
                    row.chat_id,
                    exc,
                )
        await session.commit()


async def _deliver_chat(
    bot: Bot,
    chat_id: int,
    round_row: Round,
    finished: Round | None,
    results_text: str | None,
    remember: bool = False,
) -> int | None:
    """Доставка в чат с одним ретраем после флуд-контроля. None — неудача."""
    try:
        await _deliver_day(bot, chat_id, round_row, finished, results_text, remember=remember)
        return chat_id
    except TelegramRetryAfter as exc:
        logger.warning("Флуд-контроль в чате %s: пауза %d с", chat_id, exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        # Повтор тоже может не пройти (флуд не прошёл и с паузой). Без try
        # исключение улетало из worker'а в gather и отменяло рассылку дня
        # ВООБЩЕ — из-за одного болтливого чата. Ответ симметричен ветке ниже.
        try:
            await _deliver_day(bot, chat_id, round_row, finished, results_text, remember=remember)
            return chat_id
        except Exception as exc2:
            logger.warning("Анонс дня в чат %s не доставлен (после ретрая): %s", chat_id, exc2)
            return None
    except TelegramForbiddenError:
        await deactivate_chat(chat_id)
        return None
    except Exception as exc:
        logger.warning(
            "Анонс дня %s не доставлен в чат %s: %s", round_row.day_index, chat_id, exc
        )
        lowered = str(exc).lower()
        if any(mark in lowered for mark in _FORGET_MARKS):
            await deactivate_chat(chat_id)
        return None


async def announce_new_day(
    bot: Bot | None,
    round_row: Round,
    finished: Round | None = None,
) -> list[int]:
    """Обложка и карты НОВОГО дня; итоги прошлого дня — если передан finished.

    В автопереходе итоги постятся отдельно (announce_results) сразу после
    вскрытия, а сюда новый день передаётся без finished — чтобы не дублировать
    итоги. Ручной /advance по-прежнему передаёт finished и постит всё вместе.

    Чаты доставляются параллельно ограниченным пулом: последовательная
    рассылка (~7 сообщений и 4 аплоада на чат) упирается в часы уже на
    сотнях чатов. Возвращает список чатов, куда рассылка прошла успешно.
    """
    if bot is None:
        return []
    chat_ids = await active_chat_ids()
    results_text = await results_message(finished) if finished is not None else None
    # Кнопка памяти снята вместе со слоем сюжета (эхо-система удалена).
    remember = False
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> int | None:
        async with semaphore:
            return await _deliver_chat(bot, chat_id, round_row, finished, results_text, remember=remember)

    outcomes = await asyncio.gather(*(worker(chat_id) for chat_id in chat_ids))
    delivered = [chat_id for chat_id in outcomes if chat_id is not None]
    logger.info(
        "Анонс дня %s разослан: доставлено %d из %d чатов",
        round_row.day_index,
        len(delivered),
        len(chat_ids),
    )
    # Личные дубликаты подписчикам: тот же пакет дня (итоги, обложка, кнопки
    # выбора) в личку. Без /start у игрока бот писать не может — такие молча
    # пропускаются; кнопки голосования работают из лички, как и из группы.
    if settings.player_dm:
        delivered_dm = await _dm_send_all(
            bot,
            lambda pid: _deliver_day(
                bot, pid, round_row, finished, results_text, remember=remember, is_dm=True
            ),
            f"Личный пакет дня {round_row.day_index}",
        )
        if delivered_dm:
            logger.info(
                "Личный пакет дня %s доставлен %d игроку(ам)",
                round_row.day_index,
                delivered_dm,
            )
    return delivered


async def _broadcast_text(
    bot: Bot, text: str, parse_mode: ParseMode | None = None
) -> int:
    """Одно текстовое сообщение во все живые чаты с одним ретраем и флуд-контролем.

    Плюс — личные дубликаты подписчикам (итоги, эпилог, анонсы пауз/церемоний).
    parse_mode — формат разметки: итоги дня несут HTML-ссылку на блок закона.
    Возвращает число доставленных чатов; провалы не критичны.
    """
    if not text.strip():
        return 0
    chat_ids = await active_chat_ids()
    if not chat_ids:
        logger.warning("_broadcast_text: нет активных чатов для рассылки (все деактивированы?)")
    if chat_ids:
        semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

        async def worker(chat_id: int) -> int | None:
            async with semaphore:
                try:
                    await bot.send_message(chat_id, text, parse_mode=parse_mode)
                    return chat_id
                except TelegramRetryAfter as exc:
                    logger.warning("Флуд-контроль в чате %s: пауза %d с", chat_id, exc.retry_after)
                    await asyncio.sleep(exc.retry_after + 1)
                    try:
                        await bot.send_message(chat_id, text, parse_mode=parse_mode)
                        return chat_id
                    except Exception as exc2:
                        logger.warning("Текст не доставлен в чат %s (после ретрая): %s", chat_id, exc2)
                        return None
                except TelegramForbiddenError:
                    await deactivate_chat(chat_id)
                    return None
                except Exception as exc:
                    logger.warning("Текст не доставлен в чат %s: %s", chat_id, exc)
                    if any(mark in str(exc).lower() for mark in _FORGET_MARKS):
                        await deactivate_chat(chat_id)
                    return None

        outcomes = await asyncio.gather(*(worker(chat_id) for chat_id in chat_ids))
        delivered = len([c for c in outcomes if c is not None])
    else:
        delivered = 0
    # Личные дубликаты подписчикам — даже если живых чатов нет.
    delivered_dm = await _dm_send_all(
        bot,
        lambda pid: bot.send_message(pid, text, parse_mode=parse_mode),
        "Личный текст",
    )
    return delivered + delivered_dm


async def announce_results(bot: Bot | None, finished: Round) -> int:
    """Постит СРАЗУ только итоги прошлого дня (без нейро-эпилога и без нового дня).

    Отделено от announce_new_day, чтобы итоги уходили пользователям немедленно
    после вскрытия, не дожидаясь нейро-контента нового дня. Возвращает число
    доставленных чатов.
    """
    if bot is None:
        return 0
    # СБОРКА ИТОГОВ НЕ ГЛОТАЕТСЯ. Раньше здесь был except -> text = "" ->
    # return 0: исключение наружу не уходило, поэтому откат в джобе не
    # срабатывал, коммит фиксировал results_at — и восстановитель (CLOSED &&
    # results_at IS NULL) этот день больше никогда не видел. Итоги — то, по чему
    # игрок узнаёт победителя и судьбу своей ставки; потерять их молча нельзя.
    # Оба вызывающих (_announce_results_job и _retry_results_job) уже откатывают
    # транзакцию и оставляют день без маркера, так что он уйдёт следующим тиком.
    text = await results_body(finished)
    if not text:
        return 0
    delivered = await _broadcast_text(bot, text, parse_mode=ParseMode.HTML)
    logger.info("Итоги дня %s разосланы: доставлено %d чатов", getattr(finished, "day_index", "?"), delivered)
    return delivered


def scene_label(cards: dict[int, str], position: int) -> str:
    """Короткая подпись сцены: «I. «Вскрыть крышу»» — и в алерты, и в личные итоги."""
    title = cards.get(position, "")
    label = POSITIONS[position] if position < len(POSITIONS) else str(position + 1)
    if title:
        return f"{label}. «{html.escape(title)}»"
    return label


def _build_player_result_text(
    round_row: Round,
    cards: dict[int, str],
    position: int,
    stake=None,
    payouts: list | None = None,
) -> str:
    """Персональный текст «за что голосовал и чем кончилось» для одного игрока.

    Общий пост итогов не отвечает на вопрос «а я за что голосовал и выиграл ли»:
    нужна строка про КОНКРЕТНЫЙ выбор игрока. Здесь: сцена дня, выбор игрока,
    исход, судьба ставки (если день денежный).
    """
    from app.ton_utils import from_nano

    payouts = payouts or []
    won = position == round_row.winner_card
    lines = [
        f"📼 День {round_row.day_index} — твой итог",
        "",
        f"🏆 Сцена дня: {scene_label(cards, round_row.winner_card or 0)}",
        f"🎯 Ты выбрал: {scene_label(cards, position)}",
        "",
    ]
    if won:
        lines.append("🎉 Ты угадал сцену дня!")
    else:
        lines.append("Твоя сцена не победила — но голос учтён в лидерборде.")
    money = ""
    if settings.ton_enabled and getattr(round_row, "money_mode", True) is not False:
        if stake is not None:
            stake_g = f"{from_nano(stake.amount_nanotons):g}"
            prize = sum(p.amount_nanotons for p in payouts if p.kind == "prize")
            refund = max((p.amount_nanotons for p in payouts if p.kind == "refund"), default=0)
            if prize > 0:
                money = f"💰 Ставка {stake_g} Gram в выигрыш: +{from_nano(prize):g} Gram (перевод уже в очереди)."
            elif refund > 0:
                money = f"💰 Ставка {stake_g} Gram возвращается: {from_nano(refund):g} Gram (минус газ сети) — перевод в очереди."
            elif stake.status == "confirmed":
                money = f"💰 Ставка {stake_g} Gram принята в банк дня."
        else:
            money = "💸 Ставки в этот день не было — выбор шёл голосом."
        if money:
            lines.extend(["", money])
    return "\n".join(lines)


async def _player_result_texts(finished: Round) -> list[tuple[int, str]]:
    """Персональные итоги для каждого проголосовавшего подписчика: (player_id, текст).

    Только dm_subscribed: тем, кто выключил личную рассылку, личный итог не
    лезем. Карты дня перечитываем с selectinload, чтобы названия сцен были.
    """
    from app.models import Player, Stake, Vote

    async with SessionLocal() as session:
        loaded = (
            await session.execute(
                select(Round)
                .where(Round.id == finished.id)
                .options(selectinload(Round.cards))
            )
        ).scalar_one_or_none()
        if loaded is None:
            return []
        cards = {card.position: card.title for card in loaded.cards}
        votes = (
            (
                await session.execute(
                    select(Vote)
                    .join(Player, Player.id == Vote.player_id)
                    .where(
                        Vote.round_id == finished.id,
                        Player.dm_subscribed.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        positions = {vote.player_id: vote.card_position for vote in votes}
        if not positions:
            return []
        stakes = {
            stake.player_id: stake
            for stake in (
                await session.execute(select(Stake).where(Stake.round_id == finished.id))
            )
            .scalars()
            .all()
        }
        from app.models import Payout

        payouts: dict[int, list] = {}
        for payout in (
            await session.execute(select(Payout).where(Payout.round_id == finished.id))
        ).scalars().all():
            payouts.setdefault(payout.player_id, []).append(payout)
        return [
            (
                player_id,
                _build_player_result_text(
                    loaded,
                    cards,
                    positions[player_id],
                    stakes.get(player_id),
                    payouts.get(player_id, []),
                ),
            )
            for player_id in sorted(positions)
        ]


async def announce_player_results(bot: Bot | None, finished: Round) -> int:
    """Личные итоги дня каждому проголосовавшему подписчику (мой выбор → исход).

    Дополняет групповой пост итогов: игрок видит, за какую сцену голосовал и
    чем она кончилась для его ставки. Своя рассылка с флуд-контролем; провал
    одному игроку не срывает остальных. Возвращает число доставленных.
    """
    if bot is None or not settings.player_dm:
        return 0
    try:
        texts = await _player_result_texts(finished)
    except Exception:
        logger.exception(
            "Персональные итоги дня %s не собраны",
            getattr(finished, "day_index", "?"),
        )
        return 0
    if not texts:
        return 0
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(player_id: int, text: str) -> bool:
        async with semaphore:
            try:
                await bot.send_message(player_id, text, parse_mode=ParseMode.HTML)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(player_id, text, parse_mode=ParseMode.HTML)
                    return True
                except Exception as exc2:
                    logger.warning(
                        "Личный итог дня не доставлен игроку %s (после ретрая): %s",
                        player_id,
                        exc2,
                    )
                    return False
            except Exception as exc:
                logger.warning("Личный итог дня не доставлен игроку %s: %s", player_id, exc)
                return False

    outcomes = await asyncio.gather(*(worker(pid, text) for pid, text in texts))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info(
        "Личные итоги дня %s: доставлено %d из %d игроков",
        getattr(finished, "day_index", "?"),
        delivered,
        len(texts),
    )
    return delivered


async def whisper_to_chats(bot: Bot | None, text: str) -> int:
    """Полуденный шёпот мира: короткое сообщение во все живые чаты.

    Возвращает число доставленных чатов; провалы не критичны по определению.
    """
    if bot is None or not text:
        return 0
    chat_ids = await active_chat_ids()
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> bool:
        async with semaphore:
            try:
                await bot.send_message(chat_id, text)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(chat_id, text)
                    return True
                except Exception as exc2:
                    logger.warning("Шёпот дня не доставлен в чат %s (после ретрая): %s", chat_id, exc2)
                    return False
            except TelegramForbiddenError:
                await deactivate_chat(chat_id)
                return False
            except Exception as exc:
                lowered = str(exc).lower()
                if any(mark in lowered for mark in _FORGET_MARKS):
                    await deactivate_chat(chat_id)
                return False

    outcomes = await asyncio.gather(*(worker(c) for c in chat_ids))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info("Шёпот дня разослан в %d из %d чатов", delivered, len(chat_ids))
    # Вечерний привал — и в личку подписчикам (личный дубликат вечернего поста).
    delivered_dm = await _dm_send_all(
        bot, lambda pid: bot.send_message(pid, text), "Личный шёпот (текст)"
    )
    return delivered + delivered_dm


# Личное эхо проигравшим отключено: слой сюжета снят, эхо-система удалена.
