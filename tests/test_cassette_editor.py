"""Редактор кассет: общее ядро (editor) и панель хранителя (/cassette).

Ядро (`app.story.editor`) — тот же компилятор/декомпилятор, что у CLI:
- месяц целиком разворачивается в YAML и собирается обратно (гейт — schema);
- фрагмент дня несёт метку дороги (`# дорога: …`) и патчится по дороге;
- правка внешним файлом (apply_cassette_file) атомарна: битая не трогает кассету.

Панель (`app/handlers/panel`): вход в «Редактор плёнки», скачивание сценария и
дня, намерение «жду документ» (watcher_state) и установка правки документом.
"""

import calendar
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from aiogram.types import BufferedInputFile

import app.handlers.panel as panel_mod
from app.db import SessionLocal
from app.models import Round, RoundStatus, WinRule
from app.story import editor as ed
from app.story.bay import (
    clear_edit_intent,
    get_edit_intent,
    set_edit_intent,
    set_next_cassette,
    today_road,
)
from app.story.schema import validate_file, validate_payload

HOLDER_ID = 4242


def _days_in_month(month: str) -> int:
    year, m = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, m)[1]


def _mk_cassette(
    days_n: int | None = None, month: str = "2026-02", at_day: int = 10
) -> object:
    """Синтетическая кассета: main-дорога + одна перемотка «morning» с дня at_day."""
    days_n = days_n or _days_in_month(month)
    days = [
        {
            "day_index": i,
            "station": f"станция {i}",
            "chapter_title": f"глава {i}",
            "chapter_text": f"текст {i}",
            "cards": [
                {"position": 0, "title": f"ход {i}a", "description": "описание", "consequence": "канон"},
                {"position": 1, "title": f"ход {i}b", "description": "описание", "consequence": "канон"},
                {"position": 2, "title": f"ход {i}c", "description": "описание", "consequence": "канон"},
            ],
        }
        for i in range(1, days_n + 1)
    ]
    payload = {
        "cassette_id": "mel.json".removesuffix(".json"),
        "month": month,
        "title": "Метель",
        "attribution": "Фанфик по мотивам.",
        "days": days,
        "switch": [
            {
                "to": "morning",
                "at_day": at_day,
                "winner": 0,
                "days": days[at_day - 1 :],
            }
        ],
    }
    result = validate_payload(payload)
    assert result.cassette is not None, result.errors
    return result.cassette


def _write_cassette(directory: Path, name: str = "mel.json") -> Path:
    path = directory / name
    ed.write_json(_mk_cassette(), path)
    return path


# --- ядро: месяц и фрагмент дня -------------------------------------------


def test_scenario_roundtrip_via_editor(tmp_path) -> None:
    """Кассета → YAML → кассета: модель та же, JSON на диске читается движком."""
    source = _write_cassette(tmp_path)
    original = validate_file(source).cassette
    assert original is not None
    result = ed.scenario_from_text(ed.scenario_yaml(original))
    assert result.cassette is not None, result.errors
    out = tmp_path / "compiled.json"
    ed.write_json(result.cassette, out)
    compiled = validate_file(out).cassette
    assert compiled is not None
    assert compiled.model_dump() == original.model_dump()


def test_day_fragment_carries_road_header(tmp_path) -> None:
    """Фрагмент дня несёт метку дороги: patch знает, куда мерить день."""
    cassette = _mk_cassette()
    fork_day = cassette.day_for(14, "morning")
    assert fork_day is not None
    text = ed.day_yaml(fork_day, road="morning")
    assert text.startswith("# дорога: morning")
    road, body = ed.road_from_fragment(text)
    assert road == "morning"
    assert yaml.safe_load(body)["day_index"] == 14
    road_main, body_main = ed.road_from_fragment(ed.day_yaml(fork_day, road="main"))
    assert road_main == "main"


def test_day_fragment_roundtrip(tmp_path) -> None:
    """Фрагмент дня собирается в тот же DayModel; битый — ValueError с локациями."""
    cassette = _mk_cassette()
    main_day = cassette.day_for(2, "main")
    assert main_day is not None
    road, day = ed.day_from_text(ed.day_yaml(main_day, road="main"))
    assert road == "main"
    assert day.model_dump() == main_day.model_dump()
    with pytest.raises(ValueError):
        ed.day_from_text("chapter_title: [\n")  # не YAML


