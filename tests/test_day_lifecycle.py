"""Жизненный цикл дня: самолечение застрявших дней и пауза-не-ловушка.

Инцидент: сбой доставки анонса оставлял день в TALLYING позади актуального —
тик обрабатывал только актуальный, и застрявший висел вечно (без подсчёта,
без канона, с замороженными ставками). Плюс ограждения паузы отказывали
хранителю в /advance и /resetgame, выглядя как поломка кнопок.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, NoInspectionAvailable

from app.config import settings
from app.core.registry import RUN_START_KEY
from app.db import SessionLocal
from app.handlers import admin as admin_mod
from app.handlers import cmd_advance, cmd_resetgame
from app.models import (
    Card,
    Chat,
    Income,
    Payout,
    Player,
    PreparedDay,
    RevoteGrant,
    Round,
    RoundStatus,
    Stake,
    StoryBeat,
    Vote,
    WatcherState,
    WinRule,
)
from app.ops import PAUSE_KEY, set_game_paused
from app.rounds import heal_stale_rounds
from app.rounds import lifecycle as lifecycle_mod
from app.rounds import materialization as materialization_mod

ADMIN_ID = 4242


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=ADMIN_ID),
        bot=AsyncMock(),
        text=text,
        answer=AsyncMock(),
    )


def _round(day_index: int, status: RoundStatus, *, voting_in_minutes: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=status,
        win_rule=WinRule.MAJORITY,
        chapter_title=f"День {day_index}",
        chapter_text="Текст.",

        opens_at=now - timedelta(hours=25),
        voting_ends_at=now + timedelta(minutes=voting_in_minutes),
        tally_ends_at=now + timedelta(minutes=voting_in_minutes),
    )
    for position in range(3):
        round_row.cards.append(
            Card(
                position=position,
                title=f"Тропа {position}",
                consequence="Канон дня.",
            )
        )
    return round_row


async def _wipe(days: list[int]) -> None:
    """Полная уборка дней со всеми детьми: осиротевшие ставки иначе
    «прилипают» к новым дням через переиспользованные id в SQLite."""
    async with SessionLocal() as db:
        await db.execute(delete(StoryBeat).where(StoryBeat.day_index.in_(days)))
        await db.execute(delete(Card))
        await db.execute(delete(Vote))
        await db.execute(delete(Stake))
        await db.execute(delete(Income))
        await db.execute(delete(RevoteGrant))
        await db.execute(delete(Payout))
        for round_row in (
            await db.execute(select(Round).where(Round.day_index.in_(days)))
        ).scalars().all():
            await db.delete(round_row)
        await db.execute(
            WatcherState.__table__.delete().where(WatcherState.key == PAUSE_KEY)
        )
        await db.commit()


@pytest.fixture()
def offline_all(monkeypatch, tmp_path):
    """Жизненный цикл без сети: генерация офлайн, картинки не качаются."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))


async def test_heal_stale_rounds_closes_orphan_and_writes_canon(session) -> None:
    """День, застрявший OPEN позади актуального, дочитывается сам."""
    stale = _round(700, RoundStatus.OPEN, voting_in_minutes=-30)
    current = _round(701, RoundStatus.OPEN, voting_in_minutes=600)
    session.add_all([stale, current])
    await session.commit()
    try:
        healed = await heal_stale_rounds(session)
        assert healed >= 1
        statuses = dict(
            (await session.execute(select(Round.day_index, Round.status))).all()
        )
        assert statuses[700] == RoundStatus.CLOSED
        assert statuses[701] == RoundStatus.OPEN  # актуальный не тронут
        beat = (
            await session.execute(select(StoryBeat).where(StoryBeat.day_index == 700))
        ).scalar_one()
        # Канон взят из реальной карты дня (победитель выбирает жребий сида).
        assert beat.winning_title in {f"Тропа {i}" for i in range(3)}
        assert beat.winning_text == "Канон дня."
    finally:
        await _wipe([700, 701])


