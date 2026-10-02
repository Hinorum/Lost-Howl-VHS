"""Alembic-окружение: URL и метаданные берутся из приложения.

Миграции живут в одном источнике правды с моделями: target_metadata —
Base.metadata из app.models, URL — settings.async_database_url (учитывает
DATABASE_URL). SQLite-файл и Postgres поддерживаются одним кодом через
async-движок.
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Корень проекта в sys.path — чтобы `app.*` импортировался при запуске
# `alembic` из любой директории.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.config import postgres_connect_args, settings  # noqa: E402
from app.models import Base  # noqa: E402

target_metadata = Base.metadata

# URL из настроек сильнее alembic.ini: одна точка правды с рантаймом.
config.set_main_option("sqlalchemy.url", settings.async_database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # SQLite пересоздаёт таблицу при ALTER — batch-режим обязателен
        # и безвреден для Postgres (там рендер совпадает с обычным).
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = settings.async_database_url
    raw_url = settings.database_url
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"timeout": 30}}
    else:
        kwargs = {"connect_args": postgres_connect_args(raw_url)}
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **kwargs,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
