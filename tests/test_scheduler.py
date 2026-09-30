"""Планировщик e2e: тик закрывает голосование, готовит и открывает следующий день.

Работаем с глобальной БД (SessionLocal), как настоящий тик; сетевые
генераторы заменены мгновенными — интересует только конечный автомат дня.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Card, PreparedDay, Round, RoundStatus, WinRule
from app.scheduler import tick
from app.stakes import current_network


@pytest.fixture(autouse=True)
def offline_generation(monkeypatch):
    """Шаблонная генерация работает офлайн и без стабов; тон только выключаем,
    чтобы тик не трогал сеть."""
    monkeypatch.setattr(settings, "ton_enabled", False)


async def _seed(day_index: int, status: RoundStatus, *, voting_in: timedelta, tally_in: timedelta) -> Round:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        round_row = Round(
            day_index=day_index,
            status=status,
            win_rule=WinRule.MAJORITY,
            chapter_title=f"День {day_index}",
            chapter_text="Текст.",


            opens_at=now - timedelta(hours=30),
            voting_ends_at=now + voting_in,
            tally_ends_at=now + tally_in,
            winner_card=0 if status == RoundStatus.TALLYING else None,
            vote_counts_json='{"0": 1}' if status == RoundStatus.TALLYING else "{}",
        )
        db.add(round_row)
        await db.commit()
        return round_row.id


async def _cleanup(*day_indexes: int) -> None:
    async with SessionLocal() as db:
        await db.execute(Round.__table__.delete().where(Round.day_index.in_(day_indexes)))
        await db.execute(delete(PreparedDay).where(PreparedDay.day_index.in_([d + 1 for d in day_indexes])))
        await db.commit()


async def _status_of(day_index: int) -> RoundStatus | None:
    async with SessionLocal() as db:
        row = (
            await db.execute(select(Round.status).where(Round.day_index == day_index).limit(1))
        ).scalar_one_or_none()
    return row


async def _drain_background(timeout: float = 10.0) -> None:
    """Тик плодит фоновые задачи (прегенерация, тизер, диспетчер выплат) —
    даём им закрыть сессии БД, иначе SQLite-лок валит очистку соседних тестов."""
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if pending:
        done, pending = await asyncio.wait(pending, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def test_tick_closes_voting_when_window_over() -> None:
    await _seed(9531, RoundStatus.OPEN, voting_in=timedelta(minutes=-5), tally_in=timedelta(hours=1))
    try:
        await tick(None)
        assert await _status_of(9531) == RoundStatus.TALLYING
    finally:
        # Тизер окон подсчёта уходит фоном в этом же окне — ждём, чтобы
        # его сессия БД не держала SQLite-лок для следующего теста.
        await _drain_background()
        await _cleanup(9531)


async def test_tick_does_not_prepare_next_day_during_tally_window() -> None:
    """Прегенерация убрана: в (легаси) окне подсчёта заготовка следующего
    дня не создаётся — день откроется инлайн-генерацией при финализации."""
    await _seed(9541, RoundStatus.TALLYING, voting_in=timedelta(hours=-3), tally_in=timedelta(minutes=20))
    try:
        await tick(None)
        await _drain_background()
        async with SessionLocal() as db:
            prepared = (
                await db.execute(
                    select(PreparedDay).where(PreparedDay.day_index == 9542).limit(1)
                )
            ).scalar_one_or_none()
        assert prepared is None
    finally:
        await _cleanup(9541)


async def test_tick_finishes_day_and_opens_next() -> None:
    round_id = await _seed(9551, RoundStatus.TALLYING, voting_in=timedelta(hours=-3), tally_in=timedelta(minutes=-1))
    try:
        await tick(None)
        assert await _status_of(9551) == RoundStatus.CLOSED
        # Итоги формируются в тике сразу (до создания нового дня). Пост нового
        # дня уходит фоном, когда готов нейро-контент, — ждём фоновые джобы,
        # чтобы проверить, что день всё же открылся и день-итог завершён.
        await _drain_background()
        async with SessionLocal() as db:
            fresh = (
                await db.execute(select(Round).where(Round.day_index == 9552).limit(1))
            ).scalar_one_or_none()
            card_count = 0 if fresh is None else (
                await db.execute(
                    select(func.count()).select_from(Card).where(Card.round_id == fresh.id)
                )
            ).scalar_one()
        assert fresh is not None
        assert fresh.status == RoundStatus.OPEN
        assert fresh.chapter_title
        assert card_count == 3
        del round_id
    finally:
        await _drain_background()
        await _cleanup(9551, 9552)


async def test_start_scheduler_registers_only_zero_arg_jobs(monkeypatch) -> None:
    """Инцидент-регрессия: джоба с обязательным bot роняла boot_game целиком —
    игра оставалась без тиков и watcher'а. Каждая джоба обязана вызываться
    без аргументов, а кривая регистрация не смеет убить остальные."""
    import inspect

    from app import scheduler as scheduler_mod

    registered: list[tuple[str, object, str, int | None]] = []

    def fake_add_job(func, trigger, *, id, **kwargs):
        registered.append((id, func, trigger, kwargs.get("seconds")))

    monkeypatch.setattr(scheduler_mod.scheduler, "add_job", fake_add_job)
    monkeypatch.setattr(scheduler_mod.scheduler, "start", lambda: None)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_watch_interval_seconds", 123)

    scheduler_mod.start_scheduler()

    ids = [job_id for job_id, _func, _trigger, _sec in registered]
    assert {"way-tick", "db-backup", "ton-watch", "ton-settle",
            "ws-cleanup", "vote-reminder"} <= set(ids)
    watch_trigger, watch_seconds = next(
        (trigger, seconds) for job_id, _fn, trigger, seconds in registered if job_id == "ton-watch"
    )
    assert watch_trigger == "interval"
    assert watch_seconds == 123, "частота наблюдателя берётся из настроек (рычаг экономии квоты)"
    for job_id, fn, _trigger, _sec in registered:
        try:
            inspect.signature(fn).bind()
        except TypeError as exc:
            raise AssertionError(f"джоба {job_id} требует аргументы: {exc}") from exc


def test_shutdown_scheduler_safe_when_never_started() -> None:
    """Остановка сервиса до старта планировщика не должна падать."""
    from app.scheduler import shutdown_scheduler

    if not scheduler_mod_running():
        shutdown_scheduler()  # не падает


def scheduler_mod_running() -> bool:
    from app.scheduler import scheduler as sched

    return bool(sched.running)


async def test_alert_guarded_notifies_admin_and_swallows(monkeypatch) -> None:
    """П.13: сломавшаяся фоновая задача бьёт админа в лоб, но не роняет
    планировщик — исключение не пробрасывается наружу."""
    from app import scheduler as scheduler_mod

    notified: list[str] = []

    async def fake_notify_admins(bot, text):
        notified.append(text)

    async def boom():
        raise RuntimeError("backup-сломался")

    async def fine():
        return 42

    monkeypatch.setattr(scheduler_mod, "_bot", object())
    monkeypatch.setattr(scheduler_mod.settings, "admin_ids", "1,2")
    monkeypatch.setattr("app.ops.notify_admins", fake_notify_admins)

    await scheduler_mod._alert_guarded("db-backup", boom)
    assert notified and "db-backup" in notified[0] and "backup-сломался" in notified[0]

    notified.clear()
    assert await scheduler_mod._alert_guarded("weekly-report", fine) is None
    assert not notified  # успешная задача молчит


# --- Покрытие вспомогательных джоб планировщика ----------------------------

async def _make_round(
    day_index: int,
    status: RoundStatus = RoundStatus.OPEN,
    *,
    voting_in: timedelta | None = None,
    tally_in: timedelta | None = None,
) -> int:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        r = Round(
            day_index=day_index,
            status=status,
            win_rule=WinRule.MAJORITY,
            chapter_title=f"День {day_index}",
            chapter_text="Текст.",
            opens_at=now - timedelta(hours=30),
            voting_ends_at=now + (voting_in or timedelta(hours=10)),
            tally_ends_at=now + (tally_in or timedelta(hours=11)),
            winner_card=0 if status == RoundStatus.TALLYING else None,
            vote_counts_json='{"0": 1}' if status == RoundStatus.TALLYING else "{}",
        )
        db.add(r)
        await db.commit()
        return r.id


async def _clear_rounds() -> None:
    from app.models import Vote as _Vote

    async with SessionLocal() as db:
        await db.execute(delete(Card))
        await db.execute(delete(_Vote))
        await db.execute(delete(Round))
        await db.commit()


async def test_tick_returns_early_when_paused(monkeypatch) -> None:
    """Стоп-кран: тик помечает сердцебиение и замирает до конца."""
    from app import scheduler as sched

    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=True))

    async def bomb(*_args, **_kwargs):
        raise AssertionError("тик не должен идти дальше стоп-крана")

    monkeypatch.setattr(sched, "get_latest_round", bomb)
    await sched.tick()


async def test_tick_announces_first_round(monkeypatch) -> None:
    """Первый день (previous=None) анонсится сразу, закрытие не дёргается."""
    from app import scheduler as sched

    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: now)
    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(sched, "get_latest_round", AsyncMock(return_value=None))
    monkeypatch.setattr("app.rounds.heal_stale_rounds", AsyncMock(return_value=0))
    monkeypatch.setattr("app.rounds.get_run_anchor", AsyncMock(return_value={}))

    current = SimpleNamespace(
        id=1,
        status=RoundStatus.OPEN,
        voting_ends_at=now + timedelta(hours=1),
        tally_ends_at=now + timedelta(hours=2),
    )
    monkeypatch.setattr(sched, "ensure_current_round", AsyncMock(return_value=current))
    monkeypatch.setattr(sched, "claim_announcement", AsyncMock(return_value=True))
    announced = []
    async def fake_announce(bot, round_row):
        announced.append(round_row)
    monkeypatch.setattr(sched, "announce_new_day", fake_announce)
    monkeypatch.setattr(sched, "close_voting", AsyncMock(side_effect=AssertionError("нет голосов — чистый первый день")))
    monkeypatch.setattr(sched, "finish_tally", AsyncMock(side_effect=AssertionError("нет подсчёта в первый день")))

    await sched.tick()
    assert announced == [current]


async def test_tick_swallows_internal_error_and_rolls_back(monkeypatch) -> None:
    """Исключение внутри тика глотается (журналируется), наружу не летит."""
    from app import scheduler as sched

    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(sched, "get_latest_round", AsyncMock(side_effect=RuntimeError("взрыв")))
    monkeypatch.setattr("app.rounds.heal_stale_rounds", AsyncMock())
    monkeypatch.setattr("app.rounds.get_run_anchor", AsyncMock())

    await sched.tick()  # не поднимает исключение


async def test_tick_closes_finished_day_and_kicks_background_jobs(monkeypatch) -> None:
    """День с истекшим подсчётом финализируется: очки, выплаты, фоновые джобы."""
    from app import scheduler as sched

    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: now)
    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    # previous.id=9 > current.id=2 — ветка «новый день» не срабатывает.
    monkeypatch.setattr(sched, "get_latest_round", AsyncMock(return_value=SimpleNamespace(id=9)))
    monkeypatch.setattr("app.rounds.heal_stale_rounds", AsyncMock(return_value=0))
    monkeypatch.setattr("app.rounds.get_run_anchor", AsyncMock(return_value={}))

    current = SimpleNamespace(
        id=2,
        status=RoundStatus.TALLYING,
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now - timedelta(minutes=1),
    )
    monkeypatch.setattr(sched, "ensure_current_round", AsyncMock(return_value=current))

    awarded = []
    async def fake_award(session, round_row):
        awarded.append(round_row)
    monkeypatch.setattr(sched, "award_points", fake_award)

    finalized = []
    async def fake_finalize(session, round_row):
        finalized.append(round_row)
    monkeypatch.setattr("app.stakes.finalize_day_payouts", fake_finalize)

    finished = SimpleNamespace(id=2, day_index=7)
    monkeypatch.setattr(
        sched, "finish_tally", AsyncMock(return_value=(finished, True))
    )
    spawned: list[tuple[str, object]] = []
    def fake_spawn(coro, label):
        spawned.append((label, coro))
        coro.close()  # не запускаем реально — гасим RuntimeWarning
        return None
    monkeypatch.setattr(sched, "spawn", fake_spawn)

    await sched.tick()
    assert [p for p in awarded] == [finished]
    assert [p for p in finalized] == [finished]
    assert [label for label, _c in spawned] == [
        "payout_dispatch",
        "announce_results",
        "finalize_new_day",
        "retry_results",
        "retry_new_day",
    ]


def test_set_bot_sets_global(monkeypatch) -> None:
    from app import scheduler as sched

    bot = object()
    monkeypatch.setattr(sched, "_bot", None)
    sched.set_bot(bot)
    assert sched._bot is bot
    sched.set_bot(None)
    assert sched._bot is None


async def test_announce_results_job_delivers_and_swallows(monkeypatch) -> None:
    from app import scheduler as sched

    rid = await _make_round(9701, RoundStatus.CLOSED)
    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    seen = []

    async def fake_announce(b, finished):
        seen.append((b, finished.id))

    try:
        monkeypatch.setattr("app.broadcast.announce_results", fake_announce)
        await sched._announce_results_job(rid)
        assert seen == [(bot, rid)]

        async def boom_announce(b, finished):
            raise RuntimeError("рассылка — свой канал; падение глотается")
        monkeypatch.setattr("app.broadcast.announce_results", boom_announce)
        await sched._announce_results_job(rid)  # не роняется
    finally:
        await _clear_rounds()


async def test_announce_results_job_round_missing() -> None:
    from app import scheduler as sched

    await sched._announce_results_job(-1)  # warning, без падения


async def test_announce_results_job_marks_marker_after_delivery(monkeypatch) -> None:
    """Доставил итоги — поставил results_at: восстановитель этот день не тронет."""
    from app import scheduler as sched

    rid = await _make_round(9721, RoundStatus.CLOSED)
    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)

    async def fake_announce(b, finished):
        pass

    try:
        monkeypatch.setattr("app.broadcast.announce_results", fake_announce)
        monkeypatch.setattr("app.broadcast.announce_player_results", fake_announce)
        await sched._announce_results_job(rid)
        async with SessionLocal() as db:
            row = (await db.execute(select(Round).where(Round.id == rid))).scalar_one()
            assert row.results_at is not None
    finally:
        await _cleanup(9721)


async def test_retry_results_job_redelivers_crashed_day(monkeypatch) -> None:
    """CLOSED-день без маркера (краш между коммитом и рассылкой) досылается
    восстановителем ровно один раз — повторный прогон молчит."""
    from app import scheduler as sched

    rid = await _make_round(9722, RoundStatus.CLOSED)
    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    seen = []

    async def fake_announce(b, finished):
        seen.append(finished.id)

    try:
        monkeypatch.setattr("app.broadcast.announce_results", fake_announce)
        monkeypatch.setattr("app.broadcast.announce_player_results", fake_announce)
        await sched._retry_results_job()
        assert seen == [rid, rid]  # общий пост + личные итоги
        async with SessionLocal() as db:
            row = (await db.execute(select(Round).where(Round.id == rid))).scalar_one()
            assert row.results_at is not None
        seen.clear()
        await sched._retry_results_job()
        assert seen == []
    finally:
        await _cleanup(9722)


async def test_retry_results_rolls_back_marker_on_failure(monkeypatch) -> None:
    """Крах в середине рассылки снимает маркер откатом — повтор доставляет
    (at-least-once), а не теряет итоги навсегда."""
    from app import scheduler as sched

    rid = await _make_round(9723, RoundStatus.CLOSED)
    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    tries = {"n": 0}

    async def flaky(b, finished):
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("крах посередине рассылки")

    try:
        monkeypatch.setattr("app.broadcast.announce_results", flaky)
        monkeypatch.setattr("app.broadcast.announce_player_results", flaky)
        await sched._retry_results_job()  # падение проглочено
        async with SessionLocal() as db:
            row = (await db.execute(select(Round).where(Round.id == rid))).scalar_one()
            assert row.results_at is None  # маркер снят откатом

        async def ok(b, finished):
            pass

        monkeypatch.setattr("app.broadcast.announce_results", ok)
        monkeypatch.setattr("app.broadcast.announce_player_results", ok)
        await sched._retry_results_job()
        async with SessionLocal() as db:
            row = (await db.execute(select(Round).where(Round.id == rid))).scalar_one()
            assert row.results_at is not None  # повтор доставил
    finally:
        await _cleanup(9723)


async def test_finalize_new_day_job_opens_next_and_announces(monkeypatch) -> None:
    from app import scheduler as sched

    rid = await _make_round(9711, RoundStatus.CLOSED)
    epilogues = []
    async def fake_epilogue(session, finished):
        epilogues.append(finished.day_index)
        return "текст"
    monkeypatch.setattr("app.rounds.write_epilogue", fake_epilogue)
    marks = []
    async def fake_mark(session, finished):
        marks.append(finished.day_index)
    monkeypatch.setattr("app.leaderboard.mark_leaderboards_for_finished", fake_mark)
    created = []
    announced: list[int] = []
    async def fake_announce(bot, round_row):
        announced.append(round_row.id)
    monkeypatch.setattr(sched, "announce_new_day", fake_announce)

    async def fake_create(session, *, base_day_index):
        created.append(base_day_index)
        # Реальный открытый день: джоба анонсит его под claim-меткой (свежая
        # выборка из своей сессии — SimpleNamespace тут не маячит).
        nxt = Round(
            day_index=9713,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="День 9713",
            chapter_text="Текст.",
            opens_at=datetime.now(UTC) - timedelta(hours=1),
            voting_ends_at=datetime.now(UTC) + timedelta(hours=9),
            tally_ends_at=datetime.now(UTC) + timedelta(hours=10),
            vote_counts_json="{}",
        )
        async with SessionLocal() as db:
            db.add(nxt)
            await db.commit()
            await db.refresh(nxt)
        return nxt, True

    monkeypatch.setattr("app.rounds.create_next_round_detailed", fake_create)

    waited: list[bool] = []
    async def wait_results():
        waited.append(True)

    try:
        await sched._finalize_new_day_job(rid, wait_results=wait_results())
        assert epilogues and marks
        assert waited == [True]  # анонс ждёт доставку итогов
        assert announced and len(announced) == 1
        async with SessionLocal() as db:
            row = (await db.execute(select(Round).where(Round.day_index == 9713))).scalar_one()
            assert row.announced_at is not None  # claim-метка стоит: дубля не будет
    finally:
        await _clear_rounds()


async def test_finalize_new_day_job_round_missing() -> None:
    from app import scheduler as sched

    await sched._finalize_new_day_job(-1)  # warning, без падения


async def test_announce_round_releases_marker_on_failure(monkeypatch) -> None:
    """Сбой вещания снимает claim-метку (at-least-once): восстановитель может
    объявить день снова, а не потерять его пост навсегда."""
    from app import scheduler as sched

    did = 9724
    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    async with SessionLocal() as db:
        day = Round(
            day_index=did,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title=f"День {did}",
            chapter_text="Текст.",
            opens_at=datetime.now(UTC) - timedelta(hours=1),
            voting_ends_at=datetime.now(UTC) + timedelta(hours=9),
            tally_ends_at=datetime.now(UTC) + timedelta(hours=10),
            vote_counts_json="{}",
        )
        db.add(day)
        await db.commit()
        row_id = day.id

    async def boom(bot_, round_row):
        raise RuntimeError("сеть легла в середине рассылки")

    try:
        monkeypatch.setattr(sched, "announce_new_day", boom)
        async with SessionLocal() as db:
            day = await db.get(Round, row_id)
            with pytest.raises(RuntimeError):
                await sched._announce_round(db, day, bot)
            fresh = await db.get(Round, row_id)
            assert fresh.announced_at is None  # метка снята — повтор возможен

        # Повторный проход (следующий тик) доставляет и ставит метку.
        seen = []
        async def ok(bot_, round_row):
            seen.append(round_row.id)
        monkeypatch.setattr(sched, "announce_new_day", ok)
        async with SessionLocal() as db:
            day = await db.get(Round, row_id)
            await sched._announce_round(db, day, bot)
        assert seen == [row_id]
        async with SessionLocal() as db:
            assert (await db.get(Round, row_id)).announced_at is not None
    finally:
        await _cleanup(did)


async def test_retry_new_day_job_announces_only_open_unannounced(monkeypatch) -> None:
    """Восстановитель объявляет только OPEN-дни без метки; уже помеченные и
    подсчитываемые не трогает, повторный прогон молчит."""
    from app import scheduler as sched

    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    missing = await _make_round(9725, RoundStatus.OPEN)
    claimed = await _make_round(9726, RoundStatus.OPEN)
    tallying = await _make_round(9727, RoundStatus.TALLYING)
    async with SessionLocal() as db:
        row = await db.get(Round, claimed)
        row.announced_at = datetime.now(UTC)
        await db.commit()

    seen = []
    async def fake_announce(bot_, round_row):
        seen.append(round_row.id)
    monkeypatch.setattr(sched, "announce_new_day", fake_announce)
    try:
        await sched._retry_new_day_job()
        assert seen == [missing]  # день без поста объявлен, остальные нет
        async with SessionLocal() as db:
            assert (await db.get(Round, missing)).announced_at is not None
        seen.clear()
        await sched._retry_new_day_job()
        assert seen == []  # идемпотентно: метки стоят
    finally:
        await _cleanup(missing, claimed, tallying)


async def test_retry_new_day_job_respects_pause(monkeypatch) -> None:
    """Стоп-кран: восстановитель молчит, пока игра на паузе."""
    from app import scheduler as sched

    missing = await _make_round(9728, RoundStatus.OPEN)
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=True))
    seen = []
    async def fake_announce(bot_, round_row):
        seen.append(round_row.id)
    monkeypatch.setattr(sched, "announce_new_day", fake_announce)
    try:
        await sched._retry_new_day_job()
        assert seen == []
    finally:
        await _cleanup(missing)


async def test_finalize_new_day_job_swallows_failures(monkeypatch) -> None:
    """Сбой финализации дня не роняет планировщик."""
    from app import scheduler as sched

    rid = await _make_round(9712, RoundStatus.CLOSED)
    monkeypatch.setattr("app.rounds.write_epilogue", AsyncMock())
    monkeypatch.setattr("app.leaderboard.mark_leaderboards_for_finished", AsyncMock())

    async def boom_create(session, *, base_day_index):
        raise RuntimeError("нейро-генерация нового дня упала")

    monkeypatch.setattr("app.rounds.create_next_round_detailed", boom_create)
    try:
        await sched._finalize_new_day_job(rid)  # не роняется
    finally:
        await _clear_rounds()


async def test_payout_dispatch_job_uses_bot_and_swallows(monkeypatch) -> None:
    from app import scheduler as sched

    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    seen = []
    async def fake_dispatch(**kwargs):
        seen.append(kwargs.get("bot"))
        return 3
    monkeypatch.setattr("app.ton_pay.dispatch_pending_payouts", fake_dispatch)
    await sched._payout_dispatch_job()
    assert seen == [bot]

    async def boom_dispatch(**kwargs):
        raise RuntimeError("ton down")
    monkeypatch.setattr("app.ton_pay.dispatch_pending_payouts", boom_dispatch)
    await sched._payout_dispatch_job()  # ретраи продолжатся — не роняем тик


async def test_watch_job_guarded_passes_bot(monkeypatch) -> None:
    from app import scheduler as sched

    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    seen = []
    async def fake_watch(**kwargs):
        seen.append(kwargs.get("bot"))
    monkeypatch.setattr("app.ton_watch.watch_once", fake_watch)

    await sched._watch_job()
    await sched._watch_job_guarded()
    assert seen == [bot, bot]


async def test_ton_maintenance_runs_services_in_order(monkeypatch) -> None:
    from app import scheduler as sched

    bot = object()
    monkeypatch.setattr(sched, "_bot", bot)
    order: list[str] = []

    async def confirm(**kwargs):
        order.append("confirm")
    async def settle(**kwargs):
        order.append("settle")
    async def week(**kwargs):
        order.append("week")
    async def month(**kwargs):
        order.append("month")

    monkeypatch.setattr("app.ton_pay.confirm_broadcast_payouts", confirm)
    monkeypatch.setattr("app.ton_pay.settle_closed_rounds", settle)
    monkeypatch.setattr("app.leaderboard.settle_week_if_due", week)
    monkeypatch.setattr("app.leaderboard.settle_month_if_due", month)

    await sched._ton_maintenance()
    assert order == ["confirm", "settle", "week", "month"]


async def test_ops_sweep_logs_anomalies(monkeypatch) -> None:
    """Тревоги живут в своей джобе: список проблем — warning, не исключение."""
    from app import scheduler as sched

    monkeypatch.setattr("app.ops.check_anomalies", AsyncMock(return_value=[]))
    await sched._ops_sweep()
    monkeypatch.setattr("app.ops.check_anomalies", AsyncMock(return_value=["фонд разошёлся"]))
    await sched._ops_sweep()


async def test_ops_sweep_registered_without_ton(monkeypatch) -> None:
    """Тревоги не зависят от TON_ENABLED: иначе при деньгах-выкл их нет вовсе."""
    from app import scheduler as sched

    registered: list[str] = []
    monkeypatch.setattr(sched, "_register_job", lambda job_id, *a, **k: registered.append(job_id))
    monkeypatch.setattr(sched, "scheduler", SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(settings, "ton_enabled", False)

    sched.start_scheduler()

    assert "ops-sweep" in registered
    assert "ton-watch" not in registered


async def test_ton_maintenance_isolates_failures(monkeypatch) -> None:
    """Падение одного сервиса не останавливает остальные (каждый в своём try)."""
    from app import scheduler as sched

    rejected = []
    async def boom_confirm(**kwargs):
        raise RuntimeError("подтверждение упало")
    async def settle(**kwargs):
        rejected.append("settle")
    async def week(**kwargs):
        rejected.append("week")
    async def month(**kwargs):
        rejected.append("month")

    monkeypatch.setattr("app.ton_pay.confirm_broadcast_payouts", boom_confirm)
    monkeypatch.setattr("app.ton_pay.settle_closed_rounds", settle)
    monkeypatch.setattr("app.leaderboard.settle_week_if_due", week)
    monkeypatch.setattr("app.leaderboard.settle_month_if_due", month)

    await sched._ton_maintenance()
    assert rejected == ["settle", "week", "month"]


async def test_boot_maintenance_runs_backup(monkeypatch) -> None:
    from app import scheduler as sched

    ran: list[bool] = []
    async def fake_backup():
        ran.append(True)
    monkeypatch.setattr("app.backups.backup_job", fake_backup)

    await sched.boot_maintenance()
    assert ran == [True]


async def test_cleanup_watcher_state_removes_stale_keeps_live() -> None:
    from app import scheduler as sched
    from app.models import WatcherState

    async with SessionLocal() as db:
        for key in ("teaser:5", "img_stubs:3", "refund:abc", "micro_event:9", "ledger:42"):
            db.add(WatcherState(key=key, value="x"))
        db.add(WatcherState(key="run:anchor", value="y"))
        await db.commit()

    await sched._cleanup_watcher_state_job()

    async with SessionLocal() as db:
        keys = {row.key for row in (await db.execute(select(WatcherState))).scalars()}
    assert keys == {"run:anchor"}


async def test_cleanup_watcher_state_empty_db_noop() -> None:
    from app import scheduler as sched

    await sched._cleanup_watcher_state_job()  # без rows — тихий возврат


async def test_cleanup_watcher_state_swallows(monkeypatch) -> None:
    from app import scheduler as sched

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(sched, "select", boom)
    await sched._cleanup_watcher_state_job()  # warning, без падения


async def test_vote_reminder_skips_without_bot(monkeypatch) -> None:
    from app import scheduler as sched

    monkeypatch.setattr(sched, "_bot", None)
    await sched._vote_reminder_job()


async def test_vote_reminder_skips_without_active_round(monkeypatch) -> None:
    from app import scheduler as sched

    await _clear_rounds()
    monkeypatch.setattr(sched, "_bot", object())
    await sched._vote_reminder_job()


async def test_vote_reminder_sends_dms_once_per_day(monkeypatch) -> None:
    from app import scheduler as sched
    from app.models import Player, Vote

    await _clear_rounds()
    rid = await _make_round(9801, RoundStatus.OPEN)

    sends = AsyncMock()
    monkeypatch.setattr(sched, "_bot", Mock(send_message=sends))
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "player_dm", True)
    monkeypatch.setattr("app.broadcast.active_player_ids", AsyncMock(return_value=[501, 502]))
    reminder_now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: reminder_now)

    async with SessionLocal() as db:
        db.add(Player(id=501, username="a", first_name="A", dm_subscribed=True))
        db.add(Player(id=502, username="b", first_name="B", dm_subscribed=True))
        db.add(Player(id=503, username="c", first_name="C", dm_subscribed=False))
        db.add(Vote(round_id=rid, player_id=501, card_position=0))
        await db.commit()

    try:
        await sched._vote_reminder_job()
        # 501 уже выбрал путь — напоминание ему не уходит; 502 получает.
        assert sends.await_count == 1
        assert sends.await_args.args[0] == 502

        # Повторный заход в тот же день — маркер job:vote-reminder:<дата>
        # закоммичен и занят, участники не спамятся повторно.
        await sched._vote_reminder_job()
        assert sends.await_count == 1

        # Следующий день: маркер свеж, но все проголосовали — рассылка тихо
        # отменяется.
        next_day = reminder_now + timedelta(days=1)
        monkeypatch.setattr(sched, "_now", lambda: next_day)
        async with SessionLocal() as db:
            db.add(Vote(round_id=rid, player_id=502, card_position=1))
            await db.commit()
        await sched._vote_reminder_job()
        assert sends.await_count == 1
    finally:
        await _clear_rounds()


async def test_vote_reminder_stake_and_no_stake_lines(monkeypatch) -> None:
    """Личная строка о ставке: сделана (сумма) / не сделана."""
    from app import scheduler as sched
    from app.models import Player, Stake

    await _clear_rounds()
    rid = await _make_round(9803, RoundStatus.OPEN)

    sends = AsyncMock()
    texts: dict[int, str] = {}
    sends.side_effect = lambda pid, text: texts.update({pid: text})
    monkeypatch.setattr(sched, "_bot", Mock(send_message=sends))
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "player_dm", True)
    monkeypatch.setattr("app.broadcast.active_player_ids", AsyncMock(return_value=[521, 522]))
    reminder_now = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: reminder_now)

    async with SessionLocal() as db:
        db.add(Player(id=521, username="staked", first_name="S", dm_subscribed=True))
        db.add(Player(id=522, username="plain", first_name="P", dm_subscribed=True))
        db.add(
            Stake(
                round_id=rid,
                player_id=521,
                amount_nanotons=500_000_000_000,
                tx_hash="rm-stake-521",
                memo="m9803",
                network=current_network(),
                status="confirmed",
            )
        )
        await db.commit()

    try:
        await sched._vote_reminder_job()
        assert sends.await_count == 2
        assert "500.00 Gram уже принята" in texts[521]
        assert "путь ещё не выбран" in texts[521]
        assert "не сделана и путь не выбран" in texts[522]
    finally:
        await _clear_rounds()


async def test_vote_reminder_pending_stake_line(monkeypatch) -> None:
    """Ставка ещё парится (pending): напоминание говорит про подтверждение."""
    from app import scheduler as sched
    from app.models import Player, Stake

    await _clear_rounds()
    rid = await _make_round(9804, RoundStatus.OPEN)

    sends = AsyncMock()
    texts: dict[int, str] = {}
    sends.side_effect = lambda pid, text: texts.update({pid: text})
    monkeypatch.setattr(sched, "_bot", Mock(send_message=sends))
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "player_dm", True)
    monkeypatch.setattr("app.broadcast.active_player_ids", AsyncMock(return_value=[531]))
    reminder_now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: reminder_now)

    async with SessionLocal() as db:
        db.add(Player(id=531, username="pend", first_name="P", dm_subscribed=True))
        db.add(
            Stake(
                round_id=rid,
                player_id=531,
                amount_nanotons=700_000_000_000,
                tx_hash="rm-pend-531",
                memo="m9804",
                network=current_network(),
                status="pending",
            )
        )
        await db.commit()

    try:
        await sched._vote_reminder_job()
        assert sends.await_count == 1
        assert "700.00 Gram подтверждается" in texts[531]
    finally:
        await _clear_rounds()


async def test_vote_reminder_vote_only_mode_phrase(monkeypatch) -> None:
    """Без TON-режима используется человеческая формулировка правила."""
    from app import scheduler as sched
    from app.models import Player

    await _clear_rounds()
    await _make_round(9802, RoundStatus.OPEN)

    sends = AsyncMock()
    texts: list[str] = []
    sends.side_effect = lambda _pid, text: texts.append(text)
    monkeypatch.setattr(sched, "_bot", Mock(send_message=sends))
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(settings, "player_dm", True)
    monkeypatch.setattr("app.broadcast.active_player_ids", AsyncMock(return_value=[511]))

    async with SessionLocal() as db:
        db.add(Player(id=511, username="a", first_name="A", dm_subscribed=True))
        await db.commit()

    try:
        await sched._vote_reminder_job()
        assert sends.await_count == 1
        # Без ставок фраза апеллирует к голосам, а не к Gram.
        assert "Gram" not in texts[0]
    finally:
        await _clear_rounds()