async def test_heal_stale_rounds_survives_midloop_failure(session, monkeypatch) -> None:
    """Сбой одного застрявшего дня не убивает лечение следующих.

    Иницидент: session.rollback() в except истёк ВСЕ инстансы, на следующей
    итерации прямой read round_row.status дал MissingGreenlet (lazy load вне
    greenlet-контекста) — heal падал целиком, остальные дни висели вечно."""
    earliest = _round(712, RoundStatus.OPEN, voting_in_minutes=-30)
    stuck = _round(713, RoundStatus.OPEN, voting_in_minutes=-30)
    current = _round(714, RoundStatus.OPEN, voting_in_minutes=600)
    session.add_all([earliest, stuck, current])
    await session.commit()

    real_finish = lifecycle_mod.finish_tally
    calls = 0

    async def flaky_finish(sess, round_row):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("симуляция сбоя подсчёта")
        return await real_finish(sess, round_row)

    monkeypatch.setattr(lifecycle_mod, "finish_tally", flaky_finish)
    try:
        healed = await heal_stale_rounds(session)
        statuses = dict(
            (await session.execute(select(Round.day_index, Round.status))).all()
        )
        # Первый день упал и откатился; ВТОРОЙ вылечен тем же прогоном.
        assert healed >= 1
        assert statuses[712] == RoundStatus.TALLYING  # сбой; повторится в тике
        assert statuses[713] == RoundStatus.CLOSED    # пережил откат предшественника
        assert statuses[714] == RoundStatus.OPEN      # актуальный не тронут
    finally:
        await _wipe([712, 713, 714])


async def test_heal_skips_when_nothing_stuck(session) -> None:
    current = _round(710, RoundStatus.OPEN, voting_in_minutes=600)
    session.add(current)
    await session.commit()
    try:
        assert await heal_stale_rounds(session) == 0
    finally:
        await _wipe([710])


async def test_advance_auto_resumes_from_pause(offline_all, monkeypatch) -> None:
    """/advance под стоп-краном больше не отказывает: снимает паузу сам."""
    stale_day = 720
    round_row = _round(stale_day, RoundStatus.OPEN, voting_in_minutes=-15)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.commit()
        await set_game_paused(db, True, "техработы")

    # Создание следующего дня тяжёлое — подменяем: интересует снятие паузы.
    monkeypatch.setattr(
        admin_mod,
        "create_next_round_detailed",
        AsyncMock(return_value=(round_row, False)),
    )
    try:
        message = _message("/advance")
        await cmd_advance(message)
    finally:
        await _wipe([stale_day])
    texts = [c.args[0] for c in message.answer.await_args_list if c.args]
    joined = "\n".join(texts)
    assert "Пауза снята автоматически" in joined
    assert "сначала /resume" not in joined
    async with SessionLocal() as db:
        row = await db.get(WatcherState, PAUSE_KEY)
        assert bool(row and row.value) is False


async def test_advance_refuses_fresh_open_day(offline_all) -> None:
    """/advance не закрывает свежеоткрытый день.

    Инцидент: тик уже закрыл N и открыл N+1, а /advance брал свежий день как
    «актуальный» — закрывал его с нулём голосов и жребием, затем плодил N+2
    (target по latest+1). Свежий день не трогаем: у застрявшего окно
    голосования уже истекло."""
    fresh_day = 721
    round_row = _round(fresh_day, RoundStatus.OPEN, voting_in_minutes=600)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.commit()
    try:
        message = _message("/advance")
        await cmd_advance(message)
        replies = [c.args[0] for c in message.answer.await_args_list if c.args]
        assert any("ещё голосуется" in text for text in replies)
        async with SessionLocal() as db:
            status = (
                await db.execute(
                    select(Round.status).where(Round.day_index == fresh_day)
                )
            ).scalar_one()
        assert status == RoundStatus.OPEN  # не закрыт, день живёт
    finally:
        await _wipe([fresh_day])


async def test_resetgame_auto_resumes_from_pause(offline_all) -> None:
    """/resetgame confirm под паузой снимает её и выполняет сброс до дня 1."""
    seed_old = _round(730, RoundStatus.CLOSED, voting_in_minutes=-40)
    seed_old.winner_card = 0
    async with SessionLocal() as db:
        db.add(seed_old)
        await db.commit()
        await set_game_paused(db, True, "техработы")
    try:
        message = _message("/resetgame confirm keepstory")
        await cmd_resetgame(message)

        texts = [c.args[0] for c in message.answer.await_args_list if c.args]
        joined = "\n".join(texts)
        assert "Пауза снята автоматически" in joined
        assert "сначала /resume" not in joined
        assert "Игра обнулена" in joined  # сброс действительно прошёл
        async with SessionLocal() as db:
            row = await db.get(WatcherState, PAUSE_KEY)
            assert bool(row and row.value) is False
            days = (
                await db.execute(select(Round.day_index).order_by(Round.day_index.asc()))
            ).scalars().all()
        assert days and days[0] == 1  # мир начался с первого дня
    finally:
        async with SessionLocal() as db:
            for round_row in (await db.execute(select(Round))).scalars().all():
                await db.delete(round_row)
            await db.execute(delete(StoryBeat))
            await db.execute(
                WatcherState.__table__.delete().where(WatcherState.key == PAUSE_KEY)
            )
            await db.commit()


