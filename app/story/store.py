"""Хранилище кассет сюжета: база — источник правды, каталог на диске — зеркало.

Почему так. Библиотека кассет исторически лежала файлами `*.json` в каталоге
(`STORY_CASSETTES_DIR`, иначе `app/story/cassettes/`), и весь движок читал её
синхронно, прямо из пути. На Render файловая система эфемерна: перезапуск
деплоя стирал все правки хранителя, а сюжет месяца молча откатывался к
репозиторию — прод-инцидент, который в /health не виден никак.

Развести два мира можно было двумя способами: сделать чтение кассет асинхронным
(ломает горячий путь рендера и десятки синхронных тестов) или оставить синхронное
чтение, но перенести источник правды в базу. Выбран второй путь:

* **База** (`story_cassettes`) хранит payload кассеты, слепок перед
  перезаписью (Undo) и отметку `edited_at` — правку из /panel. Она
  переживает рестарт и репозиторий.
* **Диск** — кэш-зеркало. На старте (`bootstrap`) и раз в минуту (джоба
  `story-sync`) кассеты выкладываются из базы в каталог атомарно
  (temp + replace), поэтому движок продолжает читать обычные файлы и ничего
  не знает про базу.
* **Гонка «деплой против правки»** решается по `edited_at`: строка без
  отметки принимает файл с диска — так в базу приезжают новые месяцы и
  контент-фиксы из репозитория; строка с отметкой откатывает диск на себя —
  правка хранителя переживает деплой, а расхождение с репозиторием пишется
  в лог и сводится руками (панелью или правкой строки).
* **Правка** из `/panel` пишет в каталог (как раньше) и следом сохраняет
  результат в базу, ставя `edited_at`. Если процесс убьют между этими
  шагами, максимум теряется одна правка, а не библиотека.
* **Отказ базы не валит сюжет**: `bootstrap`/`persist` логируют ошибку и
  оставляют движок на диске (fail-open — как и при битой кассете). При этом
  `/panel` говорит хранителю, что правка ушла только в кэш.

Чего зеркало не делает: не трогает посторонние `*.json` (их приносит git —
принимает `bootstrap`) и не пишет по непроверенному имени (валидация пути
до любой работы с файлом). Удаляются только осиротевшие слепки `*.bak`.

Ограничение, о котором стоит помнить: правка из /panel попадает в базу сразу,
а на чужой диск приезжает с ближайшей синхронизацией (раз в минуту), а не
мгновенно.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import StoryCassette
from app.story.bay import default_cassettes_dir
from app.story.editor import is_safe_cassette_name

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
    """Момент времени для логов: SQLite отдаёт naive-время, Postgres — aware."""
    if moment is None:
        return "—"
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


class _Row(NamedTuple):
    """Строка story_cassettes, как её видит зеркало."""

    name: str
    payload: str
    backup: str | None
    edited_at: datetime | None


async def _rows() -> list[_Row]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    StoryCassette.name,
                    StoryCassette.payload,
                    StoryCassette.backup,
                    StoryCassette.edited_at,
                )
            )
        ).all()
    return [_Row(*row) for row in rows]


async def _upsert(
    name: str, payload: str, backup: str | None, edited_at: datetime | None
) -> None:
    async with SessionLocal() as session:
        row = await session.get(StoryCassette, name)
        if row is None:
            session.add(
                StoryCassette(
                    name=name,
                    payload=payload,
                    backup=backup,
                    edited_at=edited_at,
                    updated_at=_now(),
                )
            )
        else:
            row.payload = payload
            row.backup = backup
            row.edited_at = edited_at
            row.updated_at = _now()
        await session.commit()


def _read(path: Path) -> str | None:
    """Текст файла или None: нечитаемый файл не должен выглядеть как совпадение."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _same(path: Path, text: str) -> bool:
    """Совпадает ли файл на диске с текстом базы."""
    current = _read(path)
    return current is not None and current == text


def _read_backup(path: Path) -> str | None:
    bak = path.with_name(path.name + _BACKUP_SUFFIX)
    return _read(bak)


