"""Гард тестовой БД: тесты не могут вычистить чужую базу.

Модульная фикстура чистит ВСЕ таблицы перед каждым модулем. Раньше тестовая
БД бралась из DATABASE_URL через setdefault — то есть унаследованная
переменная приложения (а она в любой машине с .env указывает на боевую базу)
побеждала, и `pytest` в таком шелле стирал прод.

Теперь база прогона объявляется явно (TEST_DATABASE_URL или временный
SQLite-файл), DATABASE_URL перекрывается принудительно, а очистка
отказывается работать с любой другой базой.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import SessionLocal


def test_database_url_points_to_declared_test_database() -> None:
    """DATABASE_URL в тестах — всегда объявленная тестовая база.

    Переменная приложения в машине разработчика указывает на боевую базу, а
    тесты чистят таблицы. Поэтому переменная перекрывается безусловно, и
    проверяется здесь: страховка, которую никто не проверяет, однажды
    оказывается неработающей.
    """
    url = os.environ.get("DATABASE_URL")
    assert url, "conftest обязан задать DATABASE_URL"
    declared = os.environ.get("TEST_DATABASE_URL", "").strip()
    if declared:
        assert url == declared, "DATABASE_URL должен совпадать с TEST_DATABASE_URL"
    else:
        assert url.startswith("sqlite+aiosqlite:///"), url
        assert "the_ways_handlers_test" in url, url


async def test_guard_allows_declared_database(test_db_guard) -> None:
    """Объявленная тестовая база проходит гард."""
    async with SessionLocal() as session:
        test_db_guard.assert_database(session)


async def test_guard_refuses_foreign_database(test_db_guard, tmp_path) -> None:
    """Чужая база — отказ, а не предупреждение.

    Ошибочно вычищенная чужая база не восстанавливается, поэтому гард
    должен падать, а не продолжать работу.
    """
    other = tmp_path / "other.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{other.as_posix()}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            with pytest.raises(RuntimeError, match="не-тестовую БД"):
                test_db_guard.assert_database(session)
    finally:
        await engine.dispose()


async def test_cleanup_refuses_foreign_database(test_db_guard, tmp_path) -> None:
    """Очистка на чужой базе не стирает ничего.

    Проверяем не только отказ, но и то, что данные целы: гард должен
    срабатывать ДО первого DELETE, а не после.
    """
    other = tmp_path / "other.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{other.as_posix()}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            await session.execute(text("INSERT INTO t (id) VALUES (1)"))
            await session.commit()
        async with maker() as session:
            with pytest.raises(RuntimeError, match="не-тестовую БД"):
                await test_db_guard.truncate_all(session)
        async with maker() as session:
            rows = (await session.execute(text("SELECT count(*) FROM t"))).scalar_one()
            assert rows == 1, "чужая база была вычищена"
    finally:
        await engine.dispose()


def test_temp_database_file_is_unique_per_process(test_db_guard) -> None:
    """Временная база — уникальная на процесс.

    Два одновременных прогона на одной машине (две вкладки IDE, локальный
    прогон и «как в CI») не должны делить одну базу: они чистят таблицы
    друг у друга и ловят чужие ошибки.
    """
    db_path = test_db_guard.db_path
    assert str(os.getpid()) in db_path, db_path
    assert db_path.startswith(str(Path(tempfile.gettempdir()))), db_path
    assert test_db_guard.test_db_url.endswith(db_path), test_db_guard.test_db_url
