"""Контроль артефактов критичных зависимостей: перезалив на PyPI ловится.

Пин версии `pkg==1.2.3` защищает от смены версии, но не защищает от
ПЕРЕЗАЛИВА: PyPI не запрещает выложить другое содержимое под уже
занятой версией, и pip это молча примет. Для пакета, который из мнемоники
выводит приватный ключ и подписывает реальные переводы, это путь к полной
потере казны.

Поэтому для критичных пакетов ведётся список артефактов с sha256, а перед
сборкой и деплоем он сверяется с тем, что PyPI отдаёт СЕЙЧАС.

Что именно проверяется и почему так:

* хеш каждого артефакта — содержимое совпадает с тем, что мы ревьюи��или;
* ТОЖЕ САМОЕ множество имён файлов — новый артефакт под той же версией тоже
  является атакой, иначе достаточно добавить platform-specific wheel, который
  pip предпочтёт, а в пин-файле его не будет.

Чего это НЕ делает и почему так выбрано:

* `--require-hashes` в requirements.txt не используется. Он требует хешей на
  ВСЮ транзитивную closure (десятки пакетов) и на каждом бампе требует
  перегенерации lock-файла, то есть ломает еженедельную автоматику Dependabot.
  Здесь выбран другой порядок приоритетов: автоматика обязана работать, а
  защита ставится точечно туда, где цена компрометации максимальна;
* транзитивные зависимости не проверяются: их состав меняется при каждом
  резолве и без lock-файла не зафиксирован. Сузить этот пункт до полного
  lock'а — отдельная работа, требующая решения по автоматике.

Использование:
    python -m scripts.dependency_hashes            # проверить (exit 1 при расхождении)
    python -m scripts.dependency_hashes --write    # обновить пин-файл с PyPI
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PIN_FILE = ROOT / "requirements-critical-hashes.txt"

# Пакеты, у которых цена компрометации выше всего: ВСЕ прямые зависимости
# проекта, закреплённые в requirements.txt.
#
# pytoniq / pytoniq-core напрямую держат мнемонику, выводят из неё приватный
# ключ и подписывают исходящие переводы: их компрометация — мгновенная кража
# казны. Остальные ключа не держат, но исполняются в том же процессе, который
# читает TREASURY_MNEMONIC из окружения, — то есть дают тот же результат,
# только длинной цепочкой: компрометация любой из них равна компрометации казны.
#
# Список расширяемый: добавить сюда пакет — значит признать, что его
# компрометация не ограничивается худшим исходом. Новая прямая зависимость в
# requirements.txt обязана попасть сюда же.
CRITICAL = (
    "aiohttp",
    "aiogram",
    "sqlalchemy",
    "aiosqlite",
    "asyncpg",
    "pydantic-settings",
    "apscheduler",
    "httpx",
    "pyyaml",
    "python-dotenv",
    "greenlet",
    "pytoniq",
    "pytoniq-core",
    "alembic",
)

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
TIMEOUT_SECONDS = 30


def _safe_console() -> None:
    """Не дать выводу уронить проверку из-за кодировки консоли.

    Скрипт работает и в CI, и в контейнере, и локально на Windows, где
    консоль по умолчанию cp1251 и не знает часть символов. Ошибка кодировки при
    печати диагностики превратилась бы в UnicodeEncodeError и потеряла бы
    сам отчёт — то есть проверка молча выглядела бы сломанной.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def normalize(name: str) -> str:
    """Имя пакета в виде PyPI: `pytoniq-core`, как в requirements.txt."""
    return name.strip().lower().replace("_", "-")


def pinned_versions(requirements: Path = REQUIREMENTS) -> dict[str, str]:
    """Версии из requirements.txt: только прямые зависимости с `==`."""
    versions: dict[str, str] = {}
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = normalize(name.split("[")[0])
        version = version.split("#")[0].strip()
        if name and version:
            versions[name] = version
    return versions


def fetch_artifacts(name: str, version: str) -> dict[str, str]:
    """Имя файла -> sha256, как сейчас отдаёт PyPI."""
    url = PYPI_JSON.format(name=name, version=version)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {
        entry["filename"]: entry["digests"]["sha256"]
        for entry in payload.get("urls", [])
        if entry.get("filename")
    }


