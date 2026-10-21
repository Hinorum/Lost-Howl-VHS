"""Лок планировщика: тик перепроверяет его и замирает, если лок чужой.

Лок — сессионный advisory lock на отдельном соединении с Postgres. Соединение
может умереть само (сбой сети, рестарт PG, pgbouncer): на сервере лок
освобождается сразу, а процесс продолжает считать себя лидером. Второй
инстанс берёт лок — и оба тикают: анонсы дня уходят дважды, выплаты могут
уйти дважды.

Раньше лок перепроверялся только в фоновом retry-цикле, который запускается
лишь при НЕудачном старте: на счастливом пути соединение могло умереть, и
процесс тикал до конца, считая себя единственным.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ops, scheduler_lock
from app import scheduler as sched
from app.core.registry import LOCK_LOST_KEY
from app.db import SessionLocal
from app.models import WatcherState


@pytest.fixture(autouse=True)
def _clean_marker():
    """Метка «лок потерян» не должна пережить тест."""
    yield
    sched._last_lock_check = 0.0


async def _clear_marker() -> None:
    async with SessionLocal() as db:
        await db.execute(
            WatcherState.__table__.delete().where(WatcherState.key == LOCK_LOST_KEY)
        )
        await db.commit()


async def test_lock_lost_and_not_regained_freezes_the_tick(monkeypatch) -> None:
    """Лок потерян и не вернулся — тик не трогает игру, метка оставлена."""
    monkeypatch.setattr(sched, "_last_lock_check", 0.0)
    monkeypatch.setattr(scheduler_lock, "verify_scheduler_lock", AsyncMock(return_value=False))
    monkeypatch.setattr(scheduler_lock, "acquire_scheduler_lock", AsyncMock(return_value=False))
    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))

    async def bomb(*_args, **_kwargs):
        raise AssertionError("тик без лока не должен трогать игру")

    monkeypatch.setattr(sched, "get_latest_round", bomb)
    try:
        await sched.tick()
        async with SessionLocal() as db:
            assert await db.get(WatcherState, LOCK_LOST_KEY) is not None
    finally:
        await _clear_marker()


async def test_regained_lock_clears_marker_and_lets_tick_work(monkeypatch) -> None:
    """Лок вернулся (умерло соединение, а лок был свободен) — игра едет дальше."""
    monkeypatch.setattr(sched, "_last_lock_check", 0.0)
    monkeypatch.setattr(scheduler_lock, "verify_scheduler_lock", AsyncMock(return_value=False))
    monkeypatch.setattr(scheduler_lock, "acquire_scheduler_lock", AsyncMock(return_value=True))
    monkeypatch.setattr("app.ops.mark_tick", AsyncMock())
    monkeypatch.setattr("app.ops.is_game_paused", AsyncMock(return_value=False))
    latest = AsyncMock(side_effect=RuntimeError("дошли до игры"))
    monkeypatch.setattr(sched, "get_latest_round", latest)
    try:
        # Метка от прошлого случая должна быть снята.
        await sched._mark_lock_lost()
        assert await sched._ensure_scheduler_lock() is True
        async with SessionLocal() as db:
            assert await db.get(WatcherState, LOCK_LOST_KEY) is None
        # И тик доходит до игры (падает там уже по другой причине — не важно).
        await sched.tick()
        assert latest.await_count == 1
    finally:
        await _clear_marker()


async def test_lock_check_is_throttled_between_ticks(monkeypatch) -> None:
    """Между проверками тик не дёргает базу: лок перепроверяется раз в 5 минут."""
    monkeypatch.setattr(sched, "_last_lock_check", 0.0)
    verify = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler_lock, "verify_scheduler_lock", verify)
    monkeypatch.setattr(scheduler_lock, "acquire_scheduler_lock", AsyncMock(return_value=True))
    assert await sched._ensure_scheduler_lock() is True
    assert verify.await_count == 1
    # Второй вызов — внутри окна: проверки нет.
    assert await sched._ensure_scheduler_lock() is True
    assert verify.await_count == 1


async def test_anomaly_alert_fires_when_lock_lost(monkeypatch) -> None:
    """Метка о локе поднимает тревогу: без неё потеря лока была бы молчаливой."""
    monkeypatch.setattr(ops.settings, "ton_enabled", False)
    bot = SimpleNamespace(send_message=AsyncMock())
    try:
        await sched._mark_lock_lost()
        problems = await ops.check_anomalies(bot=bot)
        assert any("лок планировщика потерян" in problem for problem in problems), problems
        assert bot.send_message.await_count >= 1
    finally:
        await _clear_marker()


async def test_snapshot_reports_lock_state() -> None:
    """В снимке /health видно, держит ли этот процесс лок."""
    snap = await ops.snapshot()
    assert snap["scheduler_lock"] in ("held", "lost")
