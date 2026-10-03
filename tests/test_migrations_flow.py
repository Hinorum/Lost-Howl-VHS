"""Локальный Alembic-flow: цепочка миграций против текущих моделей.

Зачем эти тесты. Схема живёт по правилу «свежая база — create_all с
штампом на head, существующая — alembic upgrade head». Правило держится на
двух неочевидных вещах, и обе молчали:

  * цепочка миграций, собранная с нуля, должна давать ту же схему, что и
    create_all, — иначе база, поднятая руками по README, ведёт себя иначе,
    чем прод, и отличить это можно только по симптому в проде;
  * init_db на базе, которую уже вёл alembic, обязан быть no-op'ом и не
    терять данные — иначе рестарт бота может стать миграцией.

Проверяем ровно это. alembic запускается subprocess'ом, как это делает
app.db._run_alembic, чтобы тест проверял ровно тот путь, который живёт в
рантайме (и не перенастраивал logging внутри процесса pytest).

Известное расхождение закреплено в KNOWN_DIVERGENCES: ревизия
a7b8c9d0e1f2 добавила rounds.referral_nanotons с server_default=0, модель
объявляет только питоновский default=0. На живых базах (create_all)
серверного дефолта нет, ORM значение всегда подставляет сам, так что
поведение одинаковое; rounds ради косметики не переписываем. Список не
«у Allowance на будущее», а утверждение: новое расхождение тест не
пропустит, а исчезновение закреплённого — заставит обновить список и README.

Про диалект. SQLite-проверки выше — не «дешёвый задел»: прод живёт на
Postgres (Supabase), и диалектные расхождения уже стоили инцидента
(truncation в VARCHAR, жизненный цикл соединений пулера). Раньше flow целиком
шёл по SQLite, поэтому в PG-джобе CI эти тесты тоже поднимали SQLite-файлы, а
цепочка миграций на боевом диалекте не проверялась. Ниже тот же flow на
Postgres — каждый тест получает свою одноразовую базу, снимается с сервера.

Смешанная природа здесь не украшение, а требование драйвера: синхронного
драйвера Postgres в проекте нет вообще (требования — asyncpg, как в проде),
поэтому рефлексию и правки в PG-базе делаем через run_sync асинхронного
движка. Это же означает «честно»: тест проверяет ровно тот драйвер, которым
работает прод, а не второй, случайно оказавшийся в requirements.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings, sqlalchemy_url
from app.models import Base

ROOT = Path(__file__).resolve().parents[1]

KNOWN_DIVERGENCES = {"modify_default:rounds.referral_nanotons"}


def _alembic_head() -> str:
    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


def _alembic_ok(url: str, *args: str) -> str:
    result = _run_alembic(url, *args)
    assert result.returncode == 0, f"alembic {' '.join(args)} упал:\n{result.stderr[-2000:]}"
    return result.stdout


def _sync_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _async_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _version(url: str) -> str | None:
    with create_engine(url).connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar_one_or_none()


def _table_exists(url: str, table: str) -> bool:
    with create_engine(url).connect() as conn:
        return inspect(conn).has_table(table)


def _shape(url: str) -> dict:
    """Отражённая форма схемы SQLite-базы: таблицы, колонки, типы, nullability, PK,
    уникальные ограничения и индексы. Server defaults сознательно НЕ в
    сравнении — их расхождение разбирает compare_models (см. KNOWN)."""
    engine = create_engine(url)
    shape = _shape_of_connection(inspect(engine), engine.dialect)
    engine.dispose()
    return shape


def _compare_models(url: str) -> set[str]:
    """Расхождения БД и моделей строками 'op:table.column' / 'op:table'.

    compare_metadata отдаёт группы (список списков кортежей), а кортежи
    начинаются с (op, schema, table, ...). Разворачиваем в читаемые ключи,
    чтобы падение показывало расхождение, а не индекс в кортеже.
    """
    engine = create_engine(url)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diffs = [item for group in compare_metadata(context, Base.metadata) for item in group]
    engine.dispose()
    found = set()
    for diff in diffs:
        op, _schema, table, *rest = diff
        key = f"{op}:{table}"
        if rest and isinstance(rest[0], str):
            key = f"{key}.{rest[0]}"
        found.add(key)
    return found


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    """Отдельная пустая база на каждый тест. Настройки приложения тоже
    сюда: alembic берёт URL из settings, а не из alembic.ini."""
    path = tmp_path / "flow.db"
    monkeypatch.setenv("DATABASE_URL", _async_url(path))
    monkeypatch.setattr(settings, "database_url", _async_url(path))
    return path


@pytest.fixture
async def app_db(db_path, monkeypatch):
    """Тот же путь, но и движок приложения — чтобы init_db работал с
    временной базой, а не с общей тестовой."""
    import app.db as db_module

    engine = create_async_engine(_async_url(db_path), connect_args={"timeout": 30})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    yield db_path
    await engine.dispose()


def _chain_to_head(path: Path) -> None:
    _alembic_ok(_async_url(path), "upgrade", "head")


def test_chain_from_empty_matches_models(db_path: Path):
    """Цепочка с нуля строит схему моделей. База, поднятая по README
    вручную, не должна отличаться от продовой create_all-базы."""
    _chain_to_head(db_path)

    assert _version(_sync_url(db_path)) == _alembic_head()
    found = _compare_models(_sync_url(db_path))
    assert found - KNOWN_DIVERGENCES == set(), (
        f"цепочка миграций разошлась с моделями: {sorted(found - KNOWN_DIVERGENCES)}"
    )
    assert KNOWN_DIVERGENCES - found == set(), (
        "закреплённое расхождение исчезло — обнови KNOWN_DIVERGENCES и README, "
        "иначе следующее расхождение приедет незамеченным"
    )


async def test_init_db_fresh_matches_chain_schema(app_db: Path):
    """create_all (путь init_db на свежей базе) и цепочка миграций дают
    одну и ту же форму схемы."""
    from app.db import init_db

    _chain_to_head(app_db)
    chain_shape = _shape(_sync_url(app_db))

    app_db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(app_db) + suffix).unlink(missing_ok=True)

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    assert _shape(_sync_url(app_db)) == chain_shape


def test_repeat_upgrade_is_noop_and_keeps_data(db_path: Path):
    """Повторный upgrade на базе, уже видущейся alembic'ом, ничего не
    переигрывает и не теряет строки."""
    _chain_to_head(db_path)
    before = _shape(_sync_url(db_path))
    with create_engine(_sync_url(db_path)).begin() as conn:
        conn.execute(
            text("insert into watcher_state (key, value) values ('k', 'v')")
        )

    _chain_to_head(db_path)

    assert _version(_sync_url(db_path)) == _alembic_head()
    assert _shape(_sync_url(db_path)) == before
    with create_engine(_sync_url(db_path)).connect() as conn:
        assert conn.execute(text("select value from watcher_state where key='k'")).scalar_one() == "v"


def _read_markers(conn) -> dict[int, tuple]:
    """Маркеры доставки по дням. SQLite отдаёт datetime из raw text() строкой —
    приводим к datetime, иначе сравнение строки с datetime всегда «разное»."""
    out = {}
    for row in conn.execute(
        text(
            "select day_index, announced_at, results_at, awards_at from rounds"
            " order by day_index"
        )
    ):
        out[row.day_index] = tuple(
            datetime.fromisoformat(value) if isinstance(value, str) else value
            for value in (row.announced_at, row.results_at, row.awards_at)
        )
    return out


def test_backfill_marks_history_but_spares_fresh_days(db_path: Path):
    """Бэкфилл маркеров доставки: старые дни помечаются обработанными,
    СВЕЖИЕ остаются с NULL-маркером (их NULL — настоящий краш, живой догон
    обязан их доставить), и выплаты миграция не трогает.

    Сценарий прода: миграции добавили announced_at/results_at/awards_at без
    бэкфилла, у всей истории маркеры NULL, и восстановители тика приняли
    историю за недоставленную (27 постов флудом, двойные очки, пересозданные
    выплаты). Бэкфилл чинит именно это, не подавляя свежий догон.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - timedelta(days=100)
    fresh = now - timedelta(hours=2)

    def _insert(conn) -> None:
        rows = [
            # (day, status, opens_at, voting_ends_at, winner_card)
            (901, "closed", old, old, 0),  # история, закрытый, с победителем
            (902, "closed", old, old, None),  # история, закрытый, без победителя
            (903, "open", old, old, None),  # история, открытый
            (904, "closed", fresh, fresh, 0),  # свежий закрытый: НЕ трогаем
        ]
        for day, status, opens_at, voting_ends_at, winner in rows:
            conn.execute(
                text(
                    "insert into rounds (day_index, status, win_rule, chapter_title,"
                    " chapter_text, opens_at, voting_ends_at, tally_ends_at,"
                    " winner_card, vote_counts_json, pot_nanotons, rake_nanotons,"
                    " weekly_nanotons, referral_nanotons, payouts_finalized, epilogue_text,"
                    " money_mode)"
                    " values (:d, :s, 'majority', 't', 'x', :o, :v, :v, :w, '{}',"
                    " 0, 0, 0, 0, 0, '', 'ton')"
                ),
                {
                    "d": day,
                    "s": status,
                    "o": opens_at,
                    "v": voting_ends_at,
                    "w": winner,
                },
            )
        # Выплаты история уже имеет — миграция обязана их не трогать.
        conn.execute(
            text(
                "insert into payouts (round_id, player_id, kind, amount_nanotons,"
                " status, created_at, dest_address, network, attempts, alerted)"
                " select id, 424242, 'refund', 1, 'pending', :now, '0:dead',"
                " 'testnet', 0, 0 from rounds where day_index = 901"
            ),
            {"now": now},
        )

    # База доводится до предпоследней ревизии (маркеры есть, бэкфилла нет),
    # туда кладются «исторические» строки — как на проде до фикса.
    _alembic_ok(_async_url(db_path), "upgrade", "e5d6a7c8b901")
    with create_engine(_sync_url(db_path)).begin() as conn:
        _insert(conn)
    with create_engine(_sync_url(db_path)).connect() as conn:
        payouts_before = {
            row.day_index: row.payouts_finalized
            for row in conn.execute(text("select day_index, payouts_finalized from rounds"))
        }

    _chain_to_head(db_path)

    with create_engine(_sync_url(db_path)).connect() as conn:
        markers = _read_markers(conn)
        payouts_finalized = {
            row.day_index: row.payouts_finalized
            for row in conn.execute(text("select day_index, payouts_finalized from rounds"))
        }
        payout_count = conn.execute(text("select count(*) from payouts")).scalar_one()

    assert markers[901] == (old, old, old), "закрытый день с победителем помечен"
    assert markers[902] == (old, old, old), (
        "закрытый день без победителя тоже обработан: итоги объявлять было "
        "что (ничья/без победителя), очки начислять нечего — долга нет"
    )
    assert markers[903][0] == old, "открытый исторический день помечен"
    assert markers[903][1] is None, "у открытого дня итогов нет"
    assert markers[904] == (None, None, None), (
        "свежий день остаётся с NULL-маркерами — живой догон обязан его доставить"
    )
    assert payouts_finalized[901] == payouts_before[901], (
        "миграция не трогает payouts_finalized: выплаты — денежная логика, "
        "у них своя идемпотентность, и blanket-true скрыл бы реальные долги"
    )
    assert payout_count == 1, "существующие выплаты не тронуты и не продублированы"

    # Повторный прогон (переигровка миграции на проде) — no-op.
    _alembic_ok(_async_url(db_path), "upgrade", "e5d6a7c8b901")
    _chain_to_head(db_path)
    with create_engine(_sync_url(db_path)).connect() as conn:
        again = _read_markers(conn)
    assert again == markers