async def _adopt_disk(directory: Path) -> SyncResult:
    """Принять файлы каталога в базу: новые и ещё не тронутые хранителем.

    Правила гонки «деплой против правки»:
    * имени нет в базе (новый месяц из репозитория) — берём файл;
    * строка без `edited_at` (файл ни разу не правили из /panel) — берём
      файл: это контент-фикс из репозитория, он и должен приехать;
    * строка с `edited_at` — не трогаем: деплой не имеет права откатить
      правку хранителя, расхождение логируется для ручной сводки.

    Берутся только валидные кассеты: битый файл иначе поселился бы в базе
    навсегда и поехал в зеркало каждый такт.
    """
    from app.story.schema import validate_file

    result = SyncResult()
    if not directory.is_dir():
        return result
    rows = {row.name: row for row in await _rows()}
    for path in sorted(directory.glob("*.json")):
        if not is_safe_cassette_name(path.name):
            logger.error(
                "Кассета %s: подозрительное имя файла — в базу не берётся",
                path.name,
            )
            result.failed = True
            continue
        row = rows.get(path.name)
        if row is not None and row.edited_at is not None:
            disk_text = _read(path) if path.is_file() else None
            if disk_text is not None and disk_text != row.payload:
                logger.warning(
                    "Кассета %s: на диске другая версия, но база хранит правку "
                    "хранителя от %s — побеждает база; сверьте контент вручную",
                    path.name,
                    _stamp(row.edited_at),
                )
            continue
        checked = validate_file(path)
        if checked.cassette is None:
            logger.error(
                "Кассета %s не принимается из каталога (файл битый): %s",
                path.name,
                "; ".join(checked.errors),
            )
            result.failed = True
            continue
        payload = _read(path)
        if payload is None:
            logger.error("Кассета %s не читается с диска", path.name)
            result.failed = True
            continue
        await _upsert(path.name, payload, _read_backup(path), edited_at=None)
        result.written += 1
    if result.written:
        logger.info("Кассеты приняты из каталога: %d", result.written)
    return result


async def bootstrap() -> SyncResult:
    """Подготовка хранилища на старте процесса: приём диска + выкладка зеркала.

    Ошибки не поднимаются: сюжет — не касса. Если база недоступна, движок
    продолжает читать то, что лежит на диске (или шаблон, если кассет нет).
    """
    result = SyncResult()
    if not enabled():
        return result
    directory = default_cassettes_dir()
    try:
        result = await _adopt_disk(directory)
        mirror = await sync_from_db()
        result.written += mirror.written
        result.removed += mirror.removed
        result.failed = result.failed or mirror.failed
    except Exception:
        logger.exception(
            "Хранилище кассет в базе недоступно — движок читает кассеты с диска "
            "(правки из /panel не переживут рестарт, пока база лежит)"
        )
        result.failed = True
    return result


async def sync_from_db() -> SyncResult:
    """Выложить базу в каталог-зеркало: только изменившиеся файлы.

    Сравнение идёт по содержимому, а не по mtime: метка времени файла —
    момент записи на диск, `updated_at` — момент записи в базу, и по
    времени зеркало переписывалось бы каждый такт впустую. Пустая база
    ничего не трогает (файлы — работа git, их примет bootstrap), посторонние
    `*.json` не удаляются; убираются только осиротевшие слепки `*.bak`.
    """
    result = SyncResult()
    if not enabled():
        return result
    rows = await _rows()
    if not rows:
        return result
    directory = default_cassettes_dir()
    known_bak = {row.name + _BACKUP_SUFFIX for row in rows}
    for row in rows:
        if not is_safe_cassette_name(row.name):
            logger.error(
                "Кассета %s: недопустимое имя в базе — зеркало её не трогает",
                row.name,
            )
            result.failed = True
            continue
        path = directory / row.name
        try:
            if not _same(path, row.payload):
                _atomic_write(path, row.payload)
                result.written += 1
            bak = path.with_name(path.name + _BACKUP_SUFFIX)
            if row.backup is not None:
                if not _same(bak, row.backup):
                    _atomic_write(bak, row.backup)
            elif bak.is_file():
                # Слепка в базе нет — вычищаем старый .bak с диска.
                bak.unlink()
                result.removed += 1
        except OSError:
            result.failed = True
            logger.exception("Зеркало кассеты %s не обновилось", row.name)
    for stale in sorted(directory.glob(f"*{_BACKUP_SUFFIX}")):
        if stale.name not in known_bak and stale.is_file():
            try:
                stale.unlink()
                result.removed += 1
            except OSError:
                result.failed = True
    return result