def render_pin_file(entries: dict[str, dict[str, str]]) -> str:
    """Пин-файл: имя пакета, версия, затем артефакты с хешами."""
    lines = [
        "# Артефакты критичных зависимостей: sha256 каждого файла на PyPI.",
        "#",
        "# Смысл: `pkg==1.2.3` защищает от смены версии, но не от перезалива —",
        "# PyPI разрешает выложить другое содержимое под занятой версией, и pip",
        "# примет это молча. Любая зависимость ниже исполняется в том процессе,",
        "# что читает приватный ключ казначея из окружения, — компрометация",
        "# любой из них путь к полной потере казны.",
        "#",
        "# Проверяется и хеш, и САМО множество файлов: новый артефакт под той же",
        "# версией — тоже атака (достаточно добавить platform-specific wheel,",
        "# который pip предпочтёт).",
        "#",
        "# Обновление (вручную, после ревью новой версии):",
        "#     python -m scripts.dependency_hashes --write",
        "#",
        "# Проверка (в CI и при сборке на деплое):",
        "#     python -m scripts.dependency_hashes",
        "#",
        "# Перезалив НЕ проходит молча: pip install в этом проекте оставлен",
        "# без --require-hashes сознательно — он требует хешей на всю",
        "# транзитивную closure и ломает еженедельную автоматику Dependabot.",
        "# Защита точечная: только прямые зависимости, без транзитивной closure.",
        "",
    ]
    for name in sorted(entries):
        artifacts = entries[name]
        version_line = f"{name}=={artifacts['__version__']}"
        lines.append(version_line)
        for filename in sorted(k for k in artifacts if k != "__version__"):
            lines.append(f"#   {filename} sha256:{artifacts[filename]}")
        lines.append("")
    return "\n".join(lines)


def write_pin_file(requirements: Path = REQUIREMENTS, target: Path = PIN_FILE) -> int:
    """Перезаписать пин-файл актуальными данными PyPI."""
    versions = pinned_versions(requirements)
    entries: dict[str, dict[str, str]] = {}
    for name in CRITICAL:
        version = versions.get(name)
        if version is None:
            print(f"✗ {name} не найден в {requirements.name}", file=sys.stderr)
            return 2
        entries[name] = {"__version__": version, **fetch_artifacts(name, version)}
    target.write_text(render_pin_file(entries), encoding="utf-8")
    print(f"✓ пин-файл обновлён: {target.relative_to(ROOT)}")
    for name, artifacts in entries.items():
        print(f"  {artifacts['__version__']} {name}: {len(artifacts) - 1} артефактов")
    return 0


def parse_pin_file(path: Path = PIN_FILE) -> dict[str, dict[str, str]]:
    """Разобрать пин-файл в структуру {пакет: {имя_файла: sha256}}."""
    packages: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Артефакты лежат комментариями (иначе pip принял бы их за требования),
        # поэтому эта ветка обязана проверяться ДО общего пропуска комментариев.
        if line.startswith("#   "):
            filename, _, digest = line[4:].partition(" sha256:")
            if current is not None:
                current[filename.strip()] = digest.strip()
            continue
        if line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        current = {"__version__": version.strip()}
        packages[normalize(name)] = current
    return packages


def verify(requirements: Path = REQUIREMENTS, pin_path: Path = PIN_FILE) -> int:
    """Сверить пин-файл с тем, что PyPI отдаёт сейчас. 0 = совпало."""
    expected = parse_pin_file(pin_path)
    versions = pinned_versions(requirements)
    problems: list[str] = []

    for name, artifacts in sorted(expected.items()):
        version = artifacts.get("__version__", "")
        in_requirements = versions.get(name)
        if in_requirements is None:
            problems.append(f"{name}: есть в пин-файле, но нет в requirements.txt")
            continue
        if in_requirements != version:
            problems.append(
                f"{name}: версия разошлась — requirements.txt={in_requirements}, "
                f"пин-файл={version}. Обнови пин-файл: "
                f"python -m scripts.dependency_hashes --write"
            )
            continue
        try:
            actual = fetch_artifacts(name, version)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            problems.append(f"{name}=={version}: не удалось получить данные PyPI ({exc})")
            continue

        for filename, digest in sorted(
            (k, v) for k, v in artifacts.items() if k != "__version__"
        ):
            if filename not in actual:
                problems.append(f"{name}=={version}: артефакт {filename} исчез с PyPI")
            elif actual[filename] != digest:
                problems.append(
                    f"{name}=={version}: {filename} — ХЕШ НЕ СОВПАДАЕТ\n"
                    f"    ожидался sha256:{digest}\n"
                    f"    отдаётся  sha256:{actual[filename]}\n"
                    "    Перезалив артефакта под уже занятой версией. Не обновляй "
                    "пин-файл, разберись, что изменилось."
                )
        extra = set(actual) - {k for k in artifacts if k != "__version__"}
        for filename in sorted(extra):
            problems.append(
                f"{name}=={version}: на PyPI появился НОВЫЙ артефакт {filename}, "
                "его нет в пин-файле"
            )

    for problem in problems:
        print(f"✗ {problem}", file=sys.stderr)
    if problems:
        print(f"\nЦелостность критичных зависимостей не подтверждена ({len(problems)}).", file=sys.stderr)
        return 1
    print(f"✓ критичные зависимости совпадают с пин-файлом ({', '.join(sorted(expected))})")
    return 0


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="обновить пин-файл данными PyPI (после ревью новой версии)",
    )
    args = parser.parse_args(argv)
    return write_pin_file() if args.write else verify()


if __name__ == "__main__":
    raise SystemExit(main())
