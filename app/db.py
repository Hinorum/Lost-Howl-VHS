import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import postgres_connect_args, settings
from app.models import Base

logger = logging.getLogger("way.db")


def _sqlite_connect_args() -> dict:
    """SQLite: ждём освобождения записи до 30 с — фоновые задачи (тизер,
    преген) и тик могут пересекаться в тестах и на слабых дисках."""
    return {"timeout": 30}


engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    # Пулеры (Supabase/pgbouncer) закрывают простаивающие серверные соединения
    # по server_lifetime (~15 мин у Supabase; поверх этого копятся «Idle session
    # timeout»). Пересоздаём нашу сторону явно и раньше — 900 с < 15 мин — чтобы
    # pre_ping не зацеплял мёртвый сокет (лишний round-trip на каждый checkout)
    # и слоты пулера не висели занятыми весь срок жизни серверного коннекта.
    pool_recycle=900,
    connect_args=(
        _sqlite_connect_args()
        if settings.async_database_url.startswith("sqlite")
        else postgres_connect_args(settings.database_url)
    ),
)

# Safety net: if a connection is returned to the pool with a failed
# transaction (InFailedSQLTransactionError), roll it back so the next
# session doesn't inherit the poison.
if settings.async_database_url.startswith("postgresql"):
    @event.listens_for(engine.sync_engine, "checkout")
    def _reset_on_checkout(dbapi_conn, connection_record, connection_proxy):
        try:
            dbapi_conn.rollback()
        except Exception:
            logger.warning("Пул отдал подключение с зависшей транзакцией — rollback не удался", exc_info=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Единственный источник правды о схеме — migrations/ (Alembic). Код запуска не
# держит ни одной строки DDL руками:
#   * свежая база собирается из моделей (Base.metadata.create_all) ровно под
#     текущую версию и штампуется на head (последующий upgrade — честный no-op);
#   * существующая база мигрирует `alembic upgrade head` по своему таймлайну;
#   * базы create_all-эпохи БЕЗ alembic-истории конвергируются в таймлайн через
#     идемпотентную ревизию legacy_convergence (см. _migrate).
_LEGACY_RECONCILE_ANCHOR = "a7b8c9d0e1f2"


def _alembic_head() -> str:
    """Ревизия head из alembic-скриптов без подключения к БД."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    """Запуск alembic-команды в окружении приложения (subprocess).

    migrations/env.py сам подхватывает настройки приложения (URL, CA),
    поэтому аргументов достаточно. CWD — корень репозитория (alembic.ini
    рядом). DATABASE_URL подставляется из окружения или settings.database_url.
    """
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.setdefault("DATABASE_URL", settings.database_url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=root,
        env=env,
    )


def _tables(sync_conn) -> set[str]:
    return set(inspect(sync_conn).get_table_names())


def _orphan_columns(sync_conn) -> dict[str, list[str]]:
    """NOT NULL-колонки без DEFAULT, которых больше нет в моделях.

    Такой столбец ловится моделью до её перестройки: INSERT нового дня не
    передаёт значение дропнутой механики и падает на NOT NULL. Возвращает
    словарь «таблица → колонки»; дроп делает _handle_orphan_columns.
    """
    meta = Base.metadata
    orphans: dict[str, list[str]] = {}
    for table_name in inspect(sync_conn).get_table_names():
        if table_name not in meta.tables:
            continue
        model_columns = set(meta.tables[table_name].columns.keys())
        for col in inspect(sync_conn).get_columns(table_name):
            name = col["name"]
            if name in model_columns:
                continue
            if col.get("nullable") is False and col.get("default") is None:
                orphans.setdefault(table_name, []).append(name)
    return orphans


def _drop_orphan_columns(sync_conn) -> None:
    for table_name, columns in _orphan_columns(sync_conn).items():
        for name in columns:
            logger.info("DROP orphan NOT NULL column %s.%s", table_name, name)
            sync_conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {name}"))


async def _handle_orphan_columns() -> None:
    """Осиротевшие NOT NULL-колонки без DEFAULT (дропнутые механики) ломают
    INSERT новых дней. Диагностика всегда; дроп — только по флагу
    DROP_ORPHAN_COLUMNS: раньше init_db сносил их на каждом старте без спроса,
    и при откате/канарее старая версия могла удалить колонку, нужную новой."""
    async with engine.connect() as conn:
        found = await conn.run_sync(_orphan_columns)
    if not found:
        return
    if not settings.drop_orphan_columns:
        logger.warning(
            "Найдены осиротевшие NOT NULL-колонки — INSERT новых дней может "
            "падать: %s. Если колонки больше не нужны, включи "
            "DROP_ORPHAN_COLUMNS=true на один деплой и верни в false.",
            found,
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_drop_orphan_columns)
    logger.warning("Осиротевшие NOT NULL-колонки удалены: %s", found)


async def _stamp_alembic_head(conn) -> None:
    """Асимметрия create_all ↔ alembic: бутстрап через create_all создаёт
    таблицы, но не alembic_version — ручной `alembic upgrade head` на такой
    базе упёрся бы в «table already exists». Ставим якорь на head: повторный
    upgrade становится честным no-op. БД, которая уже ведётся alembic'ом
    (alembic_version непуста), не трогаем."""
    try:
        head = _alembic_head()
    except Exception:
        logger.warning("alembic head не читается — stamp пропущен", exc_info=True)
        return
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num))"
        )
    )
    current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().first()
    if current is not None:
        return
    await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:head)").bindparams(head=head))
    logger.info("alembic_version помечена на head=%s после create_all", head)


async def _migrate() -> None:
    """Привести схему к текущим моделям ИСКЛЮЧИТЕЛЬНО через alembic.

    Обычный путь — `upgrade head` по таймлайну базы. Если он не проходит
    (легаси-база create_all-эпохи вне таймлайна: история ревизий расходится
    с фактической схемой), дотягиваем недостающие таблицы по моделям,
    штампуемся на якорь и сходимся единственной идемпотентной ревизией
    legacy_convergence — без прогона промежуточной геометрии, которая могла
    бы упереться в уже существующие колонки.
    """
    result = await asyncio.to_thread(_run_alembic, "upgrade", "head")
    if result.returncode == 0:
        logger.info("Схема приведена к head: alembic upgrade")
        return

    logger.warning(
        "alembic upgrade head не прошёл на живой базе — реконсиляция легаси. "
        "Хвост ошибки: %s",
        result.stderr.strip()[-800:],
    )

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
            await conn.execute(text("SET search_path TO public"))
        await conn.run_sync(Base.metadata.create_all)

    stamped = await asyncio.to_thread(_run_alembic, "stamp", _LEGACY_RECONCILE_ANCHOR)
    if stamped.returncode != 0:
        raise RuntimeError(
            "Реконсиляция: не удалось заштамповать легаси-базу на якорь. " + stamped.stderr.strip()[-500:]
        )
    converged = await asyncio.to_thread(_run_alembic, "upgrade", "head")
    if converged.returncode != 0:
        raise RuntimeError(
            "Реконсиляция: ревизия legacy_convergence не сошлась. " + converged.stderr.strip()[-500:]
        )
    logger.warning("Легаси-база конвергирована в alembic-таймлайн: версия на head")


async def init_db() -> None:
    Path("data").mkdir(exist_ok=True)

    async with engine.connect() as conn:
        has_tables = bool(await conn.run_sync(_tables))

    if not has_tables:
        # Свежая база: схему собираем из моделей напрямую (create_all) и
        # штампуем alembic-версию на head — последующий `alembic upgrade head`
        # становится честным no-op. Дальнейшая эволюция схемы — только alembic.
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
                await conn.execute(text("SET search_path TO public"))
            await conn.run_sync(Base.metadata.create_all)
            await _stamp_alembic_head(conn)
        logger.info("Свежая база собрана из моделей: alembic-версия на head")
    else:
        await _migrate()

    if settings.async_database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))

    await _handle_orphan_columns()