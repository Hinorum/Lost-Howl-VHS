"""Взаимоисключение планировщика: один активный процесс на одну базу.

Инцидент: на Render проснулся сервис, уснувший от лимита аккаунта, и поднял
СТАРЫЙ билд с безграничными догонами по маркерам. Он делил с основным
инстансом и DATABASE_URL, и BOT_TOKEN — то есть тикал по той же базе, и вся
история закрытых дней ушла в чат повторно. Никакой защиты от двух
инстансов в коде не было: планировщик поднимался в каждом процессе.

Лок — сессионный advisory lock PostgreSQL на ОТДЕЛЬНОМ соединении,
принадлежащем процессу:

  * арбитраж делает сама БД, а не координатор, поэтому не нужно ни heartbeat,
    ни clock skew, ни сравнения таймстемпов;
  * соединение умирает — лок освобождается сам, вместе с упавшим инстансом
    (не нужен TTL, который мог бы «протухнуть» на живом процессе);
  * лок живёт на физическом соединении, а не в пуле: соединение намеренно
    НЕ возвращается в пул, иначе лок уехал бы с чужой сессией.

Вторая сессия того же процесса тоже не пройдёт: pg_try_advisory_lock даёт
одну лок-сессию на базу, а не на процесс.

SQLite (локальная разработка и весь тестовый прогон) advisory lock не
поддерживает — там предполагается один процесс, и guard отдаёт True.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Стабильный ключ: пара «ASCII "TheWay1"» в bigint. Менять нельзя — иначе
# старый и новый инстансы начнут считать друг друга разными процессами.
_LOCK_KEY = 0x54686557_31000000

# Соединение-держатель лока. Модуль хранит ссылку намеренно: если её потерять,
# соединение заберёт сборщик мусора и вернётся в пул вместе с локом.
_lock_conn = None


async def acquire_scheduler_lock() -> bool:
    """Забрать право тикать. False — эту базу уже обслуживает другой процесс.

    False НЕ повод падать: процесс продолжает отдавать /health (иначе Render
    убьёт его и отметит как неполадку), просто не трогает игру.
    """
    global _lock_conn
    if _lock_conn is not None:
        return True

    from app.db import engine

    if engine.dialect.name != "postgresql":
        # Один процесс на одну базу — предположение SQLite-окружения.
        return True

    conn = await engine.connect()
    try:
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar()
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return False
    _lock_conn = conn
    logger.info("Лок планировщика взят: этот процесс — единственный тикающий")
    return True


async def release_scheduler_lock() -> None:
    """Отдать лок наshutdown. Закрытие соединения освобождает его и само."""
    global _lock_conn
    conn, _lock_conn = _lock_conn, None
    if conn is None:
        return
    try:
        await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    except Exception:
        # Лок всё равно освободится вместе с соединением — логируем и молчим.
        logger.warning("Не удалось отдать лок планировщика явно", exc_info=True)
    finally:
        await conn.close()


def scheduler_lock_held() -> bool:
    """Диагностика для /health и /panel."""
    if _lock_conn is not None:
        return True
    from app.db import engine

    return engine.dialect.name != "postgresql"