"""Резервные копии базы: как уходит пароль в pg_dump и как живёт ротация.

Главное свойство здесь одно и оно про утечку: пароль боевой базы не должен
попадать в argv. argv процесса виден любому пользователю системы (`ps aux`,
`/proc/<pid>/cmdline`) и попадает в выводы сторонних мониторов. libpq штатно
читает пароль из PGPASSWORD, поэтому способ доставки меняется без изменения
поведения — если, конечно, DSN при этом не потерял параметры провайдера.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app import backups
from app.config import settings


def _capture_pg_dump(monkeypatch) -> dict:
    """Подмена subprocess.run: возвращает зафиксированные argv и окружение."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


# ---------- Пароль не уходит в argv ----------


def test_pg_password_never_lands_in_argv(monkeypatch, tmp_path: Path) -> None:
    """Секрет базы не должен быть в командной строке pg_dump.

    Проверяется именно argv: окружение секрет содержит по необходимости
    (его читает сам процесс), а вот список аргументов виден извне.
    """
    secret = "p@ssw0rd-with-ur!chars"
    monkeypatch.setattr(
        settings, "database_url", f"postgresql://postgres.x:{secret}@db.host:5432/postgres"
    )
    captured = _capture_pg_dump(monkeypatch)
    backups._pg_dump_sync(tmp_path / "dump.sql")

    joined = " ".join(captured["argv"])
    assert secret not in joined
    assert "secret" not in joined.lower() or secret not in joined
    # Пароль уехал в PGPASSWORD, а остальной DSN остался рабочим.
    assert captured["env"]["PGPASSWORD"] == secret
    assert "db.host:5432/postgres" in joined
    assert "postgres.x@" in joined


def test_pg_dsn_keeps_provider_params(monkeypatch, tmp_path: Path) -> None:
    """Чистится ТОЛЬКО пароль — параметры провайдера терять нельзя.

    У Supabase в строке подключения живут sslmode и sslrootcert. Выбросив их,
    pg_dump уехал бы без проверки сертификата (или вообще не подключился), а
    ошибка ушла бы в stderr — и дамп был бы объявлен успешным.
    """
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:secret@db.host:5432/postgres?sslmode=verify-full&sslrootcert=certs/ca.pem",
    )
    captured = _capture_pg_dump(monkeypatch)
    backups._pg_dump_sync(tmp_path / "dump.sql")

    joined = " ".join(captured["argv"])
    assert "secret" not in joined
    assert "sslmode=verify-full" in joined
    assert "sslrootcert=certs/ca.pem" in joined


def test_pg_password_is_percent_decoded() -> None:
    """Спецсимволы пароля приходят в DSN percent-encoded — PGPASSWORD ждёт сырой.

    У реального пароля Supabase регулярно есть «@» и «/», которые DSN
    кодирует. Отдав PGPASSWORD как есть (из URL), pg_dump получил бы мусор и
    упал бы с «password authentication failed» — отказ от argv превратился бы
    в отказ от дампа.
    """
    env, clean = backups._pg_env("postgresql://u:p%40ss%2Fword@db.host:5432/postgres")
    assert env["PGPASSWORD"] == "p@ss/word"
    assert "p%40ss" not in clean


def test_pg_env_without_password_adds_no_pgpassword() -> None:
    """DSN без пароля (peer/локальный сокет) не должен получать PGPASSWORD.

    Иначе мы бы подставили PGPASSWORD из чужой переменной окружения и сломали
    аутентификацию, которой не было.
    """
    env, clean = backups._pg_env("postgresql:///postgres?host=/var/run/postgresql")
    assert "PGPASSWORD" not in env
    assert "host=/var/run/postgresql" in clean


def test_pg_env_preserves_ambient_environment() -> None:
    """Окружение не заменяется целиком: PATH и прочее остаются на месте.

    pg_dump — не один: он зовёт системные библиотеки, и подмена окружения на
    «только PGPASSWORD» обнулила бы PATH вместе с остальным.
    """
    env, _ = backups._pg_env("postgresql://u:s@db.host:5432/postgres")
    assert env.get("PATH") == os.environ.get("PATH")


# ---------- Ротация ----------


def test_prune_keeps_exactly_keep_newest(tmp_path: Path) -> None:
    """Хранится ровно keep свежих копий, хвост уходит — диск не растёт вечно."""
    for index in range(backups.KEEP + 3):
        (tmp_path / f"backup-202401{index:02d}-0000.db").write_bytes(b"x")
    removed = backups._prune(tmp_path, backups.KEEP)
    assert removed == 3
    survivors = sorted(path.name for path in tmp_path.iterdir())
    assert len(survivors) == backups.KEEP
    # Удалены самые старые, самый свежий на месте.
    assert "backup-20240100-0000.db" not in survivors
    assert survivors[-1] == f"backup-202401{backups.KEEP + 2:02d}-0000.db"


def test_prune_respects_smaller_keep_than_stored(tmp_path: Path) -> None:
    """keep меньше, чем уже накоплено, — тоже чистит, а не «не трогаем»."""
    for index in range(10):
        (tmp_path / f"backup-202401{index:02d}-0000.db").write_bytes(b"x")
    assert backups._prune(tmp_path, 3) == 7
    assert len(list(tmp_path.iterdir())) == 3


def test_prune_on_empty_directory_is_noop(tmp_path: Path) -> None:
    """Пустой каталог — не ошибка: ротировать нечего, падать тоже не от чего."""
    assert backups._prune(tmp_path, backups.KEEP) == 0


def test_prune_ignores_foreign_files(tmp_path: Path) -> None:
    """Чужие файлы в каталоге копий не трогаются.

    Ротация удаляет строго backup-*: иначе она рано или поздно съест что-то,
    что положили рядом руками (например, разобранный инцидент).
    """
    (tmp_path / "keep-me.txt").write_bytes(b"x")
    (tmp_path / "backup-20240101-0000.db").write_bytes(b"x")
    backups._prune(tmp_path, 1)
    assert (tmp_path / "keep-me.txt").exists()


async def test_backup_now_skipped_without_pg_dump(monkeypatch) -> None:
    """Нет pg_dump в окружении — дамп пропускается с внятным логом, не падает.

    Иначе Render-контейнер без postgresql-client падал бы на каждом суточном
    бэкапе, и отсутствие копий выяснилось бы в день, когда они понадобятся.
    """
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@db.host:5432/postgres")
    monkeypatch.setattr(backups.shutil, "which", lambda _name: None)
    assert await backups.backup_now() is None


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@h:5432/db",
        "postgresql://u:p@h:5432/db",
        # Схема с драйвером — тоже Postgres. Раньше бэкап на такой DSN
        # выключался молча: is_postgres() давало False, sqlite_file_path() —
        # тоже None, и backup_now() возвращал None без записи в лог.
        "postgresql+asyncpg://u:p@h:5432/db",
    ],
)
def test_is_postgres_detects_every_postgres_scheme(monkeypatch, url: str) -> None:
    monkeypatch.setattr(settings, "database_url", url)
    assert backups.is_postgres() is True


def test_is_postgres_rejects_sqlite(monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./data/x.db")
    assert backups.is_postgres() is False