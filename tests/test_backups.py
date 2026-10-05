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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

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


# ---------- Целостность копии и отметка успеха ----------


async def test_verify_rejects_empty_file(tmp_path: Path) -> None:
    """Пустой файл — не бэкап, даже если pg_dump не упал с ненулевым кодом."""
    empty = tmp_path / "backup-20240101-0400.db"
    empty.touch()
    with pytest.raises(RuntimeError, match="пустой"):
        await backups._verify_backup(empty)


async def test_verify_rejects_unreadable_sqlite_copy(tmp_path: Path) -> None:
    """Недописанный файл не читается как база — это провал с внятной причиной.

    Без перехвата sqlite3.DatabaseError уехал бы из задачи планировщика мимо
    алерта: хранитель увидел бы «бэкап не удался», не зная, что копия есть и
    она мусор.
    """
    broken = tmp_path / "backup-20240101-0400.db"
    broken.write_bytes(b"\x00" * 4096)

    with pytest.raises(RuntimeError, match="не читается"):
        await backups._verify_backup(broken)


async def test_verify_rejects_sqlite_copy_without_tables(tmp_path: Path) -> None:
    """Валидная база без единой таблицы — не бэкап."""
    import sqlite3

    broken = tmp_path / "backup-20240101-0400.db"
    with sqlite3.connect(str(broken)) as conn:
        conn.execute("create table t(x)")
        conn.execute("drop table t")

    with pytest.raises(RuntimeError, match="без таблиц"):
        await backups._verify_backup(broken)


async def test_verify_rejects_dump_without_pgdmp_magic(tmp_path: Path) -> None:
    """Дамп без заголовка PGDMP pg_restore не примет — считаем это провалом."""
    broken = tmp_path / "backup-20240101-0400.dump"
    broken.write_bytes(b"not a pg dump at all")
    with pytest.raises(RuntimeError, match="PGDMP"):
        await backups._verify_backup(broken)


async def test_verify_accepts_real_sqlite_copy(tmp_path: Path) -> None:
    """Настоящая копия проходит проверку."""
    import sqlite3

    good = tmp_path / "backup-20240101-0400.db"
    with sqlite3.connect(str(good)) as conn:
        conn.execute("create table t(x)")
        conn.execute("insert into t values (1)")
    await backups._verify_backup(good)


async def test_successful_backup_marks_last_ok(monkeypatch, tmp_path: Path) -> None:
    """Успешный бэкап оставляет отметку — тревога свежести будет чем питаться."""
    import sqlite3

    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    source = tmp_path / "live.db"
    with sqlite3.connect(str(source)) as conn:
        conn.execute("create table t(x)")
        conn.execute("insert into t values (1)")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}")
    monkeypatch.setattr(backups, "sqlite_file_path", lambda: source)

    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
        )
        await session.commit()

    try:
        await backups.backup_now()

        async with SessionLocal() as session:
            value = (
                await session.execute(
                    select(WatcherState.value).where(WatcherState.key == BACKUP_LAST_OK_KEY)
                )
            ).scalar_one_or_none()
        assert value is not None, "успешный бэкап обязан оставить отметку для тревоги"
        assert backups.sqlite_file_path() is not None
    finally:
        async with SessionLocal() as session:
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
            )
            await session.commit()


async def test_failed_backup_leaves_no_success_mark(monkeypatch, tmp_path: Path) -> None:
    """Провал бэкапа не должен выглядеть как успешный."""
    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    source = tmp_path / "live.db"
    source.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@db:5432/postgres")
    monkeypatch.setattr(backups.shutil, "which", lambda _name: "/usr/bin/pg_dump")

    def _fake_dump(dest: Path) -> None:
        # pg_dump «успешно» создал файл — но мусорный.
        dest.write_bytes(b"garbage")

    monkeypatch.setattr(backups, "_pg_dump_sync", _fake_dump)

    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
        )
        await session.commit()

    try:
        with pytest.raises(RuntimeError, match="PGDMP"):
            await backups.backup_now()

        async with SessionLocal() as session:
            value = (
                await session.execute(
                    select(WatcherState.value).where(WatcherState.key == BACKUP_LAST_OK_KEY)
                )
            ).scalar_one_or_none()
        assert value is None, "провалившийся бэкап не должен оставлять отметку успеха"
    finally:
        async with SessionLocal() as session:
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
            )
            await session.commit()


# ---------- Тревога свежести ----------


