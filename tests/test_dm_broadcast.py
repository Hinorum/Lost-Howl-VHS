"""Личная рассылка (DM): итоги, новый день с обложкой и вечерний пост дублируются
в личку подписанным игрокам; отписка в /start уважается, флаг player_dm глушит всё."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError

from app.broadcast import (
    _deliver_day,
    _dm_send_all,
    active_player_ids,
    announce_new_day,
    announce_results,
    whisper_to_chats,
)
from app.config import settings
from app.db import SessionLocal
from app.handlers import on_dm_toggle
from app.models import Card, Player, Round, RoundStatus, WinRule


def _round(day_index: int, media_dir) -> Round:
    cover = f"day{day_index}_cover.jpg"
    (media_dir / cover).write_bytes(b"")
    round_row = Round(
        id=88_000 + day_index,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День личной рассылки",
        chapter_text="Текст.",


        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC) + timedelta(hours=23),
        tally_ends_at=datetime.now(UTC) + timedelta(hours=24),
    )
    for position in range(3):
        name = f"day{day_index}_card{position}.jpg"
        (media_dir / name).write_bytes(b"")
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


def _bot() -> SimpleNamespace:
    async def sender(chat_id, *args, **kwargs):
        return SimpleNamespace()

    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=sender),
        send_media_group=AsyncMock(side_effect=sender),
        send_message=AsyncMock(side_effect=sender),
    )


async def _add_player(player_id: int, subscribed: bool = True) -> None:
    async with SessionLocal() as db:
        db.add(Player(id=player_id, username=f"p{player_id}", first_name="P", dm_subscribed=subscribed))
        await db.commit()


async def _cleanup_player(player_id: int) -> None:
    async with SessionLocal() as db:
        await db.execute(Player.__table__.delete().where(Player.id == player_id))
        await db.commit()


async def test_new_day_text_goes_to_subscribed_players(tmp_path, monkeypatch) -> None:
    """Подписанные игроки получают текст дня в личку, отписавшиеся — нет.
    Без живых чатов рассылка всё равно доходит; медиа дня отключено."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    subscribed_a, subscribed_b, muted = 88_111, 88_112, 88_113
    await _add_player(subscribed_a)
    await _add_player(subscribed_b)
    await _add_player(muted, subscribed=False)
    try:
        bot = _bot()
        await announce_new_day(bot, _round(90001, media_dir), finished=None)
        media_chat_ids = {
            call.args[0] for call in bot.send_media_group.await_args_list
        }
        text_chat_ids = {call.args[0] for call in bot.send_message.await_args_list}
        assert media_chat_ids == set()
        assert text_chat_ids == {subscribed_a, subscribed_b}
        assert muted not in text_chat_ids
    finally:
        for pid in (subscribed_a, subscribed_b, muted):
            await _cleanup_player(pid)


async def test_player_dm_flag_kills_private_broadcast(tmp_path, monkeypatch) -> None:
    """player_dm=false возвращает прежнее поведение: личных дубликатов нет."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    subscribed = 88_121
    await _add_player(subscribed)
    try:
        bot = _bot()
        monkeypatch.setattr(settings, "player_dm", False)
        await announce_new_day(bot, _round(90002, media_dir), finished=None)
        assert bot.send_media_group.await_count == 0
        assert bot.send_message.await_count == 0
    finally:
        monkeypatch.setattr(settings, "player_dm", True)
        await _cleanup_player(subscribed)


async def test_results_reach_subscribed_players(tmp_path, monkeypatch) -> None:
    """Итоги дня приходят в личку, даже если живых чатов нет вовсе."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    subscribed = 88_131
    await _add_player(subscribed)
    finished = _round(90003, media_dir)
    finished.status = RoundStatus.CLOSED
    finished.winner_card = 1
    finished.vote_counts_json = '{"0":1,"1":2,"2":0}'
    try:
        bot = _bot()
        await announce_results(bot, finished)
        sent = {
            (call.args[0], call.args[1])
            for call in bot.send_message.await_args_list
            if call.args and "кадр записан" in str(call.args[1])
        }
        assert any(pid == subscribed for pid, _ in sent)
    finally:
        await _cleanup_player(subscribed)


