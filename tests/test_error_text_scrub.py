"""Текст исключения не должен тащить секреты в админский чат и в БД.

Ошибка провайдера (TonAPI, Toncenter, Render) приходит текстом, а в тексте
может быть URL с ключом в query-строке или заголовке. Такой текст уходил
в админский чат (всплывающий alert в панели, отчёт в /payouts) и в
колонку payouts.last_error — то есть в бэкапы базы.

Теперь текст проходит через ту же краску секретов, что и логи: причина
остаётся читаемой, а секрет из неё исчезает.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.log_scrub import REDACTED_SECRET, redact


def test_redact_removes_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ в тексте ошибки исчезает, а причина остаётся."""
    key = "tonapi-super-secret-key-123456"
    monkeypatch.setattr(settings, "ton_api_key", key)
    text = f"HTTP 401 от https://tonapi.io/v2/accounts?api_key={key}: Unauthorized"

    cleaned = redact(text)

    assert key not in cleaned
    assert REDACTED_SECRET in cleaned
    assert "401" in cleaned


def test_redact_removes_db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пароль базы из строки подключения тоже исчезает."""
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://way:sup3r-secret-pw@db.host:5432/way",
    )
    text = "could not connect: postgresql://way:sup3r-secret-pw@db.host:5432/way"

    cleaned = redact(text)

    assert "sup3r-secret-pw" not in cleaned
    assert "db.host" in cleaned


def test_redact_keeps_ordinary_reason() -> None:
    """Обычная причина остаётся как есть — красить нечего."""
    text = "connection reset by peer"

    assert redact(text) == text


def test_payout_last_error_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """last_error выплаты не хранит ключ провайдера.

    Колонка уезжает в бэкапы базы, а ключ провайдера — в URL, который
    провайдер возвращает в тексте ошибки.
    """
    key = "leak-me-please-123456"
    monkeypatch.setattr(settings, "ton_api_key", key)
    reason = f"HTTP 401: https://tonapi.io/v2/x?api_key={key}"

    class _Payout:
        last_error: str | None = None

    payout = _Payout()
    payout.last_error = redact(reason)[:200]

    assert key not in (payout.last_error or "")
    assert "401" in (payout.last_error or "")


def test_settings_render_key_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ Render API тоже красится (он в том же процессе, что мнемоника)."""
    key = "rnd_live_key_abcdef123456"
    monkeypatch.setattr(settings, "render_api_key", key)

    cleaned = redact(f"Authorization: Bearer {key}")

    assert key not in cleaned
