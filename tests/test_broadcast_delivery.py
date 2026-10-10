"""Доставка анонсов: флуд-контроль, забытые чаты и отказы отдельных слоёв.

Рассылка дня — единственный способ узнать о новом дне, поэтому её не имеет
права отменять ни один чат. Здесь закрыты пути, где раньше не было тестов:
повтор после RetryAfter (и его провал), одно вложение вместо медиа-группы,
забытый чат по марке Telegram, и все четыре «мягких отказа» в results_body —
каждый из них обязан оставить пост читаемым, а не оборвать его на середине.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import delete, select

from app import broadcast as bc
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Chat, Player, Round, RoundStatus, WatcherState, WinRule


def _round(day_index: int = 9300) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День доставки",
        chapter_text="Текст.",
        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
        vote_counts_json='{"0": 3, "1": 1, "2": 0}',
        stake_counts_json='{"0": 1000000000}',
        winner_card=0,
    )
    for position in range(3):
        round_row.cards.append(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"Путь {position}",
                consequence="канон",
                tag="care",
            )
        )
    return round_row


async def _wipe(*tables) -> None:
    async with SessionLocal() as db:
        for table in tables:
            await db.execute(delete(table))
        await db.commit()


def _flaky_bot(failures: dict[int, list[Exception]]) -> SimpleNamespace:
    """Бот, у которого для чата заготовлена очередь ошибок: первый вызов
    падает, второй (после паузы) — нет."""
    calls: dict[int, int] = {}

    async def sender(chat_id, *_args, **_kwargs):
        queue = failures.get(chat_id, [])
        attempt = calls.get(chat_id, 0)
        calls[chat_id] = attempt + 1
        if attempt < len(queue):
            raise queue[attempt]
        return SimpleNamespace()

    return SimpleNamespace(
        send_message=AsyncMock(side_effect=sender),
        send_photo=AsyncMock(side_effect=sender),
        send_media_group=AsyncMock(side_effect=sender),
    )


def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(bc.asyncio, "sleep", AsyncMock())


async def test_deliver_day_does_not_repeat_results_on_status_flood(monkeypatch) -> None:
    """Флуд на статусе не должен отправлять итоги дня второй раз.

    Пакет дня — это ДВА сообщения: итоги прошлого дня и пост нового с кнопками.
    Ретрай раньше стоял над всем пакетом, поэтому флуд-контроль на втором
    сообщении заставлял отправить первое заново: игрок видел итоги дважды —
    счёт, победивший путь и судьбу своей ставки два раза подряд.

    Ретрай должен быть на уровне сообщения, а не пакета.
    """
    _no_sleep(monkeypatch)
    await _wipe(Chat)
    chat = 777_621
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="group", active=True))
        await db.commit()
    try:
        round_row = _round(9406)
        finished = _round(9407)
        finished.status = RoundStatus.CLOSED
        sent_texts: list[str] = []

        async def sender(_chat_id, text=None, **_kwargs):
            sent_texts.append(text)
            # Флуд ровно на втором сообщении пакета — на посте дня.
            if len(sent_texts) == 2:
                raise TelegramRetryAfter(None, "flood", retry_after=1)
            return SimpleNamespace(message_id=42)

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=sender))
        results = "ИТОГИ ДНЯ"
        assert await bc._deliver_day(bot, chat, round_row, finished, results_text=results)
        # Итоги ушли ровно один раз; пост дня отбит флудом и отправлен повторно.
        assert sent_texts.count(results) == 1, sent_texts
        assert len(sent_texts) == 3, sent_texts
        assert sent_texts[0] == results, sent_texts
        assert sent_texts[1] == sent_texts[2], "повтор поста дня должен быть тем же текстом"
    finally:
        await _wipe(Chat)


# ── обрезка текстов и клавиатура ────────────────────────────────────────────


def test_clamp_cuts_by_word_and_keeps_short_text() -> None:
    """Обрезка по словам: обрывок слова в конце поста выглядит как опечатка."""
    assert bc._clamp("коротко", 80) == "коротко"
    long_text = "слово " * 60
    clamped = bc._clamp(long_text, 50)
    assert len(clamped) <= 51
    assert clamped.endswith("…")
    assert " " not in clamped[-2:]


def test_clamp_handles_text_without_spaces() -> None:
    """Строка без пробелов (хеш, адрес) обрезается по границе лимита."""
    clamped = bc._clamp("0" * 100, 10)
    assert clamped == "0" * 10 + "…"


def _cards() -> list[SimpleNamespace]:
    return [SimpleNamespace(position=position, title=f"Путь {position}") for position in range(3)]


def test_cards_keyboard_puts_each_vote_button_in_its_own_row() -> None:
    """Три кнопки голосования идут столбиком, по одной в строке: на мобиле
    длинные названия сцен не сжимаются, как в одной строке из трёх.
    callback_data по-прежнему несёт position для отметки голоса."""
    rows = bc.cards_keyboard(7, cards=_cards()).inline_keyboard
    assert [row[0].callback_data for row in rows] == [
        "vote:7:0",
        "vote:7:1",
        "vote:7:2",
    ]
    # Ровно три строки, в каждой — ровно одна кнопка.
    assert len(rows) == 3
    for row in rows:
        assert len(row) == 1


def test_cards_keyboard_signs_buttons_with_scene_titles() -> None:
    """Подписи кнопок — названия сцен из кассеты: игрок выбирает конкретное
    действие. Длинное название обрезается в пределах защитного капа Telegram;
    последствий в кнопке нет — их текст дня не показывает."""
    cards = [
        SimpleNamespace(position=0, title="Идти к реке"),
        SimpleNamespace(position=1, title="Слушать эхо"),
        SimpleNamespace(position=2, title="О" * 80),
    ]
    rows = bc.cards_keyboard(7, cards=cards).inline_keyboard
    labels = [row[0].text for row in rows]
    assert labels[:2] == ["Идти к реке", "Слушать эхо"]
    assert len(labels[2]) <= 64
    assert labels[2].endswith("…")


async def test_status_text_for_closed_and_tallying_phases() -> None:
    """Фаза дня читается из статуса: «подсчёт» и «день закрыт» — разные
    сообщения, а не одна и та же строка."""
    round_row = _round()
    round_row.status = RoundStatus.TALLYING
    assert "Подсчёт" in await bc.status_text(round_row)
    round_row.status = RoundStatus.CLOSED
    closed = await bc.status_text(round_row)
    assert "День закрыт" in closed
    assert "Сцена дня" not in closed


# ── доставка пакета дня ─────────────────────────────────────────────────────
async def test_finished_results_text_computed_once_per_chat(monkeypatch) -> None:
    """Экономика дня считается один раз на рассылку, а не на каждый чат."""
    results = AsyncMock(return_value="ИТОГИ")
    monkeypatch.setattr(bc, "results_body", results)
    finished = _round(9299)
    finished.status = RoundStatus.CLOSED
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_media_group=AsyncMock())
    round_row = _round()
    await bc._deliver_day(bot, 1, round_row, finished=finished, results_text=None)
    results.assert_awaited_once()
    assert "ИТОГИ" in bot.send_message.await_args_list[0].args[1]


async def test_retry_after_failure_does_not_abort_day(monkeypatch) -> None:
    """Чат, который и после паузы отвечает флуд-контролем, не имеет права
    отменить рассылку остальным: раньше исключение улетало из worker'а в
    gather и анонс дня не уходил НИКУДА."""
    _no_sleep(monkeypatch)
    await _wipe(Chat)
    loud, healthy = 777_101, 777_102
    async with SessionLocal() as db:
        db.add_all(
            [
                Chat(id=loud, type="group", active=True),
                Chat(id=healthy, type="group", active=True),
            ]
        )
        await db.commit()
    try:
        flood = TelegramRetryAfter(None, "Too Many Requests", retry_after=3)
        bot = _flaky_bot({loud: [flood, flood]})
        delivered = await bc.announce_new_day(bot, _round(9301), finished=None)
        assert delivered == [healthy]
        # Болтливый чат не считается пропавшим — вернёмся к нему в следующий раз.
        async with SessionLocal() as db:
            assert (await db.get(Chat, loud)).active is True
    finally:
        await _wipe(Chat, Round)


async def test_retry_after_then_success_keeps_chat(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    await _wipe(Chat)
    chat = 777_103
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="group", active=True))
        await db.commit()
    try:
        bot = _flaky_bot({chat: [TelegramRetryAfter(None, "flood", retry_after=1)]})
        assert await bc.announce_new_day(bot, _round(9302), finished=None) == [chat]
    finally:
        await _wipe(Chat, Round)


async def test_announce_delivers_round_fetched_without_cards() -> None:
    """Анонс дня обязан доставлять раунд, поднятый голым session.get.

    Регресс прода («итоги приходят, новый день нет»): финализатор и
    восстановитель брали день через session.get без selectinload, ленивая
    загрузка карт падала MissingGreenlet, падение ловилось молча в
    _deliver_chat («0 из N» без единого виноватого), claim оставался — и
    день не уходил никогда, тогда как итоги (там selectinload) доезжали.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.attributes import NO_VALUE

    # Чистый слэб: _wipe(Round) не трогает карты (FK не каскадит в SQLite),
    # и вставка раунда в пустую таблицу повторила бы id=1 на осиротевших
    # картах — UNIQUE round_id+position.
    await _wipe(Chat, Round, Card)
    chat = 777_106
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="group", active=True))
        await db.commit()
    round_row = _round(9310)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.commit()
    async with SessionLocal() as db:  # отдельная сессия — как у финализатора
        bare = await db.get(Round, round_row.id)
        # Дыра воспроизводима: коллекция карт не подгружена.
        assert sa_inspect(bare).attrs.cards.loaded_value is NO_VALUE
    try:
        delivered = await bc.announce_new_day(_flaky_bot({}), bare, finished=None)
        assert delivered == [chat]
    finally:
        await _wipe(Chat, Round, Card)


