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
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

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
    """Postgres или нет — по схеме DSN, а не по точному префиксу.

    app.config хранит database_url ровно как задали в окружении, а
    postgresql+asyncpg:// — законная запись (её нормализует sqlalchemy_url,
    и db.py с ней работает). Раньше проверка ловила только postgres:// и
    postgresql://, и при «+asyncpg» бэкап молча выключался: is_postgres()
    давало False, sqlite_file_path() — тоже None, и backup_now() возвращал
    None без единой записи в лог. Место, где бэкапов нет, обязано хотя бы
    говорить об этом вслух.
    """
    return settings.database_url.startswith(("postgres://", "postgresql://", "postgresql+"))


def _pg_env(url: str) -> tuple[dict[str, str], str]:
    """(окружение для pg_dump, DSN без пароля).

    Пароль передаётся через PGPASSWORD, а не аргументом командной строки.
    Причина: argv виден любому процессу системы (`ps aux`, /proc/*/cmdline,
    сторонние мониторы), и секрет базы попадает в вывод и в логи процессов.
    libpq штатно читает пароль из PGPASSWORD, поэтому поведение не меняется —
    меняется только способ доставки. Прокси/цепочка в DSN
    (postgresql://...?...&options=-c...) сохраняется как есть: libpq сам
    разберёт параметры из строки подключения.
    """
    parsed = urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    clean = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env, clean


def _pg_dump_sync(dest: Path) -> None:
    """pg_dump в custom-формате: сжатый, восстанавливается pg_restore.

    DSN отдаётся как есть, кроме пароля (см. _pg_env): libpq понимает
    postgres:// и параметры провайдера, sslmode и прочие вычищать не нужно.
    """
    env, clean_url = _pg_env(settings.database_url)
    result = subprocess.run(
        ["pg_dump", "--format=custom", f"--file={dest}", clean_url],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        env=env,
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
    size_kb = dest.stat().st_size / 1024
    logger.info(
        "Бэкап БД готов: %s (%.1f КБ)%s",
        dest.name,
        size_kb,
        f", удалено старых: {pruned}" if pruned else "",
    )
    await _verify_backup(dest)
    await _mark_backup_ok()
    return dest


async def _verify_backup(dest: Path) -> None:
    """Файл бэкапа должен существовать и быть непустым.

    Для SQLite проверка настоящая: открываем копию и читаем с неё схему. Дамп
    без данных (упавший процесс, нехватка места) останется валидным файлом, и
    тревога «бэкапов давно нет» молчала бы, хотя восстанавливать нечего.
    """
    if not dest.exists():
        raise RuntimeError(f"бэкап не создан: {dest}")
    size = dest.stat().st_size
    if size == 0:
        raise RuntimeError(f"бэкап пустой: {dest}")
    if dest.suffix == ".db":
        def _probe() -> int:
            conn = sqlite3.connect(str(dest))
            try:
                row = conn.execute(
                    "select count(*) from sqlite_master where type='table'"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()

        try:
            tables = await asyncio.to_thread(_probe)
        except sqlite3.DatabaseError as exc:
            # Битый или недописанный файл: это провал бэкапа, а не сбой проверки.
            # Без перехвата сырое исключение уехало бы из задачи планировщика
            # мимо алерта, и хранитель увидел бы только «бэкап БД не удался»
            # без указания, что копия есть и она мусор.
            raise RuntimeError(f"копия SQLite не читается ({exc}): {dest}") from exc
        if tables == 0:
            raise RuntimeError(f"бэкап без таблиц: {dest}")
        logger.info("Бэкап проверен: %d таблиц в копии", tables)
    else:
        # pg_dump в custom-формате начинается магическим заголовком PGDMP;
        # без него файл не восстановится.
        with dest.open("rb") as handle:
            magic = handle.read(5)
        if magic != b"PGDMP":
            raise RuntimeError(f"дамп без заголовка PGDMP: {dest}")


async def _mark_backup_ok() -> None:
    """Отметить время последнего успешного бэкапа для тревоги в ops.

    Отметка ставится только после успешной записи файла и его чтения обратно.
    Нулевой или урезанный дамп — это не бэкап, и считать его успешным нельзя:
    иначе тревога «бэкапов нет давно» молчала бы, пока бэкапы перестали бы
    восстанавливаться в принципе.
    """
    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    async with SessionLocal() as session:
        session.add(
            WatcherState(key=BACKUP_LAST_OK_KEY, value=datetime.now(UTC).isoformat())
        )
        await session.commit()


async def backup_job() -> None:
    """Задача планировщика: SQLite напрямую, Postgres через pg_dump."""
    try:
        await backup_now()
    except FileNotFoundError:
        logger.warning("pg_dump недоступен — бэкап Postgres пропущен.")
    except Exception:
        logger.exception("Бэкап БД не удался")