async def test_evening_whisper_goes_to_players(tmp_path, monkeypatch) -> None:
    """Вечерний шёпот дублируется в личку подписчикам; отписавшийся молчит."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    subscribed, muted = 88_141, 88_142
    await _add_player(subscribed)
    await _add_player(muted, subscribed=False)
    try:
        bot = _bot()
        await whisper_to_chats(bot, "Кофейный остывает за углом.")
        chat_ids = {call.args[0] for call in bot.send_message.await_args_list}
        assert chat_ids == {subscribed}
    finally:
        for pid in (subscribed, muted):
            await _cleanup_player(pid)


async def test_dm_toggle_flips_subscription() -> None:
    """Кнопка в /start: отписанный снова получает рассылку и наоборот."""
    pid = 88_151
    await _add_player(pid, subscribed=False)
    try:
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=pid, username=None, first_name="P"),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            answer=AsyncMock(),
        )
        await on_dm_toggle(callback)
        callback.answer.assert_awaited()
        alert = callback.answer.await_args.kwargs.get("show_alert")
        assert alert is True
        async with SessionLocal() as db:
            player = await db.get(Player, pid)
        assert player is not None and player.dm_subscribed is True

        text = callback.answer.await_args.args[0]
        assert "снова приходят" in text
    finally:
        await _cleanup_player(pid)


async def test_active_player_ids_respects_flag() -> None:
    """В подписку входят только взявшие кнопку dm_subscribed."""
    yes, no = 88_161, 88_162
    await _add_player(yes, subscribed=True)
    await _add_player(no, subscribed=False)
    try:
        ids = await active_player_ids()
        assert yes in ids and no not in ids
    finally:
        for pid in (yes, no):
            await _cleanup_player(pid)


async def test_blocked_player_is_unsubscribed_like_a_chat() -> None:
    """Заблокировавший бота отписывается — ровно как чат без прав.

    У чатов для этого есть deactivate_chat, а у игроков не было ничего: forbidden
    попадал в общий except, писался в лог, и тот же адрес перебирался каждой
    рассылкой заново и вечно. Докстринг _dm_send_all при этом обещал, что
    молчать нельзя, — обещание было ложным.

    Побочный, но главный эффект: пока такой адрес в подписке, метка доставки
    никогда не набирает «N из N», и счётчик врёт постоянно.
    """
    blocked, alive = 88_181, 88_182
    await _add_player(blocked)
    await _add_player(alive)
    bot = SimpleNamespace()
    try:
        delivered = await _dm_send_all(
            bot,
            lambda pid: _refuse(pid, blocked),
            "Тест",
        )
        assert delivered == 1
        async with SessionLocal() as db:
            assert (await db.get(Player, blocked)).dm_subscribed is False
            assert (await db.get(Player, alive)).dm_subscribed is True
        # И выпадает из подписки, а не просто переживает рассылку.
        assert blocked not in await active_player_ids()
    finally:
        for pid in (blocked, alive):
            await _cleanup_player(pid)
        async with SessionLocal() as db:
            await db.execute(
                Player.__table__.delete().where(Player.id.in_([blocked, alive]))
            )
            await db.commit()


async def test_unreachable_player_stays_subscribed() -> None:
    """Прочие сбои (сеть, таймаут) подписку не трогают.

    Отписать игрока можно только когда Telegram сказал «сюда писать нельзя».
    Всё остальное — временно, иначе один сбой сети тихо лишил бы игрока
    рассылки навсегда.
    """
    flaky = 88_191
    await _add_player(flaky)
    try:
        async def boom(pid):
            raise TimeoutError("сеть легла")

        assert await _dm_send_all(SimpleNamespace(), boom, "Тест") == 0
        async with SessionLocal() as db:
            assert (await db.get(Player, flaky)).dm_subscribed is True
    finally:
        await _cleanup_player(flaky)


async def _refuse(pid: int, blocked: int) -> None:
    if pid == blocked:
        raise TelegramForbiddenError(
            method=None, message="bot was blocked by the user"
        )


async def test_deliver_day_private_sends_finished_results(tmp_path, monkeypatch) -> None:
    """Из лички результаты прошлого дня в составе пакета уходят и игроку."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    subscribed = 88_171
    finished = _round(90004, media_dir)
    finished.status = RoundStatus.CLOSED
    finished.winner_card = 2
    finished.vote_counts_json = '{"0":1,"1":1,"2":3}'
    next_day = _round(90005, media_dir)
    results = "🎊 Итог дня 90004\n📜 Канон: Путь 2\nканон-текст"
    try:
        bot = _bot()
        await _deliver_day(bot, subscribed, next_day, finished, results_text=results)
        sent_texts = [
            call.args[1] if len(call.args) > 1 else ""
            for call in bot.send_message.await_args_list
        ]
        assert results in sent_texts
    finally:
        await _cleanup_player(subscribed)