def test_patch_cassette_main_and_fork(tmp_path) -> None:
    """Правка дня встаёт и на main, и на перемотку; вне дороги — отказ."""
    cassette = _mk_cassette()
    new_main = cassette.days[2].model_copy(update={"chapter_title": "правка main"})
    result = ed.patch_cassette(cassette, "main", new_main)
    assert result.cassette is not None, result.errors
    assert result.cassette.days[2].chapter_title == "правка main"
    new_fork = cassette.day_for(20, "morning").model_copy(
        update={"chapter_title": "правка ветки"}
    )
    result = ed.patch_cassette(cassette, "morning", new_fork)
    assert result.cassette is not None, result.errors
    fork = result.cassette.switch[0]
    assert fork.days[10].chapter_title == "правка ветки"
    assert result.cassette.days[19].chapter_title != "правка ветки"


def test_patch_out_of_range_rejected(tmp_path) -> None:
    cassette = _mk_cassette()
    ghost = cassette.days[0].model_copy(update={"day_index": 99})
    result = ed.patch_cassette(cassette, "main", ghost)
    assert result.cassette is None
    assert any("вне пределов" in error for error in result.errors)


# --- ядро: правка внешним файлом (то, что присылает боту/CLI хранитель) ----


def test_apply_month_edits_cassette(tmp_path) -> None:
    source = _write_cassette(tmp_path)
    cassette = validate_file(source).cassette
    text = ed.scenario_yaml(cassette).replace(cassette.title, "Метель зима")
    ok, lines, _final = ed.apply_cassette_file(
        text.encode("utf-8"), "mel.json", "month", tmp_path
    )
    assert ok, lines
    relit = validate_file(source).cassette
    assert relit is not None
    assert relit.title == "Метель зима"
    assert len(relit.days) == 28


def test_apply_day_edits_fork_road(tmp_path) -> None:
    """Фрагмент дня возвращается с меткой дороги: правка — в switch, не в main."""
    source = _write_cassette(tmp_path)
    cassette = validate_file(source).cassette
    assert cassette is not None
    fork_day = cassette.day_for(14, "morning")
    assert fork_day is not None
    fragment = ed.day_yaml(fork_day, road="morning").replace(
        fork_day.chapter_title, "Правка с бота"
    )
    ok, lines, _final = ed.apply_cassette_file(
        fragment.encode("utf-8"), "mel.json", "day", tmp_path
    )
    assert ok, lines
    relit = validate_file(source).cassette
    assert relit is not None
    assert relit.switch[0].days[4].chapter_title == "Правка с бота"
    assert relit.days[13].chapter_title != "Правка с бота"


def test_apply_broken_month_keeps_file(tmp_path) -> None:
    """Битая правка месяца не изменяет кассету (отняли день против календаря)."""
    source = _write_cassette(tmp_path)
    before = source.read_bytes()
    cassette = validate_file(source).cassette
    text = ed.scenario_yaml(cassette)
    payload = yaml.safe_load(text)
    payload["days"].pop()
    ok, lines, _final = ed.apply_cassette_file(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
        "mel.json",
        "month",
        tmp_path,
    )
    assert not ok
    assert "не принят" in lines[0]
    assert source.read_bytes() == before


def test_apply_broken_day_keeps_file(tmp_path) -> None:
    source = _write_cassette(tmp_path)
    before = source.read_bytes()
    ok, lines, _final = ed.apply_cassette_file(
        yaml.safe_dump({"day_index": 3, "chapter_text": "обрывок без карт"}).encode("utf-8"),
        "mel.json",
        "day",
        tmp_path,
    )
    assert not ok
    assert source.read_bytes() == before
    assert any("cards" in line for line in lines), lines


def test_apply_unknown_file_and_mode(tmp_path) -> None:
    ok, lines, _final = ed.apply_cassette_file(b"", "ghost.json", "month", tmp_path)
    assert not ok
    assert "нет в библиотеке" in lines[0]
    _write_cassette(tmp_path)
    ok, lines, _final = ed.apply_cassette_file(b"{}", "mel.json", "nonsense", tmp_path)
    assert not ok
    assert "режим" in lines[0]