# ── личные дубликаты ────────────────────────────────────────────────────────


async def test_dm_send_all_is_silent_without_bot_or_subscribers(monkeypatch) -> None:
    """Без бота или без подписчиков личная рассылка не делает ничего."""
    monkeypatch.setattr(settings, "player_dm", True)
    deliver = AsyncMock()
    assert await bc._dm_send_all(None, deliver, "пусто") == 0
    await _wipe(Player)
    assert await bc._dm_send_all(SimpleNamespace(), deliver, "пусто") == 0
    deliver.assert_not_awaited()
    monkeypatch.setattr(settings, "player_dm", False)
    assert await bc._dm_send_all(SimpleNamespace(), deliver, "выключено") == 0


async def test_dm_send_all_retries_and_counts_failures(monkeypatch) -> None:
    """Флуд-контроль в личке: повтор через паузу, а не потеря сообщения;
    неотправленное честно вычитается из счётчика."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(settings, "player_dm", True)
    await _wipe(Player)
    ids = [777_201, 777_202, 777_203]
    async with SessionLocal() as db:
        db.add_all([Player(id=pid, dm_subscribed=True) for pid in ids])
        # dm_subscribed по умолчанию True — отписку задаём явно.
        db.add(Player(id=777_204, dm_subscribed=False))
        await db.commit()
    try:
        flood = TelegramRetryAfter(None, "flood", retry_after=2)
        seen: list[int] = []

        async def deliver(pid: int) -> None:
            seen.append(pid)
            if pid == ids[0] and seen.count(pid) == 1:
                raise flood
            if pid == ids[1]:
                raise flood

        delivered = await bc._dm_send_all(
            SimpleNamespace(), deliver, "личный пакет"
        )
        # Первый пережил флуд после паузы, второй не пережил, третий прошёл.
        assert delivered == 2
        assert seen.count(ids[1]) == 2
    finally:
        await _wipe(Player)


async def test_dm_send_all_survives_plain_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "player_dm", True)
    await _wipe(Player)
    async with SessionLocal() as db:
        db.add(Player(id=777_301, dm_subscribed=True))
        await db.commit()
    try:

        async def deliver(_pid: int) -> None:
            raise ValueError("telegram timeout")

        assert await bc._dm_send_all(SimpleNamespace(), deliver, "личный пакет") == 0
    finally:
        await _wipe(Player)


async def test_dm_send_all_only_narrows_audience_and_count(monkeypatch) -> None:
    """`only` сужает аудиторию: и вызовы, и знаменатель счётчика.

    Без него «доставлено» включало тех, кому отправлять было нечего. На
    напоминании о голосовании это значило, что уже проголосовавшие попадали в
    «отправлено N сообщений» — и число в логе было больше реального.
    """
    monkeypatch.setattr(settings, "player_dm", True)
    await _wipe(Player)
    ids = [777_401, 777_402, 777_403]
    async with SessionLocal() as db:
        db.add_all([Player(id=pid, dm_subscribed=True) for pid in ids])
        await db.commit()
    try:
        called: list[int] = []

        async def deliver(pid: int) -> None:
            called.append(pid)

        assert await bc._dm_send_all(
            SimpleNamespace(), deliver, "напоминание", only={ids[0], ids[2]}
        ) == 2
        assert sorted(called) == [ids[0], ids[2]]
        # Пустое пересечение — ни отправки, ни счёта.
        assert await bc._dm_send_all(
            SimpleNamespace(), deliver, "напоминание", only=set()
        ) == 0
    finally:
        await _wipe(Player)


# ── мягкие отказы в тексте итогов ───────────────────────────────────────────


async def test_results_body_survives_broken_session(monkeypatch) -> None:
    """Каждый слой итогов (карты, экономика, плагины) — необязательный.
    Падение любого оставляет читаемый пост, а не пустоту и не исключение."""
    tally = importlib.import_module("app.tally")

    class _Broken:
        async def execute(self, _stmt):
            raise RuntimeError("closed session")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(tally, "day_economics", AsyncMock(return_value={"path_stakes": {}, "multiplier": 1.0}))
    monkeypatch.setattr(tally, "format_economics", Mock(return_value=""))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(return_value="плагины"))

    finished = _round(9400)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=_Broken())
    assert "плагины" in text
    assert text.strip()


async def test_results_body_without_economics_keeps_dry_results(monkeypatch) -> None:
    """Сбой подсчёта экономики убирает коэффициент, но не сухие итоги."""
    tally = importlib.import_module("app.tally")
    monkeypatch.setattr(tally, "day_economics", AsyncMock(side_effect=RuntimeError("нет ставок")))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(return_value=""))
    finished = _round(9401)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=SimpleNamespace())
    assert text.strip()


async def test_results_body_survives_broken_economics_format(monkeypatch) -> None:
    """Форматирование экономики — тоже мягкий слой."""
    tally = importlib.import_module("app.tally")
    monkeypatch.setattr(tally, "day_economics", AsyncMock(return_value={"path_stakes": {}, "multiplier": 2.0}))
    monkeypatch.setattr(tally, "format_economics", Mock(side_effect=ValueError("битый формат")))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(side_effect=RuntimeError("плагины сломаны")))
    finished = _round(9402)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=SimpleNamespace())
    assert text.strip()


async def test_day_diary_message_escapes_html(monkeypatch) -> None:
    """Дневник снят из поста итогов и собирается для своей рассылки (17:00):
    формат «📖 …», HTML-экранированный; нет дневника/сбой — пустая строка."""
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value="<заметка> & эхо"))
    assert await bc.day_diary_message(SimpleNamespace(), _round(9403)) == "📖 &lt;заметка&gt; &amp; эхо"
    # Fail-open: сбой чтения кассеты или пустое поле — «», рассылки не будет.
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(side_effect=RuntimeError("нет кассеты")))
    assert await bc.day_diary_message(SimpleNamespace(), _round(9403)) == ""
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value=""))
    assert await bc.day_diary_message(SimpleNamespace(), _round(9403)) == ""


async def test_announce_day_diary_broadcasts_or_stays_silent(monkeypatch) -> None:
    """Дневник дня — отдельная рассылка (джоба story-diary): чатам и личкой
    через _broadcast_text; пустой дневник или бот-None — тихий ноль."""
    story_bay = importlib.import_module("app.story.bay")
    sent = AsyncMock(return_value=5)
    monkeypatch.setattr(bc, "_broadcast_text", sent)
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value="дневник"))
    assert await bc.announce_day_diary(SimpleNamespace(), _round(9403)) == 5
    assert sent.await_args.args[1] == "📖 дневник"

    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value=""))
    sent.reset_mock()
    assert await bc.announce_day_diary(SimpleNamespace(), _round(9403)) == 0
    sent.assert_not_awaited()
    # Без бота — тоже тихий ноль.
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value="дневник"))
    assert await bc.announce_day_diary(None, _round(9403)) == 0


# ── текстовая рассылка и шёпот ──────────────────────────────────────────────


async def test_broadcast_text_ignores_blank(monkeypatch) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await bc._broadcast_text(bot, "   \n ") == 0
    bot.send_message.assert_not_awaited()


async def test_broadcast_text_reaches_dm_even_without_chats(monkeypatch) -> None:
    """Живых чатов нет, но подписчики в личке есть — дубликат им всё равно
    уходит, иначе игроки потеряют анонс из-за мёртвого чата."""
    monkeypatch.setattr(settings, "player_dm", True)
    await _wipe(Chat, Player)
    async with SessionLocal() as db:
        db.add(Player(id=777_401, dm_subscribed=True))
        await db.commit()
    try:
        bot = SimpleNamespace(send_message=AsyncMock())
        assert await bc._broadcast_text(bot, "текст") == 1
        assert bot.send_message.await_args.args[0] == 777_401
    finally:
        await _wipe(Chat, Player)


async def test_broadcast_text_retry_and_failures(monkeypatch) -> None:
    """Флуд-контроль, повтор, отказ после ретрая, заблокированный чат и
    «чат не найден» — каждый случай разбирается, никто не подвисает."""
    _no_sleep(monkeypatch)
    await _wipe(Chat)
    flood, forbidden, gone, flaky, healthy = (
        777_501,
        777_502,
        777_503,
        777_504,
        777_505,
    )
    async with SessionLocal() as db:
        db.add_all(
            [Chat(id=cid, type="group", active=True) for cid in (flood, forbidden, gone, flaky, healthy)]
        )
        await db.commit()
    try:
        bot = _flaky_bot(
            {
                flood: [TelegramRetryAfter(None, "flood", retry_after=1)],
                flaky: [TelegramRetryAfter(None, "flood", retry_after=1), ValueError("again")],
                forbidden: [TelegramForbiddenError(None, "bot was blocked by the user")],
                gone: [ValueError("chat not found")],
            }
        )
        delivered = await bc._broadcast_text(bot, "текст")
        assert delivered == 2  # пережил флуд + здоровый
        async with SessionLocal() as db:
            states = {
                row.id: row.active
                for row in (await db.execute(select(Chat))).scalars()
                if row.id in {flood, forbidden, gone, flaky, healthy}
            }
        assert states[forbidden] is False
        assert states[gone] is False
        # Флуд без «чат исчез» — это не повод забыть чат.
        assert states[flood] is True
        assert states[flaky] is True
    finally:
        await _wipe(Chat)


async def test_announce_results_propagates_build_failure(monkeypatch) -> None:
    """Сборка итогов упала — исключение обязано дойти до вызывающего.

    Тест раньше назывался «falls_back_to_dry_template» и обещал в докстринге,
    что игроки «всё равно получат хоть что-то», но утверждал ровно обратное:
    announce_results возвращала 0 и send_message не вызывался. Никакого
    фолбэка не было — был тихий возврат нуля, из-за которого маркер results_at
    фиксировался, и день оставался без итогов навсегда.

    Теперь контракт другой и честный: сбой уходит наверх, вызывающий откатывает
    транзакцию, день остаётся без маркера, и восстановитель дошлёт его позже.
    Пустой текст без исключения — это другое (нечего слать) и остаётся нулём.
    """
    monkeypatch.setattr(bc, "results_body", AsyncMock(side_effect=RuntimeError("БД лежит")))
    bot = SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(RuntimeError):
        await bc.announce_results(bot, _round(9404))
    bot.send_message.assert_not_awaited()
    # Без бота — тихий ноль, исключение тут не при чём.
    assert await bc.announce_results(None, _round(9404)) == 0
    # Собрался пустой пост — тоже ноль, но без исключения.
    monkeypatch.setattr(bc, "results_body", AsyncMock(return_value=""))
    assert await bc.announce_results(SimpleNamespace(), _round(9404)) == 0


async def test_announce_results_broadcasts_message(monkeypatch) -> None:
    monkeypatch.setattr(bc, "results_body", AsyncMock(return_value="ИТОГИ"))
    sent = AsyncMock(return_value=7)
    monkeypatch.setattr(bc, "_broadcast_text", sent)
    finished = _round(9405)
    assert await bc.announce_results(SimpleNamespace(), finished) == 7
    assert sent.await_args.args[1] == "ИТОГИ"


async def test_announce_results_without_epilogue(monkeypatch) -> None:
    """Эпилог (canon дня) снят из поста итогов — указ владельца.

    epilogue_text остаётся в БД (им маркируется готовность лидерборда
    к выплате), но в пост игрокам не дописывается — туда идут только
    сухие итоги с экономикой (дневник — отдельной рассылкой в 17:00).
    """
    monkeypatch.setattr(bc, "results_body", AsyncMock(return_value="СУХИЕ ИТОГИ"))
    sent = AsyncMock(return_value=1)
    monkeypatch.setattr(bc, "_broadcast_text", sent)
    finished = _round(9415)
    finished.epilogue_text = "Канон дня."
    await bc.announce_results(SimpleNamespace(), finished)
    # Текст уходит ровно тем, что собрал results_body — без дописывания.
    assert sent.await_args.args[1] == "СУХИЕ ИТОГИ"


async def test_announce_results_marks_delivery_by_day(monkeypatch) -> None:
    """Метка доставки итогов обязана называть день.

    Раньше вид рассылки брался из первых 16 символов текста («Итоги дня 9405»),
    и метка была «text:Итоги дня 9405» — но не потому, что день решает что-то,
    а потому что совпало с текстом. Смена текста, обрезка, другой язык — и
    привязка к дню рассыпается, а с ней и тревога «у дня N нет отметки».
    """
    monkeypatch.setattr(bc, "results_body", AsyncMock(return_value="ИТОГИ"))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=1))
    async with SessionLocal() as db:
        db.add(Chat(id=-9406, type="group", active=True))
        await db.commit()
    try:
        await bc.announce_results(bot, _round(9406))
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(WatcherState).where(
                        WatcherState.key == "delivery:results:9406"
                    )
                )
            ).scalar_one_or_none()
        assert row is not None and row.value.endswith("/1"), row
    finally:
        await _wipe(Chat)
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(
                    WatcherState.key == "delivery:results:9406"
                )
            )
            await db.commit()


async def test_whisper_requires_bot_and_text(monkeypatch) -> None:
    assert await bc.whisper_to_chats(None, "шёпот") == 0
    assert await bc.whisper_to_chats(SimpleNamespace(send_message=AsyncMock()), "") == 0


async def test_whisper_handles_every_failure_mode(monkeypatch) -> None:
    """Шёпот не должен ни рассылаться не туда, ни падать из-за одного чата."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(settings, "player_dm", False)
    await _wipe(Chat)
    flood, forbidden, gone, flaky, healthy = (
        777_601,
        777_602,
        777_603,
        777_604,
        777_605,
    )
    async with SessionLocal() as db:
        db.add_all(
            [Chat(id=cid, type="group", active=True) for cid in (flood, forbidden, gone, flaky, healthy)]
        )
        await db.commit()
    try:
        bot = _flaky_bot(
            {
                flood: [TelegramRetryAfter(None, "flood", retry_after=1)],
                flaky: [TelegramRetryAfter(None, "flood", retry_after=1), ValueError("again")],
                forbidden: [TelegramForbiddenError(None, "bot was blocked by the user")],
                gone: [ValueError("kicked")],
            }
        )
        assert await bc.whisper_to_chats(bot, "полуденный шёпот") == 2
        async with SessionLocal() as db:
            states = {row.id: row.active for row in (await db.execute(select(Chat))).scalars()}
        assert states[forbidden] is False
        assert states[gone] is False
        assert states[flood] is True
        assert states[flaky] is True
    finally:
        await _wipe(Chat)


