"""Взаимоисключение выплат: один вещающий процесс на одну базу.

`ton_pay.state._DISPATCH_LOCK` — обычный asyncio.Lock, то есть защита только
внутри одного процесса. Планировщик уже берёт scheduler_lock, но ручные
команды (/finalize, /refinalize, /payout retry) обслуживает webhook, а он
может жить в другом инстансе. Два диспетчера на одной очереди — это не
«двойной цикл», а двойное вещание: `_reset_retriable` одного процесса
возвращает sending → pending, пока другой ещё держит строку в работе, и оба
отправляют один и тот же перевод.

Лок — сессионный advisory lock PostgreSQL на ОТДЕЛЬНОМ соединении, как у
scheduler_lock: арбитраж делает БД, соединение умерло — лок освобождён,
TTL не нужен. SQLite (тесты и локальная разработка) предполагает один
процесс, поэтому guard отдаёт True.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Стабильный ключ, ОТЛИЧНЫЙ от scheduler_lock (0x54686557_31000000): иначе
# диспетчер и планировщик считали бы один лок двумя разными и мешали друг
# другу. Менять нельзя — старый и новый инстансы должны видеть один лок.
_LOCK_KEY = 0x5061794473707431  # ASCII "PayDspt1"

# Соединение-держатель лока. Модуль хранит ссылку намеренно: если её
# потерять, соединение заберёт сборщик мусора и вернётся в пул вместе с
# локом. Свой движок с NullPool — чтобы лок не занимал слот основного пула.
_lock_conn = None
_lock_engine = None


class PayoutDispatchLockLost(RuntimeError):
    """Advisory lock пропал посреди прохода: вещать дальше нельзя.

    Соединение-держатель могло умереть (сеть, пулер, рестарт PG), и лок на
    сервере уже свободен — второй процесс вправе начать свой цикл. Продолжать
    вещание в этом состоянии значит снова открыть окно двойной отправки.
    """


def _lock_engine_instance():
    """Движок для advisory-лока: свой, без пула."""
    global _lock_engine
    if _lock_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import postgres_connect_args, settings
        from app.db import engine as app_engine

        _lock_engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            echo=False,
            pool_pre_ping=True,
            connect_args=postgres_connect_args(settings.database_url),
        )
    return _lock_engine


async def acquire_payout_dispatch_lock() -> bool:
    """True — можно вещать. False — очередь уже разгребает другой процесс.

    False не повод падать: текущий цикл пропускается, следующий тик
    попробует снова. Если сам лок взять не удалось (БД недоступна), тоже
    возвращаем False: вещать без подтверждённого лока нельзя — это ровно
    тот случай, когда второй процесс мог остаться незамеченным.
    """
    global _lock_conn
    if _lock_conn is not None:
        return True

    from app.db import engine

    if engine.dialect.name != "postgresql":
        # Один процесс на одну базу — предположение SQLite-окружения.
        return True

    conn = await _lock_engine_instance().connect()
    try:
        got = (
            await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})
        ).scalar()
    except Exception:
        await conn.close()
        logger.exception("Лок выплат: advisory lock не взят — цикл пропущен")
        return False
    if not got:
        await conn.close()
        logger.info("Лок выплат занят другим процессом — цикл пропущен")
        return False
    _lock_conn = conn
    logger.info("Лок выплат взят: этот процесс — единственный вещающий")
    return True


async def verify_payout_dispatch_lock() -> bool:
    """Пинг соединения-держателя: лок всё ещё наш?

    Сессионный advisory lock живёт на физическом соединении. Если оно умерло,
    сервер уже отпустил лок, хотя `_lock_conn` ещё не None. Проверяем лёгким
    запросом перед каждым вещанием: мёртвое соединение сбрасываем, чтобы
    вызывающий остановил проход и не вещал без межпроцессной защиты.
    """
    global _lock_conn
    if _lock_conn is None:
        from app.db import engine

        return engine.dialect.name != "postgresql"
    try:
        await _lock_conn.execute(text("select 1"))
    except Exception:
        logger.warning(
            "Лок выплат потерян: соединение-держатель мертво — вещание "
            "останавливаю до следующего цикла",
            exc_info=True,
        )
        import contextlib

        with contextlib.suppress(Exception):
            await _lock_conn.close()
        _lock_conn = None
        return False
    return True


async def release_payout_dispatch_lock() -> None:
    """Отдать лок после цикла. Закрытие соединения освобождает его и само."""
    global _lock_conn
    conn, _lock_conn = _lock_conn, None
    if conn is None:
        return
    try:
        await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    except Exception:
        # Лок всё равно освободится вместе с соединением — логируем и молчим.
        logger.warning("Не удалось отдать лок выплат явно", exc_info=True)
    finally:
        await conn.close()


def payout_dispatch_lock_held() -> bool:
    """Диагностика для тестов и /health-подобных проверок."""
    if _lock_conn is not None:
        return True
    from app.db import engine

    return engine.dialect.name != "postgresql"