def test_apply_dry_run_month_validates_without_write(tmp_path) -> None:
    """Проверка без записи: отчёт «проверено», файл на диске тот же."""
    source = _write_cassette(tmp_path)
    before = source.read_bytes()
    cassette = validate_file(source).cassette
    assert cassette is not None
    text = ed.scenario_yaml(cassette).replace(cassette.title, "Метель зима")
    ok, lines, final = ed.apply_cassette_file(
        text.encode("utf-8"), "mel.json", "month", tmp_path, dry_run=True
    )
    assert ok, lines
    assert final == "mel.json"
    assert "не изменён" in "\n".join(lines)
    assert source.read_bytes() == before
    relit = validate_file(source).cassette
    assert relit is not None
    assert relit.title == "Метель"


def test_apply_dry_run_day_rejects_broken(tmp_path) -> None:
    source = _write_cassette(tmp_path)
    before = source.read_bytes()
    ok, lines, final = ed.apply_cassette_file(
        b"day_index: [", "mel.json", "day", tmp_path, dry_run=True
    )
    assert not ok
    assert source.read_bytes() == before


def test_apply_new_cassette_derives_name(tmp_path) -> None:
    """Хранитель присылает сценарий новой плёнки — файл получает имя дорожкой."""
    text = ed.scenario_yaml(_mk_cassette())
    ok, lines, final = ed.apply_cassette_file(
        text.encode("utf-8"), "<new>", "new", tmp_path
    )
    assert ok, lines
    assert final == "mel-2026-02.json"
    target = tmp_path / final
    assert target.is_file()
    assert validate_file(target).cassette is not None


def test_apply_new_refuses_overwrite(tmp_path) -> None:
    """Новая кассета с именем существующей плёнки — отказ, без записи."""
    (tmp_path / "mel-2026-02.json").touch()
    text = ed.scenario_yaml(_mk_cassette())
    ok, lines, final = ed.apply_cassette_file(
        text.encode("utf-8"), "<new>", "new", tmp_path
    )
    assert not ok
    assert final == "mel-2026-02.json"
    assert "уже есть" in lines[0]


def test_backup_and_restore_after_apply(tmp_path) -> None:
    """Каждый apply оставляет .bak; restore возвращает предыдущий вариант."""
    source = _write_cassette(tmp_path)
    cassette = validate_file(source).cassette
    assert cassette is not None
    text_a = ed.scenario_yaml(cassette).replace(cassette.title, "Версия A")
    ok, _lines, _final = ed.apply_cassette_file(
        text_a.encode("utf-8"), "mel.json", "month", tmp_path
    )
    assert ok
    assert (tmp_path / "mel.json.bak").is_file()
    assert validate_file(source).cassette.title == "Версия A"
    text_b = ed.scenario_yaml(_mk_cassette()).replace("Метель", "Версия B")
    ok, _lines, _final = ed.apply_cassette_file(
        text_b.encode("utf-8"), "mel.json", "month", tmp_path
    )
    assert ok
    assert validate_file(source).cassette.title == "Версия B"
    ok, lines = ed.restore_backup("mel.json", tmp_path)
    assert ok, lines
    assert validate_file(source).cassette.title == "Версия A"  # предыдущий вариант


def test_restore_without_backup_fails(tmp_path) -> None:
    _write_cassette(tmp_path)
    ok, lines = ed.restore_backup("mel.json", tmp_path)
    assert not ok
    assert "Бэкапа" in lines[0]
    assert "mel.json.bak" in lines[0]


def test_restore_broken_backup_rejected(tmp_path) -> None:
    """Битый бэкап не трогает рабочую кассету (валидация перед копией)."""
    source = _write_cassette(tmp_path)
    (tmp_path / "mel.json.bak").write_text("не json", encoding="utf-8")
    ok, lines = ed.restore_backup("mel.json", tmp_path)
    assert not ok
    assert "нечитаем" in lines[0]
    assert validate_file(source).cassette is not None


# --- bay: намерение «жду документ» ---------------------------------------


async def test_edit_intent_roundtrip(session) -> None:
    assert await get_edit_intent(session) == (None, None)
    await set_edit_intent(session, "mel.json", "month")
    assert await get_edit_intent(session) == ("mel.json", "month")
    await set_edit_intent(session, "mel.json", "day")
    assert await get_edit_intent(session) == ("mel.json", "day")
    await clear_edit_intent(session)
    assert await get_edit_intent(session) == (None, None)


