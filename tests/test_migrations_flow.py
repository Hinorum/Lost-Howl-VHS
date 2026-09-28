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
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
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


def _shape(url: str) -> dict:
    """Отражённая форма схемы: таблицы, колонки, типы, nullability, PK,
    уникальные ограничения и индексы. Server defaults сознательно НЕ в
    сравнении — их расхождение разбирает compare_models (см. KNOWN)."""
    engine = create_engine(url)
    inspector = inspect(engine)
    shape = {}
    for table in sorted(inspector.get_table_names()):
        if table.startswith("sqlite_") or table == "alembic_version":
            continue
        shape[table] = {
            "columns": {
                column["name"]: (column["type"].compile(dialect=engine.dialect), column["nullable"])
                for column in inspector.get_columns(table)
            },
            "pk": sorted(inspector.get_pk_constraint(table)["constrained_columns"]),
            "unique": sorted(
                tuple(sorted(u["column_names"])) for u in inspector.get_unique_constraints(table)
            ),
            "indexes": sorted((i["name"], tuple(i["column_names"] or [])) for i in inspector.get_indexes(table)),
        }
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
    inspector = inspect(engine)
    shape = {}
    for table in sorted(inspector.get_table_names()):
        if table.startswith("sqlite_") or table == "alembic_version":
            continue
        shape[table] = {
            "columns": {
                column["name"]: (column["type"].compile(dialect=engine.dialect), column["nullable"])
                for column in inspector.get_columns(table)
            },
            "pk": sorted(inspector.get_pk_constraint(table)["constrained_columns"]),
            "unique": sorted(
                tuple(sorted(u["column_names"])) for u in inspector.get_unique_constraints(table)
            ),
            "indexes": sorted((i["name"], tuple(i["column_names"] or [])) for i in inspector.get_indexes(table)),
        }
    engine.dispose()
    return shape
