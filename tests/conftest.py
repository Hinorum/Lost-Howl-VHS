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

import asyncio
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler as _AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

# Страховка от планировщика, пережившего свой тест.
#
# AsyncIOScheduler живёт на loop.call_later (TimerHandle), а не в asyncio-задаче,
# поэтому _quiesce_background_tasks его не видит: настоящий планировщик,
# поднятый тестом, садится на общий (session-scoped) event loop и без
# остановки бежит до конца прогона. Так и было: way-tick писал в общую БД, а
# ton-watch гонял watch_once -> _collect_transfers и съедал страницы
# скриптованного HTTP соседнего теста — отсюда flake
# test_toncenter_pagination_walks_by_offset. Каждый старт регистрируем, а на
# выходе из теста глушим: даже если тест забыл сам, утекать нечему.
_started_schedulers: list = []
_original_scheduler_start = _AsyncIOScheduler.start


def _track_scheduler_start(self, paused: bool = False):
    result = _original_scheduler_start(self, paused)
    if self not in _started_schedulers:
        _started_schedulers.append(self)
    return result


_AsyncIOScheduler.start = _track_scheduler_start


async def _stop_started_schedulers() -> None:
    """Остановить планировщики, пережившие свой тест (teardown каждого теста).

    shutdown(wait=False) дополнительно отменяет in-flight задачи джоб
    (AsyncIOExecutor.shutdown), поэтому заботливо дожидаемся и их.
    """
    while _started_schedulers:
        scheduler = _started_schedulers.pop()
        if not scheduler.running:
            continue
        scheduler.shutdown(wait=False)
        # shutdown уходит в call_soon_threadsafe — даём циклу отработать
        # и саму остановку, и отмену уже запущенных джоб.
        await asyncio.sleep(0.01)
        assert not scheduler.running, (
            "планировщик не остановился — он продолжит дёргать джобы "
            "посреди чужих тестов"
        )


@pytest.fixture
def scheduler_guard() -> tuple[list, object]:
    """Реестр и остановка планировщиков, переживших свой тест.

    Отдаётся тесту, чтобы он мог проверить саму страховку: старт должен попасть
    в реестр, остановка — вытащить оттуда и погасить планировщик.
    """
    return _started_schedulers, _stop_started_schedulers


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


async def ensure_player(player_id: int) -> None:
    """Гарантировать игрока в глобальной БД — родителя для payouts/stakes/votes.

    SQLite внешние ключи не проверяет, поэтому тесты исторически сыпали
    player_id «из воздуха»: на SQLite зелёно, на Postgres IntegrityError.
    Хелпер создаёт родителя, если его нет; существующего не трогает.
    Импорт — `from conftest import ensure_player` (pytest добавляет tests/ в
    sys.path для модулей без __init__.py).
    """
    from app.db import SessionLocal
    from app.models import Player

    async with SessionLocal() as session:
        if await session.get(Player, player_id) is None:
            session.add(Player(id=player_id))
            await session.commit()


async def ensure_round(round_id: int) -> None:
    """Гарантировать раунд c id=round_id в глобальной БД — родителя для строк.

    Тесты привязаны к фиксированному id раунда (комменты вида way:<day>#<id>,
    day_index), поэтому день создаётся с day_index=id: приложение держит их
    выровненными, а расхождение дало бы уникальный конфликт на day_index.
    Идемпотентно: существующий раунд не трогается.
    """
    from app.db import SessionLocal
    from app.models import Round, RoundStatus, WinRule

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        if await session.get(Round, round_id) is None:
            session.add(
                Round(
                    id=round_id,
                    day_index=round_id,
                    status=RoundStatus.OPEN,
                    win_rule=WinRule.MAJORITY,
                    chapter_title=f"Тестовый день {round_id}",
                    chapter_text="Тестовая глава",
                    opens_at=now,
                    voting_ends_at=now,
                    tally_ends_at=now,
                )
            )
            await session.commit()


@pytest.fixture(autouse=True)
async def _quiesce_background_tasks():
    """Дождаться фоновых задач, переживших свой тест, и отменить висящие.

    spawn() создаёт настоящие asyncio-задачи (app/async_utils.py). Тест, дёрнувший
    tick(), может запустить финализацию дня с фоновыми джобами — и они продолжают
    писать в ОБЩУЮ БД, когда тест уже завершился и следующий уже начался.

    Именно это делало прогон нестабильным: падали по очереди
    test_tick_heartbeat::test_snapshot_reports_counter,
    test_failed_tick_leaves_heartbeat_stale и
    test_ton::test_repeat_stake_and_closed_day_transfers_are_refunded — по
    разным причинам, но все три читали глобальные ключи и счётчики, которые
    досасыпала чужая задача (TICK_FAIL_KEY, TICK_KEY, открытый раунд).

    Тесты ничего не теряют: задачи, которые тест хочет дождаться, он ждёт сам.
    Здесь гасим только то, что осталось висеть, и делаем это ДО очистки БД
    следующего теста.

    Отдельно гасится переживший тест планировщик (см. _track_scheduler_start):
    он живёт на TimerHandle, а не в задаче, и без остановки продолжал бы
    дёргать way-tick/ton-watch уже в чужом тесте.
    """
    from app.async_utils import _TASKS

    yield
    # Планировщик раньше задач: он сам их порождает, глушим источник первым.
    await _stop_started_schedulers()
    pending = [task for task in list(_TASKS) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _TASKS.clear()


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
