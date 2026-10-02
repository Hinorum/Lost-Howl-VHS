"""Симметрия create_all ↔ alembic.

Бутстрап схемы идёт через create_all (init_db) и таблицы совпадают с
моделями, но alembic_version при этом не создавалась: ручной
`alembic upgrade head` на такой базе упёрся бы в «table already exists».
init_db теперь ставит якорь версии на head — повторный upgrade становится
честным no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import db
from app.config import settings
from app.db import SessionLocal, _alembic_head, _handle_orphan_columns, init_db
from app.models import Base

_REPO_ROOT = Path(__file__).resolve().parents[1]


async def test_init_db_stamps_alembic_version_at_head() -> None:
    head = _alembic_head()
    assert head, "в migrations/versions должны быть ревизии"

    await init_db()

    async with SessionLocal() as session:
        row = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert row == head


def test_upgrade_head_is_noop_after_bootstrap() -> None:
    """`alembic upgrade head` на базе, бутстрапнутой через create_all со штампом,
    отрабатывает без «table already exists»."""
    db_url = os.environ.get("DATABASE_URL")
    assert db_url, "conftest задал DATABASE_URL"
    env = dict(os.environ) | {"DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "already exists" not in proc.stdout.lower() + proc.stderr.lower()


def test_alembic_check_reports_no_drift(tmp_path) -> None:
    """Свежая база (upgrade head) сходится с моделями: alembic check молчит.

    Дублирует CI-джобы (SQLite и Postgres) локально: любой будущий дрейф
    схемы от моделей роняет тест, а не только прод-миграцию.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'drift.db'}"
    env = dict(os.environ) | {"DATABASE_URL": db_url}
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    check = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout


def test_upgrade_head_removes_leftover_tx_hash_unique(tmp_path) -> None:
    """Миграция 3197cf14cbeb снимает унаследованный дубль UNIQUE(tx_hash).

    Базовая ревизия создала безымянный unique на tx_hash, а squash — только
    составной (tx_hash, network): на fresh upgrade остаётся единичный unique
    сверх модели. После head от него не должно остаться следа.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'leftover.db'}"
    env = dict(os.environ) | {"DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'leftover.db'}")
    try:
        with engine.connect() as conn:
            uniques = sa.inspect(conn).get_unique_constraints("stakes")
    finally:
        engine.dispose()
    names = {u["name"] for u in uniques}
    assert names == {"uq_stake_round_player", "uq_stake_tx_network"}
    assert all(u["column_names"] != ["tx_hash"] for u in uniques)


async def test_orphan_column_drop_is_flag_gated(tmp_path, monkeypatch) -> None:
    """Осиротевшая NOT NULL-колонка (старая механика) с flag off не трогается,
    с flag on — удаляется. Раньше init_db сносил её на каждом старте без спроса."""
    db_path = tmp_path / "orphan.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")

    def _column_exists(sync_conn) -> bool:
        return "legacy_tag" in {
            col["name"] for col in sa.inspect(sync_conn).get_columns("rounds")
        }

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE rounds ADD COLUMN legacy_tag VARCHAR(16) NOT NULL")
    engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(db, "engine", async_engine)
    try:
        monkeypatch.setattr(settings, "drop_orphan_columns", False)
        await _handle_orphan_columns()
        async with async_engine.connect() as conn:
            assert await conn.run_sync(_column_exists), "flag off обязан сохранить колонку"

        monkeypatch.setattr(settings, "drop_orphan_columns", True)
        await _handle_orphan_columns()
        async with async_engine.connect() as conn:
            assert not await conn.run_sync(_column_exists), "flag on обязан удалить колонку"
    finally:
        await async_engine.dispose()