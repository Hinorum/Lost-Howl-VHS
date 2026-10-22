"""Границы слоёв: сюжетный слой не знает про движок.

Проверку дублирует контракт импортов в CI (.importlinter, контракт 3) —
этот тест даёт тот же ответ за секунды, локально, без запуска линтера.

Петля, которую слои обязаны не замыкать: story -> rounds -> ops -> story.
Сюжетный слой тянет движок, стоит движку потянуть ops, а ops читает сюжетный
слой за недостающей кассетой — и граф замыкается. Слой общего (якорь забега,
реестр провайдера дня) живёт в app/core, а шаблонный день собирает движок и
передаёт провайдеру готовую базу.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORY = ROOT / "app" / "story"

# Кого сюжетный слой не знает: движок (роль дня, якорь, шаблон), Telegram-слой,
# деньги и рассылки.
_FORBIDDEN_PREFIXES = (
    "app.rounds",
    "app.handlers",
    "app.ton_pay",
    "app.broadcast",
    "app.scheduler",
)


def _imported_modules(path: Path) -> set[str]:
    """Все импортируемые модули файла, включая ленивые (внутри функций)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            # «from app.rounds import rendering» — ребро и на app.rounds.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_story_layer_does_not_import_engine_or_money() -> None:
    """Ни один модуль app/story не тянет движок, Telegram-слой и деньги."""
    offenders: list[str] = []
    for path in sorted(STORY.glob("*.py")):
        for module in _imported_modules(path):
            if module.startswith(_FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {module}")
    assert not offenders, (
        "Сюжетный слой тянет запрещённый слой: "
        + ", ".join(offenders)
        + ". Общее — в app/core (anchor, day_provider), шаблонный день "
        "приходит от движка готовой базой."
    )


def test_engine_does_not_import_story() -> None:
    """Движок не знает про сюжетный слой: инициатива регистрации — у сюжета."""
    offenders: list[str] = []
    for path in sorted((ROOT / "app" / "rounds").glob("*.py")):
        for module in _imported_modules(path):
            if module.startswith("app.story"):
                offenders.append(f"{path.name}: {module}")
    assert not offenders, (
        "Движок тянет сюжетный слой: " + ", ".join(offenders)
    )


def test_shared_vocabulary_lives_in_core() -> None:
    """Общее — в app/core: якорь забега и реестр провайдера дня."""
    core = ROOT / "app" / "core"
    assert (core / "anchor.py").is_file(), "якорь забега — общее, ему место в core"
    assert (core / "day_provider.py").is_file(), "реестр провайдера — общее"
    assert not (ROOT / "app" / "rounds" / "anchor.py").exists(), (
        "якорь забега вернулся в движок — сюжетный слой снова его потянет"
    )