async def persist(file_name: str, directory: Path | None = None) -> bool:
    """Сохранить кассету с диска в базу. True — дошло.

    Вызывается после успешной записи редактора: диск уже изменён, осталось
    перенести изменение в долговечное хранилище. Успех ставит `edited_at` —
    с этого момента строка побеждает контент репозитория при деплое.
    Отказ честно возвращается вызывающему — /panel обязан сказать
    хранителю, что правка осталась в кэше.
    """
    if not enabled():
        return True
    if not is_safe_cassette_name(file_name):
        logger.error("Кассета %s: недопустимое имя — в базу не сохраняем", file_name)
        return False
    directory = directory or default_cassettes_dir()
    path = directory / file_name
    if not path.is_file():
        logger.error("Кассета %s не найдена на диске — в базу нечего сохранять", file_name)
        return False
    payload = _read(path)
    if payload is None:
        logger.error("Кассета %s не читается — в базу не сохраняем", file_name)
        return False
    try:
        json.loads(payload)  # зеркало базы должно быть читаемым JSON
    except ValueError as exc:
        logger.error("Кассета %s не сохранится в базу: %s", file_name, exc)
        return False
    try:
        await _upsert(file_name, payload, _read_backup(path), edited_at=_now())
    except Exception:
        logger.exception(
            "Кассета %s записана только в кэш на диске: база недоступна, "
            "правка пропадёт при рестарте",
            file_name,
        )
        return False
    return True


async def persist_backup(file_name: str, directory: Path | None = None) -> bool:
    """Сохранить слепок `.bak` в базу, не меняя payload и edited_at.

    Ручная кнопка «Снять бэкап» — не правка контента: ей нет причин
    отмечать строку правленной (иначе деплой навсегда перестал бы принимать
    для неё контент-фиксы из репозитория). В базу идёт только колонка
    backup — долговечная копия слепка, переживающая рестарт и деплой.
    """
    if not enabled():
        return True
    if not is_safe_cassette_name(file_name):
        logger.error("Кассета %s: недопустимое имя — слепок не сохраняем", file_name)
        return False
    directory = directory or default_cassettes_dir()
    path = directory / file_name
    backup = _read_backup(path)
    if backup is None:
        logger.error("Слепок кассеты %s не найден — в базу не сохраняем", file_name)
        return False
    try:
        json.loads(backup)  # слепок в зеркале базы тоже обязан быть JSON
    except ValueError as exc:
        logger.error("Слепок кассеты %s не сохранится в базу: %s", file_name, exc)
        return False
    try:
        async with SessionLocal() as session:
            row = await session.get(StoryCassette, file_name)
            if row is None:
                # Строки ещё нет (кассету не правили из панели): поднимаем её
                # целиком, payload берём с диска — как это делает adopt.
                payload = _read(path)
                if payload is None:
                    logger.error("Кассета %s не читается — слепок не сохраняем", file_name)
                    return False
                session.add(
                    StoryCassette(
                        name=file_name,
                        payload=payload,
                        backup=backup,
                        edited_at=None,
                        updated_at=_now(),
                    )
                )
            else:
                row.backup = backup
                row.updated_at = _now()
            await session.commit()
    except Exception:
        logger.exception(
            "Слепок кассеты %s остался только на диске — база недоступна", file_name
        )
        return False
    return True
