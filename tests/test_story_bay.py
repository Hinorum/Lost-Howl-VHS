"""Проигрыватель кассет: библиотека, активация по календарному месяцу, патч.

Кассета играется, только когда её месяц (YYYY-MM) совпал с текущим; день =
день календарного месяца; на стыке месяцев движок молчит шаблоном. install_bay
патчит _plan_and_render в ДВУХ местах (rendering и lifecycle — тот импортировал
функцию по значению) и всегда возвращает оригинал при uninstall.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from app.rounds import lifecycle as lifecycle_mod
from app.rounds import rendering as rendering_mod
from app.story import bay
from app.story import editor as ed
from app.story.bay import (
    active_cassette,
    get_next_cassette,
    install_bay,
    set_next_cassette,
    uninstall_bay,
)


def _day(index: int) -> dict:
    return {
        "day_index": index,
        "station": f"Станция {index}",
        "chapter_title": f"Глава {index}",
        "chapter_text": "Стая собирается у котла и смотрит на рельсы.",
        "hook_text": None,
        "rule_hint": "any",
        "cards": [
            {
                "position": 0,
                "title": f"Путь А {index}",
                "description": "Громкий путь.",
                "consequence": "Стая пошла путём А.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 1,
                "title": f"Путь Б {index}",
                "description": "Тихий путь.",
                "consequence": "Стая ушла путём Б.",
                "tag": "care",
                "image_path": "",
            },
            {
                "position": 2,
                "title": f"Путь В {index}",
                "description": "Середина.",
                "consequence": "Стая осталась.",
                "tag": "care",
                "image_path": "",
            },
        ],
        "tie_note": None,
    }


def _write_cassette(
    directory, file_name: str, month: str, n_days: int, cassette_id: str
) -> None:
    payload = {
        "cassette_id": cassette_id,
        "month": month,
        "title": f"Кассета {cassette_id}",
        "logline": "проверка проигрывателя.",
        "days": [_day(i) for i in range(1, n_days + 1)],
    }
    (directory / file_name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


_FIXED_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)


class _FakeDatetime:
    """Подмена app.story.bay.datetime: план дня видит фиксированное «сегодня»."""

    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW


@pytest.fixture(autouse=True)
def _bay_off_after_each_test():
    """Проигрыватель — глобальное состояние модуля: гасим после каждого теста."""
    yield
    if bay._patched:
        uninstall_bay()


def test_install_patches_both_modules_and_restores(tmp_path) -> None:
    _write_cassette(tmp_path, "may.json", "2026-05", 31, "may-kasseta")
    original_rendering = rendering_mod._plan_and_render
    original_lifecycle = lifecycle_mod._plan_and_render
    assert install_bay(tmp_path) is True
    try:
        assert rendering_mod._plan_and_render is bay._plan_and_render
        assert lifecycle_mod._plan_and_render is bay._plan_and_render
        # Повторная установка не заворачивает обёртку в обёртку.
        assert install_bay(tmp_path) is True
        assert rendering_mod._plan_and_render is bay._plan_and_render
    finally:
        uninstall_bay()
    assert rendering_mod._plan_and_render is original_rendering
    assert lifecycle_mod._plan_and_render is original_lifecycle


def test_install_skips_without_library_dir(tmp_path) -> None:
    missing = tmp_path / "net_takogo"
    assert install_bay(missing) is False
    assert rendering_mod._plan_and_render is not bay._plan_and_render


def test_active_cassette_respects_calendar_month(tmp_path) -> None:
    _write_cassette(tmp_path, "okt.json", "2026-10", 31, "oct-kasseta")
    _write_cassette(tmp_path, "noj.json", "2026-11", 30, "nov-kasseta")
    assert (
        active_cassette(date(2026, 10, 3), directory=tmp_path).cassette_id
        == "oct-kasseta"
    )
    assert (
        active_cassette(date(2026, 11, 15), directory=tmp_path).cassette_id
        == "nov-kasseta"
    )
    # Сентябрь: кассет на этот месяц нет — шаблон.
    assert active_cassette(date(2026, 9, 1), directory=tmp_path) is None


def test_selected_resolves_same_month(tmp_path) -> None:
    _write_cassette(tmp_path, "a.json", "2026-10", 31, "kasseta-a")
    _write_cassette(tmp_path, "b.json", "2026-10", 31, "kasseta-b")
    today = date(2026, 10, 3)
    assert active_cassette(today, directory=tmp_path).cassette_id == "kasseta-a"
    assert (
        active_cassette(today, selected="b.json", directory=tmp_path).cassette_id
        == "kasseta-b"
    )
    # Неизвестный выбор не ломает: берём первую по алфавиту.
    assert (
        active_cassette(today, selected="z.json", directory=tmp_path).cassette_id
        == "kasseta-a"
    )


async def test_plan_day_uses_active_cassette(
    session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(bay, "datetime", _FakeDatetime)  # сегодня: 2026-05-11
    _write_cassette(tmp_path, "may.json", "2026-05", 31, "may-kasseta")
    await set_next_cassette(session, "may.json")
    assert install_bay(tmp_path) is True
    try:
        payload = await bay._plan_and_render(session, 7, entropy="100:deadbeef")
        # Контент кассеты поверх честного payload движка:
        assert payload["day_index"] == 7
        assert payload["rule_entropy"] == "100:deadbeef"
        assert payload["chapter_title"] == "Глава 11"  # 11 мая
        assert [card["position"] for card in payload["cards"]] == [0, 1, 2]
        assert payload["cards"][0]["title"] == "Путь А 11"
    finally:
        uninstall_bay()


async def test_plan_day_falls_back_to_engine(
    session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(bay, "datetime", _FakeDatetime)
    # Кассета на июнь, а «сегодня» май: месяц не совпал — движок живёт шаблоном.
    _write_cassette(tmp_path, "jun.json", "2026-06", 30, "jun-kasseta")
    install_bay(tmp_path)
    try:
        payload = await bay._plan_and_render(session, 7)
        assert payload["chapter_title"] == "День 7"
        assert len(payload["cards"]) == 3
    finally:
        uninstall_bay()


async def test_next_cassette_roundtrip(session) -> None:
    assert await get_next_cassette(session) is None
    await set_next_cassette(session, "may.json")
    assert await get_next_cassette(session) == "may.json"
    await set_next_cassette(session, "jun.json")
    assert await get_next_cassette(session) == "jun.json"
    await set_next_cassette(session, None)
    assert await get_next_cassette(session) is None


def test_list_cassettes_reports_broken_files(tmp_path) -> None:
    _write_cassette(tmp_path, "ok.json", "2026-10", 31, "ok-kasseta")
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    by_name = {entry.file_name: entry for entry in bay.list_cassettes(tmp_path)}
    assert by_name["ok.json"].cassette is not None
    assert by_name["ok.json"].errors == []
    assert by_name["bad.json"].cassette is None
    assert by_name["bad.json"].errors


async def test_editor_edit_picked_up_on_next_render(
    session, tmp_path, monkeypatch
) -> None:
    """Правка редактором (apply_cassette_file) видна проигрывателю по mtime."""
    monkeypatch.setattr(bay, "datetime", _FakeDatetime)  # сегодня: 2026-05-11
    _write_cassette(tmp_path, "may.json", "2026-05", 31, "may-kasseta")
    await set_next_cassette(session, "may.json")
    assert install_bay(tmp_path) is True
    try:
        before = await bay._plan_and_render(session, 7, entropy="100:deadbeef")
        assert before["chapter_title"] == "Глава 11"

        cassette = active_cassette(date(2026, 5, 11), directory=tmp_path)
        assert cassette is not None
        edited = ed.scenario_yaml(cassette).replace(
            "Глава 11", "Глава 11 (правка хранителя)"
        )
        ok, lines, _final = ed.apply_cassette_file(
            edited.encode("utf-8"), "may.json", "month", tmp_path
        )
        assert ok, lines

        after = await bay._plan_and_render(session, 7, entropy="100:deadbeef")
        assert after["chapter_title"] == "Глава 11 (правка хранителя)"
    finally:
        uninstall_bay()


async def test_plan_day_prepends_echo_of_yesterday_winner(
    session, tmp_path, monkeypatch
) -> None:
    """Поле `prev`: следующий день «знает» вчерашний выбор по закрытому кадру.

    День 11 (2026-05-11) помнит победителя дня 10: эхо в начале главы, ровно
    тот вариант, что уцелел по движку. Других победителей нет — эха нет.
    """
    monkeypatch.setattr(bay, "datetime", _FakeDatetime)  # сегодня: 2026-05-11

    async def fake_winner(session_arg, decision: date) -> int | None:
        return 1 if decision.day == 10 else None

    monkeypatch.setattr(bay, "_decision_day_winner", fake_winner)

    payload = {
        "cassette_id": "echo-kasseta",
        "month": "2026-05",
        "title": "Эхо",
        "days": [
            {
                **_day(i),
                "prev": {
                    0: "Вчера стая пошла на свет.",
                    1: "Вчера стая пошла на тень.",
                    2: "Вчера стая осталась.",
                },
            }
            for i in range(1, 32)
        ],
    }
    (tmp_path / "may-echo.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    await set_next_cassette(session, "may-echo.json")
    assert install_bay(tmp_path) is True
    try:
        rendered = await bay._plan_and_render(session, 7, entropy="100:deadbeef")
        assert rendered["day_index"] == 7
        assert rendered["rule_entropy"] == "100:deadbeef"
        assert rendered["chapter_text"].startswith("Вчера стая пошла на тень.")
        assert "Стая собирается у котла" in rendered["chapter_text"]
    finally:
        uninstall_bay()


async def test_plan_day_echo_absent_without_yesterday_winner(
    session, tmp_path, monkeypatch
) -> None:
    """Нет закрытого кадра за вчера — глава дня идёт без эха (fail-open)."""
    monkeypatch.setattr(bay, "datetime", _FakeDatetime)

    async def fake_winner(session_arg, decision: date) -> int | None:
        return None

    monkeypatch.setattr(bay, "_decision_day_winner", fake_winner)

    payload = {
        "cassette_id": "echo-kasseta",
        "month": "2026-05",
        "title": "Эхо",
        "days": [
            {**_day(i), "prev": {0: "Вчера стая пошла на свет."}} for i in range(1, 32)
        ],
    }
    (tmp_path / "may-echo.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    await set_next_cassette(session, "may-echo.json")
    assert install_bay(tmp_path) is True
    try:
        rendered = await bay._plan_and_render(session, 7, entropy="100:deadbeef")
        assert rendered["chapter_text"].startswith("Стая собирается у котла")
    finally:
        uninstall_bay()


async def test_day_diary_reads_cassette(session, tmp_path, monkeypatch) -> None:
    """Запись дневника дня читается из активной кассеты для поста итогов."""
    from types import SimpleNamespace

    monkeypatch.setattr(bay, "datetime", _FakeDatetime)  # сегодня: 2026-05-11
    payload = {
        "cassette_id": "diary-kasseta",
        "month": "2026-05",
        "title": "Дневник",
        "days": [{**_day(i), "diary": "Щенок записал: мама почти выздоровела."} for i in range(1, 32)],
    }
    (tmp_path / "may-diary.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    await set_next_cassette(session, "may-diary.json")
    assert install_bay(tmp_path) is True
    try:
        finished = SimpleNamespace(
            day_index=11,
            opens_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        )
        assert (
            await bay.day_diary(session, finished)
            == "Щенок записал: мама почти выздоровела."
        )
        # Нет даты открытия — дневника нет (пусто, без ошибок).
        assert await bay.day_diary(session, SimpleNamespace(day_index=11, opens_at=None)) == ""
    finally:
        uninstall_bay()