def test_downgrade_base_then_upgrade_head(db_path: Path):
    """Вся цепочка откатывается до нуля и поднимается обратно: локально
    можно переиграть миграцию, не заводя мусорную базу руками."""
    _chain_to_head(db_path)
    head_shape = _shape(_sync_url(db_path))

    _alembic_ok(_async_url(db_path), "downgrade", "base")
    with create_engine(_sync_url(db_path)).connect() as conn:
        leftover = [
            table
            for table in inspect(conn).get_table_names()
            if not table.startswith("sqlite_")
        ]
    assert leftover == ["alembic_version"], f"после downgrade base остались таблицы: {leftover}"

    _chain_to_head(db_path)
    assert _shape(_sync_url(db_path)) == head_shape


async def test_init_db_on_migrated_db_keeps_rows(app_db: Path):
    """Рестарт бота на базе, уже приведённой к head, — no-op, а не
    пересборка: строки на месте."""
    from app.db import init_db

    _chain_to_head(app_db)
    with create_engine(_sync_url(app_db)).begin() as conn:
        conn.execute(text("insert into watcher_state (key, value) values ('k', 'v')"))

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    with create_engine(_sync_url(app_db)).connect() as conn:
        assert conn.execute(text("select value from watcher_state where key='k'")).scalar_one() == "v"


