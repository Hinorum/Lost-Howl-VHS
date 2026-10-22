"""Allow-list сканера секретов: обещанный механизм должен работать.

Сканер обещает в своём тексте файл .secrets-allowlist, но не читал его:
ложное срабатывание (например, на 24-словном примере мнемоники в тесте)
разблокировать было нечем, и разработчик обходил сканер флагом --no-verify —
то есть защита выключалась целиком.

Теперь allow-list читается и применяется.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "check_secrets.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("check_secrets_under_test", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hook():
    return _load_hook()


def test_allowlist_file_is_read(hook, tmp_path, monkeypatch) -> None:
    """Подстроки из .secrets-allowlist читаются."""
    allowlist = tmp_path / ".secrets-allowlist"
    allowlist.write_text("# комментарий\n\ntest-phrase-here\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert hook._allowlist() == ["test-phrase-here"]


def test_missing_allowlist_allows_nothing(hook, tmp_path, monkeypatch) -> None:
    """Нет файла — ничего не разрешено (сканер остаётся строгим)."""
    monkeypatch.chdir(tmp_path)

    assert hook._allowlist() == []


def test_allowed_line_is_skipped(hook, tmp_path, monkeypatch) -> None:
    """Строка, попавшая в allow-list, не считается находкой."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".secrets-allowlist").write_text(
        "the quick brown fox jumps over the lazy dog\n", encoding="utf-8"
    )
    target = tmp_path / "notes.txt"
    target.write_text(
        "the quick brown fox jumps over the lazy dog\n", encoding="utf-8"
    )

    assert hook._scan_file(target, hook._allowlist()) == []


def test_other_lines_are_still_scanned(hook, tmp_path, monkeypatch) -> None:
    """Allow-list не выключает сканер целиком."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".secrets-allowlist").write_text("разрешённая строка\n", encoding="utf-8")
    target = tmp_path / "notes.txt"
    # 12 слов из публичного wordlist BIP-39 в строковой константе — то, что
    # сканер ловит.
    words = " ".join(hook._BIP39_HEAD[:12])
    target.write_text(f'SEED_PHRASE = "{words}"\n', encoding="utf-8")

    findings = hook._scan_file(target, hook._allowlist())

    assert findings, "разрешённая строка не должна отключать сканер"