async def test_resetgame_refused_with_unfinalized_stakes(offline_all) -> None:
    """Вторая линия защиты: ставки дня без финализации — деньги игроков в
    казнее; сброс обязан отказать, а не стирать память об обязательствах."""
    from app.models import Stake

    round_row = _round(740, RoundStatus.CLOSED, voting_in_minutes=-40)
    round_row.winner_card = 0  # payouts_finalized остаётся False
    async with SessionLocal() as db:
        db.add(round_row)
        await db.flush()
        db.add(
            Stake(
                round_id=round_row.id,
                player_id=1,
                amount_nanotons=300_000_000,
                tx_hash="reset-guard-tx",
                status="confirmed",
            )
        )
        await db.commit()
    try:
        message = _message("/resetgame confirm keepstory")
        await cmd_resetgame(message)
        texts = [c.args[0] for c in message.answer.await_args_list if c.args]
        joined = "\n".join(texts)
        assert "неразыгранные ставки" in joined and "740" in joined
        assert not any("Игра обнулена" in t for t in texts)
        async with SessionLocal() as db:
            alive = (
                await db.execute(select(Round).where(Round.day_index == 740))
            ).scalar_one()
            assert alive is not None  # ничего не стёрто
    finally:
        await _wipe([740])


async def test_announce_new_day_is_text_only(monkeypatch, tmp_path) -> None:
    """Шаблонный день: медиа снято — анонс идёт текстом статуса с кнопками."""
    from app.broadcast import announce_new_day

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    cover = Path(tmp_path) / "day8001_cover.jpg"
    cover.write_bytes(b"\xff\xd8\xfffakejpeg")
    round_row = Round(
        id=800_001,
        day_index=8001,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="Один кадр",
        chapter_text="Текст.",


        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC) + timedelta(hours=20),
        tally_ends_at=datetime.now(UTC) + timedelta(hours=21),
    )
    round_row.cards.append(
        Card(position=0, title="t", consequence="c")
    )
    async with SessionLocal() as db:
        db.add(Chat(id=555_777, type="group", active=True))
        await db.commit()
    try:
        bot = SimpleNamespace(
            send_photo=AsyncMock(),
            send_media_group=AsyncMock(),
            send_message=AsyncMock(),
        )
        delivered = await announce_new_day(bot, round_row)
        assert delivered == [555_777]
        bot.send_media_group.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_awaited_once()  # статус с кнопками дошёл
    finally:
        async with SessionLocal() as db:
            chat = await db.get(Chat, 555_777)
            if chat is not None:
                await db.delete(chat)
            await db.commit()


# ---------- Сброс игры: FK-полный wipe (инцидент incomes_round_id_fkey) ----------


async def test_resetgame_wipes_income_links(offline_all) -> None:
    """Регрессия: Income.round_id держит FK на rounds — сброс падал
    ForeignKeyViolation и молча откатывался целиком."""
    from app.models import Income

    player_id = 880_001
    round_row = _round(900, RoundStatus.CLOSED, voting_in_minutes=-40)
    round_row.winner_card = 0
    async with SessionLocal() as db:
        db.add(Player(id=player_id, username="reset_p"))
        db.add(round_row)
        await db.flush()
        db.add(Income(kind="ton", amount_nanotons=1, round_id=round_row.id, unit_ref="r1"))
        await db.commit()

    message = _message("/resetgame confirm keepstory")
    await cmd_resetgame(message)

    texts = [c.args[0] for c in message.answer.await_args_list if c.args]
    assert any("Игра обнулена" in t for t in texts)
    async with SessionLocal() as db:
        assert (await db.execute(select(Income))).scalars().all() == []
        days = (
            await db.execute(select(Round.day_index).order_by(Round.day_index.asc()))
        ).scalars().all()
    assert days and days[0] == 1
    await _wipe([1])


