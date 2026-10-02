"""Перемотки (switch) кассеты: контракт, выбор дороги, проигрыватель.

Ветвление месяца строго «домашнее»: кассета объявляет условия (какой
победитель движка за день до развилки включает какую дорогу), а решает
всё равно движок — проигрыватель читает winner_card закрытых дней по дате
opens_at. Сбой дороги = fail-open: играем главную.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from app.story import bay
from app.story.bay import install_bay, set_next_cassette, uninstall_bay
from app.story.schema import validate_payload


def _day(index: int, chapter: str | None = None) -> dict:
    chapter = chapter or f"Глава {index}"
    return {
        "day_index": index,
        "station": f"Колодец {index}",
        "chapter_title": chapter,
        "chapter_text": "Стая слушает шорох плёнки.",
        "hook_text": None,
        "rule_hint": "any",
        "cards": [
            {
                "position": 0,
                "title": f"Юг {index}",
                "description": "Тёплый след.",
                "consequence": "Стая пошла на юг.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 1,
                "title": f"Запад {index}",
                "description": "Ветер с полей.",
                "consequence": "Стая пошла на запад.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 2,
                "title": f"Крыша {index}",
                "description": "Близко и высоко.",
                "consequence": "Стая осталась на крыше.",
                "tag": "care",
                "image_path": "",
            },
        ],
        "tie_note": None,
    }


def _fork_payload() -> dict:
    """Ноябрь-2026 (30 дней), главная дорога + перемотка «b» с 8-го дня."""
    return {
        "cassette_id": "fork-kasseta",
        "month": "2026-11",
        "title": "Перемотанная плёнка",
        "attribution": "фанатская плёнка по мотивам «Lost Dogs: The Way»",
        "switch": [
            {
                "to": "b",
                "at_day": 8,
                "winner": 1,
                "days": [_day(i, f"Ветка Глава {i}") for i in range(8, 31)],
            }
        ],
        "days": [_day(i) for i in range(1, 31)],
    }


def test_valid_fork_passes() -> None:
    result = validate_payload(_fork_payload())
    assert result.ok, result.errors
    cassette = result.cassette
    assert cassette is not None
    assert cassette.attribution
    assert cassette.switch[0].to == "b"
    assert len(cassette.switch[0].days) == 23  # 8..30


def test_fork_without_wire_ok() -> None:
    payload = _fork_payload()
    payload.pop("switch")
    assert validate_payload(payload).ok


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda p: p["switch"].__setitem__(0, {**p["switch"][0], "winner": 3}),
            "winner должен быть 0, 1 или 2",
        ),
        (
            lambda p: p["switch"][0].__setitem__("at_day", 1),
            "at_day",
        ),
        (
            lambda p: p["switch"][0].__setitem__("at_day", 31),
            "за пределами месяца",
        ),
        (
            lambda p: p["switch"][0]["days"].pop(),
            "дни дороги должны идти 8..30",
        ),
        (
            lambda p: p["switch"].append(
                {"to": "b", "at_day": 12, "winner": 0, "days": [_day(i) for i in range(12, 31)]}
            ),
            "не должны дублироваться",
        ),
    ],
)
def test_bad_forks_rejected(mutate, needle) -> None:
    payload = _fork_payload()
    mutate(payload)
    result = validate_payload(payload)
    assert not result.ok
    assert any(needle in error for error in result.errors), result.errors


def test_too_many_forks_rejected() -> None:
    payload = _fork_payload()
    for n, at in ((60, 10), (70, 12), (80, 14), (90, 16)):
        payload["switch"].append(
            {"to": f"r{n}", "at_day": at, "winner": 0, "days": [_day(i) for i in range(at, 31)]}
        )
    result = validate_payload(payload)
    assert not result.ok
    assert any("перемоток" in error for error in result.errors)


def test_fork_days_must_start_contiguous() -> None:
    payload = _fork_payload()
    payload["switch"][0]["days"] = [_day(i) for i in range(9, 31)]
    result = validate_payload(payload)
    assert not result.ok


def test_road_stays_main_until_trigger() -> None:
    cassette = validate_payload(_fork_payload()).cassette
    assert cassette is not None
    # День 7: перемотки ещё не видно (at_day=8).
    assert cassette.road(7, {7: 1}) == "main"
    # День 8: победитель 7-го кадра не совпал — главная дорога.
    assert cassette.road(8, {7: 2}) == "main"
    # Совпал (карта 1) — перемотка на «b».
    assert cassette.road(8, {7: 1}) == "b"
    # Победитель неизвестен движку — тихо main.
    assert cassette.road(8, {}) == "main"


def test_road_cascades_by_last_trigger() -> None:
    payload = _fork_payload()
    payload["switch"].append(
        {"to": "c", "at_day": 15, "winner": 2, "days": [_day(i, f"Ветка Глава {i}") for i in range(15, 31)]}
    )
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    # Обе перемотки сработали — последняя по дате «c».
    assert cassette.road(16, {7: 1, 14: 2}) == "c"
    # Вторая не сработала — остались на «b».
    assert cassette.road(16, {7: 1, 14: 0}) == "b"
    # Перемотки независимы: вторая ветвит и без первой (решает движок).
    assert cassette.road(16, {7: 0, 14: 2}) == "c"


def test_day_for_picks_road_content() -> None:
    cassette = validate_payload(_fork_payload()).cassette
    assert cassette is not None
    assert cassette.day_for(5, "main").day_index == 5
    assert cassette.day_for(5, "main").chapter_title == "Глава 5"
    # Ветка начинается с 8-го дня.
    assert cassette.day_for(5, "b") is None
    assert cassette.day_for(8, "b").day_index == 8
    assert cassette.day_for(8, "b").chapter_title == "Ветка Глава 8"
    assert cassette.day_for(30, "b").day_index == 30
    # Неизвестная дорога — None.
    assert cassette.day_for(12, "нет-такой") is None


_FIXED_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)


class _FakeDatetime:
    """Подмена app.story.bay.datetime: план дня видит фиксированное «сегодня»."""

    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW


def _write_cassette(directory, payload: dict, file_name: str) -> None:
    (directory / file_name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _bay_off_after_each_test():
    yield
    if bay._patched:
        uninstall_bay()


async def _plan(monkeypatch, session, tmp_path, winners: dict[int, int]):
    """Прогон _plan_and_render с фикс-датой 2026-05-11 и заданными победителями."""
    payload = _fork_payload()
    payload["month"] = "2026-05"
    payload["switch"] = [
        {
            "to": "b",
            "at_day": 8,
            "winner": 1,
            "days": [_day(i, f"Ветка Глава {i}") for i in range(8, 32)],
        }
    ]
    payload["days"] = [_day(i) for i in range(1, 32)]

    async def fake_winner(session_arg, decision: date) -> int | None:
        return winners.get(decision.day)

    monkeypatch.setattr(bay, "datetime", _FakeDatetime)
    monkeypatch.setattr(bay, "_decision_day_winner", fake_winner)
    _write_cassette(tmp_path, payload, "may-fork.json")
    await set_next_cassette(session, "may-fork.json")
    assert install_bay(tmp_path) is True
    try:
        return await bay._plan_and_render(session, 7, entropy="200:feed")
    finally:
        uninstall_bay()


async def test_plan_day_uses_fork_road(session, tmp_path, monkeypatch) -> None:
    payload = await _plan(monkeypatch, session, tmp_path, winners={7: 1})
    # 11 мая, дорога «b»: ветка с 8-го дня → Ветка Глава 11 (карта 1 победила 7-го).
    assert payload["chapter_title"] == "Ветка Глава 11"
    assert payload["rule_entropy"] == "200:feed"
    assert [card["position"] for card in payload["cards"]] == [0, 1, 2]


async def test_plan_day_stays_main_on_other_winner(
    session, tmp_path, monkeypatch
) -> None:
    payload = await _plan(monkeypatch, session, tmp_path, winners={7: 2})
    assert payload["chapter_title"] == "Глава 11"


async def test_plan_day_stays_main_when_winners_unknown(
    session, tmp_path, monkeypatch
) -> None:
    payload = await _plan(monkeypatch, session, tmp_path, winners={})
    assert payload["chapter_title"] == "Глава 11"