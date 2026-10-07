"""Повествовательный линт кассет: ротация, дубли, эхо, штампы.

Ядро одно на двух клиентов: CLI хранителя (`cassette_tool lint`, та же
команда гоняется в CI) и панель `/cassette`. Раньше линт жил только в
`scripts/cassette_tool.py`, и панель показывала лишь schema-предупреждения —
35 редакционных замечаний октябрьской кассеты существовали вне бота
(находка анализа ядра кассет).

Замечания — не гейт: схема чиста, а редакционные накопления (дубли
станций, эхо-пересказ канона, штампы, обвал ротации тегов) замечает
только человек. CLI по умолчанию печатает и выходит с 0, `--strict`
валит процесс для редакционного прогона; панель только показывает.

Проверки (промпт §4 редактора):
* ротация — каждая стратегия care/dare/trick сидит в каждой позиции
  не меньше N/12 дней и не держится в позиции три дня подряд;
* дубли — станции и имена карт внутри дороги не повторяются;
* эхо — `prev` докладывает, ЧТО изменилось, а не пересказывает канон;
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
            met = [day for day in days if day.cards[position].tag == tag]
            if len(met) < floor:
                warnings.append(
                    f"ротация (main): стратегия «{tag}» в позиции {position} лишь "
                    f"{len(met)} {_plural_days(len(met))} из {len(days)} (нужно ≥ {floor})"
                )
            streak = 0
            for day in days:
                streak = streak + 1 if day.cards[position].tag == tag else 0
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
    """Дубли внутри дороги: станции и имена карт не повторяются (промпт §4)."""
    warnings: list[str] = []
    for road, days in _roads(cassette):
        stations: dict[str, int] = {}
        for day in days:
            if day.station in stations:
                warnings.append(
                    f"дубль станции ({road}): «{day.station}» в дни "
                    f"{stations[day.station]} и {day.day_index}"
                )
            else:
                stations[day.station] = day.day_index
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


def _echo_retells(echo: str, consequence: str) -> bool:
    """Эхо (prev) — последствие, а не пересказ канона: редакционный признак."""
    left = " ".join(echo.lower().split())
    right = " ".join(consequence.lower().split())
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    words_l = set(left.split())
    words_r = set(right.split())
    if len(words_l) >= 4 and words_l & words_r and len(words_l & words_r) / len(words_l) > 0.8:
        return True
    return False


def _consequence_for(day: DayModel, position: int) -> str | None:
    for card in day.cards:
        if card.position == position:
            return card.consequence
    return None


def _echo_warnings(cassette: Cassette) -> list[str]:
    """prev докладывает, ЧТО изменилось после выбора, а не повторяет его текст."""
    warnings: list[str] = []
    main = list(cassette.days)
    by_road = {"main": main}
    for fork in cassette.switch:
        by_road[fork.to] = list(fork.days)
    forks = {"main": None, **{fork.to: fork for fork in cassette.switch}}
    for road, days in by_road.items():
        for i, day in enumerate(days):
            if not day.prev:
                continue
            if i == 0 and road == "main":
                continue  # первый день месяца — без эха
            if i >= 1:
                yester = days[i - 1]
            else:
                fork = forks[road]
                idx = fork.at_day - 2
                if not 0 <= idx < len(main):
                    continue
                yester = main[idx]
            for position, echo in day.prev.items():
                consequence = _consequence_for(yester, position)
                if consequence and _echo_retells(echo, consequence):
                    preview = echo[:60] + ("…" if len(echo) > 60 else "")
                    warnings.append(
                        f"эхо {road} д. {day.day_index} пересказывает канон карты "
                        f"{position} д. {yester.day_index}: «{preview}»"
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
            fields = [day.chapter_title, day.chapter_text, day.station]
            if day.diary:
                fields.append(day.diary)
            if day.prev:
                fields.extend(day.prev.values())
            for card in day.cards:
                fields.extend((card.title, card.description, card.consequence))
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

    Порядок разрядов фиксирован (ротация → дубли → эхо → штампы): первый
    показанный фрагмент каждого непустого разряда даёт полную картину
    кассеты, не заставляя листать хвост из десятков строк.
    """
    return [
        ("ротация", _rotation_warnings(cassette)),
        ("дубли", _duplicate_warnings(cassette)),
        ("эхо", _echo_warnings(cassette)),
        ("штампы", _style_warnings(cassette)),
    ]


def lint_warnings(cassette: Cassette) -> list[str]:
    """Повествовательный линт кассеты: ротация, дубли, эхо, штампы."""
    return [warning for _label, group in lint_groups(cassette) for warning in group]
