"""Тревоги о «деньги потеряны»: парсер роняет переводы, переводы зависли.

Две дыры, из-за которых деньги могли пропасть молча, и у которых до сих пор
не было НИ ОДНОГО теста:
- счётчик parse-сбоев: парсер провайдера уронил item'ы, и переводы просто
  не появились в игре (раньше ошибка парсера возвращала None и сливалась с
  jetton-старыми-пустыми item'ами — один разломанный перевод казался чистой
  страницей, а курсор уезжал за него);
- stuck-список watcher'а: переводы зависли в казне (инцидент, на который
  прямо ссылается код: «1 G казны висел из-за схема-бага, а сообщения не
  приходило»).

Пока путь не покрыт тестом, он может сломаться снова — и никто не заметит:
проверка тревоги стоит на том же счётчике, который она и читает.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ops
from app.config import settings
from app.core.registry import (
    ALERT_PARSE_FAIL_KEY,
    ALERT_STUCK_KEY,
    PARSE_FAIL_COUNT_KEY,
    PARSE_FAIL_LAST_KEY,
    STUCK_TX_KEY,
)
from app.db import SessionLocal
from app.models import WatcherState
from app.ton_watch import state as watch_state


@pytest.fixture(autouse=True)
def ton_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)


async def _clear() -> None:
    async with SessionLocal() as db:
        await db.execute(
            WatcherState.__table__.delete().where(
                WatcherState.key.in_(
                    [
                        PARSE_FAIL_COUNT_KEY,
                        PARSE_FAIL_LAST_KEY,
                        STUCK_TX_KEY,
                        ALERT_PARSE_FAIL_KEY,
                        ALERT_STUCK_KEY,
                    ]
                )
            )
        )
        await db.commit()


async def test_parse_fail_counter_alerts_admin() -> None:
    """Парсер роняет переводы — админ узнаёт об этом, а не прокручивает логи."""
    bot = SimpleNamespace(send_message=AsyncMock())
    try:
        async with SessionLocal() as db:
            await watch_state._bump_parse_fail(db, 2, sample=("abc123", 1_700_000_000))

        async with SessionLocal() as db:
            row = await db.get(WatcherState, PARSE_FAIL_COUNT_KEY)
            assert row is not None and row.value == "2"
            last = await db.get(WatcherState, PARSE_FAIL_LAST_KEY)
            assert last is not None
            payload = json.loads(last.value)
            assert payload["tx_hash"] == "abc123"
            assert payload["delta"] == 2

        problems = await ops.check_anomalies(bot=bot)
        assert any("парсер отбросил" in problem for problem in problems), problems
        assert bot.send_message.await_count >= 1
        assert any(
            "парсер" in str(call.args[1]).lower()
            for call in bot.send_message.await_args_list
        )
    finally:
        await _clear()


async def test_parse_fail_counter_resets_after_clean_cycle() -> None:
    """Чистый цикл обнуляет счётчик — и тревога уходит."""
    bot = SimpleNamespace(send_message=AsyncMock())
    try:
        async with SessionLocal() as db:
            await watch_state._bump_parse_fail(db, 3)

        async with SessionLocal() as db:
            # Возврат нужен для читаемого сообщения: оператор должен увидеть
            # масштаб прошедшей проблемы, а не только факт обнуления.
            assert await watch_state._reset_parse_fail(db) == 3

        async with SessionLocal() as db:
            assert await db.get(WatcherState, PARSE_FAIL_COUNT_KEY) is None
            assert await db.get(WatcherState, PARSE_FAIL_LAST_KEY) is None

        problems = await ops.check_anomalies(bot=bot)
        assert not any("парсер отбросил" in problem for problem in problems), problems
    finally:
        await _clear()


async def test_stuck_transfers_alert_admin() -> None:
    """Переводы зависли в казне — админ видит, что деньги не дошли."""
    bot = SimpleNamespace(send_message=AsyncMock())
    try:
        async with SessionLocal() as db:
            await watch_state._write_stuck(
                db,
                {
                    "stuck-hash-1": {"utime": int(time.time()), "fails": 3},
                    "stuck-hash-2": {"utime": int(time.time()), "fails": 5, "reported": True},
                },
            )

        problems = await ops.check_anomalies(bot=bot)
        assert any("stuck" in problem for problem in problems), problems
        assert bot.send_message.await_count >= 1
        assert any(
            "stuck" in str(call.args[1]).lower()
            for call in bot.send_message.await_args_list
        )
    finally:
        await _clear()