async def test_edit_intent_via_global_db(monkeypatch, tmp_path) -> None:
    """Бот ставит/держит намерение в SessionLocal (та же БД, что watcher)."""
    async with SessionLocal() as session:
        await set_edit_intent(session, "mel.json", "month")
        assert await get_edit_intent(session) == ("mel.json", "month")


# --- bay: контекст плёнки (today_road) ------------------------------------


async def test_today_road_main_without_engine_decisions(session) -> None:
    """Без закрытых раундов движка кассета идёт по main (fail-open)."""
    today = datetime.now(UTC).date()
    cassette = _mk_cassette(month=today.strftime("%Y-%m"), at_day=2)
    road, dates = await today_road(session, cassette, today)
    assert road == "main"
    assert dates == []


async def test_today_road_resolves_fork_with_engine_winner(session) -> None:
    """Закрытый раунд движка в день перемотки — кассета сворачивает на ветку.

    Опорная дата фиксирована внутри месяца, а не берётся «сегодня»: форк
    учитывается только когда at_day <= today.day, поэтому на 1-м числе
    месяца реальная дата (at_day не меньше 2) делала проверку
    неисполнимой — прогон падал 1-го числа и зависел от календаря.
    """
    today = date(2026, 6, 15)
    at_day = 10
    cassette = _mk_cassette(month=today.strftime("%Y-%m"), at_day=at_day)
    fork = cassette.switch[0]
    decision = datetime.combine(
        date(today.year, today.month, at_day - 1),
        time(12, 0),
        tzinfo=UTC,
    )
    session.add(
        Round(
            day_index=at_day - 1,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            chapter_title="раунд",
            chapter_text="x",
            opens_at=decision,
            voting_ends_at=decision,
            tally_ends_at=decision,
            winner_card=fork.winner,
        )
    )
    await session.commit()
    road, dates = await today_road(session, cassette, today)
    assert road == "morning"
    assert decision.date() in dates


# --- панель: сцена «Редактор плёнки» --------------------------------------


def _make_message(text: str = "/cassette") -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=HOLDER_ID),
        text=text,
        answer=AsyncMock(),
    )


def _make_callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=HOLDER_ID),
        answer=AsyncMock(),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            answer_document=AsyncMock(),
            chat=SimpleNamespace(type="private"),
        ),
    )


def _enable_library(monkeypatch, tmp_path) -> Path:
    """Правим настройки панели: хранитель и каталог кассет — во временную папку."""
    monkeypatch.setattr(panel_mod.settings, "admin_ids", str(HOLDER_ID))
    monkeypatch.setattr(panel_mod.settings, "story_cassettes_dir", str(tmp_path))
    return _write_cassette(tmp_path)