async def test_legacy_create_all_db_converges(app_db: Path):
    """Легаси-база create_all-эпохи (схема есть, alembic-истории нет)
    обязана сойтись в таймлайн, а не упасть на 'table already exists'."""
    from app.db import init_db

    async def _create_all() -> None:
        engine = create_async_engine(_async_url(app_db))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
    await _create_all()
    with create_engine(_sync_url(app_db)).begin() as conn:
        conn.execute(text("insert into watcher_state (key, value) values ('k', 'v')"))

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    with create_engine(_sync_url(app_db)).connect() as conn:
        assert conn.execute(text("select value from watcher_state where key='k'")).scalar_one() == "v"
    assert _shape(_sync_url(app_db)) == _shape_of_models()


async def test_legacy_old_geometry_db_converges(app_db: Path):
    """Легаси-база НЕ на текущих моделях, а на геометрии якоря, и без
    истории: ровно тот случай, ради которого писались legacy_convergence и
    хвост. Проверяем, что конвергенция не только не падает, но и делает свою
    работу — create_all не умеет снимать ограничения, это обязан сделать
    3197cf14cbeb."""
    from app.db import init_db

    _alembic_ok(_async_url(app_db), "upgrade", "a7b8c9d0e1f2")
    with create_engine(_sync_url(app_db)).begin() as conn:
        conn.execute(text("insert into watcher_state (key, value) values ('k', 'v')"))
        # Стираем историю — база становится неуправляемой alembic'ом.
        conn.execute(text("drop table alembic_version"))
    assert _leftover_tx_hash_unique(app_db) is not None, "фикстура не воспроизводит легаси-геометрию"

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    assert _leftover_tx_hash_unique(app_db) is None, "единичный unique по tx_hash остался"
    with create_engine(_sync_url(app_db)).connect() as conn:
        assert conn.execute(text("select value from watcher_state where key='k'")).scalar_one() == "v"
        assert "inspiration" not in {
            column["name"] for column in inspect(conn).get_columns("players")
        }
    assert _shape(_sync_url(app_db)) == _shape_of_models()


