"""Хранилище кассет в базе (app/story/store.py): зеркало, гонка, fail-open.

Контракт, который здесь ловится:
- база — источник правды, диск — зеркало: bootstrap принимает каталог и
  выкладывает строки, sync сравнивает СОДЕРЖИМОЕ (не mtime — иначе зеркало
  переписывается каждый такт);
- гонка «деплой против правки»: строка без edited_at принимает файл из
  git, строка с edited_at побеждает деплой и ругается в лог;
- правка из /panel ставит edited_at; битое имя и битый файл не берутся;
- выключенный флаг STORY_CASSETTES_DB — честный no-op без базы.
"""

import logging

import pytest
from sqlalchemy import delete
from test_story_bay import _write_cassette

from app.config import settings
from app.db import SessionLocal
from app.models import StoryCassette
from app.story import store


@pytest.fixture(autouse=True)
async def _clean_story_rows():
    """Строки кассет живут в глобальной БД тестов: чистим вокруг каждого теста."""
    async with SessionLocal() as db:
        await db.execute(delete(StoryCassette))
        await db.commit()
    yield
    async with SessionLocal() as db:
        await db.execute(delete(StoryCassette))
        await db.commit()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Боевой режим хранилища в миниатюре: свой каталог, флаг включён."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    monkeypatch.setattr(settings, "story_cassettes_dir", str(directory))
    monkeypatch.setattr(settings, "story_cassettes_db", True)
    return directory


async def _row(name: str) -> tuple[str, object, str | None] | None:
    """(payload, edited_at, backup) по имени кассеты или None."""
    async with SessionLocal() as session:
        row = await session.get(StoryCassette, name)
        if row is None:
            return None
        return row.payload, row.edited_at, row.backup


async def test_bootstrap_seeds_catalog_and_mirrors_payload(library) -> None:
    """Первый запуск: файл уезжает в базу, зеркало совпадает с ней."""
    name = "store-seed-2099-01.json"
    _write_cassette(library, name, "2099-01", 31, "seed")

    result = await store.bootstrap()

    assert not result.failed
    assert result.written == 1
    payload, edited_at, _backup = await _row(name)
    assert edited_at is None  # провенанс: файл не правили
    assert (library / name).read_text(encoding="utf-8") == payload


async def test_bootstrap_adopts_repo_update_while_not_edited(library) -> None:
    """Контент-фикс из git доезжает в базу, пока строку не правили из /panel."""
    name = "store-repo-2099-02.json"
    _write_cassette(library, name, "2099-02", 28, "v1")
    await store.bootstrap()

    _write_cassette(library, name, "2099-02", 28, "v2")  # деплой с фиксом
    result = await store.bootstrap()

    assert not result.failed
    payload, edited_at, _backup = await _row(name)
    assert '"v2"' in payload
    assert edited_at is None


async def test_keeper_edit_survives_repo_deploy(library, caplog) -> None:
    """Правка хранителя переживает деплой: база побеждает и откатывает зеркало."""
    name = "store-keep-2099-03.json"
    _write_cassette(library, name, "2099-03", 31, "repo")
    await store.bootstrap()
    # Правка из /panel: файл на диске изменён, persist ставит edited_at.
    _write_cassette(library, name, "2099-03", 31, "holder")
    assert await store.persist(name)
    _payload, edited_at, _backup = await _row(name)
    assert edited_at is not None

    # Деплой вернул в каталог версию из репозитория.
    _write_cassette(library, name, "2099-03", 31, "repo")
    with caplog.at_level(logging.WARNING, logger="app.story.store"):
        result = await store.bootstrap()

    assert not result.failed
    payload, _edited, _backup = await _row(name)
    assert '"holder"' in payload, "деплой не имеет права откатывать правку"
    assert (library / name).read_text(encoding="utf-8") == payload, (
        "зеркало обязано откатиться к базе"
    )
    assert any("побеждает база" in record.getMessage() for record in caplog.records), (
        "расхождение с репозиторием обязано быть видно в логе для ручной сводки"
    )


async def test_sync_skips_identical_and_restores_tampered_file(library) -> None:
    """Синхронизация по содержимому: совпадает — не трогаем, портили — возвращаем."""
    name = "store-sync-2099-04.json"
    _write_cassette(library, name, "2099-04", 30, "sync")
    await store.bootstrap()
    path = library / name
    stamp = path.stat().st_mtime_ns

    idle = await store.sync_from_db()
    assert idle.written == 0, "без изменений зеркало не должно переписываться"
    assert path.stat().st_mtime_ns == stamp

    path.write_text('{"битая": "правка"}', encoding="utf-8")
    active = await store.sync_from_db()
    assert active.written == 1
    payload, _edited, _backup = await _row(name)
    assert path.read_text(encoding="utf-8") == payload


async def test_sync_cleans_stale_backup_and_keeps_foreign_json(library) -> None:
    """Сирота-слепок убирается, посторонний *.json (работа git) — нет."""
    name = "store-bak-2099-05.json"
    _write_cassette(library, name, "2099-05", 31, "bak")
    await store.bootstrap()
    stale_bak = library / f"{name}.bak"
    stale_bak.write_text("осиротевший слепок", encoding="utf-8")
    foreign = library / "foreign.json"
    foreign.write_text('{"принес": "git"}', encoding="utf-8")

    result = await store.sync_from_db()

    assert not stale_bak.exists()
    assert result.removed >= 1
    assert foreign.is_file(), "чужой файл каталога — не к зеркалу"
    assert await _row(name) is not None


async def test_persist_marks_edited_and_rejects_unsafe_name(library) -> None:
    """persist ставит edited_at; имя с выходом из каталога не трогает ничего."""
    name = "store-per-2099-06.json"
    _write_cassette(library, name, "2099-06", 30, "before")
    await store.bootstrap()
    _write_cassette(library, name, "2099-06", 30, "after")

    assert await store.persist(name)
    payload, edited_at, _backup = await _row(name)
    assert '"after"' in payload
    assert edited_at is not None

    assert await store.persist("../evil-2099-07.json") is False
    assert await _row("../evil-2099-07.json") is None
    assert not (library.parent / "evil-2099-07.json").exists()


async def test_broken_catalog_file_is_not_adopted(library) -> None:
    """Битый файл не селится в базе: ошибка честно в результате, строки нет."""
    (library / "store-broken-2099-08.json").write_text("{бито", encoding="utf-8")

    result = await store.bootstrap()

    assert result.failed
    assert await _row("store-broken-2099-08.json") is None


async def test_disabled_flag_turns_store_into_noop(library, monkeypatch) -> None:
    """STORY_CASSETTES_DB=false: ни базы, ни зеркала, persist — честный no-op."""
    monkeypatch.setattr(settings, "story_cassettes_db", False)
    name = "store-off-2099-09.json"
    _write_cassette(library, name, "2099-09", 30, "off")

    result = await store.bootstrap()

    assert (result.written, result.removed, result.failed) == (0, 0, False)
    assert await _row(name) is None
    assert await store.persist(name) is True
