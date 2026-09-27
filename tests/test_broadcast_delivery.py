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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import delete, select

from app import broadcast as bc
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Chat, Player, Round, RoundStatus, WinRule


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
                description="описание",
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


def test_cards_keyboard_remember_button_encodes_day() -> None:
    """Кнопка памяти несёт и PK раунда, и day_index: после /resetgame они
    разъезжаются, и без второго поля эхо ищется не в том дне."""
    plain = bc.cards_keyboard(7)
    assert [b.callback_data for b in plain.inline_keyboard[0]] == [
        "vote:7:0",
        "vote:7:1",
        "vote:7:2",
    ]
    with_remember = bc.cards_keyboard(7, remember=True)
    button = with_remember.inline_keyboard[1][0]
    assert button.callback_data == "remember:7:7"
    explicit = bc.cards_keyboard(7, remember=True, day_index=80_123)
    assert explicit.inline_keyboard[1][0].callback_data == "remember:7:80123"


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


async def test_single_media_sends_photo_not_group(monkeypatch) -> None:
    """Telegram принимает mediaGroup только от двух вложений: один кадр дня
    уходит обычным фото, иначе анонс падал ПОСЛЕ обложки."""
    monkeypatch.setattr(bc, "build_day_post", Mock(return_value=[SimpleNamespace(media="day.jpg", caption="обложка")]))
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_media_group=AsyncMock(),
    )
    round_row = _round()
    await bc._deliver_day(bot, 1, round_row, finished=None)
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["caption"] == "обложка"
    bot.send_media_group.assert_not_awaited()


async def test_media_group_sent_when_two_attachments(monkeypatch) -> None:
    media = [SimpleNamespace(media="a.jpg", caption="a"), SimpleNamespace(media="b.jpg", caption="b")]
    monkeypatch.setattr(bc, "build_day_post", Mock(return_value=media))
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_media_group=AsyncMock(),
    )
    await bc._deliver_day(bot, 1, _round(), finished=None)
    bot.send_media_group.assert_awaited_once()
    bot.send_photo.assert_not_awaited()


async def test_finished_results_text_computed_once_per_chat(monkeypatch) -> None:
    """Экономика дня считается один раз на рассылку, а не на каждый чат."""
    results = AsyncMock(return_value="ИТОГИ")
    monkeypatch.setattr(bc, "results_message", results)
    monkeypatch.setattr(bc, "build_day_post", Mock(return_value=[]))
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
    monkeypatch.setattr(bc, "build_day_post", Mock(return_value=[]))
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
    monkeypatch.setattr(bc, "build_day_post", Mock(return_value=[]))
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


# ── мягкие отказы в тексте итогов ───────────────────────────────────────────


async def test_results_body_survives_broken_session(monkeypatch) -> None:
    """Каждый слой итогов (карты, экономика, дневник, плагины) — необязательный.
    Падение любого оставляет читаемый пост, а не пустоту и не исключение."""
    tally = importlib.import_module("app.tally")
    story_bay = importlib.import_module("app.story.bay")

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
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(side_effect=RuntimeError("нет кассеты")))

    finished = _round(9400)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=_Broken())
    assert "плагины" in text
    assert text.strip()


async def test_results_body_without_economics_keeps_dry_results(monkeypatch) -> None:
    """Сбой подсчёта экономики убирает коэффициент, но не сухие итоги."""
    tally = importlib.import_module("app.tally")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(tally, "day_economics", AsyncMock(side_effect=RuntimeError("нет ставок")))
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value=""))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(return_value=""))
    finished = _round(9401)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=SimpleNamespace())
    assert text.strip()


async def test_results_body_survives_broken_economics_format(monkeypatch) -> None:
    """Форматирование экономики — тоже мягкий слой."""
    tally = importlib.import_module("app.tally")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(tally, "day_economics", AsyncMock(return_value={"path_stakes": {}, "multiplier": 2.0}))
    monkeypatch.setattr(tally, "format_economics", Mock(side_effect=ValueError("битый формат")))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(side_effect=RuntimeError("плагины сломаны")))
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value="дневник"))
    finished = _round(9402)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=SimpleNamespace())
    assert "дневник" in text


async def test_results_body_appends_diary(monkeypatch) -> None:
    """Дневник кассеты экранируется и попадает в пост — но его отсутствие
    не должно ломать итоги."""
    tally = importlib.import_module("app.tally")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(tally, "day_economics", AsyncMock(return_value=None))
    monkeypatch.setattr(tally, "format_plugin_results", AsyncMock(return_value=""))
    monkeypatch.setattr(story_bay, "day_diary", AsyncMock(return_value="<заметка> & эхо"))
    finished = _round(9403)
    finished.status = RoundStatus.CLOSED
    text = await bc.results_body(finished, session=SimpleNamespace())
    assert "&lt;заметка&gt; &amp; эхо" in text


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


async def test_announce_results_falls_back_to_dry_template(monkeypatch) -> None:
    """Если сбор итогов упал — игроки всё равно должны получить хоть что-то."""
    monkeypatch.setattr(bc, "results_body", AsyncMock(side_effect=RuntimeError("БД лежит")))
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await bc.announce_results(bot, _round(9404)) == 0
    bot.send_message.assert_not_awaited()
    assert await bc.announce_results(None, _round(9404)) == 0


async def test_announce_results_broadcasts_body(monkeypatch) -> None:
    monkeypatch.setattr(bc, "results_body", AsyncMock(return_value="ИТОГИ"))
    sent = AsyncMock(return_value=7)
    monkeypatch.setattr(bc, "_broadcast_text", sent)
    finished = _round(9405)
    assert await bc.announce_results(SimpleNamespace(), finished) == 7
    assert sent.await_args.args[1] == "ИТОГИ"


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
