"""Миграции при подъёме: потолок по времени и внятная ошибка.

Схема применяется в процессе бота (`alembic upgrade head` при старте). Зависшая
миграция — обычно на блокировке таблицы, которую держит старый инстанс, —
держала бы контейнер на старте вечно: /alive не отвечает, Render перезапускает,
и причина не видна.

Поэтому у alembic-команды есть потолок, а по его истечении — ошибка с
пояснением, что делать.
"""

from __future__ import annotations

import subprocess

import pytest

from app import db


def test_alembic_has_timeout(monkeypatch) -> None:
    """У alembic-команды есть потолок по времени."""
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    db._run_alembic("upgrade", "head")

    assert captured.get("timeout") == db._ALEMBIC_TIMEOUT_SECONDS


def test_alembic_timeout_raises_readable_error(monkeypatch) -> None:
    """Истечение потолка — внятная ошибка, а не traceback таймаута."""
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(db.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        db._run_alembic("upgrade", "head")

    text = str(excinfo.value)
    assert "не уложился" in text
    assert "alembic upgrade head" in text
    # Подсказка, что делать: миграция зависла на блокировке.
    assert "вручную" in text
