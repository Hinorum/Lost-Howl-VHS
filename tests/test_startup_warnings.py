"""Предупреждения на старте: пути самолечения и потолок ставок.

Тумблеры (TON_PROVIDER_REBUILD, HTTP_TONAPI_FALLBACK) в бою включаются
вручную, а в манифесте render.yaml стоят нейтральными: Blueprint Sync
перезапишет ими живое состояние вслепую. После такого синка канал остаётся
без второго вещателя и без пересборки пула — и это видно только по
симптомам (выплаты встали на 429 от Toncenter).

Потолок одной ставки (STAKE_MAX_TON=0) — про деньги: одна ставка может
унести весь фонд дня.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.main import _self_healing_warnings


@pytest.fixture(autouse=True)
def _money_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)


def test_warns_about_disabled_self_healing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оба пути самолечения выключены — бот говорит об этом на старте."""
    monkeypatch.setattr(settings, "http_tonapi_fallback", False)
    monkeypatch.setattr(settings, "ton_provider_rebuild", False)

    warnings = _self_healing_warnings()
    joined = " ".join(warnings)
    assert "HTTP_TONAPI_FALLBACK" in joined
    assert "TON_PROVIDER_REBUILD" in joined


def test_silent_when_self_healing_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включённые пути самолечения не упоминаются."""
    monkeypatch.setattr(settings, "http_tonapi_fallback", True)
    monkeypatch.setattr(settings, "ton_provider_rebuild", True)
    monkeypatch.setattr(settings, "stake_max_ton", 5.0)

    assert _self_healing_warnings() == []


def test_warns_about_missing_stake_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потолка ставок нет — об этом стоит сказать: одна ставка уносит фонд."""
    monkeypatch.setattr(settings, "stake_max_ton", 0.0)

    joined = " ".join(_self_healing_warnings())
    assert "STAKE_MAX_TON" in joined


def test_no_warnings_without_money(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без денег эти замечания неуместны."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(settings, "stake_max_ton", 0.0)

    assert _self_healing_warnings() == []
