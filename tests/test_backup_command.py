"""/backup — ручной снимок БД из Телеграма по требованию хранителя.

Автоматика снимков уже есть (cron db-backup, снимок на старте процесса,
офсайтовый GitHub Actions db-backup) — команда нужна для репетиции
восстановления на стенде и страховки перед ручными операциями.

Что здесь ловится: отчёт честен (имя файла и размер есть только у
успеха), эфемерность data/backups на Render не скрывается, а отказ
(pg_dump недоступен / сбой дампа) не выглядит как успех.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.handlers.admin import cmd_backup


def _message(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=user_id),
        text="/backup",
        answer=AsyncMock(),
    )


def _admin_id() -> int:
    return next(iter(settings.admin_id_set))


async def test_backup_success_reports_name_size_and_ephemeral_warning(
    tmp_path, monkeypatch
) -> None:
    dest = tmp_path / "backup-20261008-1200.dump"
    dest.write_bytes(b"x" * 2048)
    fake = AsyncMock(return_value=dest)
    monkeypatch.setattr("app.backups.backup_now", fake)

    message = _message(_admin_id())
    await cmd_backup(message)

    fake.assert_awaited_once()
    assert message.answer.await_count == 2  # сначала «занято», потом отчёт
    report = message.answer.call_args_list[-1].args[0]
    assert "Снимок БД" in report
    assert dest.name in report
    assert "2 КБ" in report
    # Честность: эфемерность на Render и долговечная копия Actions сказаны.
    assert "эфемерн" in report
    assert "db-backup" in report
    # Кассеты в дампе — прямая помощь к репетиции восстановления.
    assert "story_cassettes" in report


async def test_backup_none_is_reported_as_failure(monkeypatch) -> None:
    monkeypatch.setattr("app.backups.backup_now", AsyncMock(return_value=None))

    message = _message(_admin_id())
    await cmd_backup(message)

    report = message.answer.call_args_list[-1].args[0]
    assert "не создан" in report
    assert "Снимок БД:" not in report, "отказ не должен выглядеть как успех"


async def test_backup_exception_is_reported_as_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.backups.backup_now", AsyncMock(side_effect=RuntimeError("pg_dump blew up"))
    )

    message = _message(_admin_id())
    await cmd_backup(message)

    report = message.answer.call_args_list[-1].args[0]
    assert "не удался" in report
    assert "Снимок БД:" not in report
