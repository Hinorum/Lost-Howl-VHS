"""Аудит молчащих исключений: пассить можно только с причиной.

`except: pass` — это выключенная сигнализация. Иногда молчание честное
(Telegram отвечает «message is not modified» на повторное нажатие), иногда
молчание вредит: пульт не показал зависшие выплаты и выглядит здоровым. Разница
не в коде, а в том, написал ли автор почему здесь тихо.

Проверка требует у каждого `pass` в обработчике исключения либо записи в лог,
либо пояснения рядом. Все оставшиеся молчания (6 штук) перечислены в
DOCUMENTED: main.py — Ctrl+C и неподдерживаемый Windows-сигнал, ton_codec.py —
проверка формата адреса, panel.py — повторное нажатие в пульте.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

# Почему молчание допустимо — человек читает это, а не тест.
DOCUMENTED = (
    "main.py: KeyboardInterrupt при остановке — штатный сценарий",
    "main.py: NotImplementedError у add_signal_handler (Windows/песочница)",
    "ton_codec.py: «не hex» — ответ проверки формата, а не сбой",
    "panel.py: «message is not modified» — повторное нажатие без правки",
)

# Сколько молчаний с пояснением должно остаться. Число, а не номера строк:
# нумерация сдвигается от каждой правки, а решение — нет. И добавление
# молчания, и его тихое удаление должны заставлять пересмотреть этот список.
EXPECTED_DOCUMENTED = 6


def _handlers(path: Path) -> list[ast.ExceptHandler]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        base = func.value
        prefix = base.id if isinstance(base, ast.Name) else ""
        return f"{prefix}.{func.attr}" if prefix else func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _logs(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee_name(node).startswith(("logger.", "log."))
        for node in ast.walk(handler)
    )


def _pass_lines(handler: ast.ExceptHandler) -> list[int]:
    return [stmt.lineno for stmt in ast.walk(handler) if isinstance(stmt, ast.Pass)]


def _explained(path: Path, handler: ast.ExceptHandler) -> bool:
    """Внутри самого обработчика есть пояснение: молчание должно быть описано
    там же, где оно написано, а не в соседнем файле или в этом тесте."""
    source = path.read_text(encoding="utf-8").splitlines()
    last = max(
        node.end_lineno or handler.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.stmt)
    )
    return any(
        "#" in line and len(line.split("#", 1)[1].strip()) >= 12
        for line in source[handler.lineno - 1 : last]
    )


def _silent_sites() -> tuple[list[str], list[str]]:
    """(молчания без причины, молчания с причиной) как "путь:строка"."""
    unexplained: list[str] = []
    documented: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for handler in _handlers(path):
            if _logs(handler):
                continue
            for pass_line in _pass_lines(handler):
                if _explained(path, handler):
                    documented.append(f"{rel}:{pass_line}")
                else:
                    unexplained.append(f"{rel}:{pass_line}")
    return unexplained, documented


def test_no_silent_passes_without_reason() -> None:
    """Каждое молчание в app/ либо пишет в лог, либо объяснено рядом с собой."""
    unexplained, _documented = _silent_sites()
    assert not unexplained, (
        "Молчание без причины: "
        + ", ".join(unexplained)
        + ". Либо залогируй, либо объясни в комментарии и в списке DOCUMENTED."
    )


def test_documented_silences_match_the_audit() -> None:
    """Список молчаний не должен протухать молча: и новое, и удалённое
    заставляют пересмотреть DOCUMENTED."""
    _unexplained, documented = _silent_sites()
    assert len(documented) == EXPECTED_DOCUMENTED, (
        f"Ожидалось {EXPECTED_DOCUMENTED} молчаний с пояснением, найдено "
        f"{len(documented)}: {', '.join(documented)}. Пересмотри DOCUMENTED."
    )


def test_audit_notes_stay_readable() -> None:
    """Причины перечислены словами: тест, который их проверяет, должен быть
    понятен человеку, а не только машине."""
    assert len(DOCUMENTED) == 4
    assert all(":" in note and len(note) > 20 for note in DOCUMENTED)
