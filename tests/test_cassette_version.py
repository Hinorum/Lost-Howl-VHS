"""Версия формата кассеты: правка не должна терять поля молча.

Два риска в одном месте:

1. Формат без версии. Редактор переписывает файл через model_dump — то
   есть ТОЛЬКО известными полями. Любое поле, которого модель не знает,
   пропадало при первом же сохранении, и правка сюжета исчезала без следа.
   Теперь у кассеты есть version, а неизвестные ключи — ошибка валидации
   (их видно в линте и в панели), а не тихое удаление.

2. Название кассеты — авторский текст, а панель отправляет его в
   HTML-разметке: «&» или «<» в названии ломали весь экран (Telegram
   отвергает сообщение целиком). Теперь название и список дорог
   экранируются.

Заодно убран текст «ПЛЁНКА:» — слово из тестовой фикстуры, попавшее в
прод-экран панели.
"""

from __future__ import annotations

import pytest

from app.story.schema import validate_payload


def _payload(**extra) -> dict:
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
        for i in range(1, 29)
    ]
    payload = {
        "cassette_id": "mel",
        "month": "2026-02",
        "title": "Метель",
        "days": days,
    }
    payload.update(extra)
    return payload


def test_cassette_has_version() -> None:
    """У кассеты есть версия формата."""
    result = validate_payload(_payload())
    assert result.cassette is not None, result.errors
    assert result.cassette.version == 1


def test_unknown_field_is_kept_and_reported() -> None:
    """Незнакомый ключ сохраняется, а о нём говорит валидатор.

    Иначе правка через редактор (round-trip через model_dump) стирала бы
    поле, которого модель ещё не знает, и сюжет терялся бы без следа.
    """
    result = validate_payload(_payload(new_field="что-то новое"))

    assert result.cassette is not None, "незнакомый ключ не должен ломать файл"
    assert result.cassette.new_field == "что-то новое"
    assert any("new_field" in warning for warning in result.warnings), result.warnings


def test_roundtrip_keeps_all_fields() -> None:
    """Пересохранение не теряет ни одного поля."""
    result = validate_payload(_payload())
    assert result.cassette is not None
    dumped = result.cassette.model_dump(mode="json")

    again = validate_payload(dumped)
    assert again.cassette is not None
    assert again.cassette.model_dump(mode="json") == dumped
    assert again.cassette.version == 1


def test_version_can_be_raised() -> None:
    """Версия формата — обычное число: её можно поднять при смене контракта."""
    result = validate_payload(_payload(version=2))
    assert result.cassette is not None, result.errors
    assert result.cassette.version == 2


def test_panel_escapes_cassette_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Название кассеты с «&» не ломает экран панели."""
    from app.handlers import panel as panel_mod

    result = validate_payload(_payload(title="Метель & Пепел"))
    assert result.cassette is not None

    class _Entry:
        file_name = "mel.json"
        warnings: list[str] = []
        errors: list[str] = []
        cassette = result.cassette

    text = panel_mod._scene_text(_Entry(), "")
    assert "&amp;" in text
    assert "Метель & Пепел" not in text
    # Само название при этом читаемо.
    assert "Метель" in text
