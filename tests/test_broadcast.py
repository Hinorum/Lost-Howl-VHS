"""Рассылка дня: падение одного чата не мешает дню открыться в остальных."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from app.broadcast import _deliver_day, announce_new_day
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Chat, Player, Round, RoundStatus, WinRule


def _round(day_index: int, media_dir) -> Round:
    cover = f"day{day_index}_cover.jpg"
    (media_dir / cover).write_bytes(b"")
    round_row = Round(
        id=90_000 + day_index,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День проверки рассылки",
        chapter_text="Текст.",
        dilemma="Каким кадром останется день?",


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


def _bot(side_effects: dict[int, Exception]) -> SimpleNamespace:
    async def sender(chat_id, *args, **kwargs):
        exc = side_effects.get(chat_id)
        if exc is not None:
            raise exc
        return SimpleNamespace()

    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=sender),
        send_media_group=AsyncMock(side_effect=sender),
        send_message=AsyncMock(side_effect=sender),
    )


async def test_forbidden_chat_does_not_block_day(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    """Заблокировавший бота чат деактивируется; день доходит до остальных."""
    blocked, healthy = 777_001, 777_002
    async with SessionLocal() as db:
        db.add_all([Chat(id=blocked, type="group", active=True), Chat(id=healthy, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({blocked: TelegramForbiddenError(None, "bot was blocked by the user")})
        delivered = await announce_new_day(bot, _round(9100, media_dir), finished=None)
        assert delivered == [healthy]
        # Медиа дня отключено — канал только текстовый, оба чата получили статус.
        assert bot.send_media_group.await_count == 0
        assert bot.send_message.await_count == 2
        async with SessionLocal() as db:
            rows = {
                row.id: row.active
                for row in (await db.execute(select(Chat).where(Chat.id.in_([blocked, healthy])))).scalars()
            }
        assert rows == {blocked: False, healthy: True}
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([blocked, healthy])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_transient_failure_does_not_stop_other_chats(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    """Сетевой сбой в одном чате не отменяет рассылку и не деактивирует его."""
    flaky, healthy = 777_003, 777_004
    async with SessionLocal() as db:
        db.add_all([Chat(id=flaky, type="group", active=True), Chat(id=healthy, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({flaky: ValueError("telegram timeout")})
        delivered = await announce_new_day(bot, _round(9101, media_dir), finished=None)
        assert delivered == [healthy]
        async with SessionLocal() as db:
            rows = {
                row.id: row.active
                for row in (await db.execute(select(Chat).where(Chat.id.in_([flaky, healthy])))).scalars()
            }
        # «Не похоже на удаление» — чат остаётся активным, попробуем в следующий раз.
        assert rows == {flaky: True, healthy: True}
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([flaky, healthy])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_migrated_chat_is_forgotten(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    gone, alive = 777_005, 777_006
    async with SessionLocal() as db:
        db.add_all([Chat(id=gone, type="group", active=True), Chat(id=alive, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({gone: ValueError("chat not found")})
        delivered = await announce_new_day(bot, _round(9102, media_dir), finished=None)
        assert delivered == [alive]
        async with SessionLocal() as db:
            row = await db.get(Chat, gone)
        assert row is not None and row.active is False
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([gone, alive])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_no_bot_no_broadcast() -> None:
    assert await announce_new_day(None, SimpleNamespace(day_index=1)) == []


async def test_status_bank_line_shows_amount_only(monkeypatch, tmp_path) -> None:
    """Банк дня в посте — только сумма, без числа ставок."""
    from app.broadcast import status_text
    from app.models import Stake

    monkeypatch.setattr(settings, "ton_enabled", True)
    round_row = _round(9400, tmp_path)
    async with SessionLocal() as db:
        try:
            # Раунд и игрок — родители ставки: иначе Postgres отвергнет INSERT
            # stakes по внешнему ключу (SQLite FK не проверяет).
            db.add(round_row)
            db.add(Player(id=9_401, username="u9401"))
            await db.flush()
            db.add(
                Stake(
                    round_id=round_row.id,
                    player_id=9_401,
                    amount_nanotons=1_250_000_000,
                    tx_hash="t9400",
                    status="confirmed",
                )
            )
            await db.commit()
            text = await status_text(round_row)
            assert "Банк дня: 1.25 Gram" in text
            assert "ставок" not in text
        finally:
            await db.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
            await db.execute(Card.__table__.delete().where(Card.round_id == round_row.id))
            await db.execute(Round.__table__.delete().where(Round.id == round_row.id))
            await db.execute(Player.__table__.delete().where(Player.id == 9_401))
            await db.commit()

    # Пустой банк — строка остаётся: банк дня виден с самого открытия.
    text = await status_text(round_row)
    assert "Банк дня: 0.00 Gram" in text


async def test_status_keeps_paths_out_of_text(tmp_path) -> None:
    """Варианты в текст поста не идут — только в кнопки (см. cards_keyboard):
    пост несёт сюжет и дилемму, а не витрину трёх карт."""
    from app.broadcast import status_text

    round_row = _round(9300, tmp_path)
    status = await status_text(round_row)
    assert "I. Путь 0" not in status
    assert "описание" not in status
    assert "Каким кадром останется день?" in status
    assert len(status) <= 4096


async def test_status_carries_story_between_title_and_dilemma(tmp_path) -> None:
    """Единственная структура дня: заголовок → сюжет (эхо в его начале) →
    блок «что предстоит решить»."""
    from app.broadcast import status_text

    round_row = _round(9302, tmp_path)
    status = await status_text(round_row)
    assert "День проверки рассылки" in status
    assert "Текст." in status
    title_at = status.index("День проверки рассылки")
    story_at = status.index("Текст.")
    dilemma_at = status.index("Каким кадром останется день?")
    assert title_at < story_at < dilemma_at


async def test_status_escapes_cassette_text(tmp_path) -> None:
    """Текст кассеты уходит в HTML-пост экранированным, а не сырым.

    Сырой `<`/`&` из правки в /panel Telegram отвергает всё сообщение целиком:
    пакет дня не дошёл бы ни в один чат, ни в одну личку («0 из N»), хотя
    виноватого получателя среди них нет. Обрезка — ДО экранирования, чтобы
    сущность (&amp;) не порвалась клампом.
    """
    from app.broadcast import status_text

    round_row = _round(9304, tmp_path)
    round_row.chapter_title = "Свет & тень <ночи>"
    round_row.chapter_text = "Стая помнит R&D и <следы>."
    round_row.dilemma = "Мост & река: <куда> пойти?"
    status = await status_text(round_row)
    assert "Свет &amp; тень &lt;ночи&gt;" in status
    assert "Стая помнит R&amp;D и &lt;следы&gt;." in status
    assert "Мост &amp; река: &lt;куда&gt; пойти?" in status
    assert "<ночи>" not in status
    assert "<следы>" not in status
    assert "<куда>" not in status


async def test_status_single_structure_needs_dilemma(tmp_path) -> None:
    """Единственная структура дня: сюжет → блок «что предстоит решить»,
    без витрины трёх карт — варианты выбора только в кнопках."""
    from app.broadcast import status_text

    round_row = _round(9306, tmp_path)
    status = await status_text(round_row)
    assert "Каким кадром останется день?" in status
    assert "I. Путь 0" not in status
    # Механика дня цела: правило/дедлайн хвоста поста не задеты.
    assert "Сцена дня" in status

    # NULL дилеммы — переходные строки до миграции c4e8a1b7d9f2: пост
    # остаётся читаемым (сюжет и хвост на месте), падения нет.
    round_row.dilemma = None
    legacy = await status_text(round_row)
    assert "Текст." in legacy
    assert "I. Путь 0" not in legacy
    assert "Сцена дня" in legacy


async def test_status_keeps_deadline_when_core_overflows(tmp_path, monkeypatch) -> None:
    """При переполнении поста режется «верх», а дедлайн/правило дня всегда целы."""
    from app import broadcast
    from app.broadcast import status_text

    round_row = _round(9303, tmp_path)
    round_row.chapter_text = "д" * 500
    round_row.dilemma = "д" * 400
    monkeypatch.setattr(broadcast, "_MAX_TEXT_LEN", 300)
    status = await status_text(round_row)
    vote = round_row.voting_ends_at.strftime("%H:%M")
    tally = round_row.tally_ends_at.strftime("%H:%M")
    assert status.endswith(
        f"🗳 Голосование до: {vote} UTC · 🏁 Итоги и новый день: {tally} UTC"
    )
    assert len(status) <= 300


def _finished(day_index: int, media_dir) -> Round:
    finished = _round(day_index, media_dir)
    finished.status = RoundStatus.CLOSED
    finished.winner_card = 1
    finished.vote_counts_json = '{"0":2,"1":1,"2":4}'
    return finished


async def test_finished_day_results_sent_as_text_no_photo(tmp_path, monkeypatch) -> None:
    """Итоги дня — только текстом: без фото победившей ветки (это был дубль
    обложки нового дня). Текст «Итог дня» уходит обычным сообщением."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    finished = _finished(9110, media_dir)
    next_day = _round(9111, media_dir)
    bot = _bot({})
    results = "🎊 Итог дня 1\n📜 Канон: Путь 1\nканон-текст"

    await _deliver_day(bot, 777_010, next_day, finished, results_text=results)

    sent_texts = [c.args[1] if len(c.args) > 1 else "" for c in bot.send_message.await_args_list]
    assert results in sent_texts
    # Ни один фото-пост не несёт «Итог дня» в подписи: картинку итога не шлём.
    for call in bot.send_photo.await_args_list:
        caption = call.kwargs.get("caption", "") or ""
        assert "Итог дня" not in caption