def _leftover_tx_hash_unique(path: Path) -> tuple[str | None, list[str]] | None:
    """Мёртвый единичный unique по stakes.tx_hash, как его видит рефлексия."""
    engine = create_engine(_sync_url(path))
    inspector = inspect(engine)
    found = None
    for constraint in inspector.get_unique_constraints("stakes"):
        if constraint.get("column_names") == ["tx_hash"]:
            found = (constraint.get("name"), constraint["column_names"])
    engine.dispose()
    return found


async def test_db_left_by_failed_reconcile_heals(app_db: Path):
    """База, которую оставила УПАВШАЯ реконсиляция: create_all уже применился,
    а история встала на якорь и не пошла дальше. Именно так выглядит база
    бота, не смогшего стартовать, — следующий запуск обязан долечиться сам.

    Плюс постусловие из README: после конвергенции оператору обещано чистое
    `alembic check`. Проверяем именно ту команду, которой пользуются CI и
    деплой, а не только внутреннее сравнение с моделями."""
    from app.db import init_db

    _alembic_ok(_async_url(app_db), "upgrade", "a7b8c9d0e1f2")
    engine = create_async_engine(_async_url(app_db))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    assert _leftover_tx_hash_unique(app_db) is not None, "фикстура не воспроизводит упавшую реконсиляцию"

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    assert _leftover_tx_hash_unique(app_db) is None, "единичный unique по tx_hash остался"
    assert _shape(_sync_url(app_db)) == _shape_of_models()
    assert "No new upgrade operations detected" in _alembic_ok(
        _async_url(app_db), "check"
    ), "`alembic check` после долечивания не чист — README обещает обратное"


async def test_broken_migration_on_managed_db_stops_boot(app_db: Path, monkeypatch):
    """Сбой миграции на базе с историей alembic останавливает старт.

    Реконсиляция существует для баз create_all-эпохи, у которых истории нет
    вовсе. Раньше её триггером был «upgrade не прошёл», а не «истории нет», то
    есть на базе, управляемой таймлайном, ЛЮБАЯ ошибка миграции приводила к
    create_all по живой базе и stamp на якорь: версия объявлялась та, которую
    никто не проверял. На базе с реальными деньгами и выплатами это молчаливое
    расхождение схемы, единственным следом которого был WARNING в логе.

    Теперь сбой upgrade при непустом alembic_version роняет старт.
    """
    import app.db as db_module
    from app.db import init_db

    _chain_to_head(app_db)
    assert _version(_sync_url(app_db)) is not None, "фикстура обязана дать базу с историей"

    # _run_alembic зовётся через asyncio.to_thread, то есть синхронно.
    def failing(*args, **kwargs):
        return _FakeResult(1, stderr="RuntimeError: сломанная ревизия")

    monkeypatch.setattr(db_module, "_run_alembic", failing)

    with pytest.raises(RuntimeError) as excinfo:
        await init_db()

    assert "таймлайном" in str(excinfo.value)
    # Версия не переписана: база осталась на том, на чём была.
    assert _version(_sync_url(app_db)) == _alembic_head()


