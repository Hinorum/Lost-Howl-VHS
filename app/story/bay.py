"""Проигрыватель кассет (VCR).

Модуль-обработчик между движком и библиотекой кассет. install_bay()
регистрирует провайдера дня в app.rounds.rendering (register_plan_provider):
сперва спрашивает активную кассету (месяц кассеты == текущему календарному
месяцу), есть день — подставляет его главу и три пути поверх честного payload
движка (закон дня rule/rule_entropy неизменно считает движок из энтропии TON)
и возвращает результат; нет — вызывает шаблонный рендер, движок живёт
шаблоном.

Контракт явный: rendering.plan_and_render ищет провайдера в момент вызова,
поэтому lifecycle импортирует функцию по значению без monkeypatch-патчей.
uninstall_bay() снимает регистрацию.

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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.core.registry import (
    STORY_CASSETTE_EDIT_KEY,
    STORY_CASSETTE_NEXT_KEY,
)
from app.story.schema import Cassette, validate_file

logger = logging.getLogger(__name__)

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


def missing_cassette_months(
    today: date | None = None,
    directory: Path | None = None,
) -> list[str]:
    """Месяцы без валидной кассеты: текущий и следующий.

    Кассета месяца — это весь сюжет месяца. Если её нет (или она отвергнута
    валидатором), движок тихо играет шаблоном: «День N, сцена I/II/III» —
    и месяц сюжета просто исчезает, без единого сигнала. Оператор узнаёт
    об этом от игроков («а где сюжет?»), а не из /ops.

    Возвращает список месяцев-ключей («2026-11»), которых не хватает.
    """
    today = today or datetime.now(UTC).date()
    months: list[str] = []
    year, month = today.year, today.month
    for offset in (0, 1):
        if offset:
            month += 1
            if month > 12:
                month = 1
                year += 1
        key = f"{year:04d}-{month:02d}"
        if not any(
            cassette.month == key for cassette in _read_library(directory).values()
        ):
            months.append(key)
    return months


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


async def _anchor_info(session, day_index: int) -> tuple[date, int]:
    """(календарная дата дня движения, день месяца якоря).

    Кассета живёт календарными днями месяца, движок — своей
    последовательностью дней. Дата берётся от ЯКОРЯ ЗАБЕГА (начала сезона):
    день N — это якорь + N−1 дней.

    Именно якорь, а не «сегодня»: после простоя движок догоняет
    пропущенные календарные дни, и «сегодня» указало бы на контент, который
    уже показан (или на тот, что покажут позже) — сюжет поехал бы
    относительно канона. В нормальном ходе якорь и календарь совпадают, так
    что поведение не меняется.

    Без якоря (совсем свежий запуск) — сегодня: тогда день 1 и есть
    сегодняшний день.
    """
    from app.rounds.anchor import get_run_anchor

    try:
        anchor = await get_run_anchor(session)
    except Exception:
        logger.debug("Якорь забега не прочитан — берём сегодняшний день", exc_info=True)
        return datetime.now(UTC).date(), datetime.now(UTC).day
    anchor = anchor or {}
    start_iso = anchor.get("start_iso")
    dom = anchor.get("dom")
    if not isinstance(dom, int) or dom < 1 or dom > 31:
        dom = 1
    if isinstance(start_iso, str) and len(start_iso) >= 10:
        try:
            # Только дата: время суток якоря не влияет на календарь кассеты.
            start = date.fromisoformat(start_iso[:10])
        except ValueError:
            start = None
        if start is not None:
            return start + timedelta(days=max(0, day_index - 1)), dom
    today = datetime.now(UTC).date()
    return today, today.day


def _engine_day_for(anchor_dom: int, cassette_day: int) -> int:
    """Номер дня движка, соответствующий дню кассеты.

    Смещение одно на весь забег: день движения N открывается на дате
    (якорь + N − 1 дней), то есть кассетный день D соответствует дню
    движка D − (якорь − 1).

    Через смещение, а не через поиск раунда по дате: в догоняющих днях на
    одну дату приходится несколько раундов, и поиск по дате брал бы не тот
    (победителя чужого дня — то есть перемотка включалась бы по чужому
    решению).
    """
    return cassette_day - (anchor_dom - 1)


async def _decision_day_winner(session, engine_day: int) -> int | None:
    """Честный победитель движка за день с указанным номером, или None.

    Перемотка решается только закрытым кадром: winner_card дня из базы
    движка. Кассета ничего не выдумывает, а ищет раунд по НОМЕРУ дня
    движка (он уникален) — по дате искать нельзя: в догоняющих днях на
    одну дату приходится несколько раундов, и дата выбрала бы не тот.
    Победителя ещё нет / раунда нет — перемотка не срабатывает
    (fail-open).
    """
    from sqlalchemy import select

    from app.models import Round

    row = await session.scalar(
        select(Round).where(
            Round.day_index == engine_day,
            Round.winner_card.is_not(None),
        )
    )
    return int(row.winner_card) if row is not None else None


async def _prev_echo(session, engine_day: int) -> str:
    """Эхо вчерашнего выбора: канон победившей карты дня N−1.

    При рендере дня N берём вчерашний закрытый кадр движка, а из него — его
    `day_index`, по которому в StoryBeat записан `winning_text`: consequence
    победившей карты. Победителя нет / канона нет / день первый — пусто
    (fail-open).
    """
    if engine_day <= 1:
        return ""
    from sqlalchemy import select

    from app.models import Round, StoryBeat

    row = await session.scalar(
        select(Round).where(
            Round.day_index == engine_day - 1,
            Round.winner_card.is_not(None),
        )
    )
    if row is None:
        return ""
    text = await session.scalar(
        select(StoryBeat.winning_text).where(StoryBeat.day_index == row.day_index)
    )
    return (text or "").strip()


async def day_diary(session, round_row) -> str:
    """Запись дневника (`diary` кассеты) для отдельного поста дневника (17:00 UTC).

    Дневник — ПОВ-контраст, не канон: запись читается из активной кассеты
    месяца по дате и дню переданного (открытого) раунда — память месяца
    локальна. Сбой/нет кассеты/нет поля — пусто: рассылка просто не уходит.
    """
    try:
        opens = getattr(round_row, "opens_at", None)
        item_day = getattr(round_row, "day_index", None)
        if opens is None or not item_day:
            return ""
        from app.rounds.anchor import get_run_anchor

        day = opens.date() if getattr(opens, "tzinfo", None) else opens.replace(tzinfo=UTC).date()
        selected = await get_next_cassette(session)
        cassette = active_cassette(day, selected=selected, directory=_library_dir)
        if cassette is None:
            return ""
        anchor = await get_run_anchor(session)
        dom = (anchor or {}).get("dom")
        road, _ = await _resolution(
            session, cassette, day, dom if isinstance(dom, int) else None
        )
        item = cassette.day_for(day.day, road)
        return (item.diary or "").strip() if item else ""
    except Exception:
        logger.debug("Дневник дня не прочитан", exc_info=True)
        return ""


async def _resolution(
    session, cassette: Cassette, today: date, anchor_dom: int | None = None
) -> tuple[str, list[date]]:
    """Дорога дня и календарные даты решений, по которым она считается.

    Отдельный шаг с чистыми аргументами, чтобы тест мог проверить выбор
    дороги без базы: winners кассета получает от движка, а не сама.

    anchor_dom — день месяца якоря забега: по нему дни кассеты переводятся
    в дни движка (см. _engine_day_for).
    """
    decision_days = {
        fork.at_day - 1 for fork in cassette.switch if fork.at_day <= today.day
    }
    winners: dict[int, int] = {}
    decision_dates: list[date] = []
    for n in sorted(decision_days):
        decision_date = date(today.year, today.month, n)
        engine_day = (
            _engine_day_for(anchor_dom, n) if anchor_dom is not None else None
        )
        winner = (
            await _decision_day_winner(session, engine_day)
            if engine_day is not None
            else None
        )
        if winner is not None:
            winners[n] = winner
            decision_dates.append(decision_date)
    return cassette.road(today.day, winners), decision_dates


async def today_road(
    session, cassette: Cassette, today: date | None = None
) -> tuple[str, list[date]]:
    """Дорога, по которой сегодня идёт кассета (для контекста редактора).

    Читает только честные прошлые решения движка (закрытые раунды по
    номеру дня); сбоя нет — main. Дата по умолчанию — сегодня.
    """
    from app.rounds.anchor import get_run_anchor

    today = today or datetime.now(UTC).date()
    anchor = await get_run_anchor(session)
    dom = (anchor or {}).get("dom")
    return await _resolution(
        session, cassette, today, dom if isinstance(dom, int) else None
    )


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
    трогает. Подставляются только глава и три пути. Перемотки (switch)
    кассеты включаются победителем движка за день до развилки; сбой расчёта
    дороги не роняет кадр — играем главную дорогу (fail-open).
    """
    from app.rounds.rendering import render_template_day

    payload = await render_template_day(
        session, day_index, opens_hint=opens_hint, entropy=entropy
    )
    try:
        selected = await get_next_cassette(session)
    except Exception:
        selected = None
    today, anchor_dom = await _anchor_info(session, day_index)
    cassette = active_cassette(
        today, selected=selected, directory=_library_dir
    )
    if cassette is None:
        return payload
    try:
        road, decision_dates = await _resolution(session, cassette, today, anchor_dom)
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
        echo = await _prev_echo(session, day_index)
        if echo:
            chapter_text = f"{echo}\n\n{chapter_text}"
    except Exception:
        logger.debug("Эхо вчерашнего дня не добавлено", exc_info=True)
    payload["chapter_title"] = day.chapter_title
    payload["chapter_text"] = chapter_text
    payload["dilemma"] = day.dilemma
    payload["cards"] = [card.model_dump() for card in day.cards]
    return payload


def install_bay(directory: Path | None = None) -> bool:
    """Включает проигрыватель кассет (идемпотентно). True = провайдер активен.

    Если каталог библиотеки отсутствует — ничего не регистрируем, движок живёт
    шаблоном (нулевое вмешательство). Регистрация идёт в rendering; lifecycle
    зовёт ту же точку входа и отдельного патча не требует.
    """
    global _patched, _library_dir
    if _patched:
        _read_library(directory)
        return True
    library_dir = directory or default_cassettes_dir()
    if not library_dir.is_dir():
        logger.info(
            "Кассетная библиотека %s не найдена — движок без сюжета", library_dir
        )
        return False
    from app.rounds import rendering as rendering_mod

    _library_dir = library_dir
    rendering_mod.register_plan_provider(_plan_and_render)
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
    """Снимает регистрацию провайдера — движок снова играет шаблон."""
    global _patched
    if not _patched:
        return
    from app.rounds import rendering as rendering_mod

    rendering_mod.unregister_plan_provider(_plan_and_render)
    _patched = False
    logger.info("Проигрыватель кассет выключен — движок играет шаблон")