"""Имя кассеты из callback_data не должно выводить за пределы библиотеки.

file_name приходит из `callback.data`, то есть от клиента, и раньше
подставлялся в путь как есть. Telegram ограничивает callback_data 64 байтами,
поэтому `cassette:scene:../../../../etc/passwd` в него помещался:

* чтение — любая операция сцены/скачивания читала и валидировала произвольный
  файл контейнера (вместе с содержимым ошибок в ответе хранителю);
* запись — `cassette:restore:<путь>` копировал `.bak` в произвольный путь, где
  такой файл есть.

Это не эскалация привилегий: гейт по ADMIN_IDS на месте, и имя приходит от
самого хранителя. Но превращать игровую фичу в чтение/запись произвольных
файлов контейнера (где лежат data/the_way.db и дампы бэкапов) — плохая
идея: один скомпрометированный аккаунт хранителя, или одно нажатие на
пересланное сообщение, дают чтение файлов и запись в них.

Проверяем и функцию, и реальные пути: имя проходит только если это базовое
имя с расширением .json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.handlers import panel as panel_mod
from app.story import editor as editor_mod

BAD_NAMES = [
    "../../../../etc/passwd",
    "..%2f..%2fetc/passwd",
    "..",
    ".",
    "",
    "sub/dir.json",
    "sub\\dir.json",
    "dir\\..\\..\\file.json",
    "/etc/passwd.json",
    "C:\\Windows\\win.json",
    "cassette.json.bak",
    "cassette.txt",
    "no-extension",
]

GOOD_NAMES = [
    "cassette-2026-10.json",
    "day-15.json",
    "a.json",
]


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_library_style_names_are_allowed(name: str) -> None:
    assert editor_mod.is_safe_cassette_name(name) is True
    assert panel_mod._safe_cassette_name(name) == name


@pytest.mark.parametrize("name", BAD_NAMES)
def test_traversal_names_are_rejected(name: str) -> None:
    assert editor_mod.is_safe_cassette_name(name) is False
    assert panel_mod._safe_cassette_name(name) is None


def test_handler_helper_and_editor_agree() -> None:
    """Два места проверяют одно правило: расхождение означало бы дыру.

    Хендлер отсекает имя на границе callback, модуль редактора — на границе
    записи (его же использует CLI-инструмент). Правило должно быть одно.
    """
    for name in GOOD_NAMES + BAD_NAMES:
        expected = name if editor_mod.is_safe_cassette_name(name) else None
        assert panel_mod._safe_cassette_name(name) == expected, name


def test_library_entry_refuses_to_read_outside_library(tmp_path: Path) -> None:
    """Попытка прочитать файл вне библиотеки не возвращает запись."""
    secret = tmp_path / "secret.json"
    secret.write_text("{}", encoding="utf-8")

    assert panel_mod._library_entry("../secret.json") is None
    assert panel_mod._library_entry("secret.json") is None, "вне библиотеки нечего искать"


def test_library_entry_reads_real_cassette(tmp_path: Path, monkeypatch) -> None:
    """Проверка не сломала обычный путь: файл библиотеки читается."""
    payload = {
        "cassette_id": "test",
        "month": "2026-10",
        "days": [],
    }
    (tmp_path / "cassette-2026-10.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(panel_mod, "default_cassettes_dir", lambda: tmp_path)

    entry = panel_mod._library_entry("cassette-2026-10.json")
    assert entry is not None
    assert entry.file_name == "cassette-2026-10.json"


def test_has_backup_refuses_outside_library(tmp_path: Path, monkeypatch) -> None:
    """Бэкап ищется только внутри библиотеки, иначе путь тоже утекает."""
    monkeypatch.setattr(panel_mod, "default_cassettes_dir", lambda: tmp_path)
    (tmp_path / "inside.json.bak").write_text("x", encoding="utf-8")

    assert panel_mod._has_backup("inside.json") is True
    assert panel_mod._has_backup("../inside.json") is False


def test_restore_backup_refuses_traversal(tmp_path: Path) -> None:
    """restore_backup пишет в файл — имя проверяется до касания путей."""
    outside = tmp_path / "outside.json"
    bak = tmp_path / "payload.json.bak"
    bak.write_text(json.dumps({"cassette_id": "x", "month": "2026-01", "days": []}), encoding="utf-8")

    ok, lines = editor_mod.restore_backup("../payload.json", tmp_path)

    assert ok is False
    assert "Недопустимое" in lines[0]
    assert not outside.exists(), "файл вне библиотеки создан быть не должен"


def test_apply_rejects_traversal_file_name(tmp_path: Path) -> None:
    """Правка по имени извне не пишет за пределы каталога."""
    data = json.dumps({"cassette_id": "x", "month": "2026-01", "days": []}).encode()

    ok, lines, _name = editor_mod.apply_cassette_file(
        data, "../evil.json", "month", tmp_path
    )

    assert ok is False
    assert "Недопустимое" in lines[0]
    assert not (tmp_path.parent / "evil.json").exists()


def test_apply_rejects_traversal_in_derived_name(tmp_path: Path, monkeypatch) -> None:
    """Конечное имя выводится из сценария, то есть из самого документа.

    Сценарий загружается посторонним файлом, поэтому cassette_id из него — тоже
    недоверенный ввод, и через него имя попадает на диск. Сценарий здесь
    валидный (настоящий редактор его принимает), подменено только имя.
    """
    cassette = _valid_cassette_text()
    monkeypatch.setattr(
        editor_mod,
        "_derive_name",
        lambda _cassette: "../escaped.json",
    )

    ok, lines, name = editor_mod.apply_cassette_file(cassette, "<new>", "new", tmp_path)

    assert ok is False, "кассета с выходом за пределы каталога записана"
    assert any("Недопустимое" in line for line in lines), lines
    assert name is None
    assert not (tmp_path.parent / "escaped.json").exists()


def _valid_cassette_text() -> bytes:
    """Настоящий сценарий, который редактор принимает как новую кассету."""
    from app.story.schema import validate_payload

    days = [
        {
            "day_index": index,
            "station": f"станция {index}",
            "chapter_title": f"глава {index}",
            "chapter_text": f"текст {index}",
            "dilemma": f"Что решит день {index}?",
            "cards": [
                {
                    "position": position,
                    "title": f"ход {position}",
                    "consequence": "канон",
                }
                for position in (0, 1, 2)
            ],
        }
        for index in range(1, 29)  # весь месяц: схема требует полного состава
    ]
    result = validate_payload(
        {
            "cassette_id": "mel",
            "month": "2026-02",
            "title": "Метель",
            "days": days,
        }
    )
    assert result.cassette is not None, result.errors
    return editor_mod.scenario_yaml(result.cassette).encode("utf-8")