async def test_stale_backup_mark_raises_problem() -> None:
    """Нет успешного бэкапа дольше порога — проблема видна в /health и /ops."""
    from app import ops
    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    stale = (datetime.now(UTC) - timedelta(hours=40)).isoformat()
    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
        )
        session.add(WatcherState(key=BACKUP_LAST_OK_KEY, value=stale))
        await session.commit()
    try:
        problems = await ops.check_anomalies(bot=None)
        assert any("бэкап" in problem for problem in problems), problems
    finally:
        async with SessionLocal() as session:
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
            )
            await session.commit()


async def test_backup_alarm_names_the_offsite_chain(monkeypatch) -> None:
    """Тревога о бэкапах различает локальный pg_dump и GitHub-воркфлоу.

    Оба называются db-backup, и текст тревоги вёл только в /ops — то есть к
    локальному крону Render. Между тем локальные копии лежат на эфемерном
    диске, а переживает деплой только цепочка GitHub Actions, которую бот не
    видит вообще. Разбор, начатый по старому тексту, молча упирался в не ту
    сторону: локальный бэкап был исправен, а офсайтовых копий не было двое
    суток.
    """
    from app import ops
    from app.core.registry import ALERT_BACKUP_KEY, BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    # Админ нужен, иначе notify_admins выйдет на «админов нет» и тревога некуда
    # слать; метка троттлинга — иначе предыдущий тест этого же файла поставил
    # её минуту назад и повторная отправка была бы подавлена как «уже сказали».
    monkeypatch.setattr(ops.settings, "admin_ids", "42")
    stale = (datetime.now(UTC) - timedelta(hours=40)).isoformat()
    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(
                WatcherState.key.in_([BACKUP_LAST_OK_KEY, ALERT_BACKUP_KEY])
            )
        )
        session.add(WatcherState(key=BACKUP_LAST_OK_KEY, value=stale))
        await session.commit()
    try:
        sent: list[str] = []

        class _Bot:
            async def send_message(self, chat_id: int, text: str) -> None:
                sent.append(text)

        ops._problems_in_flight.clear()
        ops._problem_entry.clear()
        await ops.check_anomalies(_Bot())
        joined = "\n".join(sent)
        # Оба контура названы поимённо. Проверка на «есть слово GitHub» держалась
        # бы на одной фразе и пропускала главное: что локальная джоба и офсайтовая
        # разведены. Именно их разведение и было нужно — иначе «проверь джобу
        # db-backup» отправляло в Render, где всё исправно.
        assert "Render" in joined, joined
        assert "GitHub Actions" in joined, joined
        assert "db-backup" in joined, joined
        assert "BACKUP_PASSPHRASE" in joined, joined
    finally:
        ops._problems_in_flight.clear()
        ops._problem_entry.clear()
        async with SessionLocal() as session:
            await session.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([BACKUP_LAST_OK_KEY, ALERT_BACKUP_KEY])
                )
            )
            await session.commit()


async def test_fresh_backup_mark_is_quiet() -> None:
    """Свежий бэкап тревоги не вызывает: успешный бэкап раз в сутки — норма."""
    from app import ops
    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    fresh = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
        )
        session.add(WatcherState(key=BACKUP_LAST_OK_KEY, value=fresh))
        await session.commit()
    try:
        async with SessionLocal() as session:
            await ops._check_backup_freshness(session, None)
    finally:
        async with SessionLocal() as session:
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
            )
            await session.commit()


async def test_never_backed_up_is_reported() -> None:
    """Отметки нет вовсе — тоже проблема: бэкап ни разу не удался.

    Свежий процесс без единой отметки — это и есть случай «бэкап не работает»,
    и он не должен выглядеть как «просто ещё не было времени». Иначе первый же
    сломанный pg_dump на боевой базе был бы незаметен ровно до того дня, когда
    базу придётся восстанавливать.
    """
    from app import ops
    from app.core.registry import BACKUP_LAST_OK_KEY
    from app.db import SessionLocal
    from app.models import WatcherState

    async with SessionLocal() as session:
        await session.execute(
            WatcherState.__table__.delete().where(WatcherState.key == BACKUP_LAST_OK_KEY)
        )
        await session.commit()

    ops._problems_in_flight.clear()
    async with SessionLocal() as session:
        await ops._check_backup_freshness(session, None)

    assert any("бэкап" in problem for problem in ops._problems_in_flight), (
        "отсутствие отметки бэкапа обязано попасть в список проблем"
    )


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
