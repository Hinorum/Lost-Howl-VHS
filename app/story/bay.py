"""Проигрыватель кассет (VCR).

Модуль-обработчик между движком и библиотекой кассет. install_bay()
подменяет источник дня — _plan_and_render в app.rounds.rendering — обёрткой:
сперва спрашивает активную кассету (месяц кассеты == текущему календарному
месяцу), есть день — подставляет его главу и три пути поверх честного payload
движка (закон дня rule/rule_entropy неизменно считает движок из энтропии TON)
и возвращает результат; нет — вызывает оригинал, движок живёт шаблоном.

Важно: app/rounds/lifecycle.py делает `from .rendering import _plan_and_render`
(импорт по значению), поэтому патчится ОБА атрибута — на модуле rendering и на
модуле lifecycle. uninstall_bay() восстанавливает оригиналы.

Библиотека — файлы *.json в app/story/cassettes/ (или STORY_CASSETTES_DIR).
Кассета играется, только когда её месяц совпал с текущим: день = день месяца;
на стыке месяцев активной кассеты нет — движок молчит шаблоном до появления
кассеты нового месяца. Выбор «следующей» кассеты из библиотеки делает храните
в /panel (ключ STORY_CASSETTE_NEXT_KEY) — он разрешает лишь конфликт, когда в
библиотеке несколько кассет одного месяца.

Перемотки внутри месяца (switch кассеты) включаются по честному победителю
движка: winner_card дня за день до развилки берётся из базы по дате opens_at.
Кассета дорогу не выдумывает, ядро всё знает заранее.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.config import settings
from app.core.registry import (
    STORY_CASSETTE_EDIT_KEY,
    STORY_CASSETTE_NEXT_KEY,
)
from app.story.schema import Cassette, DayModel, validate_file

logger = logging.getLogger(__name__)

_original_rendering = None
_original_lifecycle = None
_patched = False
_library_dir: Path | None = None
_library: dict[str, Cassette] = {}
_mtimes: dict[str, float] = {}


@dataclass
class LibraryEntry:
    """Строка библиотеки для пульта: имя файла, кассета (или ошибки), замечания."""

    file_name: str
    cassette: Cassette | None
    errors: list[str]
    warnings: list[str]


def default_cassettes_dir() -> Path:
    """Библиотека по умолчанию: каталог cassettes/ пакета app/story."""
    if settings.story_cassettes_dir.strip():
        return Path(settings.story_cassettes_dir)
    return Path(__file__).resolve().parent / "cassettes"


def _read_library(directory: Path | None = None) -> dict[str, Cassette]:
    """Кэш валидных кассет библиотеки: перечитываем только изменённые файлы.

    Битые кассеты логируются и в кэш не попадают — движок их просто не видит
    (fail-open: сюжет отключается, игра идёт шаблоном).
    """
    global _library, _mtimes
    directory = directory or default_cassettes_dir()
    fresh: dict[str, Cassette] = {}
    if not directory.is_dir():
        _library = fresh
        return fresh
    for path in sorted(directory.glob("*.json")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _library.get(path.name)
        if cached is not None and _mtimes.get(path.name) == mtime:
            fresh[path.name] = cached
            continue
        result = validate_file(path)
        if result.cassette is not None:
            fresh[path.name] = result.cassette
            _mtimes[path.name] = mtime
            if result.warnings:
                logger.info(
                    "Кассета %s: замечания — %s", path.name, "; ".join(result.warnings)
                )
        else:
            _mtimes.pop(path.name, None)
            logger.error(
                "Кассета %s отвергнута (движок продолжит шаблоном): %s",
                path.name,
                "; ".join(result.errors),
            )
    _library = fresh
    return fresh


def active_cassette(
    today: date,
    selected: str | None = None,
    directory: Path | None = None,
) -> Cassette | None:
    """Кассета текущего месяца из библиотеки, иначе None.

    Совпадение по месяцу (YYYY-MM), не по дню: сверстниц одного месяца может
    быть несколько (пилотная рядом с продакшн-версией) — предпочитаем
    назначенную «следующей» в /panel (по имени файла или cassette_id),
    иначе первую по алфавиту.
    """
    month = today.strftime("%Y-%m")
    candidates = [
        (name, cassette)
        for name, cassette in _read_library(directory).items()
        if cassette.month == month
    ]
    if not candidates:
        return None
    if selected:
        for name, cassette in candidates:
            if name == selected or cassette.cassette_id == selected:
                return cassette
    return candidates[0][1]


def list_cassettes(directory: Path | None = None) -> list[LibraryEntry]:
    """Снимок библиотеки для пульта как есть (без кэша — правки файлов видны).

    Из одного файла делается ровно одна запись: валидная кассета или список
    жёстких ошибок.
    """
    directory = directory or default_cassettes_dir()
    entries: list[LibraryEntry] = []
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        result = validate_file(path)
        entries.append(
            LibraryEntry(
                file_name=path.name,
                cassette=result.cassette,
                errors=result.errors,
                warnings=result.warnings,
            )
        )
    return entries


async def get_next_cassette(session) -> str | None:
    """Имя файла кассеты, назначенной «следующей» в /panel, или None."""
    from app.models import WatcherState

    row = await session.get(WatcherState, STORY_CASSETTE_NEXT_KEY)
    return row.value if row is not None else None


async def _decision_day_winner(session, decision: date) -> int | None:
    """Честный победитель движка за календарный день, или None.

    Перемотка решается только закрытым кадром: winner_card дня (at_day - 1)
    из базы движка. Кассета ничего не выдумывает, а ищет раунд, открытый в
    этот день (opens_at попадает в сутки). Победителя ещё нет / раунда нет —
    перемотка не срабатывает (fail-open).
    """
    from sqlalchemy import select

    from app.models import Round

    start = datetime.combine(decision, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    row = await session.scalar(
        select(Round)
        .where(
            Round.opens_at >= start,
            Round.opens_at < end,
            Round.winner_card.is_not(None),
        )
        .order_by(Round.id.desc())
    )
    return int(row.winner_card) if row is not None else None


async def _prev_echo(session, today: date, day: DayModel) -> str:
    """Эхо вчерашнего выбора (поле `prev` дня): как стая вспомнит канон.

    Отвечает на вопрос «когда следующий день знает о предыдущем»: здесь, при
    рендере дня N — ровно по закрытому кадру дня N−1 (winner_card движка).
    Победителя нет / эха под него нет / день первый — пусто (fail-open).
    """
    if not day.prev or today.day <= 1:
        return ""
    yesterday = today - timedelta(days=1)
    winner = await _decision_day_winner(session, yesterday)
    if winner in day.prev:
        return day.prev[winner].strip()
    return ""


async def day_diary(session, finished) -> str:
    """Запись дневника (`diary` кассеты) для поста итогов; «» при отсутствии.

    Канон дня движок не трогает и не хранит — запись читается из активной
    кассеты месяца по дате и дню закрытого раунда (память месяца локальна).
    Сбой/нет кассеты/нет поля — пусто: итоги дня не зависят от дневника.
    """
    try:
        opens = getattr(finished, "opens_at", None)
        item_day = getattr(finished, "day_index", None)
        if opens is None or not item_day:
            return ""
        day = opens.date() if getattr(opens, "tzinfo", None) else opens.replace(tzinfo=UTC).date()
        selected = await get_next_cassette(session)
        cassette = active_cassette(day, selected=selected, directory=_library_dir)
        if cassette is None:
            return ""
        road, _ = await _resolution(session, cassette, day)
        item = cassette.day_for(day.day, road)
        return (item.diary or "").strip() if item else ""
    except Exception:
        logger.debug("Дневник дня не прочитан", exc_info=True)
        return ""


async def _resolution(
    session, cassette: Cassette, today: date
) -> tuple[str, list[date]]:
    """Дорога дня и календарные даты решений, по которым она считается.

    Отдельный шаг с чистыми аргументами, чтобы тест мог проверить выбор
    дороги без базы: winners кассета получает от движка, а не сама.
    """
    decision_days = {
        fork.at_day - 1 for fork in cassette.switch if fork.at_day <= today.day
    }
    winners: dict[int, int] = {}
    decision_dates: list[date] = []
    for n in sorted(decision_days):
        decision_date = date(today.year, today.month, n)
        winner = await _decision_day_winner(session, decision_date)
        if winner is not None:
            winners[n] = winner
            decision_dates.append(decision_date)
    return cassette.road(today.day, winners), decision_dates


async def today_road(
    session, cassette: Cassette, today: date | None = None
) -> tuple[str, list[date]]:
    """Дорога, по которой сегодня идёт кассета (для контекста редактора).

    Читает только честные прошлые решения движка (закрытые раунды по opens_at);
    сбоя нет — main. Дата по умолчанию — сегодня.
    """
    today = today or datetime.now(UTC).date()
    return await _resolution(session, cassette, today)


async def set_next_cassette(session, file_name: str | None) -> None:
    """Назначает/снимает «следующую» кассету (file_name=None — снять выбор)."""
    from app.models import WatcherState

    row = await session.get(WatcherState, STORY_CASSETTE_NEXT_KEY)
    if file_name is None:
        if row is not None:
            await session.delete(row)
            await session.commit()
        return
    if row is None:
        session.add(WatcherState(key=STORY_CASSETTE_NEXT_KEY, value=file_name))
    else:
        row.value = file_name
    await session.commit()


async def get_edit_intent(session) -> tuple[str | None, str | None]:
    """Намерение правки из /cassette: (имя файла, месяц|день) или (None, None)."""
    from app.models import WatcherState

    row = await session.get(WatcherState, STORY_CASSETTE_EDIT_KEY)
    if row is None:
        return None, None
    file_name, sep, mode = row.value.partition("|")
    if not sep:
        return None, None
    return file_name, mode


async def set_edit_intent(session, file_name: str, mode: str) -> None:
    """Ставит намерение правки кассеты; следующий документ — правок её сценария."""
    from app.models import WatcherState

    row = await session.get(WatcherState, STORY_CASSETTE_EDIT_KEY)
    value = f"{file_name}|{mode}"
    if row is None:
        session.add(WatcherState(key=STORY_CASSETTE_EDIT_KEY, value=value))
    else:
        row.value = value
    await session.commit()


async def clear_edit_intent(session) -> None:
    """Снимает намерение правки (после приёма файла или кнопкой отмены)."""
    from app.models import WatcherState

    row = await session.get(WatcherState, STORY_CASSETTE_EDIT_KEY)
    if row is not None:
        await session.delete(row)
        await session.commit()


async def _plan_and_render(
    session, day_index: int, opens_hint=None, entropy: str | None = None
) -> dict:
    """Обёртка-патч: день кассеты поверх честного payload движка.

    Закон дня (rule/rule_entropy) приходит от оригинала — кассета его не
    трогает. Подставляются только глава и три пути. hook_text кассеты —
    необязательная пометка автора, движок её не персистит: месяцы играются как
    самостоятельные истории, без крючков между кассетами. Перемотки (switch)
    кассеты включаются победителем движка за день до развилки; сбой расчёта
    дороги не роняет кадр — играем главную дорогу (fail-open).
    """
    payload = await _original_rendering(
        session, day_index, opens_hint=opens_hint, entropy=entropy
    )
    try:
        selected = await get_next_cassette(session)
    except Exception:
        selected = None
    today = datetime.now(UTC).date()
    cassette = active_cassette(
        today, selected=selected, directory=_library_dir
    )
    if cassette is None:
        return payload
    try:
        road, decision_dates = await _resolution(session, cassette, today)
        day = cassette.day_for(today.day, road)
    except Exception:
        logger.warning(
            "Дорога кассеты %s не рассчитана — играем главную",
            cassette.cassette_id,
            exc_info=True,
        )
        road = "main"
        decision_dates = []
        day = cassette.day_for(today.day, "main")
    if day is None:
        return payload
    logger.info(
        "День %s (%s) из кассеты %s (%s), дорога %s, решено днями: %s",
        today,
        day_index,
        cassette.cassette_id,
        cassette.month,
        road,
        ", ".join(item.isoformat() for item in decision_dates) or "—",
    )
    chapter_text = day.chapter_text
    try:
        echo = await _prev_echo(session, today, day)
        if echo:
            chapter_text = f"{echo}\n\n{chapter_text}"
    except Exception:
        logger.debug("Эхо вчерашнего дня не добавлено", exc_info=True)
    payload["chapter_title"] = day.chapter_title
    payload["chapter_text"] = chapter_text
    payload["cards"] = [card.model_dump() for card in day.cards]
    return payload


def install_bay(directory: Path | None = None) -> bool:
    """Включает проигрыватель кассет (идемпотентно). True = патч активен.

    Если каталог библиотеки отсутствует — ничего не патчим, движок живёт
    шаблоном (нулевое вмешательство). Патчим оба атрибута _plan_and_render:
    rendering и lifecycle (lifecycle импортировал функцию по значению).
    """
    global _original_rendering, _original_lifecycle, _patched, _library_dir
    if _patched:
        _read_library(directory)
        return True
    library_dir = directory or default_cassettes_dir()
    if not library_dir.is_dir():
        logger.info(
            "Кассетная библиотека %s не найдена — движок без сюжета", library_dir
        )
        return False
    from app.rounds import lifecycle as lifecycle_mod
    from app.rounds import rendering as rendering_mod

    _library_dir = library_dir
    _original_rendering = rendering_mod._plan_and_render
    _original_lifecycle = lifecycle_mod._plan_and_render
    rendering_mod._plan_and_render = _plan_and_render
    lifecycle_mod._plan_and_render = _plan_and_render
    _patched = True
    found = _read_library(directory)
    if found:
        logger.info(
            "Проигрыватель кассет включён: %s", ", ".join(sorted(found))
        )
    else:
        logger.info(
            "Проигрыватель кассет включён, но библиотека пуста или битая — "
            "движок играет шаблон до валидной кассеты"
        )
    return True


def uninstall_bay() -> None:
    """Выключает проигрыватель и возвращает движку оригиналы _plan_and_render."""
    global _original_rendering, _original_lifecycle, _patched
    if not _patched:
        return
    from app.rounds import lifecycle as lifecycle_mod
    from app.rounds import rendering as rendering_mod

    if rendering_mod._plan_and_render is _plan_and_render:
        rendering_mod._plan_and_render = _original_rendering
    if lifecycle_mod._plan_and_render is _plan_and_render:
        lifecycle_mod._plan_and_render = _original_lifecycle
    _original_rendering = None
    _original_lifecycle = None
    _patched = False
    logger.info("Проигрыватель кассет выключен — движок играет шаблон")