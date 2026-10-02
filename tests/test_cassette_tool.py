"""Round-trip кассетного инструмента: dump → правка → compile.

Гарантии:
- YAML-сценарий — полноправный редакционный формат хранителя: каждая
  кассета библиотеки разворачивается в YAML и собирается обратно в ту же
  модель (JSON на выходе нормализуется — авто-дефолты дописываются).
- Компилятор проходит тот же `validate_payload`, что движок: кривой сценарий
  отклоняется с локациями, без записи файла.
"""

import json
from pathlib import Path

import yaml

from app.story.schema import validate_file
from scripts import cassette_tool

_CASSETTES_DIR = Path(__file__).resolve().parent.parent / "app" / "story" / "cassettes"
SHIPPED = sorted(_CASSETTES_DIR.glob("*.json"))


def _mk_cassette(month: str = "2026-02", days_n: int = 28) -> dict:
    """Кассета с главной дорогой и одной перемоткой (fork) — для тестов дорог."""
    days = []
    for i in range(1, days_n + 1):
        days.append(
            {
                "day_index": i,
                "station": f"станция {i}",
                "chapter_title": f"глава {i}",
                "chapter_text": f"текст {i}",
                "rule_hint": "any",
                "cards": [
                    {"position": 0, "title": f"ход {i}a", "description": "описание", "consequence": "канон"},
                    {"position": 1, "title": f"ход {i}b", "description": "описание", "consequence": "канон"},
                    {"position": 2, "title": f"ход {i}c", "description": "описание", "consequence": "канон"},
                ],
            }
        )
    return {
        "cassette_id": "testovaya",
        "month": month,
        "title": "Тестовая",
        "attribution": "Фанфик по мотивам.",
        "days": days,
        "switch": [
            {
                "to": "morning",
                "at_day": 10,
                "winner": 0,
                "days": days[9:],
            }
        ],
    }


