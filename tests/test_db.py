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


def test_upgrade_head_creates_partial_payout_tx_unique(tmp_path) -> None:
    """Миграция a3f7d19c5b42 ставит частичный unique на (tx_hash, network).

    БД-барьер от двойной оплаты там, где раньше был только код (claim_once
    в ton_watch.refunds и условный UPDATE по status='pending' в диспетчере).
    Условие обязано выпускать и NULL, и метку вещания bcast:<unix> — иначе два
    перевода, разосланные в одну секунду, конфликтовали бы между собой.
    """
    db_file = tmp_path / "payout_uq.db"
    env = dict(os.environ) | {"DATABASE_URL": f"sqlite+aiosqlite:///{db_file}"}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='payouts'"
            ).fetchall()
    finally:
        engine.dispose()

    ddl = {row[0]: row[1] or "" for row in rows}.get("uq_payout_tx_network", "")
    assert ddl, "частичный unique на payouts.tx_hash не создан миграцией"
    assert "UNIQUE" in ddl.upper()
    assert "WHERE tx_hash IS NOT NULL" in ddl
    assert "bcast" in ddl


def test_payout_tx_unique_migration_refuses_to_guess_over_duplicates(tmp_path) -> None:
    """Если дубли tx_hash уже накопились — миграция падает с перечнем строк.

    Молча обнулить хеш у второй строки нельзя: это вернуло бы её в сверку
    (tx_hash IS NULL попадает в confirm_broadcast_payouts) и могло привести к
    повторной отправке. Лучше старт с понятной ошибкой, чем тихая правка
    данных о деньгах.
    """
    # Ревизия ДО барьера: снимаем индекс, кладём дубли, поднимаем обратно.
    before_barrier = "d8f1a2b3c4d5"
    db_file = tmp_path / "payout_dups.db"
    env = dict(os.environ) | {"DATABASE_URL": f"sqlite+aiosqlite:///{db_file}"}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            env=env,
        )

    assert run("upgrade", "head").returncode == 0
    assert run("downgrade", before_barrier).returncode == 0

    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            for pid in (1, 2):
                conn.exec_driver_sql(
                    "INSERT INTO payouts (id, kind, amount_nanotons, dest_address, "
                    "tx_hash, network, status, attempts, alerted) "
                    f"VALUES ({pid}, 'refund', 1000000000, '0:aa', 'tx-same', "
                    "'mainnet', 'pending', 0, 0)"
                )
    finally:
        engine.dispose()

    proc = run("upgrade", "head")
    assert proc.returncode != 0, "миграция обязана отказаться гадать с дублями"
    assert "Разберись с дублями" in proc.stderr
    assert "tx-same" in proc.stderr


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