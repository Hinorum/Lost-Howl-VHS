"""Резервные копии базы: ротация файлов, раз в сутки и при старте.

Для SQLite используется штатный backup-API — снимок консистентен даже при
живых записях (WAL). Для PostgreSQL дамп делается здесь же, утилитой pg_dump
(если она установлена в окружении): копии пишутся в data/backups и ротируются
до KEEP штук. Диск Render-контейнера эфемерный — забирай файлы или включи
снапшоты провайдера; но даже эфемерный дневной дамп спасает от случайного
/resetgame и ошибочных миграций.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

KEEP = 7


def sqlite_file_path() -> Path | None:
    """Путь к файлу БД, если это локальный SQLite; иначе None."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1]
    raw = raw.split("?", 1)[0]
    return Path(raw)


def is_postgres() -> bool:
    return settings.database_url.startswith(("postgres://", "postgresql://"))


def _pg_dump_sync(dest: Path) -> None:
    """pg_dump в custom-формате: сжатый, восстанавливается pg_restore.

    URL отдаётся как есть (libpq понимает postgres:// и параметры провайдеров);
    sslmode и прочие libpq-параметры вычищать не нужно — они для pg_dump родные.
    """
    result = subprocess.run(
        ["pg_dump", "--format=custom", f"--file={dest}", settings.database_url],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    if result.stderr.strip():
        logger.warning("pg_dump предупреждает: %s", result.stderr.strip()[:500])


def _prune(directory: Path, keep: int) -> int:
    backups = sorted(directory.glob("backup-*"))
    removed = 0
    for stale in backups[:-keep] if len(backups) > keep else []:
        stale.unlink(missing_ok=True)
        removed += 1
    return removed


async def backup_now(keep: int = KEEP) -> Path | None:
    """Создаёт копию БД (SQLite или Postgres через pg_dump) и подрезает хвост."""
    directory = Path("data") / "backups"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    if is_postgres():
        if shutil.which("pg_dump") is None:
            logger.warning(
                "pg_dump не найден в окружении — бэкап Postgres пропущен. "
                "Включи снапшоты провайдера (Neon) или поставь postgresql-client."
            )
            return None
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / f"backup-{stamp}.dump"
        await asyncio.to_thread(_pg_dump_sync, dest)
    else:
        source = sqlite_file_path()
        if source is None or not source.exists():
            return None
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / f"backup-{stamp}.db"

        def _sqlite_copy() -> None:
            # Живая БД в этот момент пишется тиком: без timeout при заблокированной
            # базе копия вылетала бы «database is locked». 30с — запас на всплеск.
            src_conn = sqlite3.connect(str(source), timeout=30.0)
            try:
                dst_conn = sqlite3.connect(str(dest), timeout=30.0)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

        await asyncio.to_thread(_sqlite_copy)
    pruned = await asyncio.to_thread(_prune, directory, keep)
    logger.info(
        "Бэкап БД готов: %s (%.1f КБ)%s",
        dest.name,
        dest.stat().st_size / 1024,
        f", удалено старых: {pruned}" if pruned else "",
    )
    return dest


async def backup_job() -> None:
    """Задача планировщика: SQLite напрямую, Postgres через pg_dump."""
    try:
        await backup_now()
    except FileNotFoundError:
        logger.warning("pg_dump недоступен — бэкап Postgres пропущен.")
    except Exception:
        logger.exception("Бэкап БД не удался")
