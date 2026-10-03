import os
import tempfile

# Отдельная БД для тестов хендлеров: переменная окружения сильнее .env,
# задаём ДО первых импортов app.*. Файл пересоздаём при каждом прогоне,
# чтобы схема всегда соответствовала текущим моделям.
_DB_PATH = os.path.join(tempfile.gettempdir(), "the_ways_handlers_test.db")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + _DB_PATH)
for _suffix in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_DB_PATH + _suffix)
    except FileNotFoundError:
        pass

# Герметичность к пользовательскому .env: pydantic (app.config) читает .env из
# CWD, и файл с контуром (TON_NETWORK=testnet, TON_ENABLED=true) молча ломал
# весь прогон — ставки тестов сидились под mainnet и обнулялись. Настоящие
# переменные окружения имеют приоритет над .env, поэтому принудительно держим
# нейтральный контур. Живой e2e-прогон — ТОЛЬКО через явные шелл-переменные
# (setdefault не перетирает уже заданные), например:
#   $env:E2E_TESTNET=1; $env:TON_NETWORK=testnet; pytest -m e2e
os.environ.setdefault("TON_ENABLED", "false")
os.environ.setdefault("TON_NETWORK", "mainnet")
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("TREASURY_ADDRESS", "")
os.environ.setdefault("TREASURY_MNEMONIC", "")
# /health закрыт по умолчанию (fail closed): пользовательский .env с
# HEALTH_REQUIRE_TOKEN=false не смеет разблокировать снимок в прогоне —
# тесты дефолта (test_metrics) строят Settings() из реального окружения.
os.environ.setdefault("HEALTH_REQUIRE_TOKEN", "true")

import datetime as _dt
import sqlite3
from datetime import UTC, datetime

# Python 3.12 объявил устаревшим встроенный адаптер datetime/date в sqlite3, и
# pytest.ini гоняет DeprecationWarning как ошибку — любой raw text() с параметром
# datetime ронял прогон. Рецепт замены из документации sqlite3: адаптер
# объявляется явно. Формат ровно тот же, что был у встроенного (пробел вместо
# «T», микросекунды при наличии), поэтому поведение не меняется — исчезает
# только предупреждение. Глобальная регистрация осознанна: это рецепт из
# stdlib, иначе каждый новый фикстурный raw-SQLite тест падал бы заново.
sqlite3.register_adapter(_dt.datetime, lambda v: v.isoformat(" "))
sqlite3.register_adapter(_dt.date, lambda v: v.isoformat())

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


@pytest.fixture(scope="session", autouse=True)
async def _global_db_schema():
    """Глобальная БД (SessionLocal) пересоздаётся по текущим моделям каждый прогон."""
    from app.db import init_db

    await init_db()
    yield


@pytest.fixture(scope="module", autouse=True)
async def _clean_global_db_per_module():
    """Каждый тестовый модуль стартует с пустой глобальной БД.

    Тесты не должны зависеть от порядка запуска файлов: любые сиды,
    оставшиеся в SessionLocal от предыдущего модуля (чаты, игроки,
    состояния watcher'а), затираются до первого теста модуля.

    На Postgres чистим через TRUNCATE ... RESTART IDENTITY CASCADE: иначе
    DELETE по таблицам упирается в внешние ключи, которые SQLite по
    умолчанию не проверяет, и модуль падал бы на чужом сиде. TRUNCATE с
    CASCADE не зависит от порядка таблиц и обнуляет счётчики.
    """
    from app.db import SessionLocal

    async with SessionLocal() as db:
        await truncate_all(db)
        await db.commit()
    yield


async def truncate_all(db) -> None:
    """Полная очистка глобальной БД тестов, безопасная по внешним ключам.

    На Postgres идём через TRUNCATE ... RESTART IDENTITY CASCADE: DELETE по
    таблицам упирается во внешние ключи, которые SQLite по умолчанию не
    проверяет, и падал бы на сиде, оставшемся от другого модуля. TRUNCATE с
    CASCADE не зависит от порядка таблиц и обнуляет счётчики. В SQLite (и во
    всём, что не Postgres) остаётся прежний порядок «дети раньше родителей».
    """
    from sqlalchemy import delete, text

    tables = Base.metadata.sorted_tables
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        names = ", ".join(f'"{table.name}"' for table in tables)
        await db.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        return
    for table in reversed(tables):
        await db.execute(delete(table))


@pytest.fixture(autouse=True)
async def _clean_watcher_state_between_tests():
    """watcher_state не должен перетекать между тестами одного модуля.

    Флаги готовности (неделя/месяц), маркеры события дня («микрособытие»,
    «тизер») и точки-якоря живут в глобальной БД с уникальным ключом —
    один тест успевает освободить свой ключ только в finally, другой
    в том же модуле налетает на IntegrityError. Чистим таблицу в начале
    каждого теста (дёшево: таблица мелкая).

    Заодно ставим свежую отметку последнего бэкапа: проверка свежести
    бэкапа входит в check_anomalies, поэтому «проблем нет» требует, чтобы
    бэкап был. Тесты, которые проверяют саму эту тревогу, отметку убирают
    явно (test_backups.py).
    """
    from sqlalchemy import delete

    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    async with SessionLocal() as db:
        await db.execute(delete(WatcherState))
        db.add(
            WatcherState(
                key=BACKUP_LAST_OK_KEY,
                value=datetime.now(UTC).isoformat(),
            )
        )
        await db.commit()
    yield


@pytest.fixture(autouse=True)
def _refund_caps_off(monkeypatch):
    """Потолки авто-возвратов выключены по умолчанию.

    Лимиты считаются по реальным строкам выплат за сутки, а тесты живут в общей
    БД: модуль, насыпавший возвратов, исчерпал бы потолок для всех остальных, и
    тест падал бы не из-за своей логики, а из-за порядка прогона. Тесты самих
    потолков включают их явно (test_payout_admin.py).
    """
    from app.config import settings

    monkeypatch.setattr(settings, "refund_max_per_sender_day", 0)
    monkeypatch.setattr(settings, "refund_max_total_day", 0)


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()
