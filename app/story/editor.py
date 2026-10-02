"""Сервисы редактирования кассет: общее ядро компилятора/декомпилятора.

Общее для CLI (`scripts/cassette_tool.py`) и панели хранителя (`/cassette`):
единственный гейт — `app/story/schema.py::validate_payload`, остальное здесь
не дублирует контракт, а лишь форматирует и двигает данные.

Декомпиляция (кассета → человеку):
- `scenario_yaml` — весь месяц одним YAML-сценарием;
- `day_yaml` — фрагмент одного дня с меткой дороги (`# дорога: <road>`);
- `day_view_text` — человекочитаемый кадр для чтения в чате.

Компиляция (правки → кассета):
- `scenario_from_text` — YAML-сценарий → ValidationResult (гейт schema);
- `day_from_text` — фрагмент дня → (дорога, DayModel) с проверкой метки;
- `patch_cassette` — мерж одного дня в validated-кассету + общий гейт;
- `write_json` — атомарная запись JSON-эталона (нормализация дефолтами).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.story.schema import (
    Cassette,
    DayModel,
    ValidationResult,
    validate_file,
    validate_payload,
)

_ROAD_HEADER = "# дорога: {road}"


def _safe_dump(payload: dict) -> str:
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


def scenario_yaml(cassette: Cassette) -> str:
    """Полная кассета → YAML-сценарий (порядок ключей = контракту, defaults явные)."""
    return _safe_dump(cassette.model_dump(mode="json"))


def day_yaml(day: DayModel, road: str = "main") -> str:
    """Фрагмент одного дня: YAML дня + заголовок-метка дороги (для patch)."""
    body = _safe_dump(day.model_dump(mode="json"))
    return f"{_ROAD_HEADER.format(road=road)}\n{body}"


def day_view_text(cassette: Cassette, day: int, road: str = "main") -> str:
    """Человекочитаемый кадр одного дня на дороге (чтение в чате/терминале)."""
    item = cassette.day_for(day, road)
    if item is None:
        raise ValueError(
            f"дня {day} нет на дороге {road} (месяц {cassette.month}, "
            f"дней на main: {len(cassette.days)})"
        )
    header = f"=== {cassette.month} · День {item.day_index}"
    if road != "main":
        header += f" · дорога {road}"
    header += f" · закон-метка {item.rule_hint} · {item.station} ==="
    lines = [header, item.chapter_title, "", item.chapter_text, ""]
    for position in (0, 1, 2):
        echo = (item.prev or {}).get(position)
        if echo:
            lines.append(f"(эхо, если вчера победила карта {position}) {echo}")
    for card in sorted(item.cards, key=lambda entry: entry.position):
        lines.append(f"[{card.position}] {card.title}")
        lines.append(f"    Суть: {card.description}")
        lines.append(f"    Канон, если уцелеет: {card.consequence}")
        if card.image_path:
            lines.append(f"    image_path: {card.image_path}")
        lines.append("")
    if item.diary:
        lines.append(f"(дневник стаи) {item.diary}")
    if item.hook_text:
        lines.append(f"(пометка автора) {item.hook_text}")
    if item.tie_note:
        lines.append(f"(оговорка ничьей) {item.tie_note}")
    return "\n".join(lines).rstrip()


def road_from_fragment(text: str) -> tuple[str, str]:
    """Дорога из заголовочного комментария фрагмента + тело без комментариев."""
    head: list[str] = []
    body: list[str] = []
    in_head = True
    for line in text.splitlines():
        if in_head and line.lstrip().startswith("#"):
            head.append(line)
            continue
        in_head = False
        body.append(line)
    road = "main"
    for line in head:
        if "дорога:" in line:
            road = line.split(":", 1)[1].strip()
    return road, "\n".join(body)


def scenario_from_text(text: str) -> ValidationResult:
    """YAML-сценарий → ValidationResult (тот же гейт, что у движка)."""
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ValidationResult(cassette=None, errors=[f"не YAML: {exc}"])
    if not isinstance(payload, dict):
        return ValidationResult(cassette=None, errors=["корень YAML — объект кассеты"])
    return validate_payload(payload)


def _pydantic_lines(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    ]


def day_from_text(text: str) -> tuple[str, DayModel]:
    """Фрагмент дня → (дорога, день). ValueError — фрагмент не принят."""
    road, body_text = road_from_fragment(text)
    try:
        payload = yaml.safe_load(body_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"не YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("фрагмент дня — это объект (dump --day N -o), а не список")
    try:
        return road, DayModel.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("\n".join(f"  - {line}" for line in _pydantic_lines(exc))) from exc


def replace_day(cassette_payload: dict, road: str, new_day: dict) -> bool:
    """Вставляет день в payload кассеты. False — день вне пределов дороги."""
    n = new_day["day_index"]
    if road == "main":
        if not 1 <= n <= len(cassette_payload["days"]):
            return False
        cassette_payload["days"][n - 1] = new_day
        return True
    for fork in cassette_payload["switch"]:
        if fork["to"] != road:
            continue
        offset = n - fork["at_day"]
        if not 0 <= offset < len(fork["days"]):
            return False
        fork["days"][offset] = new_day
        return True
    return False


def patch_cassette(cassette: Cassette, road: str, new_day: DayModel) -> ValidationResult:
    """Мерж одного дня в кассету + полный гейт целого месяца."""
    payload = cassette.model_dump(mode="json")
    if not replace_day(payload, road, new_day.model_dump(mode="json")):
        return ValidationResult(
            cassette=None,
            errors=[f"день {new_day.day_index} вне пределов дороги {road}"],
        )
    return validate_payload(payload)


def write_json(cassette: Cassette, path: Path) -> None:
    """Пишет JSON-эталон кассеты (нормализованный, атомарный)."""
    text = json.dumps(cassette.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _bullet(line: str) -> str:
    return f"  - {line}"


def _warnings(result: ValidationResult) -> list[str]:
    if not result.warnings:
        return []
    lines = ["Замечания:"]
    lines.extend(_bullet(warning) for warning in result.warnings)
    return lines


def _derive_name(cassette: Cassette) -> str:
    """Имя файла новой кассеты: slug из cassette_id + месяц (только имена-дорожки)."""
    slug = "".join(
        ch if ch.isalnum() or ch in "-_" else "-" for ch in cassette.cassette_id.lower()
    ).strip("-")
    if not slug:
        slug = "kasseta"
    return f"{slug}-{cassette.month}.json"


def validate_edit(
    data: bytes,
    file_name: str,
    mode: str,
    directory: Path,
) -> tuple[bool, list[str], str | None, Cassette | None]:
    """Проверка правки файлом без записи.

    mode: month — целый сценарий поверх существующей кассеты; day — фрагмент
    одного дня (дорога и день читаются из самого фрагмента); new — новая
    кассета (имя выводится из cassette_id+месяца, существующие не трогаются).
    Возвращает (ok, строки отчёта, конечное имя файла, кассета результата).
    """
    text = data.decode("utf-8-sig")

    if mode == "new":
        result = scenario_from_text(text)
        if result.cassette is None:
            lines = ["Новая кассета не принята:", *(_bullet(e) for e in result.errors)]
            return False, lines, None, None
        cassette = result.cassette
        final_name = _derive_name(cassette)
        if (directory / final_name).is_file():
            return (
                False,
                [f"Файл {final_name} уже есть — существующую плёнку не перезаписываем.",],
                final_name,
                None,
            )
        lines = [
            f"Новая кассета готова: {cassette.title} · {cassette.month} · "
            f"{len(cassette.days)} дней → {final_name}.",
            *_warnings(result),
        ]
        return True, lines, final_name, cassette

    path = directory / file_name
    if not path.is_file():
        return False, [f"Кассеты {file_name} нет в библиотеке."], file_name, None

    if mode == "month":
        result = scenario_from_text(text)
        if result.cassette is None:
            lines = [f"Сценарий {file_name} не принят:", *(_bullet(e) for e in result.errors)]
            return False, lines, file_name, None
        cassette = result.cassette
        lines = [
            f"Сценарий {file_name} принят: {cassette.title} · {cassette.month} · "
            f"{len(cassette.days)} дней.",
            *_warnings(result),
        ]
        return True, lines, file_name, cassette

    if mode == "day":
        try:
            road, day = day_from_text(text)
        except ValueError as exc:
            return False, ["Фрагмент дня не принят:", f"  - {exc}"], file_name, None
        base = validate_file(path)
        if base.cassette is None:
            lines = [f"Кассета {file_name} сейчас не читается:", *(_bullet(e) for e in base.errors)]
            return False, lines, file_name, None
        result = patch_cassette(base.cassette, road, day)
        if result.cassette is None:
            lines = [
                f"Правка дня {day.day_index} дороги {road} не принята:",
                *(_bullet(e) for e in result.errors),
            ]
            return False, lines, file_name, None
        lines = [
            f"День {day.day_index} дороги {road} готов к записи в {file_name} · "
            f"{day.chapter_title}.",
            *_warnings(result),
        ]
        return True, lines, file_name, result.cassette

    return False, [f"Неизвестный режим правки: {mode!r}."], file_name, None


def _backup(path: Path) -> None:
    """Слепок текущего файла перед перезаписью (для Undo хранителя)."""
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def apply_cassette_file(
    data: bytes,
    file_name: str,
    mode: str,
    directory: Path,
    dry_run: bool = False,
) -> tuple[bool, list[str], str | None]:
    """Правка кассеты внешним файлом (месяц, день или новая кассета).

    Проверка всегда опережает запись: битая/невалидная правка ничего не меняет.
    Apply пишет атомарно и перед записью делает бэкап старого файла.
    dry_run=True — только проверка (`--check`/кнопка «Проверить»).
    Возвращает (ok, строки отчёта, конечное имя файла).
    """
    ok, lines, final_name, cassette = validate_edit(data, file_name, mode, directory)
    if not ok or cassette is None:
        return False, lines, final_name
    if dry_run:
        return True, [*lines, "Проверено — файл не изменён."], final_name
    path = directory / final_name
    if mode != "new":
        _backup(path)
    write_json(cassette, path)
    return True, lines, final_name


def restore_backup(file_name: str, directory: Path) -> tuple[bool, list[str]]:
    """Откат кассеты к сохранённому бэкапу (Undo правки хранителя).

    Бэкап сперва валидируется: битый бэкап не трогает рабочую кассету.
    """
    path = directory / file_name
    bak = directory / (file_name + ".bak")
    if not bak.is_file():
        return False, [f"Бэкапа {file_name}.bak нет — восстанавливать нечего."]
    result = validate_file(bak)
    if result.cassette is None:
        lines = [f"Бэкап {file_name}.bak нечитаем:", *(_bullet(e) for e in result.errors)]
        return False, lines
    shutil.copy2(bak, path)
    return True, [f"Кассета {file_name} восстановлена из {file_name}.bak."]