async def test_broken_migration_without_history_still_reconciles(app_db: Path, monkeypatch):
    """База без истории alembic по-прежнему лечится реконсиляцией.

    Это тот случай, ради которого путь и написан, и он не должен пострадать от
    ужесточения: create_all-эпоха обязана сойтись в таймлайн сама.
    """
    import app.db as db_module
    from app.db import init_db

    async def _create_all() -> None:
        engine = create_async_engine(_async_url(app_db))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    await _create_all()
    assert not _table_exists(_sync_url(app_db), "alembic_version"), (
        "фикстура обязана дать базу без истории alembic"
    )

    calls: list[tuple[str, ...]] = []
    real_run = db_module._run_alembic

    # Реконсиляция обязана была понадобиться: заставим первый upgrade упасть.
    def failing(*args, **kwargs):
        calls.append(args)
        if args and args[0] == "upgrade" and len(calls) == 1:
            return _FakeResult(1, stderr="RuntimeError: база вне таймлайна")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(db_module, "_run_alembic", failing)

    await init_db()

    assert _version(_sync_url(app_db)) == _alembic_head()
    assert any(c and c[0] == "stamp" for c in calls), calls


class _FakeResult:
    """Минимальный CompletedProcess для подменённого _run_alembic."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


async def test_orm_writes_rounds_without_server_default(db_path: Path):
    """Причина, по которой расхождение по referral_nanotons безобидно:
    ORM подставляет значение сам, поэтому цепочка-база и create_all-база
    принимают одинаковый INSERT. Если значение пришлось бы задавать
    вручную, расхождение стало бы боевым."""
    from datetime import datetime

    from app.models import Round

    _chain_to_head(db_path)
    engine = create_async_engine(_async_url(db_path))
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        row = Round(
            day_index=4242,
            status="open",
            win_rule="none",
            chapter_title="t",
            chapter_text="b",
            opens_at=datetime(2026, 1, 1),
            voting_ends_at=datetime(2026, 1, 2),
            tally_ends_at=datetime(2026, 1, 3),
            pot_nanotons=0,
            rake_nanotons=0,
            payouts_finalized=False,
            epilogue_text="e",
            weekly_nanotons=0,
            money_mode=True,
        )
        session.add(row)
        await session.commit()
    await engine.dispose()

    with create_engine(_sync_url(db_path)).connect() as conn:
        stored = conn.execute(
            text("select referral_nanotons from rounds where day_index=4242")
        ).scalar_one()
    assert stored == 0


def test_single_head_and_reachable_revisions():
    """Один head, один base, все ревизии достижимы по down_revision. Расщепление
    истории означает, что `upgrade head` на разных базах приводит к разной
    схеме — а это ровно то, чего flow не должен допускать."""
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"в истории несколько head'ов: {heads}"
    assert len(script.get_bases()) == 1, f"у истории несколько base'ов: {script.get_bases()}"

    revisions = {revision.revision for revision in script.walk_revisions()}
    chain: set[str] = set()
    current: str | None = heads[0]
    while current is not None:
        assert current not in chain, f"цикл в истории ревизий: {current}"
        chain.add(current)
        current = script.get_revision(current).down_revision

    unreachable = sorted(revisions - chain)
    assert unreachable == [], f"ревизии не достижимы от head: {unreachable}"


def _shape_of_models() -> dict:
    """Форма схемы, которую даёт create_all — эталон для сравнения."""
    engine: Engine = create_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
    shape = _shape_of_connection(inspect(engine), engine.dialect)
    engine.dispose()
    return shape


# --- PostgreSQL: тот же flow на боевом диалекте ---------------------------
#
# Каждый тест берёт СВОЮ одноразовую базу и роняет её на финише: общая база
# означала бы, что тесты зависят от порядка и от остатков чужих данных — а
# flow как раз проверяет чистый старт. Имя с uuid освобождает параллельный
# прогон (-n) и повторный прогон после падения.


def _postgres_admin_dsn() -> dict | None:
    """Реквизиты подключения к серверу из TEST_POSTGRES_URL.

    Требуется суперпользователь: тест сам создаёт и удаляет базы. Если его нет
    — тесты молча пропускаются, а не падают: без PG их просто некому выполнять.
    """
    url = os.environ.get("TEST_POSTGRES_URL") or os.environ.get("TEST_POSTGRES_ADMIN_URL")
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.hostname or not parts.username:
        return None
    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": parts.username,
        "password": parts.password or "",
    }


def _pg_engine(url: str):
    """Асинхронный движок на asyncpg.

    Схема драйвера берётся из app.config, а не пишется строкой: там же боевой
    runtime нормализует URL, и тест обязан проверять ровно то подключение, чем
    пользуется прод (asyncpg), а не случайный драйвер из requirements.
    """
    return create_async_engine(sqlalchemy_url(url))


@pytest.fixture
async def pg_url() -> AsyncIterator[str]:
    """Одноразовая база на тест: создаётся, отдаётся URL, снимается с сервера."""
    dsn = _postgres_admin_dsn()
    if dsn is None:
        pytest.skip("нужен TEST_POSTGRES_URL с суперпользователем: flow на Postgres")
    name = f"the_way_flow_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(database="postgres", **dsn)
    try:
        await admin.execute(f'create database "{name}"')
    finally:
        await admin.close()
    base = dsn["host"], dsn["port"], dsn["user"], dsn["password"]
    try:
        yield f"postgresql://{base[2]}:{base[3]}@{base[0]}:{base[1]}/{name}"
    finally:
        await _drop_pg_database(name, dsn)


async def _drop_pg_database(name: str, dsn: dict) -> None:
    """Снести базу, оборвав висящие соединения: DROP DATABASE не проходит,
    пока к базе кто-то подключён (pool из прошлого теста, например)."""
    admin = await asyncpg.connect(database="postgres", **dsn)
    try:
        await admin.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            name,
        )
        await admin.execute(f'drop database if exists "{name}"')
    finally:
        await admin.close()


async def _pg_shape(url: str) -> dict:
    """Форма схемы PG-базы: тот же снимок, что _shape, но через asyncpg."""
    engine = _pg_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: _shape_of_connection(inspect(sync_conn), sync_conn.dialect)
            )
    finally:
        await engine.dispose()


def _shape_of_connection(inspector, dialect) -> dict:
    """Отражённая форма схемы для ЛЮБОГО диалекта.

    Вынесено из _shape: одна и та же форма сравнивается и SQLite, и Postgres, а
    различаются только подключение и диалект. Server defaults сознательно не
    в сравнении — их расхождение разбирает compare_models (см. KNOWN).
    """
    shape = {}
    for table in sorted(inspector.get_table_names()):
        if table.startswith("sqlite_") or table == "alembic_version":
            continue
        shape[table] = {
            "columns": {
                column["name"]: (column["type"].compile(dialect=dialect), column["nullable"])
                for column in inspector.get_columns(table)
            },
            "pk": sorted(inspector.get_pk_constraint(table)["constrained_columns"]),
            "unique": sorted(
                tuple(sorted(u["column_names"])) for u in inspector.get_unique_constraints(table)
            ),
            "indexes": sorted((i["name"], tuple(i["column_names"] or [])) for i in inspector.get_indexes(table)),
        }
    return shape


async def _pg_version(url: str) -> str | None:
    engine = _pg_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text("select version_num from alembic_version"))
    finally:
        await engine.dispose()


async def _pg_compare_models(url: str) -> set[str]:
    """Расхождения БД и моделей на PG. Опции те же, что у _compare_models:
    на обоих диалектах мы обязаны ловить одно и то же, иначе «чисто на PG»
    ничего не значит."""
    engine = _pg_engine(url)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: _diffs_of(
                    MigrationContext.configure(
                        sync_conn,
                        opts={"compare_type": True, "compare_server_default": True},
                    )
                )
            )
    finally:
        await engine.dispose()
    return diffs


def _diffs_of(context) -> set[str]:
    diffs = [item for group in compare_metadata(context, Base.metadata) for item in group]
    found = set()
    for diff in diffs:
        op, _schema, table, *rest = diff
        key = f"{op}:{table}"
        if rest and isinstance(rest[0], str):
            key = f"{key}.{rest[0]}"
        found.add(key)
    return found


async def _pg_models_shape_url(admin_dsn: dict) -> str:
    """Одноразовая база с чистым create_all: эталон «как выглядит прод-схема
    без истории миграций». Создаётся моделями, а не цепочкой."""
    name = f"the_way_models_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(database="postgres", **admin_dsn)
    try:
        await admin.execute(f'create database "{name}"')
    finally:
        await admin.close()
    host, port, user, password = (
        admin_dsn["host"], admin_dsn["port"], admin_dsn["user"], admin_dsn["password"]
    )
    url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    engine = _pg_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    return url


@pytest.fixture
async def pg_app_db(pg_url: str, monkeypatch) -> AsyncIterator[str]:
    """То же, что app_db для SQLite, но на Postgres: init_db обязан работать с
    одноразовой PG-базой, а не с общей тестовой. Настройки перенаправлены и
    внутри процесса pytest, и для alembic-subprocess'а — migrations/env.py берёт
    URL из DATABASE_URL, а не из переданного аргумента."""
    import app.db as db_module

    engine = _pg_engine(pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setattr(settings, "database_url", pg_url)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    try:
        yield pg_url
    finally:
        await engine.dispose()


def test_postgres_chain_from_empty_matches_models(pg_url: str):
    """Цепочка с нуля на Postgres даёт схему моделей, и `alembic check` —
    команда из CI и деплоя — на ней чист.

    Это ядро дыры, которую закрываем: джоба test-postgres гоняла drift-check,
    но flow-тесты внутри неё поднимали SQLite, поэтому цепочка на боевом
    диалекте не проверялась вообще."""
    _alembic_ok(pg_url, "upgrade", "head")

    assert _alembic_ok(pg_url, "check").find("No new upgrade operations") >= 0


async def test_postgres_chain_shape_matches_models(pg_url: str):
    """Форма PG-схемы после цепочки совпадает с формой create_all-базы.

    Сравниваются две PG-базы, а не PG с SQLite: типы колонок отражаются
    по-разному, и сравнение диалектов ничего бы не значило."""
    _alembic_ok(pg_url, "upgrade", "head")

    dsn = _postgres_admin_dsn()
    models_url = await _pg_models_shape_url(dsn)
    try:
        chain_shape, models_shape = await _pg_shape(pg_url), await _pg_shape(models_url)
    finally:
        await _drop_pg_database(urlsplit(models_url).path.lstrip("/"), dsn)

    assert chain_shape == models_shape


async def test_postgres_downgrade_base_then_upgrade_head(pg_url: str):
    """Вся цепочка откатывается до нуля и поднимается обратно на Postgres.

    Downgrade на боевом диалекте — отдельный класс отказов: на PG есть
    реальные DROP TABLE с зависимостями, и обрыв посередине оставляет базу,
    из которой нельзя ни подняться, ни откатиться дальше."""
    _alembic_ok(pg_url, "upgrade", "head")
    head_shape = await _pg_shape(pg_url)

    _alembic_ok(pg_url, "downgrade", "base")

    engine = _pg_engine(pg_url)
    try:
        async with engine.connect() as conn:
            leftover = await conn.run_sync(
                lambda sync_conn: [
                    name
                    for name in inspect(sync_conn).get_table_names()
                    if not name.startswith("sqlite_")
                ]
            )
    finally:
        await engine.dispose()
    assert leftover == ["alembic_version"], f"после downgrade base остались таблицы: {leftover}"

    _alembic_ok(pg_url, "upgrade", "head")
    assert await _pg_version(pg_url) == _alembic_head()
    assert await _pg_shape(pg_url) == head_shape


async def test_postgres_repeat_upgrade_is_noop_and_keeps_data(pg_url: str):
    """Повторный upgrade на уже мигрированной PG-базе — no-op, строки на месте."""
    _alembic_ok(pg_url, "upgrade", "head")
    before = await _pg_shape(pg_url)

    engine = _pg_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(text("insert into watcher_state (key, value) values ('k', 'v')"))
    await engine.dispose()

    _alembic_ok(pg_url, "upgrade", "head")

    assert await _pg_version(pg_url) == _alembic_head()
    assert await _pg_shape(pg_url) == before
    engine = _pg_engine(pg_url)
    async with engine.connect() as conn:
        assert await conn.scalar(text("select value from watcher_state where key='k'")) == "v"
    await engine.dispose()


async def test_postgres_legacy_create_all_db_converges(pg_app_db: str):
    """Легаси-база create_all-эпохи на Postgres обязана сойтись в таймлайн.

    Именно этот сценарий боевой: прод вырос из create_all, и его пришлось
    приводить к alembic-истории. Идём через init_db, а не сырым alembic:
    сведение делает реконсиляция рантайма, и именно её надо проверить —
    запуск `alembic upgrade head` по легаси-базе падает на 'table already
    exists' на любом диалекте, это не баг."""
    from app.db import init_db

    engine = _pg_engine(pg_app_db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("insert into watcher_state (key, value) values ('k', 'v')"))
    await engine.dispose()

    await init_db()

    assert await _pg_version(pg_app_db) == _alembic_head()
    assert await _pg_compare_models(pg_app_db) - KNOWN_DIVERGENCES == set()
    engine = _pg_engine(pg_app_db)
    async with engine.connect() as conn:
        assert await conn.scalar(text("select value from watcher_state where key='k'")) == "v"
    await engine.dispose()