async def test_whisper_logs_unexpected_failure(monkeypatch, caplog) -> None:
    """Неожиданный сбой отправки виден в логе — не теряется молча.

    Здесь был единственный except в проекте без единого лога: любой сбой
    кроме «бота заблокировали» возвращал False и исчезал. Пауза, разворот или
    церемония могли не дойти до всех чатов, и по логам это было не видно —
    только число доставки, из которого непонятно, потеряно что-то или нет.
    """
    import logging

    _no_sleep(monkeypatch)
    monkeypatch.setattr(settings, "player_dm", False)
    await _wipe(Chat)
    chat = 777_611
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="group", active=True))
        await db.commit()
    try:
        bot = _flaky_bot({chat: [TimeoutError("сеть легла")]})
        with caplog.at_level(logging.INFO):
            assert await bc.whisper_to_chats(bot, "полуденный шёпот") == 0
        assert "Шёпот дня не доставлен" in caplog.text, caplog.text
        assert "TimeoutError" in caplog.text, caplog.text
        # Итоговая строка обязана называть число недоставленных.
        assert "не доставлено: 1" in caplog.text, caplog.text
        # Чата никто не отключил: сбой не значит «бот изгнан».
        async with SessionLocal() as db:
            assert (await db.get(Chat, chat)).active is True
    finally:
        await _wipe(Chat)