async def test_finalize_pending_payouts_recovers_crashed_closed_day(session) -> None:
    """Краш между коммитом finish_tally и finalize_day_payouts оставляет день
    CLOSED с payouts_finalized=false: догон создаёт возвраты и ставит маркер,
    а копилки недели/месяца больше не ждут закрытый день вечно."""
    from app.stakes import current_network, finalize_pending_payouts
    from app.ton_utils import to_nano

    player_id = 881_001
    day = 901
    round_row = _round(day, RoundStatus.CLOSED, voting_in_minutes=-40)
    round_row.winner_card = 0
    round_row.vote_counts_json = "{}"
    session.add(
        Player(id=player_id, username="crashed_p", wallet_address="0:" + "00" * 16)
    )
    session.add(round_row)
    await session.flush()
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=player_id,
            amount_nanotons=to_nano(0.3),
            tx_hash="crash-tx",
            status="confirmed",
            network=current_network(),
        )
    )
    await session.commit()
    try:
        created = await finalize_pending_payouts(session)
        assert created == 1
        paid = (
            await session.execute(select(Payout).where(Payout.round_id == round_row.id))
        ).scalars().all()
        assert len(paid) == 1 and paid[0].kind == "refund"
        claimed = await session.get(Round, round_row.id)
        assert claimed.payouts_finalized is True
        # Идемпотентен: повторный тик ничего не создаёт (claim пройден).
        assert await finalize_pending_payouts(session) == 0
        assert (
            len(
                (
                    await session.execute(
                        select(Payout).where(Payout.round_id == round_row.id)
                    )
                ).scalars().all()
            )
            == 1
        )
    finally:
        await _wipe([day])


async def test_finalize_pending_payouts_skips_historical_day(session) -> None:
    """Регрессия прода: payouts_finalized заводился с server_default=0, поэтому у
    всей истории закрытых дней маркер false. finalize_day_payouts создаёт Payout
    заново из ставок и НЕ проверяет уже существующие выплаты — без recency-гарда
    первый же тик этой версии пересоздал бы выплаты за всю историю (дубли
    призов/возвратов). Старый день догон не трогает."""
    from app.stakes import current_network, finalize_pending_payouts
    from app.ton_utils import to_nano

    day = 904
    round_row = _round(day, RoundStatus.CLOSED, voting_in_minutes=-40)
    round_row.winner_card = 0
    round_row.vote_counts_json = "{}"
    # Уводим день за окно догона (100 дней назад — фиксировано, не выводится
    # из настройки, иначе тест масштабировался бы вместе с гардом).
    stale = datetime.now(UTC) - timedelta(days=100)
    round_row.opens_at = stale
    round_row.voting_ends_at = stale
    round_row.tally_ends_at = stale
    session.add(Player(id=881_020, username="old_p", wallet_address="0:" + "00" * 16))
    session.add(round_row)
    await session.flush()
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=881_020,
            amount_nanotons=to_nano(0.3),
            tx_hash="old-tx",
            status="confirmed",
            network=current_network(),
        )
    )
    await session.commit()
    try:
        assert await finalize_pending_payouts(session) == 0
        paid = (
            await session.execute(select(Payout).where(Payout.round_id == round_row.id))
        ).scalars().all()
        assert paid == []
    finally:
        await _wipe([day])


async def test_finalize_pending_payouts_survives_midloop_crash(session, monkeypatch) -> None:
    """Упавшая финализация одного дня не обрушивает тик: её хвост откатывается,
    остальные закрытые дни догоняются, а больной день честно остаётся
    unfinalized и повторится следующим тиком."""
    from app.stakes import current_network, finalize_pending_payouts
    from app.ton_utils import to_nano

    days = [902, 903]
    for i, day in enumerate(days):
        round_row = _round(day, RoundStatus.CLOSED, voting_in_minutes=-40)
        round_row.winner_card = 0
        round_row.vote_counts_json = "{}"
        session.add(Player(id=881_010 + i))
        session.add(round_row)
        await session.flush()
        session.add(
            Stake(
                round_id=round_row.id,
                player_id=881_010 + i,
                amount_nanotons=to_nano(0.2),
                tx_hash=f"f-tx-{day}",
                status="confirmed",
                network=current_network(),
            )
        )
    await session.commit()

    import app.stakes as stakes_mod

    boom_id = (await session.execute(select(Round.id).where(Round.day_index == days[0]))).scalar_one()
    real = stakes_mod.finalize_day_payouts

    async def flaky(_session, round_row):
        if round_row.id == boom_id:
            raise RuntimeError("synthetic crash")
        return await real(_session, round_row)

    monkeypatch.setattr(stakes_mod, "finalize_day_payouts", flaky)
    try:
        created = await finalize_pending_payouts(session)
        assert created == len(days) - 1
        rows = (
            await session.execute(select(Round).where(Round.day_index.in_(days)))
        ).scalars().all()
        by_index = {row.day_index: row for row in rows}
        assert by_index[days[0]].payouts_finalized is False
        assert by_index[days[1]].payouts_finalized is True
    finally:
        await _wipe(days)


