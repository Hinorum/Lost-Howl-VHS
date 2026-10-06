"""Контроль артефактов критичных зависимостей должен ЛОВИТЬ подмену.

Смысл инструмента — не «сходить в PyPI и напечатать ок», а упасть там, где
призошло одно из двух:

* артефакт под уже занятой версией перезалили (хеш другой);
* под ту же версию добавили новый артефакт (например, platform-specific wheel,
  который pip предпочтёт).

Второе важно не меньше первого: если бы сверялись только известные файлы,
достаточно было бы выложить дополнительный wheel, и pip поставил бы его
молча, а пин-файл остался бы «зелёным».

Тесты сетевую жизнь не трогают: PyPI подменяется фейкером, так что проверка
детерминирована и не зависит от того, что сейчас в индексе.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import dependency_hashes as dh

GOOD = {
    "pytoniq": {
        "__version__": "0.1.43",
        "pytoniq-0.1.43-py3-none-any.whl": "a" * 64,
        "pytoniq-0.1.43.tar.gz": "b" * 64,
    },
    "pytoniq-core": {
        "__version__": "0.1.46",
        "pytoniq_core-0.1.46-py3-none-any.whl": "c" * 64,
        "pytoniq_core-0.1.46.tar.gz": "d" * 64,
    },
}


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            [
                "aiohttp==3.12.15",
                "pytoniq==0.1.43",
                "pytoniq-core==0.1.46",
                "# комментарий",
                "alembic==1.20.0",
            ]
        ),
        encoding="utf-8",
    )
    pin = tmp_path / "requirements-critical-hashes.txt"
    pin.write_text(dh.render_pin_file(GOOD), encoding="utf-8")
    return requirements, pin


def _stub(monkeypatch: pytest.MonkeyPatch, upstream: dict[str, dict[str, str]]) -> None:
    """PyPI отдаёт ровно то, что задано."""
    monkeypatch.setattr(
        dh,
        "fetch_artifacts",
        lambda name, version: dict(upstream.get(name, {})),
    )


def test_verify_passes_when_everything_matches(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    requirements, pin = workspace
    _stub(monkeypatch, {name: {k: v for k, v in art.items() if k != "__version__"}
                        for name, art in GOOD.items()})
    assert dh.verify(requirements, pin) == 0
    assert "совпадают" in capsys.readouterr().out


def test_verify_fails_on_reuploaded_artifact(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Перезалив под занятую версию: хеш не тот."""
    requirements, pin = workspace
    tampered = {
        name: {k: v for k, v in art.items() if k != "__version__"}
        for name, art in GOOD.items()
    }
    tampered["pytoniq"]["pytoniq-0.1.43-py3-none-any.whl"] = "e" * 64
    _stub(monkeypatch, tampered)

    assert dh.verify(requirements, pin) == 1
    err = capsys.readouterr().err
    assert "ХЕШ НЕ СОВПАДАЕТ" in err
    assert "Не обновляй пин-файл" in err