async def test_postgres_db_left_by_failed_reconcile_heals(pg_app_db: str):
    """База, которую оставила УПАВШАЯ реконсиляция на Postgres, долечивается.

    Состояние бота, не смогшего стартовать: create_all уже применился, а
    история встала на якорь. Следующий запуск обязан довести базу до head и
    оставить `alembic check` чистым — это постусловие из README."""
    from app.db import init_db

    _alembic_ok(pg_app_db, "upgrade", "a7b8c9d0e1f2")
    engine = _pg_engine(pg_app_db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    assert await _pg_leftover_tx_hash_unique(pg_app_db) is not None, (
        "фикстура не воспроизводит упавшую реконсиляцию на PG"
    )

    await init_db()

    assert await _pg_version(pg_app_db) == _alembic_head()
    assert await _pg_leftover_tx_hash_unique(pg_app_db) is None, (
        "единичный unique по stakes.tx_hash остался на PG"
    )
    assert await _pg_compare_models(pg_app_db) - KNOWN_DIVERGENCES == set()
    assert "No new upgrade operations" in _alembic_ok(
        pg_app_db, "check"
    ), "`alembic check` после долечивания на PG не чист — README обещает обратное"


async def _pg_leftover_tx_hash_unique(url: str) -> tuple[str | None, list[str]] | None:
    """Мёртвый единичный unique по stakes.tx_hash глазами рефлексии PG."""
    engine = _pg_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: _leftover_tx_hash_unique_conn(sync_conn)
            )
    finally:
        await engine.dispose()


