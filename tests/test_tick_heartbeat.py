"""Честное сердцебиение главного тика и тревога «тик падает».

Инцидент, который закрывает этот файл: битие ставилось ДЕЙСТВИЯ в теле тика,
а тело было под `try/except` с журналированием. Падающий каждые 15 секунд цикл
подтверждал собственную живость — дни не открывались, анонсы молчали, `/health`
отвечал `ok`, и тревога «планировщик не тикает» не могла сработать по
построению. Следом в логах была одна и та же строка каждые 15 секунд, а у
админа не было ни одного сигнала.

Теперь битие ставится только успешным тиком, падение считается в БД и
поднимает тревогу с кулдауном. Плюс проверки аномалий вынесены в джобу
`ops-sweep`, которая регистрируется при любой конфигурации TON — раньше при
`TON_ENABLED=false` (текущий прод) не работала ни одна тревога разом.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ops
from app.config import settings
from app.db import SessionLocal
from app.models import RoundStatus, WatcherState


async def _state(key: str) -> str | None:
    async with SessionLocal() as db:
        row = await db.get(WatcherState, key)
        return row.value if row is not None else None


def _patch_happy_tick(monkeypatch) -> None:
    """Минимальный успешный тик: новый день анонсится, ничего не падает."""
    from app import scheduler as sched

    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sched, "_now", lambda: now)
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
    monkeypatch.setattr(sched, "announce_new_day", AsyncMock())


# ---------- Собственно инцидент ----------


async def test_failed_tick_leaves_heartbeat_stale(monkeypatch) -> None:
    """Упавший тик НЕ подтверждает собственную живость (прежний баг)."""
    from app import scheduler as sched

    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    monkeypatch.setattr(sched, "get_latest_round", AsyncMock(side_effect=RuntimeError("взрыв")))

    await sched.tick()

    assert await _state(ops.TICK_KEY) is None, "битие проставлено упавшим тиком"
    assert await _state(ops.TICK_FAIL_KEY) == "1"
    assert "взрыв" in (await _state(ops.TICK_FAIL_LAST_KEY) or "")
    payload = await ops.snapshot()
    assert payload["last_tick_age"] is None
    assert payload["tick_failures"] == 1


async def test_stale_heartbeat_is_reported_as_alarm(monkeypatch) -> None:
    """Честное битие доходит до тревоги: старый биет = «планировщик не тикает»."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    old = datetime.now(UTC) - timedelta(minutes=30)
    async with SessionLocal() as db:
        db.add(WatcherState(key=ops.TICK_KEY, value=old.isoformat()))
        await db.commit()

    problems = await ops.check_anomalies(bot=bot)

    assert any("планировщик не тикает" in p for p in problems)
    assert bot.send_message.await_count == 1
    assert "планировщик не тикает" in bot.send_message.await_args[0][1].lower()


async def test_successful_tick_beats_heartbeat(monkeypatch) -> None:
    _patch_happy_tick(monkeypatch)
    from app import scheduler as sched

    beats = AsyncMock()
    monkeypatch.setattr("app.ops.mark_tick", beats)
    fails = AsyncMock()
    monkeypatch.setattr("app.ops.mark_tick_failed", fails)

    await sched.tick()

    assert beats.await_count == 1
    assert fails.await_count == 0


async def test_paused_tick_beats_heartbeat(monkeypatch) -> None:
    """Стоп-кран — это отдых, а не авария: битие обновляется."""
    from app import scheduler as sched

    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=True))

    async def bomb(*_args, **_kwargs):
        raise AssertionError("тик не должен идти дальше стоп-крана")

    monkeypatch.setattr(sched, "get_latest_round", bomb)

    await sched.tick()

    assert await _state(ops.TICK_KEY) is not None
    assert await _state(ops.TICK_FAIL_KEY) is None


# ---------- Счётчик падений ----------


async def test_two_failures_stay_quiet_third_alerts(monkeypatch) -> None:
    """Одиночные сбои — обычное дело, три подряд — расписание мертво."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())

    assert await ops.mark_tick_failed("первый", bot=bot) == 1
    assert await ops.mark_tick_failed("второй", bot=bot) == 2
    assert bot.send_message.await_count == 0

    assert await ops.mark_tick_failed(ValueError("третий"), bot=bot) == 3
    assert bot.send_message.await_count == 1
    text = bot.send_message.await_args[0][1]
    assert "ValueError: третий" in text
    assert "/pause on" in text


async def test_failure_alert_is_throttled_to_one_per_hour(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())

    for n in range(6):
        await ops.mark_tick_failed(f"сбой {n}", bot=bot)

    assert bot.send_message.await_count == 1
    assert await _state(ops.TICK_FAIL_KEY) == "6"


async def test_counter_survives_garbage(monkeypatch) -> None:
    """Битый счётчик в БД не превращает наблюдаемость в новую аварию."""
    async with SessionLocal() as db:
        db.add(WatcherState(key=ops.TICK_FAIL_KEY, value="не-число"))
        await db.commit()

    assert await ops.mark_tick_failed("сбой", bot=None) == 1


async def test_failure_recorder_never_raises(monkeypatch) -> None:
    """Наблюдаемость не имеет права уронить и без того упавший цикл."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(
        ops, "notify_admins", AsyncMock(side_effect=RuntimeError("бот недоступен"))
    )

    for _ in range(3):
        await ops.mark_tick_failed(RuntimeError("сбой"), bot=object())

    # Третий вызов дорос до порога и звал админа — упало, но наружу не вышло,
    # счётчик записан, цикл продолжает падать в тишине с уже поднятой тревогой.
    assert await _state(ops.TICK_FAIL_KEY) == "3"


async def test_success_clears_failure_counter() -> None:
    """Один успешный тик снимает аварию: три сбоя за месяц не копятся."""
    await ops.mark_tick_failed("сбой-1", bot=None)
    await ops.mark_tick_failed("сбой-2", bot=None)
    assert (await ops.snapshot())["tick_failures"] == 2

    await ops.mark_tick()

    assert await _state(ops.TICK_FAIL_KEY) is None
    assert await _state(ops.TICK_FAIL_LAST_KEY) is None
    payload = await ops.snapshot()
    assert payload["tick_failures"] == 0
    assert payload["last_tick_age"] is not None


async def test_snapshot_survives_garbage_counter() -> None:
    async with SessionLocal() as db:
        db.add(WatcherState(key=ops.TICK_FAIL_KEY, value=" мусор "))
        await db.commit()

    assert (await ops.snapshot())["tick_failures"] == 0


@pytest.mark.parametrize("value", ["3", "0", " 12 "])
async def test_snapshot_reports_counter(value: str) -> None:
    async with SessionLocal() as db:
        db.add(WatcherState(key=ops.TICK_FAIL_KEY, value=value))
        await db.commit()

    assert (await ops.snapshot())["tick_failures"] == int(value.strip())
