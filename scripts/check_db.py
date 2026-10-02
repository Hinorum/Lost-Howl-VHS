"""Прогнать alembic upgrade head + alembic check в правильном порядке.

Тонкое место: локально `alembic check` без `alembic upgrade head` валит
ошибкой «Target database is not up to date» на свежей БД — это НЕ баг
проекта, а сигнал, что схема не накатывалась. CI делает их последовательно
(.github/workflows/ci.yml, job «test» → «Alembic drift check»), но локально
легко забыть. Этот скрипт — короткая обёртка с понятным итогом.

Использование:
    python -m scripts.check_db

Опциональные переменные окружения:
    DATABASE_URL — если не задан, берётся значение из .env (через pydantic-settings),
        иначе — sqlite в ./data/check.db.
    CHECK_KEEP_DB — если задано «0» / «false», временная БД удаляется после прогона;
        по умолчанию сохраняется (полезно для отладки через `alembic history`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _read_default_database_url() -> str:
    """Поднять DATABASE_URL из .env, если он не задан в окружении.

    Без импорта pydantic-settings (он сам подгрузит .env), чтобы скрипт
    работал и без рантайма бота. Если .env нет — путь по умолчанию.
    """
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DATABASE_URL":
                quoted = value.strip().strip('"').strip("'")
                if quoted:
                    os.environ["DATABASE_URL"] = quoted
                    return quoted
    fallback = "sqlite+aiosqlite:///./data/check.db"
    os.environ["DATABASE_URL"] = fallback
    return fallback


def _run(args: list[str]) -> None:
    """Запустить подпроцесс и пробросить код возврата."""
    print(f"\n$ {' '.join(args)}")
    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        print(
            f"\n❌ Шаг {' '.join(args)} упал с кодом {result.returncode}.",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def _looks_temporary(url: str) -> bool:
    """Считать ли файл БД временным (можно удалить после прогона)."""
    if not url.startswith("sqlite"):
        return False
    return any(token in url for token in ("/data/check.db", "/check.db", ":memory:"))


def main() -> None:
    database_url = _read_default_database_url()
    print(f">>> DATABASE_URL={database_url}")

    keep = os.environ.get("CHECK_KEEP_DB", "1").lower() not in ("0", "false", "no")

    # 1. upgrade head — привести базу к актуальному состоянию.
    _run([sys.executable, "-m", "alembic", "upgrade", "head"])

    # 2. check — проверить, что модели и миграции синхронны.
    _run([sys.executable, "-m", "alembic", "check"])

    print("\n[OK] Схема БД в порядке: upgrade head выполнен, alembic check прошёл.")

    if not keep and _looks_temporary(database_url):
        target = database_url.replace("sqlite+aiosqlite:///", "")
        path = Path(target)
        for variant in (path, path.with_suffix(path.suffix + "-journal")):
            if variant.exists():
                variant.unlink()
                print(f"  удалён {variant}")


if __name__ == "__main__":
    main()