def _leftover_tx_hash_unique_conn(conn) -> tuple[str | None, list[str]] | None:
    found = None
    for constraint in inspect(conn).get_unique_constraints("stakes"):
        if constraint.get("column_names") == ["tx_hash"]:
            found = (constraint.get("name"), constraint["column_names"])
    return found


async def test_postgres_orm_writes_rounds_without_server_default(pg_url: str):
    """Расхождение по rounds.referral_nanotons безобидно и НА ПРОДЕ.

    Ревизия добавила колонку с server_default=0, модель объявляет только
    питоновский default. На SQLite это видно как modify_default, на Postgres
    alembic неInteger-сравнение пропускает — то есть на боевом диалекте
    расхождение просто НЕВИДИМО. Поэтому «невидимо» ничего не значит: проверяем
    по-настоящему, что ORM пишет такую строку и на PG."""
    from datetime import datetime

    from app.models import Round

    _alembic_ok(pg_url, "upgrade", "head")
    engine = _pg_engine(pg_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(
            Round(
                day_index=4242,
                status="open",
                win_rule="none",
                chapter_title="t",
                chapter_text="b",
                opens_at=datetime(2026, 1, 1),
                voting_ends_at=datetime(2026, 1, 2),
                tally_ends_at=datetime(2026, 1, 3),
                pot_nanotons=0,
                rake_nanotons=0,
                payouts_finalized=False,
                epilogue_text="e",
                weekly_nanotons=0,
                money_mode=True,
            )
        )
        await session.commit()
    await engine.dispose()

    engine = _pg_engine(pg_url)
    async with engine.connect() as conn:
        stored = await conn.scalar(
            text("select referral_nanotons from rounds where day_index=4242")
        )
    await engine.dispose()
    assert stored == 0