def _write_cassette(tmp_path) -> Path:
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps(_mk_cassette(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run(*argv: str) -> int:
    return cassette_tool.run(list(argv))


def _dump_yaml(tmp_path, cassette_path: Path) -> Path:
    out = tmp_path / "scenario.yaml"
    assert _run("dump", str(cassette_path), "-o", str(out)) == 0
    return out


def test_runtime_has_yaml() -> None:
    """Инструмент опирается на PyYAML: обязательный dep в requirements."""
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_roundtrip_all_shipped_cassettes(tmp_path) -> None:
    assert SHIPPED, "в библиотеке должны быть кассеты-образцы"
    for path in SHIPPED:
        yaml_path = _dump_yaml(tmp_path, path)
        json_path = tmp_path / (path.stem + ".json")
        assert _run("compile", str(yaml_path), "-o", str(json_path)) == 0
        original = validate_file(path).cassette
        compiled = validate_file(json_path).cassette
        assert original is not None and compiled is not None
        assert compiled.model_dump() == original.model_dump()


def test_edit_card_and_chapter_survives_compile(tmp_path) -> None:
    """Правка одного дня/варианта: compile записывает её в кассету, остальное цело."""
    src = SHIPPED[0]
    yaml_path = _dump_yaml(tmp_path, src)
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8-sig"))
    payload["days"][0]["chapter_title"] = "Правка: новая глава"
    payload["days"][0]["cards"][1]["title"] = "Правка: новый ход"
    yaml_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    json_path = tmp_path / "edited.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path)) == 0

    edited = validate_file(json_path).cassette
    original = validate_file(src).cassette
    assert edited is not None and original is not None
    assert edited.days[0].chapter_title == "Правка: новая глава"
    assert edited.days[0].cards[1].title == "Правка: новый ход"
    assert edited.cassette_id == original.cassette_id
    assert [day.day_index for day in edited.days] == [day.day_index for day in original.days]


def test_broken_scenario_rejected_without_write(tmp_path) -> None:
    """Отняли последний день месяца → месяц не сходится с календарём: реджект, файла нет."""
    src = SHIPPED[0]
    yaml_path = _dump_yaml(tmp_path, src)
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8-sig"))
    payload["days"].pop()
    yaml_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    json_path = tmp_path / "broken.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path)) != 0
    assert not json_path.exists()


def test_check_validates_without_write(tmp_path) -> None:
    yaml_path = _dump_yaml(tmp_path, SHIPPED[0])
    json_path = tmp_path / "check.json"
    assert _run("compile", str(yaml_path), "-o", str(json_path), "--check") == 0
    assert not json_path.exists()


def test_dump_day_view_is_readable(tmp_path, capsys) -> None:
    """`dump --day N` печатает кадр для чтения: станция, глава, три варианта."""
    src = SHIPPED[0]
    assert _run("dump", str(src), "--day", "1") == 0
    out = capsys.readouterr().out
    cassette = validate_file(src).cassette
    assert cassette is not None
    day1 = cassette.days[0]
    assert f"День {day1.day_index}" in out
    assert day1.station in out
    assert day1.chapter_title in out
    assert day1.cards[0].title in out
    assert day1.cards[0].consequence in out


def test_dump_fork_day_is_readable_via_road(tmp_path, capsys) -> None:
    """Развилки (switch) читаются по --road; main тоже читается, неизвестная — реджект."""
    ke = _write_cassette(tmp_path)
    assert _run("dump", str(ke), "--day", "14", "--road", "morning") == 0
    out = capsys.readouterr().out
    assert "дорога morning" in out
    assert "глава 14" in out
    assert "ход 14a" in out
    capsys.readouterr()
    assert _run("dump", str(ke), "--day", "14") == 0  # main покрывает весь месяц
    assert "дорога morning" not in capsys.readouterr().out
    assert _run("dump", str(ke), "--day", "14", "--road", "night") != 0


def test_dump_day_fragment_for_patch(tmp_path) -> None:
    """`dump --day N -o` пишет фрагмент одного дня для patch (редактируемый YAML)."""
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "day14.yaml"
    assert _run("dump", str(ke), "--day", "14", "--road", "morning", "-o", str(frag)) == 0
    payload = yaml.safe_load(frag.read_text(encoding="utf-8-sig"))
    assert payload["day_index"] == 14
    assert len(payload["cards"]) == 3


def test_patch_edits_single_day_in_place(tmp_path) -> None:
    """patch мержит правку одного дня в кассету; остальные дни не трогаются."""
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "day3.yaml"
    assert _run("dump", str(ke), "--day", "3", "-o", str(frag)) == 0
    payload = yaml.safe_load(frag.read_text(encoding="utf-8-sig"))
    payload["chapter_title"] = "Правка: глава 3"
    payload["cards"][0]["title"] = "Правка: ход 3a"
    frag.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "--day", "3", "-o", str(out)) == 0

    original = validate_file(ke).cassette
    patched = validate_file(out).cassette
    assert original is not None and patched is not None
    assert patched.days[2].chapter_title == "Правка: глава 3"
    assert patched.days[2].cards[0].title == "Правка: ход 3a"
    assert patched.days[2].cards[1].title == original.days[2].cards[1].title
    assert patched.days[0].chapter_title == original.days[0].chapter_title
    assert len(patched.days) == len(original.days)


def test_patch_fork_day(tmp_path) -> None:
    """Правка дня на дороге перемотки попадает в switch, а не в main."""
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "fork20.yaml"
    assert _run("dump", str(ke), "--day", "20", "--road", "morning", "-o", str(frag)) == 0
    payload = yaml.safe_load(frag.read_text(encoding="utf-8-sig"))
    payload["chapter_title"] = "Правка на ветке"
    frag.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "--day", "20", "--road", "morning", "-o", str(out)) == 0
    patched = validate_file(out).cassette
    original = validate_file(ke).cassette
    assert original is not None and patched is not None
    assert patched.days[19].chapter_title == original.days[19].chapter_title  # main не задет
    fork = next(f for f in patched.switch if f.to == "morning")
    assert fork.days[10].chapter_title == "Правка на ветке"  # день 20 = at_day 10 + offset 10


def test_patch_day_index_mismatch_rejected(tmp_path) -> None:
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "day5.yaml"
    assert _run("dump", str(ke), "--day", "5", "-o", str(frag)) == 0
    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "--day", "7", "-o", str(out)) != 0
    assert not out.exists()


def test_patch_unknown_road_rejected(tmp_path) -> None:
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "day2.yaml"
    assert _run("dump", str(ke), "--day", "2", "-o", str(frag)) == 0
    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "--day", "2", "--road", "night", "-o", str(out)) != 0
    assert not out.exists()