@pytest.mark.parametrize("retry_after", [1, 5])
async def test_retry_pause_is_honoured(monkeypatch, retry_after: int) -> None:
    """Пауза берётся из ответа Telegram, а не выдумывается: иначе повтор
    уйдёт в тот же флуд."""
    _no_sleep(monkeypatch)
    await _wipe(Chat)
    chat = 777_701
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="group", active=True))
        await db.commit()
    try:
        bot = _flaky_bot({chat: [TelegramRetryAfter(None, "flood", retry_after=retry_after)]})
        assert await bc.whisper_to_chats(bot, "шёпот") == 1
        assert bc.asyncio.sleep.await_args.args[0] == retry_after + 1
    finally:
        await _wipe(Chat)


async def test_no_asyncio_wait_coroutine_leak() -> None:
    """Рассылки не должны оставлять «висящие» таймеры: все ожидания — через
    asyncio.sleep с явной паузой, а не через несуществующие таймеры."""
    assert asyncio.isfuture(bc._BROADCAST_PARALLELISM) is False
    assert bc._BROADCAST_PARALLELISM > 0


# ── пустая аудитория ─────────────────────────────────────────────────────────


async def test_announce_without_audience_leaves_a_trace(monkeypatch, caplog) -> None:
    """Анонс дня без единого получателя обязан оставить след И СНЯТЬ claim.

    Claim дня (announced_at) стоит ДО отправки, а метка delivery:* при
    «0 из 0» не пишется — без отдельной метки, предупреждения и счётчика
    пустая аудитория была неотличима от «новость дня увидели все», и тревоги
    было некому поднять. А ещё пустая аудитория ≠ состоявшийся анонс:
    снятый claim отдаёт день восстановителю, и он досылается, как только
    получатель появится (привязанный чат, вернувшийся игрок).
    """
    from app import metrics as metrics_mod
    from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY
    from app.rounds import claim_announcement

    monkeypatch.setattr(settings, "player_dm", True)
    monkeypatch.setattr(bc, "active_chat_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(bc, "active_player_ids", AsyncMock(return_value=[]))
    metrics_mod.reset()
    round_row = _round(9305)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.commit()
        assert await claim_announcement(db, round_row)  # claim, как в тике
    try:
        with caplog.at_level(logging.WARNING, logger="app.broadcast"):
            delivered = await bc.announce_new_day(_flaky_bot({}), round_row, finished=None)
        assert delivered == []
        async with SessionLocal() as db:
            marker = await db.get(WatcherState, ANNOUNCE_EMPTY_DAY_KEY)
            fresh = await db.get(Round, round_row.id)
        assert marker is not None and marker.value == "9305"
        assert fresh is not None and fresh.announced_at is None
        assert "way_announce_no_audience_total 1" in metrics_mod.render()
        assert "ушёл в пустоту" in caplog.text
    finally:
        metrics_mod.reset()
        await _wipe(Round)
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(WatcherState.key == ANNOUNCE_EMPTY_DAY_KEY)
            )
            await db.commit()