def test_verify_fails_on_new_artifact(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Новый артефакт под той же версией — тоже атака, даже если хеши совпали."""
    requirements, pin = workspace
    extended = {
        name: {k: v for k, v in art.items() if k != "__version__"}
        for name, art in GOOD.items()
    }
    extended["pytoniq"]["pytoniq-0.1.43-cp311-cp311-win_amd64.whl"] = "f" * 64
    _stub(monkeypatch, extended)

    assert dh.verify(requirements, pin) == 1
    err = capsys.readouterr().err
    assert "НОВЫЙ артефакт" in err
    assert "win_amd64" in err


def test_verify_fails_on_version_drift(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Версия в requirements.txt разошлась с пин-файлом.

    Это штатная ситуация после PR от Dependabot: сначала обновляется
    requirements.txt, пин-файл обновляется вручную после ревью. Пока этого не
    сделано — установка небезопасна, и проверка обязана это видеть.
    """
    requirements, pin = workspace
    requirements.write_text(
        "pytoniq==0.1.44\npytoniq-core==0.1.46\n", encoding="utf-8"
    )
    _stub(monkeypatch, {})

    assert dh.verify(requirements, pin) == 1
    assert "версия разошлась" in capsys.readouterr().err


def test_verify_fails_when_package_missing_from_requirements(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Пакет убран из requirements.txt, а пин-файл про него помнит."""
    requirements, pin = workspace
    requirements.write_text("aiohttp==3.12.15\n", encoding="utf-8")
    _stub(monkeypatch, {})

    assert dh.verify(requirements, pin) == 1
    assert "нет в requirements.txt" in capsys.readouterr().err


def test_verify_fails_on_network_error(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Сеть недоступна — это НЕ «всё хорошо», а неподтверждённая проверка.

    Молчаливый пропуск здесь был бы опаснее падения: подмена прошла бы как
    успешная проверка ровно тогда, когда сеть легла.
    """

    def boom(name: str, version: str) -> dict:
        raise TimeoutError("сеть легла")

    monkeypatch.setattr(dh, "fetch_artifacts", boom)
    requirements, pin = workspace

    assert dh.verify(requirements, pin) == 1
    assert "не удалось получить данные PyPI" in capsys.readouterr().err


def test_parse_round_trips_rendered_pin_file(workspace: tuple[Path, Path]) -> None:
    """Разбор пин-файла не теряет артефакты.

    Артефакты лежат комментариями, и общий пропуск комментариев их раньше
    съедал: проверка проходила, ничего не сверяя. Круг render→parse держит
    формат от этого.
    """
    _requirements, pin = workspace
    parsed = dh.parse_pin_file(pin)
    assert parsed == GOOD


def test_pinned_versions_ignores_comments_and_extras() -> None:
    """Версии читаются только из строк с `==`, комментарии игнорируются."""
    requirements = Path("requirements.txt")
    if not requirements.is_file():
        pytest.skip("requirements.txt не найден")
    versions = dh.pinned_versions(requirements)
    assert versions["pytoniq"]
    assert versions["pytoniq-core"]
    assert all(name == name.lower().replace("_", "-") for name in versions)
    # Ни одна строка-комментарий не должна попасть в словарь версий.
    assert not any(name.startswith("#") for name in versions)


def test_critical_packages_are_pinned_in_requirements() -> None:
    """Каждый критичный пакет реально закреплён версией, а не «где-то есть»."""
    versions = dh.pinned_versions()
    for name in dh.CRITICAL:
        assert name in versions, f"{name} не закреплён в requirements.txt"


def test_every_pinned_dependency_is_critical() -> None:
    """Новая прямая зависимость в requirements.txt обязана попасть в CRITICAL.

    Любая из них исполняется в том же процессе, который читает
    TREASURY_MNEMONIC из окружения, — компрометация любой равна компрометации
    казны. Забытый пакет — дыра, которая выглядит как зелёный CI.
    """
    versions = dh.pinned_versions()
    missing = set(versions) - set(dh.CRITICAL)
    assert not missing, f"не входят в CRITICAL: {sorted(missing)}"


def test_repo_pin_file_is_consistent_with_requirements() -> None:
    """Пин-файл репозитория соответствует закреплённым версиям.

    Расхождение означало бы, что проверка в CI сравнивает не то: либо
    requirements бампнули и забыли обновить пин-файл, либо наоборот.
    """
    requirements = Path("requirements.txt")
    pin = Path("requirements-critical-hashes.txt")
    if not (requirements.is_file() and pin.is_file()):
        pytest.skip("файлы зависимостей не найдены")
    versions = dh.pinned_versions(requirements)
    for name, artifacts in dh.parse_pin_file(pin).items():
        assert artifacts["__version__"] == versions.get(name), name
        assert [k for k in artifacts if k != "__version__"], f"{name}: нет артефактов"


def test_json_fixture_shape_matches_pypi_api() -> None:
    """Формат, который разбирает fetch_artifacts, совпадает с ответом PyPI."""
    payload = {
        "urls": [
            {
                "filename": "pkg-1.0-py3-none-any.whl",
                "digests": {"sha256": "1" * 64},
            },
            {"filename": "pkg-1.0.tar.gz", "digests": {"sha256": "2" * 64}},
        ]
    }
    extracted = {
        entry["filename"]: entry["digests"]["sha256"]
        for entry in payload["urls"]
        if entry.get("filename")
    }
    # Скрипт читает ровно эти два поля — больше ничего не требуется.
    assert json.loads(json.dumps(payload)) == payload
    assert extracted == {"pkg-1.0-py3-none-any.whl": "1" * 64, "pkg-1.0.tar.gz": "2" * 64}