def _flat(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_scene_opens_editor(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:scene:mel.json")
    assert not callback.answer.called
    await panel_mod.on_cassette_action(callback)
    text = callback.message.edit_text.call_args.args[0]
    assert "ПЛЁНКА" in text
    assert "mel.json" in text
    assert "morning" in text  # дороги из switch
    labels = _flat(callback.message.edit_text.call_args.kwargs["reply_markup"])
    assert "📥 Скачать месяц (.yaml)" in labels
    assert "📤 Вернуть месяц" in labels
    assert "📤 Вернуть день" in labels
    assert "🔙 К библиотеке" in labels
    assert callback.answer.call_args.args[0] == "Готово."


async def test_scene_rejects_unknown_file(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:scene:ghost.json")
    await panel_mod.on_cassette_action(callback)
    assert "нет в библиотеке" in callback.answer.call_args.args[0]


async def test_month_download_sends_scenario(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:month:mel.json")
    await panel_mod.on_cassette_action(callback)
    args, kwargs = callback.message.answer_document.call_args
    assert isinstance(args[0], BufferedInputFile)
    assert args[0].filename == "mel.yaml"
    content = args[0].data.decode("utf-8")
    assert "Метель" in content
    assert "month:" in content


async def test_day_download_sends_fragment(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:day:mel.json:morning:dl:14")
    await panel_mod.on_cassette_action(callback)
    args, kwargs = callback.message.answer_document.call_args
    assert args[0].filename == "mel--day-14-morning.yaml"
    content = args[0].data.decode("utf-8")
    assert content.startswith("# дорога: morning")
    assert "глава 14" in content


async def test_day_view_reads_chapter(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:day:mel.json:main:view:2")
    await panel_mod.on_cassette_action(callback)
    text = callback.message.answer.call_args.args[0]
    assert "День 2" in text
    assert "глава 2" in text
    assert "ход 2a" in text


async def test_day_grid_shows_roads_and_days(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:pick:mel.json:main:dl")
    await panel_mod.on_cassette_action(callback)
    labels = _flat(callback.message.edit_text.call_args.kwargs["reply_markup"])
    assert "• main" in labels
    assert "morning" in labels
    assert "1" in labels and "28" in labels  # все дни месяца


async def test_edit_sets_and_stop_clears_intent(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:edit:mel.json:month")
    await panel_mod.on_cassette_action(callback)
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == ("mel.json", "month")
    labels = _flat(callback.message.edit_text.call_args.kwargs["reply_markup"])
    assert "⏹ Отменить загрузку" in labels
    assert "Жду документ" in callback.message.edit_text.call_args.args[0]

    callback = _make_callback("cassette:stop:mel.json")
    await panel_mod.on_cassette_action(callback)
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == (None, None)


async def test_scene_shows_placed_and_playing(monkeypatch, tmp_path) -> None:
    """Контекст плёнки: назначена следующей, играется, сегодняшний день и дорога."""
    source = _enable_library(monkeypatch, tmp_path)
    today = datetime.now(UTC).date()
    ed.write_json(_mk_cassette(month=today.strftime("%Y-%m"), at_day=2), source)
    async with SessionLocal() as session:
        await set_next_cassette(session, "mel.json")
    callback = _make_callback("cassette:scene:mel.json")
    await panel_mod.on_cassette_action(callback)
    text = callback.message.edit_text.call_args.args[0]
    assert "🟢 назначена «следующей»" in text
    assert "▶ играется" in text
    assert f"день {today.day}" in text
    assert "дорога main" in text  # без решений движка — fail-open


async def test_check_sets_check_intent(monkeypatch, tmp_path) -> None:
    """Кнопка «Проверить день»: намерение с суффиксом -check, без записи."""
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:check:mel.json:day")
    await panel_mod.on_cassette_action(callback)
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == ("mel.json", "day-check")
    text = callback.message.edit_text.call_args.args[0]
    assert "проверю фрагмент дня" in text
    labels = _flat(callback.message.edit_text.call_args.kwargs["reply_markup"])
    assert "⏹ Отменить загрузку" in labels


async def test_document_handler_check_does_not_write(monkeypatch, tmp_path) -> None:
    """Проверка без записи: отчёт ✅, файл кассеты не тронут, бэкапа нет."""
    source = _enable_library(monkeypatch, tmp_path)
    before = source.read_bytes()
    cassette = validate_file(source).cassette
    text = ed.scenario_yaml(cassette).replace(cassette.title, "Метель зима")

    async def _download(file=None, destination=None):
        destination.write(text.encode("utf-8"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-check"),
        reply=AsyncMock(),
    )
    async with SessionLocal() as session:
        await set_edit_intent(session, "mel.json", "month-check")
    await panel_mod.on_cassette_document(message)
    assert message.reply.call_args.args[0].startswith("✅")
    assert "не изменён" in message.reply.call_args.args[0]
    assert source.read_bytes() == before
    assert not (tmp_path / "mel.json.bak").exists()


async def test_new_sets_new_intent(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:new")
    await panel_mod.on_cassette_action(callback)
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == ("<new>", "new")
    text = callback.message.edit_text.call_args.args[0]
    assert "НОВАЯ КАССЕТА" in text


async def test_document_handler_creates_new_cassette(monkeypatch, tmp_path) -> None:
    """Документ новой плёнки: файл по имени дорожки, отчёт ведёт на её сцену."""
    _enable_library(monkeypatch, tmp_path)
    text = ed.scenario_yaml(_mk_cassette())

    async def _download(file=None, destination=None):
        destination.write(text.encode("utf-8"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-new"),
        reply=AsyncMock(),
    )
    async with SessionLocal() as session:
        await set_edit_intent(session, "<new>", "new")
    await panel_mod.on_cassette_document(message)
    assert message.reply.call_args.args[0].startswith("✅")
    labels = _flat(message.reply.call_args.kwargs["reply_markup"])
    assert "🎞 Плёнка" in labels
    assert "🗄 Вернуть бэкап" not in labels  # новой пленке откатывать нечего
    target = tmp_path / "mel-2026-02.json"
    assert target.is_file()
    assert validate_file(target).cassette is not None


async def test_restore_callback_restores_previous(monkeypatch, tmp_path) -> None:
    """«🗄 Вернуть бэкап»: откат к предыдущему варианту кассеты."""
    source = _enable_library(monkeypatch, tmp_path)
    cassette = validate_file(source).cassette
    old_title = cassette.title
    ok, _lines, _final = ed.apply_cassette_file(
        ed.scenario_yaml(cassette).replace(old_title, "Версия A").encode("utf-8"),
        "mel.json",
        "month",
        tmp_path,
    )
    assert ok
    assert validate_file(source).cassette.title == "Версия A"
    callback = _make_callback("cassette:restore:mel.json")
    await panel_mod.on_cassette_action(callback)
    assert callback.answer.call_args.args[0] == "Кассета восстановлена из бэкапа."
    assert validate_file(source).cassette.title == old_title
    assert "КАССЕТЫ" in callback.message.edit_text.call_args.args[0]


async def test_restore_callback_without_backup(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:restore:mel.json")
    await panel_mod.on_cassette_action(callback)
    assert "Бэкапа" in callback.answer.call_args.args[0]


async def test_snapshot_callback_takes_backup_without_edit(monkeypatch, tmp_path) -> None:
    """«💾 Снять бэкап»: слепок текущего состояния без единой правки."""
    source = _enable_library(monkeypatch, tmp_path)

    callback = _make_callback("cassette:snapshot:mel.json")
    await panel_mod.on_cassette_action(callback)

    assert callback.answer.call_args.args[0] == (
        "Слепок снят: состояние кассеты зафиксировано."
    )
    bak = tmp_path / "mel.json.bak"
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert validate_file(source).cassette is not None, "содержимое не изменилось"
    # На экране плёнки появилась кнопка отката.
    labels = _flat(callback.message.edit_text.call_args.kwargs["reply_markup"])
    assert "🗄 Вернуть бэкап" in labels
    assert "💾 Снять бэкап" in labels


async def test_snapshot_second_click_overwrites_and_skips_rerender(
    monkeypatch, tmp_path
) -> None:
    """Повторный снимок перезаписывает слепок; edit_text не дёргается — тост хватит."""
    source = _enable_library(monkeypatch, tmp_path)
    (tmp_path / "mel.json.bak").write_text("старый слепок", encoding="utf-8")

    callback = _make_callback("cassette:snapshot:mel.json")
    await panel_mod.on_cassette_action(callback)

    bak = tmp_path / "mel.json.bak"
    assert bak.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert "Слепок снят" in callback.answer.call_args.args[0]
    callback.message.edit_text.assert_not_called()


async def test_snapshot_rejects_unsafe_name(monkeypatch, tmp_path) -> None:
    """Имя с выходом из каталога отсекается до работы с файлом."""
    _enable_library(monkeypatch, tmp_path)

    callback = _make_callback("cassette:snapshot:../evil.json")
    await panel_mod.on_cassette_action(callback)

    assert "Некорректное имя кассеты." in callback.answer.call_args.args[0]
    callback.message.edit_text.assert_not_called()
    assert not (tmp_path.parent / "evil.json.bak").exists()


async def test_document_handler_applies_day_edit(monkeypatch, tmp_path) -> None:
    """Документ «фрагмент дня» компилируется и кладётся в кассету, намерение гаснет."""
    source = _enable_library(monkeypatch, tmp_path)
    cassette = validate_file(source).cassette
    assert cassette is not None
    fork_day = cassette.day_for(14, "morning")
    fragment = ed.day_yaml(fork_day, road="morning").replace(
        fork_day.chapter_title, "Правка с самого бота"
    )

    async def _download(file=None, destination=None):
        destination.write(fragment.encode("utf-8"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-14"),
        reply=AsyncMock(),
    )
    async with SessionLocal() as session:
        await set_edit_intent(session, "mel.json", "day")
    await panel_mod.on_cassette_document(message)
    assert message.reply.call_args.args[0].startswith("✅")
    labels = _flat(message.reply.call_args.kwargs["reply_markup"])
    assert "🎞 Плёнка" in labels
    assert "🗄 Вернуть бэкап" in labels  # apply сделал .bak — undo одной кнопкой
    assert "📼 К кассетам" in labels
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == (None, None)
    relit = validate_file(source).cassette
    assert relit is not None
    assert relit.switch[0].days[4].chapter_title == "Правка с самого бота"


async def test_document_handler_rejects_broken(monkeypatch, tmp_path) -> None:
    """Битый документ: отчёт «❌», намерение гаснет, кассета не тронута."""
    source = _enable_library(monkeypatch, tmp_path)
    before = source.read_bytes()

    async def _download(file=None, destination=None):
        destination.write(b"days: - {}\n")

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-bad"),
        reply=AsyncMock(),
    )
    async with SessionLocal() as session:
        await set_edit_intent(session, "mel.json", "month")
    await panel_mod.on_cassette_document(message)
    reply = message.reply.call_args.args[0]
    assert reply.startswith("❌")
    async with SessionLocal() as session:
        assert await get_edit_intent(session) == (None, None)
    assert source.read_bytes() == before


async def test_document_handler_warns_when_db_persist_fails(monkeypatch, tmp_path) -> None:
    """Правка легла на диск, а база не ответила: отчёт честно говорит про кэш.

    persist вызывается после записи файла; его отказ не валит отчёт (сюжет —
    не касса), но хранитель обязан увидеть, что правка переживёт рестарт
    только если база ответит при повторе.
    """
    from app.story import store as story_store

    source = _enable_library(monkeypatch, tmp_path)
    cassette = validate_file(source).cassette
    text = ed.scenario_yaml(cassette).replace(cassette.title, "Правка без базы")

    async def _download(file=None, destination=None):
        destination.write(text.encode("utf-8"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-nodb"),
        reply=AsyncMock(),
    )
    monkeypatch.setattr(story_store, "persist", AsyncMock(return_value=False))
    async with SessionLocal() as session:
        await set_edit_intent(session, "mel.json", "month")
    await panel_mod.on_cassette_document(message)
    reply = message.reply.call_args.args[0]
    assert reply.startswith("✅"), "сама правка удалась — деградирует только база"
    assert "ТОЛЬКО В КЭШЕ" in reply
    assert validate_file(source).cassette.title == "Правка без базы"


async def test_document_handler_ignored_without_intent(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)

    async def _download(file=None, destination=None):
        destination.write(b"anything")

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=HOLDER_ID),
        bot=SimpleNamespace(download=AsyncMock(side_effect=_download)),
        document=SimpleNamespace(file_id="doc-x"),
        reply=AsyncMock(),
    )
    await panel_mod.on_cassette_document(message)
    assert not message.reply.called
    assert not message.bot.download.called


async def test_back_returns_to_library(monkeypatch, tmp_path) -> None:
    _enable_library(monkeypatch, tmp_path)
    callback = _make_callback("cassette:back")
    await panel_mod.on_cassette_action(callback)
    text = callback.message.edit_text.call_args.args[0]
    assert "КАССЕТЫ" in text
    assert "mel.json" in text


async def test_cassette_menu_shows_narrative_lint(monkeypatch, tmp_path) -> None:
    """Меню библиотеки показывает повествовательный линт — раньше он жил только в CLI.

    Схема дубль станции не считает (редакционный признак), поэтому без
    narrative_lint панель молчала о таких замечаниях вовсе: 35 замечаний
    октябрьской кассеты существовали вне бота.
    """
    import json

    source = _enable_library(monkeypatch, tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["days"][1]["station"] = payload["days"][0]["station"]
    source.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    callback = _make_callback("cassette:back")
    await panel_mod.on_cassette_action(callback)
    text = callback.message.edit_text.call_args.args[0]
    assert "повествовательный линт" in text
    assert "дубль станции" in text, "каждый непустой разряд показывает свой пример"
    assert "ротация" in text
    assert "… и ещё" in text, "хвост списка свёрнут, а не потерян молча"


def test_chunk_message_splits_long_text() -> None:
    text = "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
    chunks = panel_mod._chunk_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= panel_mod._MAX_TEXT_CHUNK for chunk in chunks)
    assert "\n".join(chunks) == text
    assert panel_mod._chunk_message("короткий") == ["короткий"]


async def test_cmd_cassette_requires_holder(monkeypatch) -> None:
    """Команда /cassette для не-хранителя — гейт, без доступа к библиотеке."""
    monkeypatch.setattr(panel_mod.settings, "admin_ids", str(HOLDER_ID))
    outsider = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1),
        text="/cassette",
        answer=AsyncMock(),
    )
    await panel_mod.cmd_cassette(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]