async def test_announce_clears_empty_marker_when_audience_returns(monkeypatch) -> None:
    """Получатель появился — метка пустоты гаснет сама.

    Иначе тревога висела бы до следующего анонса (до суток), даже когда
    хранитель уже привязал чат или игрок вернулся в личку.
    """
    from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY

    monkeypatch.setattr(settings, "player_dm", False)
    await _wipe(Chat)
    chat = 777_705
    async with SessionLocal() as db:
        db.add(Chat(id=chat, type="channel", active=True))
        await db.commit()
    try:
        await bc.set_announce_empty_marker(9306)
        assert await bc.announce_new_day(_flaky_bot({}), _round(9306), finished=None) == [chat]
        async with SessionLocal() as db:
            assert await db.get(WatcherState, ANNOUNCE_EMPTY_DAY_KEY) is None
    finally:
        await _wipe(Chat, Round)
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(WatcherState.key == ANNOUNCE_EMPTY_DAY_KEY)
            )
            await db.commit()


async def test_announce_without_bot_leaves_a_trace(monkeypatch, caplog) -> None:
    """Третья ветка той же ловушки: bot is None при уже стоящем claim дня.

    Молчаливый `return []` оставлял «объявленный» день без единого следа —
    ни лога, ни метки, ни счётчика. Метку announce_empty_day здесь сознательно
    НЕ ставим: её читает check_anomalies, а это джоба того же планировщика.
    Бота нет — значит тиков нет — тревоги всё равно некому поднять, остаётся
    то, что видно и без планировщика: лог (Render) и /metrics.
    """
    from app import metrics as metrics_mod
    from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY

    metrics_mod.reset()
    try:
        with caplog.at_level(logging.WARNING, logger="app.broadcast"):
            delivered = await bc.announce_new_day(None, _round(9307), finished=None)
        assert delivered == []
        assert "бот не установлен" in caplog.text
        assert "way_announce_no_bot_total 1" in metrics_mod.render()
        async with SessionLocal() as db:
            assert await db.get(WatcherState, ANNOUNCE_EMPTY_DAY_KEY) is None
    finally:
        metrics_mod.reset()
        await _wipe(Round)


def test_failure_reason_normalizes_for_counter() -> None:
    """Причина для счётчика устойчива: класс + короткий текст, без переносов.

    Счётчик складывает причины всех получателей тревоги: уникальная строка
    на чат превратила бы «Причины» в сотни позиций, переносы ломали бы строку
    метки, а флуд-бакет обязан не зависеть от числа секунд в тексте.
    """
    reason = bc.failure_reason(ValueError("битая\nстрока " + "x" * 300))
    assert reason.startswith("ValueError: ")
    assert "\n" not in reason
    assert len(reason) <= 100
    flood = bc.failure_reason(TelegramRetryAfter(None, "flood", retry_after=7))
    assert flood == "флуд-контроль: ретрай не помог"
    assert bc.failure_reason(TelegramRetryAfter(None, "flood", retry_after=3)) == flood