def test_every_round_foreign_key_table_is_wiped() -> None:
    """Будущее-проф: любая новая таблица с FK на rounds обязана попасть в
    reset_game, иначе сброс снова молча откатится по ForeignKeyViolation."""
    import inspect

    from app.models import Base

    wiped = {"payouts", "stakes", "votes", "revote_grants", "cards", "incomes", "prepared_days", "status_post"}
    referencing: set[str] = set()
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "rounds":
                referencing.add(table.name)
                assert table.name in wiped, (
                    f"таблица {table.name} ссылается на rounds, но не стирается в reset_game"
                )
    source = inspect.getsource(__import__("app.rounds", fromlist=["reset_game"]).reset_game)
    assert "delete(Income)" in source


# ---------- Слепые ветки жизненного цикла: сбои открытия дня, гонки, heal ----------


async def test_ensure_current_round_keeps_recent_closed(session) -> None:
    """Закрытый день с живым таймером подсчёта не порождает новый (строки 219-220)."""
    round_row = _round(9970, RoundStatus.CLOSED, voting_in_minutes=30)
    round_row.winner_card = 0
    session.add(round_row)
    await session.commit()
    kept_id = round_row.id
    got = await lifecycle_mod.ensure_current_round(session)
    assert got.id == kept_id


async def test_public_round_view_hides_counts_until_closed() -> None:
    open_round = _round(9960, RoundStatus.OPEN, voting_in_minutes=600)
    view = lifecycle_mod.public_round_view(open_round)
    assert "winner_card" not in view  # счёт дня в открытом доступе не светится
    assert "vote_counts" not in view
    assert len(view["cards"]) == 3

    closed_round = _round(9961, RoundStatus.CLOSED, voting_in_minutes=-30)
    closed_round.winner_card = 2
    closed_round.vote_counts_json = '{"0": 1, "1": 0, "2": 3}'
    view = lifecycle_mod.public_round_view(closed_round)
    assert view["winner_card"] == 2
    assert view["vote_counts"] == {"0": 1, "1": 0, "2": 3}


async def test_open_day_fails_fast_when_latest_round_unreadable(session, monkeypatch) -> None:
    """Сбой чтения раундов на первом шаге: rollback + честный re-raise (59-62)."""

    async def boom(_sess):
        raise RuntimeError("нет соединения")

    monkeypatch.setattr(lifecycle_mod, "get_latest_round", boom)
    with pytest.raises(RuntimeError, match="нет соединения"):
        await lifecycle_mod.create_next_round_detailed(session)