def test_patch_without_flags_reads_day_and_road_from_fragment(tmp_path) -> None:
    """Без --day/--road patch берёт дорогу и день из заголовка фрагмента сам."""
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "fork-day.yaml"
    assert _run("dump", str(ke), "--day", "20", "--road", "morning", "-o", str(frag)) == 0
    payload = yaml.safe_load(frag.read_text(encoding="utf-8"))
    payload["chapter_title"] = "Правка без флагов"
    frag.write_text(
        "# дорога: morning\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "-o", str(out)) == 0
    patched = validate_file(out).cassette
    original = validate_file(ke).cassette
    assert patched is not None and original is not None
    fork = next(f for f in patched.switch if f.to == "morning")
    assert fork.days[10].chapter_title == "Правка без флагов"
    assert patched.days[19].chapter_title == original.days[19].chapter_title
    # --check без флагов тоже работает и ничего не пишет
    before = ke.read_bytes()
    assert _run("patch", str(ke), str(frag), "--check") == 0
    assert ke.read_bytes() == before


def test_patch_check_validates_without_write(tmp_path) -> None:
    ke = _write_cassette(tmp_path)
    frag = tmp_path / "day4.yaml"
    assert _run("dump", str(ke), "--day", "4", "-o", str(frag)) == 0
    out = tmp_path / "patched.json"
    assert _run("patch", str(ke), str(frag), "--day", "4", "-o", str(out), "--check") == 0
    assert not out.exists()


def _clean_cassette_payload(month: str = "2026-12", n: int = 31) -> dict:
    """Ротационно-чистая кассета: стратегии циклят по позициям без залипаний,
    станции/имена уникальны, эхо не пересказывает канон, штампов нет."""
    tags = _TAGS_CYCLE
    days = []
    for i in range(1, n + 1):
        tag = tags[i % 3]
        days.append(
            {
                "day_index": i,
                "station": f"станция {i}",
                "chapter_title": f"глава {i}",
                "chapter_text": f"текст {i}",
                "rule_hint": "any",
                "prev": (
                    {p: "Мимо след вчерашнего дня" for p in (0, 1, 2)}
                    if i > 1
                    else None
                ),
                "cards": [
                    {
                        "position": p,
                        "title": f"ход {i}-{p}",
                        "description": "описание у котла",
                        "consequence": f"канон {i}-{p}",
                        "tag": tag,
                    }
                    for p in (0, 1, 2)
                ],
            }
        )
    return {
        "cassette_id": "chistaya",
        "month": month,
        "title": "Чистая",
        "attribution": "Фанфик по мотивам.",
        "days": days,
    }


_TAGS_CYCLE = ("care", "dare", "trick")


def test_lint_clean_cassette_has_no_warnings() -> None:
    from app.story.schema import validate_payload

    parsed = validate_payload(_clean_cassette_payload())
    assert parsed.ok
    assert parsed.cassette is not None
    assert cassette_tool.lint_warnings(parsed.cassette) == []


def test_lint_rotation_streak_warns() -> None:
    from app.story.schema import validate_payload

    payload = _clean_cassette_payload()
    for i in range(1, 4):
        for card in payload["days"][i - 1]["cards"]:
            card["tag"] = "trick"
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    warnings = cassette_tool.lint_warnings(cassette)
    assert any("три дня подряд" in warning for warning in warnings)


def test_lint_rotation_underuse_warns() -> None:
    from app.story.schema import validate_payload

    payload = _clean_cassette_payload()
    for day in payload["days"]:
        day["cards"][2]["tag"] = "care"
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    warnings = cassette_tool.lint_warnings(cassette)
    assert any("позиции 2" in warning and "dare" in warning for warning in warnings)


def test_lint_duplicate_station_and_title_warn() -> None:
    from app.story.schema import validate_payload

    payload = _clean_cassette_payload()
    payload["days"][1]["station"] = payload["days"][0]["station"]
    payload["days"][1]["cards"][0]["title"] = payload["days"][0]["cards"][0]["title"]
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    warnings = cassette_tool.lint_warnings(cassette)
    assert any("дубль станции" in warning for warning in warnings)
    assert any("дубль имени карты" in warning for warning in warnings)


def test_lint_echo_retelling_consequence_warns() -> None:
    from app.story.schema import validate_payload

    payload = _clean_cassette_payload()
    yester = payload["days"][0]
    payload["days"][1]["prev"] = {0: yester["cards"][0]["consequence"]}
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    warnings = cassette_tool.lint_warnings(cassette)
    assert any("пересказывает канон" in warning for warning in warnings)


def test_lint_style_antipatterns_warn() -> None:
    from app.story.schema import validate_payload

    payload = _clean_cassette_payload()
    payload["days"][0]["chapter_text"] = "Как будто одна нота, будто один шаг, будто всё та же стена."
    for day in payload["days"][:3]:
        day["diary"] = "Впервые за месяц луна не уходит."
    cassette = validate_payload(payload).cassette
    assert cassette is not None
    warnings = cassette_tool.lint_warnings(cassette)
    assert any("штамп" in warning and "будто" in warning for warning in warnings)
    assert any("штамп" in warning and "впервые" in warning for warning in warnings)


def test_lint_cli_reports_and_strict_exit(tmp_path) -> None:
    """lint: замечания печатаются; без --strict выход 0, со строгим — 1."""
    import json as _json

    ke = tmp_path / "linty.json"
    payload = _clean_cassette_payload()
    payload["days"][0]["station"] = payload["days"][1]["station"]  # дубль станции
    ke.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _run("lint", str(ke)) == 0
    assert _run("lint", str(ke), "--strict") == 1


def test_lint_cli_rejects_broken_cassette(tmp_path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    assert _run("lint", str(broken)) != 0