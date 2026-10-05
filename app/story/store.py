"""Хранилище кассет сюжета: база — источник правды, каталог на диске — зеркало.

Почему так. Библиотека кассет исторически лежала файлами `*.json` в каталоге
(`STORY_CASSETTES_DIR`, иначе `app/story/cassettes/`), и весь движок читал её
синхронно, прямо из пути. На Render файловая система эфемерна: перезапуск
деплоя стирал все правки хранителя, а сюжет месяца молча откатывался к
репозиторию — прод-инцидент, который в /health не виден никак.

Развести два мира можно было двумя способами: сделать чтение кассет асинхронным
(ломает горячий путь рендера и десятки синхронных тестов) или оставить синхронное
чтение, но перенести источник правды в базу. Выбран второй путь:

* **База** (`story_cassettes`) хранит payload кассеты и слепок перед
  перезаписью (Undo). Она переживает рестарт и репозиторий.
* **Диск** — кэш-зеркало. На старте и раз в минуту кассеты выкладываются из
  базы в каталог атомарно (temp + replace), поэтому движок продолжает читать
  обычные файлы и ничего не знает про базу.
* **Правка** из `/panel` пишет в каталог (как раньше) и следом сохраняет
  результат в базу. Если процесс убьют между этими шагами, максимум теряется
  одна правка, а не библиотека.
* **Отказ базы не валит сюжет**: `bootstrap`/`persist` логируют ошибку и
  оставляют движок на диске (fail-open — как и при битой кассете). При этом
  `/panel` говорит хранителю, что правка ушла только в кэш.

Ограничение, о котором стоит помнить: при нескольких инстансах правка видна
остальным после ближайшей синхронизации (раз в минуту), а не мгновенно.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import StoryCassette
from app.story.bay import default_cassettes_dir

logger = logging.getLogger(__name__)

# Имя временного файла при атомарной записи зеркала.
_TMP_SUFFIX = ".tmp"
# Суффикс слепка, который ставит редактор (совпадает с editor._backup).
_BACKUP_SUFFIX = ".bak"


def enabled() -> bool:
    """Включено ли хранение кассет в базе.

    Выключается одним флагом: локальная разработка может остаться на файлах
    (без БД вообще), а если база по какой-то причине недоступна, выключатель
    не нужен — вызовы и так деградируют в диск с записью в лог.
    """
    return settings.story_cassettes_db


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None) -> str:
    """Отпечаток записи для сравнения зеркала с базой.

    Часовой пояс нормализуется к UTC: SQLite отдаёт naive-время, Postgres —
    aware, и простое сравнение строк начало бы считать зеркало устаревшим
    при каждом такте (файлы переписывались бы впустую).
    """
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SyncResult:
    """Итог синхронизации зеркала с базой."""

    written: int = 0
    removed: int = 0
    failed: bool = False


async def _rows() -> list[tuple[str, str, str | None, datetime | None]]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    StoryCassette.name,
                    StoryCassette.payload,
                    StoryCassette.backup,
                    StoryCassette.updated_at,
                )
            )
        ).all()
    return [(name, payload, backup, updated_at) for name, payload, backup, updated_at in rows]


async def _upsert(name: str, payload: str, backup: str | None) -> None:
    async with SessionLocal() as session:
        row = await session.get(StoryCassette, name)
        if row is None:
            session.add(
                StoryCassette(name=name, payload=payload, backup=backup, updated_at=_now())
            )
        else:
            row.payload = payload
            row.backup = backup
            row.updated_at = _now()
        await session.commit()


async def _seed_from_disk(directory: Path) -> int:
    """Первичный перенос библиотеки с диска в базу (один раз за жизнь базы).

    Берутся только валидные кассеты: битый файл иначе поселился бы в базе
    навсегда и поехал в зеркало каждый такт.
    """
    from app.story.schema import validate_file

    imported = 0
    if not directory.is_dir():
        return 0
    for path in sorted(directory.glob("*.json")):
        result = validate_file(path)
        if result.cassette is None:
            logger.error(
                "Кассета %s не переносится в базу (файл битый): %s",
                path.name,
                "; ".join(result.errors),
            )
            continue
        await _upsert(
            path.name,
            path.read_text(encoding="utf-8"),
            _read_backup(path),
        )
        imported += 1
    if imported:
        logger.info("Перенос кассет в базу: %d файлов", imported)
    return imported


def _read_backup(path: Path) -> str | None:
    bak = path.with_name(path.name + _BACKUP_SUFFIX)
    try:
        return bak.read_text(encoding="utf-8") if bak.is_file() else None
    except OSError:
        return None


async def bootstrap() -> SyncResult:
    """Подготовка хранилища на старте процесса: seed + выкладка зеркала.

    Ошибки не поднимаются: сюжет — не касса. Если база недоступна, движок
    продолжает читать то, что лежит на диске (или шаблон, если кассет нет).
    """
    result = SyncResult()
    if not enabled():
        return result
    directory = default_cassettes_dir()
    try:
        rows = await _rows()
        if not rows:
            await _seed_from_disk(directory)
            rows = await _rows()
        result = await sync_from_db()
    except Exception:
        logger.exception(
            "Хранилище кассет в базе недоступно — движок читает кассеты с диска "
            "(правки из /panel не переживут рестарт, пока база лежит)"
        )
        result.failed = True
    return result


async def sync_from_db() -> SyncResult:
    """Выложить базу в каталог-зеркало: только изменившиеся файлы.

    Файлы, которых больше нет в базе (кассета удалена другим инстансом),
    удаляются из зеркала — иначе /panel вечно показывал бы призраков.
    """
    result = SyncResult()
    if not enabled():
        return result
    rows = await _rows()
    if not rows:
        return result
    directory = default_cassettes_dir()
    known: set[str] = set()
    for name, payload, backup, updated_at in rows:
        known.add(name)
        known.add(name + _BACKUP_SUFFIX)
        path = directory / name
        try:
            current = _stamp(_mtime(path))
            if current != _stamp(updated_at) or not path.is_file():
                _atomic_write(path, payload)
                result.written += 1
            bak = path.with_name(path.name + _BACKUP_SUFFIX)
            if backup is not None:
                if not bak.is_file() or _stamp(_mtime(bak)) != _stamp(updated_at):
                    _atomic_write(bak, backup)
            elif bak.is_file():
                # Слепка в базе нет — вычищаем старый .bak с диска.
                bak.unlink()
                result.removed += 1
        except OSError:
            result.failed = True
            logger.exception("Зеркало кассеты %s не обновилось", name)
    for stale in sorted(directory.glob(f"*{_BACKUP_SUFFIX}")):
        if stale.name not in known and stale.is_file():
            try:
                stale.unlink()
                result.removed += 1
            except OSError:
                result.failed = True
    return result


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


async def persist(file_name: str, directory: Path | None = None) -> bool:
    """Сохранить кассету с диска в базу. True — дошло.

    Вызывается после успешной записи редактора: диск уже изменён, осталось
    перенести изменение в долговечное хранилище. Отказ честно возвращается
    вызывающему — /panel обязан сказать хранителю, что правка осталась в кэше.
    """
    if not enabled():
        return True
    directory = directory or default_cassettes_dir()
    path = directory / file_name
    if not path.is_file():
        logger.error("Кассета %s не найдена на диске — в базу нечего сохранять", file_name)
        return False
    try:
        payload = path.read_text(encoding="utf-8")
        json.loads(payload)  # зеркало базы должно быть читаемым JSON
    except (OSError, ValueError) as exc:
        logger.error("Кассета %s не сохранится в базу: %s", file_name, exc)
        return False
    try:
        await _upsert(file_name, payload, _read_backup(path))
    except Exception:
        logger.exception(
            "Кассета %s записана только в кэш на диске: база недоступна, "
            "правка пропадёт при рестарте",
            file_name,
        )
        return False
    return True


async def persist_backup(file_name: str, directory: Path | None = None) -> bool:
    """Сохранить слепок для Undo: база должна знать, что .bak теперь другое."""
    if not enabled():
        return True
    directory = directory or default_cassettes_dir()
    path = directory / (file_name + _BACKUP_SUFFIX)
    payload = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        async with SessionLocal() as session:
            row = await session.get(StoryCassette, file_name)
            if row is None:
                return False
            row.backup = payload
            row.updated_at = _now()
            await session.commit()
    except Exception:
        logger.exception("Слепок кассеты %s не сохранился в базу", file_name)
        return False
    return True


async def library_snapshot() -> dict[str, str]:
    """Имена и payload'ы кассет из базы (для пульта и проверок)."""
    if not enabled():
        return {}
    return {name: payload for name, payload, _backup, _at in await _rows()}
