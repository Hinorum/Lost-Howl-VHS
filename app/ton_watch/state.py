"""Состояние watcher'а в watcher_state: курсор, сердцебиение, источник,
список застрявших переводов. Всё, что переживает цикл, живёт здесь."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

from app import ton_watch as _pkg
from app.config import settings
from app.core.registry import BEAT_KEY, CURSOR_KEY, SOURCE_KEY, STUCK_TX_KEY
from app.models import WatcherState

logger = logging.getLogger(__name__)

_CURSOR_FALLBACK_HOURS = 12

_CURSOR_OVERLAP_SECONDS = max(0, settings.watch_cursor_overlap_seconds)

async def _read_cursor(session) -> int:
    """Курсор CI/времени с окном перекрытия.

    Чистовой курсор хранит последний обработанный utime, но читается он на
    _CURSOR_OVERLAP_SECONDS раньше: у провайдеров входящий перевод публикуется
    не мгновенно, и транзакция ТОЙ ЖЕ секунды, что курсор, не должна остаться
    за бортом навсегда. Окно пересматривается каждый цикл — лишние повторы
    гасит идемпотентность по tx_hash.

    Окно берётся из корня пакета в момент вызова: тесты подменяют
    app.ton_watch._CURSOR_OVERLAP_SECONDS, и подмена обязана дойти сюда.
    """
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is not None and row.value.isdigit():
        return max(0, int(row.value) - _pkg._CURSOR_OVERLAP_SECONDS)
    return int((datetime.now(UTC) - timedelta(hours=_CURSOR_FALLBACK_HOURS)).timestamp())

async def _read_cursor_raw(session) -> int:
    """Чистовое значение курсора (без окна перекрытия) или фолбэк.

    Нужно watch_once, чтобы курсор никогда не откатывался назад: окно
    перекрытия читается раньше, но записывать можно только значение не
    младше уже записанного.
    """
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return int((datetime.now(UTC) - timedelta(hours=_CURSOR_FALLBACK_HOURS)).timestamp())

async def _write_cursor(session, utime: int) -> None:
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is None:
        session.add(WatcherState(key=CURSOR_KEY, value=str(utime)))
    else:
        row.value = str(utime)
    await session.commit()

async def _write_beat(session) -> None:
    """Сердцебиение успешного цикла — для алертов и /health."""
    row = await session.get(WatcherState, BEAT_KEY)
    stamp = datetime.now(UTC).isoformat()
    if row is None:
        session.add(WatcherState(key=BEAT_KEY, value=stamp))
    else:
        row.value = stamp
    await session.commit()

async def _write_source(session, source: str) -> None:
    """Источник данных последнего успешного цикла (для /health)."""
    row = await session.get(WatcherState, SOURCE_KEY)
    if row is None:
        session.add(WatcherState(key=SOURCE_KEY, value=source))
    else:
        row.value = source
    await session.commit()

_STUCK_MAX_FAILS = 5

def _load_stuck(raw: str | None) -> dict:
    """Разбор JSON stuck-списка из watcher_state (битый/пустой — пустой словарь)."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        logger.warning("Стuck-список ton_watch повреждён (%r) — начинаю заново", raw[:128])
    return {}

async def _read_stuck(session) -> dict:
    row = await session.get(WatcherState, STUCK_TX_KEY)
    return _load_stuck(row.value if row is not None else None)

async def _write_stuck(session, stuck: dict) -> None:
    # Ротация: запись сбойной транзакции живёт ограниченно (stuck_retention_days).
    # Без прунинга врачующиеся (reported) входы висели бы в watcher_state вечно,
    # отравляя /blockchain и ручной разбор. Свежие незарепортированные НЕ трогаем.
    cutoff = time.time() - settings.stuck_retention_days * 86400
    stuck = {
        hash_: rec
        for hash_, rec in stuck.items()
        if isinstance(rec, dict) and float(rec.get("utime") or 0) >= cutoff
    }
    row = await session.get(WatcherState, STUCK_TX_KEY)
    if row is None:
        session.add(WatcherState(key=STUCK_TX_KEY, value=json.dumps(stuck)))
    else:
        row.value = json.dumps(stuck)
    await session.commit()
