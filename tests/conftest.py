import os
import tempfile

# Тестовая БД — отдельная и ЯВНАЯ.
#
# Раньше здесь стоял setdefault("DATABASE_URL", ...): унаследованная
# переменная побеждала, а DATABASE_URL в любой машине с .env указывает на
# БОЕВУЮ базу. Модульная фикстура чистит все таблицы перед каждым модулем —
# то есть `pytest` в шелле с экспортированным DATABASE_URL стирал прода.
#
# Теперь:
# - TEST_DATABASE_URL — осознанный выбор БД прогона (CI: Postgres 16);
# - иначе — временный SQLite-файл (имя уникально на процесс: два
#   одновременных прогона не делят одну базу и не портят её друг другу);
# - DATABASE_URL перекрывается БЕЗУСЛОВНО: переменная приложения не должна
#   указывать тестами на боевой контур.
# Живой e2e-прогон (E2E_TESTNET=1) — единственное исключение: там нужен
# тестнетовый контур целиком, и очистка таблиц отключается (см.
# _clean_global_db_per_module).
_LIVE_E2E = os.environ.get("E2E_TESTNET") == "1"
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
_DB_PATH = ""
if not _LIVE_E2E:
    if not _TEST_DB_URL:
        _DB_PATH = os.path.join(
            tempfile.gettempdir(), f"the_ways_handlers_test_{os.getpid()}.db"
        )
        _TEST_DB_URL = "sqlite+aiosqlite:///" + _DB_PATH
    os.environ["DATABASE_URL"] = _TEST_DB_URL
    # Файл пересоздаём при каждом прогоне, чтобы схема всегда соответствовала
    # текущим моделям.
    for _suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(_DB_PATH + _suffix)
        except OSError:
            # Файл может быть занят (Windows держит его открытым, пока держит
            # другой процесс) — ловим любую ошибку, а не только FileNotFoundError:
            # раньше PermissionError на коллекции ронял ВЕСЬ прогон. У каждого
            # прогона своё имя файла, поэтому занятость ему не мешает.
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
# BOT_TOKEN/ADMIN_IDS — ровно CI-шные (см. .github/workflows/ci.yml): чистый
# клон без них падал на пяти тестах (test_log_scrub: пустой набор секретов
# выключает краску, и счётчик сбоев не растёт; test_backup_command: пустой
# admin_id_set). Значения не боевые — dummy-токен и id=1, как в CI.
os.environ.setdefault("BOT_TOKEN", "123456:ci-dummy-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("TREASURY_ADDRESS", "")
os.environ.setdefault("TREASURY_MNEMONIC", "")
# /health закрыт по умолчанию (fail closed): пользовательский .env с
# HEALTH_REQUIRE_TOKEN=false не смеет разблокировать снимок в прогоне —
# тесты дефолта (test_metrics) строят Settings() из реального окружения.
os.environ.setdefault("HEALTH_REQUIRE_TOKEN", "true")
# Сам токен тоже держим пустым: боевой .env с непустым HEALTH_TOKEN иначе
# закрывает /health и /metrics для тестов без заголовка (401 вместо 200) —
# прогон снова зависел бы от машины разработчика при зелёном CI (в CI .env
# нет). Тесты, которым нужен закрытый контур, задают токен сами.
os.environ.setdefault("HEALTH_TOKEN", "")
# DATABASE_CA из боевого .env подставляет кастомный SSL-контекст ЛЮБОМУ
# Postgres-хосту (settings.database_ca приоритетнее автоматики pooler-суффикса),
# и тесты connect-args, ожидающие ssl=True, падали при зелёном CI.
os.environ.setdefault("DATABASE_CA", "")
# Ключ Render API в боевом .env — тестам он не нужен, а его попадание в
# repr настроек (падающий тест печатает Settings целиком) означало бы утечку
# ключа в вывод прогона. Пустая переменная перекрывает .env: приоритет у
# переменных окружения выше.
os.environ.setdefault("RENDER_API_KEY", "")

import asyncio
import datetime as _dt
import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

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


class _TestDbGuard(NamedTuple):
    """Доступ к гарду тестовой БД из теста (см. фикстуру test_db_guard)."""

    assert_database: object
    truncate_all: object
    db_path: str
    test_db_url: str

# Хранилище кассет в базе (app/story/store.py) по умолчанию ВЫКЛЮЧЕНО в тестах:
# его включают только тесты самого хранилища — им нужны свой каталог и свои
# строки. При включённом флаге boot-тесты сеяли бы реальную библиотеку в
# тестовую БД, а панельные толкали бы тестовые файлы (mel.json) в каталог
# репозитория общим зеркалом — рабочее дерево становилось бы грязным.
from app.config import settings as _settings
from app.models import Base

_settings.story_cassettes_db = False

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


@pytest.fixture(scope="session")
def test_db_guard():
    """Гард тестовой БД: отдаётся тесту, чтобы он проверил сам себя.

    Смысл гарда — тот же, что у scheduler_guard: страховка, которую никто не
    проверяет, однажды оказывается неработающей. Тест получает и проверку
    БД, и саму очистку, и путь к временной базе — импорт conftest вторым
    модулем исполнял бы его верхний код ещё раз (и снёс бы файл базы).
    """
    return _TestDbGuard(
        assert_database=_assert_test_database,
        truncate_all=truncate_all,
        db_path=_DB_PATH,
        test_db_url=_TEST_DB_URL,
    )


@pytest.fixture(scope="session", autouse=True)
async def _global_db_schema():
    """Глобальная БД (SessionLocal) пересоздаётся по текущим моделям каждый прогон."""
    from app.db import init_db

    await init_db()
    yield
    # Временная база прогона — на диске её оставать незачем: файл в темпе
    # копится от прогона к прогону и ничего не объясняет.
    if _DB_PATH:
        for _suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(_DB_PATH + _suffix)
            except OSError:
                pass


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

    В живом e2e-режиме (E2E_TESTNET=1) очистка НЕ выполняется: там база —
    тестнетовый контур, и снос таблиц уничтожил бы его состояние.
    """
    from app.db import SessionLocal

    if _LIVE_E2E:
        yield
        return
    async with SessionLocal() as db:
        await truncate_all(db)
        await db.commit()
    yield


def _assert_test_database(db) -> None:
    """Очистка таблиц — только в той БД, которую прогон объявил тестовой.

    Фикстура выше чистит ВСЕ таблицы перед каждым модулем. На боевой базе
    это означало бы wipe прода, поэтому прогон обязан убедиться, что цель —
    именно та БД, которую он объявил тестовой (TEST_DATABASE_URL или
    временный SQLite-файл). Всё остальное — отказ, а не предупреждение:
    ошибочно вычищенная чужая база не восстанавливается.
    """
    from sqlalchemy.engine import make_url

    if _LIVE_E2E:
        return
    bind = db.get_bind()
    url = bind.url
    expected = make_url(_TEST_DB_URL)
    same_backend = url.get_backend_name() == expected.get_backend_name()
    same_name = (url.database or "") == (expected.database or "")
    same_host = (url.host or "") == (expected.host or "")
    if not (same_backend and same_name and same_host):
        raise RuntimeError(
            "тесты отказываются чистить не-тестовую БД: "
            f"подключено {url.get_backend_name()}://{url.host or ''}/"
            f"{url.database or ''}, а прогон объявил тестовой "
            f"{expected.get_backend_name()}://{expected.host or ''}/"
            f"{expected.database or ''}. Проверь TEST_DATABASE_URL: тесты "
            "перекрывают DATABASE_URL принудительно и не наследуют её."
        )


async def truncate_all(db) -> None:
    """Полная очистка глобальной БД тестов, безопасная по внешним ключам.

    На Postgres идём через TRUNCATE ... RESTART IDENTITY CASCADE: DELETE по
    таблицам упирается во внешние ключи, которые SQLite по умолчанию не
    проверяет, и падал бы на сиде, оставшемся от другого модуля. TRUNCATE с
    CASCADE не зависит от порядка таблиц и обнуляет счётчики. В SQLite (и во
    всём, что не Postgres) остаётся прежний порядок «дети раньше родителей».
    """
    from sqlalchemy import delete, text

    _assert_test_database(db)
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

    Заодно ставим свежую отметку последнего бэкапа и свежий бит тика:
    проверка свежести бэкапа и тревога «никогда не тикал» входят в
    check_anomalies, поэтому «проблем нет» требует, чтобы бэкап был, а
    планировщик считался работающим. Тесты, которые проверяют сами эти
    тревоги, отметки убирают явно (test_backups.py, test_tick_heartbeat.py).
    """
    from sqlalchemy import delete

    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState
    from app.ops import TICK_KEY

    now_iso = datetime.now(UTC).isoformat()
    async with SessionLocal() as db:
        await db.execute(delete(WatcherState))
        db.add(WatcherState(key=BACKUP_LAST_OK_KEY, value=now_iso))
        db.add(WatcherState(key=TICK_KEY, value=now_iso))
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
