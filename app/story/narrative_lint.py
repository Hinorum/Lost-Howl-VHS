"""Повествовательный линт кассет: ротация, дубли, штампы.

Ядро одно на двух клиентов: CLI хранителя (`cassette_tool lint`, та же
команда гоняется в CI) и панель `/cassette`. Раньше линт жил только в
`scripts/cassette_tool.py`, и панель показывала лишь schema-предупреждения —
35 редакционных замечаний октябрьской кассеты существовали вне бота
(находка анализа ядра кассет).

Замечания — не гейт: схема чиста, а редакционные накопления (дубли
заголовков дней, штампы, обвал ротации тегов) замечает
только человек. CLI по умолчанию печатает и выходит с 0, `--strict`
валит процесс для редакционного прогона; панель только показывает.

Проверки (промпт §4 редактора):
* ротация — каждая стратегия care/dare/trick сидит в каждой позиции
  не меньше N/12 дней и не держится в позиции три дня подряд;
* дубли — заголовки дней и имена карт внутри дороги не повторяются;
* штампы — ≤1 «как будто/будто» на день, ≤2 «впервые» на месяц.
"""

from __future__ import annotations

import re

from app.story.schema import Cassette, DayModel

_CARD_TAGS = ("care", "dare", "trick")


def _roads(cassette: Cassette) -> list[tuple[str, list[DayModel]]]:
    roads: list[tuple[str, list[DayModel]]] = [("main", list(cassette.days))]
    for fork in cassette.switch:
        roads.append((fork.to, list(fork.days)))
    return roads


def _tag_at(day: DayModel, position: int) -> str | None:
    """Тег карты, стоящей в позиции position, — по полю position.

    Контракт требует лишь МНОЖЕСТВО позиций {0,1,2}: порядок карт в списке
    не зафиксирован. По индексу списка линт считал бы не тот тег — замечания
    о ротации выходили бы ложными (или молчали там, где нужен был).
    """
    for card in day.cards:
        if card.position == position:
            return card.tag
    return None


def _rotation_warnings(cassette: Cassette) -> list[str]:
    """Ротация стратегий (промпт §4) — только для главной дороги: каждая
    стратегия care/dare/trick обязана садиться в каждую позицию (0/1/2)
    не меньше N/12 раз и не держаться одной позиции три дня подряд."""
    days = list(cassette.days)
    if not days:
        return []
    floor = max(1, len(days) // 12)
    warnings: list[str] = []
    for position in range(3):
        for tag in _CARD_TAGS:
            met = [day for day in days if _tag_at(day, position) == tag]
            if len(met) < floor:
                warnings.append(
                    f"ротация (main): стратегия «{tag}» в позиции {position} лишь "
                    f"{len(met)} {_plural_days(len(met))} из {len(days)} (нужно ≥ {floor})"
                )
            streak = 0
            for day in days:
                streak = streak + 1 if _tag_at(day, position) == tag else 0
                if streak >= 3:
                    warnings.append(
                        f"ротация (main): «{tag}» в позиции {position} три дня подряд "
                        f"(день {day.day_index})"
                    )
                    break
    return warnings


def _plural_days(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    return "дня" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "дней"


def _duplicate_warnings(cassette: Cassette) -> list[str]:
    """Дубли внутри дороги: заголовки дней и имена карт не повторяются (промпт §4)."""
    warnings: list[str] = []
    for road, days in _roads(cassette):
        headers: dict[str, int] = {}
        for day in days:
            if day.chapter_title in headers:
                warnings.append(
                    f"дубль заголовка дня ({road}): «{day.chapter_title}» в дни "
                    f"{headers[day.chapter_title]} и {day.day_index}"
                )
            else:
                headers[day.chapter_title] = day.day_index
        titles: dict[str, list[int]] = {}
        for day in days:
            for card in day.cards:
                titles.setdefault(card.title, []).append(day.day_index)
        for title, where in titles.items():
            if len(where) > 1:
                warnings.append(
                    f"дубль имени карты ({road}): «{title}» в дни {where}"
                )
    return warnings


def _style_warnings(cassette: Cassette) -> list[str]:
    """Антипаттерны текста (промпт §4): ≤1 «как будто/будто» на день,
    ≤2 «впервые» на месяц."""
    warnings: list[str] = []
    first_time_total = 0
    first_time_days: list[int] = []
    for road, days in _roads(cassette):
        for day in days:
            fields = [day.chapter_title, day.chapter_text]
            if day.dilemma:
                fields.append(day.dilemma)
            if day.diary:
                fields.append(day.diary)
            for card in day.cards:
                fields.extend((card.title, card.consequence))
            haystack = " ".join(fields)
            as_if = len(re.findall(r"\b(?:как\s+будто|будто)\b", haystack, re.IGNORECASE))
            if as_if > 1:
                warnings.append(
                    f"штамп ({road}) д. {day.day_index}: «как будто/будто» {as_if} раза — "
                    "не больше 1 на день"
                )
            if "впервые" in haystack.lower():
                first_time_total += 1
                first_time_days.append(day.day_index)
    if first_time_total > 2:
        warnings.append(
            f"штамп «впервые» {first_time_total} раза на месяц (дни {first_time_days}) — "
            "не больше 2"
        )
    return warnings


def lint_groups(cassette: Cassette) -> list[tuple[str, list[str]]]:
    """Замечания по разрядам — для панели (/cassette) и человекочитаемых отчётов.

    Порядок разрядов фиксирован (ротация → дубли → штампы): первый
    показанный фрагмент каждого непустого разряда даёт полную картину
    кассеты, не заставляя листать хвост из десятков строк.
    """
    return [
        ("ротация", _rotation_warnings(cassette)),
        ("дубли", _duplicate_warnings(cassette)),
        ("штампы", _style_warnings(cassette)),
    ]


def lint_warnings(cassette: Cassette) -> list[str]:
    """Повествовательный линт кассеты: ротация, дубли, штампы."""
    return [warning for _label, group in lint_groups(cassette) for warning in group]
