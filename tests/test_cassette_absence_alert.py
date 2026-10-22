"""Месяц без кассеты — это месяц без сюжета, и об этом должен узнать оператор.

Кассета месяца — весь сюжет месяца. Если её нет (или валидатор её отверг),
движок тихо играет шаблоном: «День N, сцена I/II/III». Месяц сюжета просто
исчезает, без единого сигнала — узнать можно от игроков или вообще никак.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ops
from app.story import bay


def test_missing_months_are_detected(tmp_path) -> None:
    """Месяц без кассеты виден: и текущий, и следующий."""
    empty = tmp_path / "empty"
    empty.mkdir()

    months = bay.missing_cassette_months(date(2026, 11, 5), directory=empty)

    assert months == ["2026-11", "2026-12"]


def test_existing_cassette_covers_the_month(tmp_path) -> None:
    """Есть кассета на месяц — месяц не в списке."""
    library = tmp_path / "lib"
    library.mkdir()
    (library / "nov.json").write_text(
        _cassette_json("2026-11"), encoding="utf-8"
    )
    (library / "dec.json").write_text(
        _cassette_json("2026-12"), encoding="utf-8"
    )

    months = bay.missing_cassette_months(date(2026, 11, 5), directory=library)

    assert months == []


def test_year_boundary_is_handled(tmp_path) -> None:
    """Декабрь → январь: месяц считается через год, а не «месяц 13»."""
    empty = tmp_path / "empty2"
    empty.mkdir()

    months = bay.missing_cassette_months(date(2026, 12, 20), directory=empty)

    assert months == ["2026-12", "2027-01"]


async def test_missing_cassette_raises_alert(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Оператор узнаёт о пропаже месяца сюжета из тревоги, а не от игроков."""
    monkeypatch.setattr(
        "app.story.bay.missing_cassette_months", lambda *a, **k: ["2026-11"]
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    await ops._check_missing_cassette(session, bot)

    assert bot.send_message.await_count == 1
    assert "2026-11" in str(bot.send_message.await_args.args[1])


async def test_no_alert_when_cassette_exists(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кассета есть — тревоги нет."""
    monkeypatch.setattr("app.story.bay.missing_cassette_months", lambda *a, **k: [])
    bot = SimpleNamespace(send_message=AsyncMock())

    await ops._check_missing_cassette(session, bot)

    assert bot.send_message.await_count == 0


def _cassette_json(month: str) -> str:
    """Минимальная валидная кассета месяца (столько дней, сколько в месяце)."""
    import calendar
    import json

    year, month_number = (int(part) for part in month.split("-"))
    count = calendar.monthrange(year, month_number)[1]
    days = [
        {
            "day_index": i,
            "chapter_title": f"Глава {i}",
            "chapter_text": f"Текст {i}",
            "dilemma": f"Дилемма {i}?",
            "cards": [
                {"position": p, "title": f"Сцена {p}", "consequence": "след"}
                for p in range(3)
            ],
        }
        for i in range(1, count + 1)
    ]
    return json.dumps(
        {
            "cassette_id": "nov",
            "month": month,
            "title": "Ноябрь",
            "days": days,
        },
        ensure_ascii=False,
    )