async def test_open_day_fails_fast_when_round_query_broken(session, monkeypatch) -> None:
    """Сбой запроса day_index: лог + rollback + re-raise (строки 73-76)."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("нет раундов")

    monkeypatch.setattr(lifecycle_mod, "select", boom)
    with pytest.raises(RuntimeError, match="нет раундов"):
        await lifecycle_mod.create_next_round_detailed(session)


async def test_open_day_fails_fast_when_prepared_day_query_broken(session, monkeypatch) -> None:
    """Сбой чтения заготовки дня: лог + rollback + re-raise (строки 82-85)."""
    monkeypatch.setattr(lifecycle_mod, "PreparedDay", object)
    with pytest.raises(NoInspectionAvailable):
        await lifecycle_mod.create_next_round_detailed(session)


async def test_open_day_drops_stale_prepared_day(offline_all, session) -> None:
    """Устаревшая заготовка под целевой день выбрасывается до открытия (86-89)."""
    latest = (
        await session.execute(
            select(Round.day_index).order_by(Round.day_index.desc()).limit(1)
        )
    ).scalar_one_or_none()
    target = (latest or 0) + 1
    session.add(PreparedDay(day_index=target, payload='{"устарело": true}'))
    await session.commit()
    round_row, created = await lifecycle_mod.create_next_round_detailed(session)
    assert created is True and round_row.day_index == target
    stale = await session.get(PreparedDay, target)
    assert stale is None  # строки 86-89: устаревшая заготовка удалена


async def test_open_day_materialize_race_returns_existing(offline_all, session, monkeypatch) -> None:
    """IntegrityError при материализации: откат и возврат существующего дня (111-116)."""

    async def race(_sess, _payload, _latest):
        raise IntegrityError("INSERT rounds", {}, Exception("гонка открытия"))

    monkeypatch.setattr(lifecycle_mod, "_materialize_round", race)

    # а) в базе есть раунды → возвращается существующий (строка 116)
    seed = _round(9910, RoundStatus.OPEN, voting_in_minutes=600)
    session.add(seed)
    await session.commit()
    got, created = await lifecycle_mod.create_next_round_detailed(session)
    assert created is False and got.id == seed.id

    # б) база «пуста» для тика → re-raise, сбой увиден (строки 114-115)
    monkeypatch.setattr(lifecycle_mod, "get_latest_round", AsyncMock(return_value=None))
    with pytest.raises(IntegrityError):
        await lifecycle_mod.create_next_round_detailed(session)


async def test_open_day_commit_race_returns_existing(offline_all, session, monkeypatch) -> None:
    """Конфликт вставки всплывает на commit (а не раньше): 117-124."""
    older = _round(9919, RoundStatus.CLOSED, voting_in_minutes=-30)
    anchor = _round(9920, RoundStatus.OPEN, voting_in_minutes=600)
    session.add_all([older, anchor])
    await session.commit()
    older_id, anchor_id = older.id, anchor.id
    # Старый раунд отсоединяем: иначе add(дубликат) — identity-conflict (SAWarning).
    session.expunge(older)

    async def collide(sess, _payload, _latest):
        # PK берём у СТАРОГО раунда: якорь get_latest подгрузит в карту
        # идентичности, а конфликт с ним SQLAlchemy штумует SAWarning.
        duplicate = Round(
            id=older_id,
            day_index=9921,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="X",
            chapter_text="X",
            opens_at=datetime.now(UTC),
            voting_ends_at=datetime.now(UTC) + timedelta(hours=20),
            tally_ends_at=datetime.now(UTC) + timedelta(hours=21),
        )
        sess.add(duplicate)
        return duplicate

    monkeypatch.setattr(lifecycle_mod, "_materialize_round", collide)
    # Без запроса состояния внутри _stamp автофлеш не сработает раньше commit.
    monkeypatch.setattr(
        materialization_mod, "money_mode_enabled", AsyncMock(return_value=True)
    )
    got, created = await lifecycle_mod.create_next_round_detailed(session)
    assert created is False and got.id == anchor_id


async def test_finish_tally_returns_when_not_tallying(session) -> None:
    """Не-TALLYING день: подсчёт не трогаем, отдаём как есть (строки 373-375)."""
    round_row = _round(9941, RoundStatus.OPEN, voting_in_minutes=600)
    session.add(round_row)
    await session.commit()
    loaded, closed_here = await lifecycle_mod.finish_tally(session, round_row)
    assert closed_here is False
    assert loaded.status == RoundStatus.OPEN


async def test_finish_tally_malformed_tie_entropy_falls_back(session) -> None:
    """Нечитаемый жребий блока не валит подсчёт: заметка без ссылки (415-420)."""
    round_row = _round(9930, RoundStatus.TALLYING, voting_in_minutes=-30)
    round_row.tie_entropy = "мусор-без-двоеточия"
    session.add(Player(id=991001))
    session.add(Player(id=991002))
    session.add(round_row)
    await session.flush()
    session.add(Vote(round_id=round_row.id, player_id=991001, card_position=0))
    session.add(Vote(round_id=round_row.id, player_id=991002, card_position=1))
    await session.commit()
    loaded, closed_here = await lifecycle_mod.finish_tally(session, round_row)
    assert closed_here is True
    assert loaded.status == RoundStatus.CLOSED
    assert loaded.tie_note and "жребий блока" not in loaded.tie_note


async def test_finish_tally_lost_update_race(session) -> None:
    """Проигравшая гонку закрытия сторона откатывается и отдаёт состояние (452-455)."""
    round_row = _round(9940, RoundStatus.TALLYING, voting_in_minutes=-30)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.commit()
        # конкурирующий процесс закрыл день; инстанс отстал со статусом TALLYING
        loaded_status = round_row.status  # грузим значение до отсоединения
        assert loaded_status == RoundStatus.TALLYING
        db.expunge(round_row)
        await db.commit()
        async with SessionLocal() as other:
            await other.execute(
                update(Round).where(Round.id == round_row.id).values(status=RoundStatus.CLOSED)
            )
            await other.commit()
        loaded, closed_here = await lifecycle_mod.finish_tally(db, round_row)
        assert closed_here is False
        assert loaded is not None and loaded.id == round_row.id
    await _wipe([9940])


async def test_finish_tally_commit_race_on_duplicate_story_beat(session) -> None:
    """Конфликт канона (day_index уникален): commit падает, закрытие откатывается (475-478)."""
    round_row = _round(9950, RoundStatus.TALLYING, voting_in_minutes=-30)
    session.add(round_row)
    session.add(
        StoryBeat(
            day_index=9950,
            winning_title="занято",
            winning_text="x",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()
    # Отсоединяем до гонки: rollback() истекает прикреплённые инстансы, а чтение
    # атрибута с истёкшего объекта — lazy load вне greenlet (MissingGreenlet).
    round_row_id = round_row.id
    session.expunge(round_row)
    loaded, closed_here = await lifecycle_mod.finish_tally(session, round_row)
    assert closed_here is False
    assert loaded is not None and loaded.id == round_row_id
    assert loaded.status == RoundStatus.TALLYING  # закрытие откатилось


async def test_close_voting_lost_claim_race(session) -> None:
    """Второй закрывающий молча уходит: UPDATE не захватил день (строки 329-330)."""
    round_row = _round(9980, RoundStatus.OPEN, voting_in_minutes=-30)
    async with SessionLocal() as db1:
        db1.add(round_row)
        await db1.commit()
        assert round_row.status == RoundStatus.OPEN  # кэш значений до отсоединения
        db1.expunge(round_row)
        await db1.commit()
        async with SessionLocal() as other:
            await other.execute(
                update(Round).where(Round.id == round_row.id).values(status=RoundStatus.TALLYING)
            )
            await other.commit()
        got = await lifecycle_mod.close_voting(db1, round_row)
        assert got is round_row
        status = (
            await db1.execute(select(Round.status).where(Round.id == round_row.id))
        ).scalar_one()
        assert status == RoundStatus.TALLYING
    await _wipe([9980])


async def test_heal_survives_finalize_failure(session, monkeypatch) -> None:
    """Сбой финализации ставок вылеченного дня: warning + rollback, лечение живёт (264-271)."""
    stale = _round(750, RoundStatus.OPEN, voting_in_minutes=-30)
    current = _round(751, RoundStatus.OPEN, voting_in_minutes=600)
    session.add_all([stale, current])
    await session.commit()

    import app.stakes as stakes_mod

    monkeypatch.setattr(
        stakes_mod,
        "finalize_day_payouts",
        AsyncMock(side_effect=RuntimeError("касса упала")),
    )
    try:
        healed = await heal_stale_rounds(session)
        assert healed >= 1
        statuses = dict(
            (await session.execute(select(Round.day_index, Round.status))).all()
        )
        assert statuses[750] == RoundStatus.CLOSED  # подсчёт прошёл несмотря на сбой
    finally:
        await _wipe([750, 751])


async def test_reset_game_full_wipe_rewrites_anchor(offline_all, session) -> None:
    """keep_story=False стирает канон; повторный сброс перезаписывает якорь (165, 178)."""
    session.add(
        StoryBeat(
            day_index=9902,
            winning_title="t",
            winning_text="x",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()
    first = await lifecycle_mod.reset_game(session)  # строка 165: delete(StoryBeat)
    assert first.day_index == 1
    assert (await session.execute(select(StoryBeat))).scalar_one_or_none() is None
    row = await session.get(WatcherState, RUN_START_KEY)
    first_anchor = row.value if row is not None else None
    assert first_anchor  # якорь свежего забега записан (строка 176)

    second = await lifecycle_mod.reset_game(session)  # строка 178: перезапись якоря
    assert second.day_index == 1
    row = await session.get(WatcherState, RUN_START_KEY)
    assert row is not None and row.value
