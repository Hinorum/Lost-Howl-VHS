"""Взаимоисключение планировщика: один тикающий процесс на одну базу.

Инцидент, который закрывает этот модуль: на Render проснулся сервис, уснувший
от лимита аккаунта, и поднял старый билд с безграничными догонами по маркерам
— с тем же DATABASE_URL и BOT_TOKEN, что у основного инстанса. Тик по чужой
базе отправил всю историю закрытых дней в чат повторно.

Тесты на SQLite проверяют контракт «advisory lock не поддерживается → guard
прозволяет тикать» (иначе весь прогон встал бы). Сама семантика лока — что
вторая сессия в той же базе лок НЕ получает — проверяется на Postgres.
"""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text

import app.scheduler_lock as slock


@pytest.fixture
async def pg_url():
    """Одноразовая база Postgres, если сервер доступен. Advisory lock —
    механизм Postgres, на SQLite его не проверить в принципе."""
    dsn = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if not dsn:
        pytest.skip("нужен TEST_POSTGRES_URL: проверка advisory lock только на Postgres")
    from urllib.parse import urlsplit

    parts = urlsplit(dsn)
    if not parts.hostname or not parts.username:
        pytest.skip("TEST_POSTGRES_URL без хоста/пользователя")
    admin_dsn = {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": parts.username,
        "password": parts.password or "",
    }
    name = f"the_way_lock_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(database="postgres", **admin_dsn)
    try:
        await admin.execute(f'create database "{name}"')
    finally:
        await admin.close()
    base = admin_dsn
    try:
        yield f"postgresql://{base['user']}:{base['password']}@{base['host']}:{base['port']}/{name}"
    finally:
        admin = await asyncpg.connect(database="postgres", **admin_dsn)
        try:
            await admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'drop database if exists "{name}"')
        finally:
            await admin.close()


async def test_guard_is_permissive_on_sqlite() -> None:
    """SQLite advisory lock не умеет: там предполагается один процесс, и guard
    обязан отпустить тикать, иначе встал бы весь тестовый прогон."""
    from app.db import engine

    if engine.dialect.name == "postgresql":
        pytest.skip(
            "контракт про SQLite; на Postgres exclusivity проверяется "
            "test_real_postgres_lock_is_exclusive"
        )
    assert await slock.acquire_scheduler_lock() is True
    # Повторный захват того же «лока» тоже безопасен.
    assert await slock.acquire_scheduler_lock() is True
    assert slock.scheduler_lock_held() is True


async def test_release_on_sqlite_is_noop_and_safe() -> None:
    """Освобождение без захвата не должно падать: shutdown зовут всегда."""
    await slock.release_scheduler_lock()
    await slock.acquire_scheduler_lock()
    await slock.release_scheduler_lock()
    await slock.release_scheduler_lock()


async def test_second_instance_is_rejected(monkeypatch) -> None:
    """Ключевой контракт: если лок уже держит «чужой процесс», второй не
    должен тикать. Имитируем две сессии на Postgres и проверяем, что вторая
    получает False, а освободивший лок снова может его взять."""
    calls = {"got": iter([True, False, True])}

    class _Result:
        def scalar(self):
            return next(calls["got"])

    class _Conn:
        def __init__(self):
            self.closed = False
            self.queries = []

        async def execute(self, stmt, params=None):
            self.queries.append((str(stmt), params))
            return _Result()

        async def close(self):
            self.closed = True

    conns = []

    async def _connect():
        conn = _Conn()
        conns.append(conn)
        return conn

    class _Engine:
        dialect = type("d", (), {"name": "postgresql"})()

        connect = staticmethod(_connect)

    monkeypatch.setattr(slock, "_lock_conn", None)
    monkeypatch.setattr("app.db.engine", _Engine())

    assert await slock.acquire_scheduler_lock() is True  # первый процесс забрал
    assert len(conns) == 1 and conns[0].closed is False, "лок держится на своём соединении"
    assert slock.scheduler_lock_held() is True

    assert await slock.acquire_scheduler_lock() is True  # тот же процесс, повтор
    assert len(conns) == 1, "повторный захват не плодит соединения"

    # Сбрасываем модульное состояние, чтобы сыграть «второй процесс».
    monkeypatch.setattr(slock, "_lock_conn", None)
    assert await slock.acquire_scheduler_lock() is False  # база занята
    assert len(conns) == 2 and conns[1].closed is True, "отказ не оставляет висящих соединений"
    assert slock.scheduler_lock_held() is False

    await slock.release_scheduler_lock()
    await slock.release_scheduler_lock()


async def test_real_postgres_lock_is_exclusive(pg_url: str) -> None:
    """Проверка на настоящем Postgres: лок держится на соединении и отдаётся
    только его закрытием. Это ровно тот механизм, на котором держится защита
    от двух инстансов на проде."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import sqlalchemy_url

    engine = create_async_engine(sqlalchemy_url(pg_url))
    key = 0x54686557_31000000
    try:
        async with engine.connect() as first:
            got = (
                await first.execute(text("select pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar()
            assert got is True

            # Второе соединение той же базы — «второй инстанс» — не получает лок.
            async with engine.connect() as second:
                assert (
                    await second.execute(
                        text("select pg_try_advisory_lock(:k)"), {"k": key}
                    )
                ).scalar() is False

            # Пока первое соединение живо, лок занят даже после unlock-оборота.
            assert (
                await first.execute(text("select pg_advisory_unlock(:k)"), {"k": key})
            ).scalar() is True
        # Соединение закрыто — лок освобождён, второй инстанс может взять.
        async with engine.connect() as third:
            assert (
                await third.execute(text("select pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar() is True
    finally:
        await engine.dispose()