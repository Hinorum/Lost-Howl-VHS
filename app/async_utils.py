"""Защищённые фоновые задачи: создание, журналирование сбоев, реестр ссылок.

Проблемы паттерна «просто asyncio.create_task(...)»: пока слабая ссылка на
задачу — она может быть собрана GC до завершения; исключение внутри задачи
глотается с "Task exception was never retrieved" — мониторинг не увидит
падение фоновой шлифовки.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def _done_callback(label: str, task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Фоновая задача %s упала: %s", label, exc, exc_info=exc)


def spawn(coro: Coroutine, label: str) -> asyncio.Task:
    """Создаёт фоновую задачу с реестром ссылок и журнализацией сбоев."""
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(lambda finished: _done_callback(label, finished))
    return task