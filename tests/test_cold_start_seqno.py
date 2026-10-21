"""Холодный старт кошелька: сверка seqno лайтсерверов и Toncenter.

Пока бот лежал, казначей мог сделать внешние переводы (Tonkeeper, руками):
лейтсерверы отстают от цепочки и отдают устаревший seqno, а подписанный
старым номером перевод уходит «в пустоту». Сверка читает тот же номер
через Toncenter (HTTP, всегда свежий) и сравнивает.

Источники читаются НЕЗАВИСИМО: мёртвые лайтсерверы (а они умирают отдельно
от Toncenter) не должны уносить сверку целиком — живой Toncenter всё равно
стоит спросить, а результат записать (на боевом mainnet 2026-10-21 первая
версия именно так и молчала).

Проверка не должна ничего ломать: сбой — предупреждение, кошелёк остаётся
рабочим.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import delete

from app.core.registry import COLD_START_SEQNO_KEY
from app.db import SessionLocal
from app.models import WatcherState
from app.ton_pay import state as ton_state
from app.ton_pay import wallet as wallet_mod


@pytest.fixture(autouse=True)
async def _clean_cold_start_marker():
    async with SessionLocal() as session:
        await session.execute(
            delete(WatcherState).where(WatcherState.key == COLD_START_SEQNO_KEY)
        )
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(
            delete(WatcherState).where(WatcherState.key == COLD_START_SEQNO_KEY)
        )
        await session.commit()


class _FakeWallet:
    """Кошелёк с заранее заданным seqno от лайтсерверов."""

    def __init__(self, seqno: int | None, error: Exception | None = None) -> None:
        self._seqno = seqno
        self._error = error

    async def get_seqno(self) -> int:
        if self._error is not None:
            raise self._error
        return int(self._seqno)


def _patch_http(monkeypatch: pytest.MonkeyPatch, value: int | Exception) -> None:
    """Подменить чтение seqno через Toncenter: значение или исключение."""
    from app.ton_pay import http_channel

    async def fake_seqno(wallet, *, address=None) -> int:
        if isinstance(value, Exception):
            raise value
        return int(value)

    monkeypatch.setattr(http_channel, "http_get_wallet_seqno", fake_seqno)


async def _marker() -> dict | None:
    import json

    async with SessionLocal() as session:
        row = await session.get(WatcherState, COLD_START_SEQNO_KEY)
        if row is None:
            return None
        return json.loads(row.value)


async def test_cold_start_records_matching_seqno(monkeypatch, caplog) -> None:
    """Номера совпали — метка записана, тревоги нет."""
    _patch_http(monkeypatch, 25)
    with caplog.at_level(logging.INFO, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(_FakeWallet(25))

    marker = await _marker()
    assert marker is not None
    assert marker["lite"] == 25 and marker["http"] == 25
    assert marker["delta"] == 0
    assert not marker.get("error")
    assert not any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_warns_on_divergence(monkeypatch, caplog) -> None:
    """Расхождение больше 1 — предупреждение и метка с дельтой."""
    _patch_http(monkeypatch, 30)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(_FakeWallet(24))

    marker = await _marker()
    assert marker is not None
    assert marker["delta"] == -6
    assert any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_tolerates_small_divergence(monkeypatch, caplog) -> None:
    """Расхождение в 1 — не тревога: гонка фиксации блока легитимна."""
    _patch_http(monkeypatch, 26)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(_FakeWallet(25))

    marker = await _marker()
    assert marker is not None and marker["delta"] == -1
    assert not any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_dead_liteservers_still_records_http_side(monkeypatch, caplog) -> None:
    """Мёртвые лайтсерверы НЕ уносят сверку: живой Toncenter опрошен.

    Регресс боевого mainnet 2026-10-21: источники читались одним
    try-блоком, и падение лайтсерверной стороны означала «сверки не было»
    — метка не появлялась, в /treasury было пусто.
    """
    _patch_http(monkeypatch, 27)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(
            _FakeWallet(None, error=RuntimeError("no alive peers"))
        )

    marker = await _marker()
    assert marker is not None, "метка должна появиться даже при мёртвых лайтсерверах"
    assert marker["lite"] is None
    assert marker["http"] == 27
    assert marker["delta"] is None
    assert "no alive peers" in (marker.get("error") or "")
    assert any("неполная" in record.message for record in caplog.records)


async def test_cold_start_dead_toncenter_still_records_liteserver_side(monkeypatch, caplog) -> None:
    """Молчащий Toncenter — тоже не «нет сверки»: лайтсерверная сторона записана."""
    _patch_http(monkeypatch, RuntimeError("toncenter молчит"))
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(_FakeWallet(25))

    marker = await _marker()
    assert marker is not None
    assert marker["lite"] == 25
    assert marker["http"] is None
    assert marker["delta"] is None
    assert "toncenter" in (marker.get("error") or "")


async def test_cold_start_both_sources_dead_still_records_reason(monkeypatch, caplog) -> None:
    """Оба источника молчат — причина всё равно попадает в метку."""
    _patch_http(monkeypatch, RuntimeError("toncenter молчит"))
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(
            _FakeWallet(None, error=RuntimeError("no alive peers"))
        )

    marker = await _marker()
    assert marker is not None
    assert marker["lite"] is None and marker["http"] is None
    error = marker.get("error") or ""
    assert "лайтсерверы" in error and "toncenter" in error


async def test_cold_start_never_breaks_startup(monkeypatch, caplog) -> None:
    """Сбой проверки не выходит наружу: кошелёк остаётся рабочим."""
    _patch_http(monkeypatch, RuntimeError("toncenter молчит"))
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod.cold_start_seqno_check(
            _FakeWallet(None, error=RuntimeError("no alive peers"))
        )

    assert await _marker() is not None


async def test_ensure_cold_start_check_runs_once_per_process(monkeypatch) -> None:
    """Проба без выплат: сверка случается один раз на процесс."""
    _patch_http(monkeypatch, 27)
    calls: list[int] = []

    async def fake_get_wallet():
        # Настоящий get_wallet() сам ставит флаг, когда строит кошелёк.
        calls.append(1)
        ton_state._cold_start_checked = True
        return _FakeWallet(27)

    monkeypatch.setattr(wallet_mod, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(ton_state, "_cold_start_checked", False, raising=False)
    monkeypatch.setattr(ton_state, "_cold_start_next_attempt_at", None, raising=False)

    await wallet_mod.ensure_cold_start_check()
    await wallet_mod.ensure_cold_start_check()

    assert len(calls) == 1, "кошелёк не должен собираться каждый цикл обслуживания"


async def test_ensure_cold_start_check_backs_off_after_failure(monkeypatch) -> None:
    """Кошелёк не собрался — следующая попытка не раньше чем через 10 минут."""
    calls: list[int] = []

    async def failing_wallet():
        calls.append(1)
        raise RuntimeError("лайтсерверы мертвы")

    monkeypatch.setattr(wallet_mod, "get_wallet", failing_wallet)
    monkeypatch.setattr(ton_state, "_cold_start_checked", False, raising=False)
    monkeypatch.setattr(ton_state, "_cold_start_next_attempt_at", None, raising=False)

    await wallet_mod.ensure_cold_start_check()
    await wallet_mod.ensure_cold_start_check()

    assert len(calls) == 1, "после неудачи должна быть пауза, а не попытка каждый цикл"
    assert ton_state._cold_start_next_attempt_at is not None


def test_treasury_line_shows_divergence() -> None:
    """Расхождение в отчёте /treasury видно и с дельтой, и временем."""
    from app.ton_pay.treasury import _cold_start_lines

    lines = _cold_start_lines(
        '{"lite": 24, "http": 30, "delta": -6, "at": "2026-10-21T09:00:00+00:00", "error": null}'
    )
    assert len(lines) == 1
    assert "24" in lines[0] and "30" in lines[0]
    assert "-6" in lines[0]
    assert "внешние переводы" in lines[0]


def test_treasury_line_reports_match() -> None:
    """Совпадение — спокойная строка без предупреждения."""
    from app.ton_pay.treasury import _cold_start_lines

    lines = _cold_start_lines(
        '{"lite": 25, "http": 25, "delta": 0, "at": "2026-10-21T09:00:00+00:00", "error": null}'
    )
    assert len(lines) == 1
    assert "совпал" in lines[0]
    assert "⚠️" not in lines[0]


def test_treasury_line_shows_incomplete_check() -> None:
    """Неполная сверка — видна как «сверка неполная», с причиной."""
    from app.ton_pay.treasury import _cold_start_lines

    lines = _cold_start_lines(
        '{"lite": null, "http": 27, "delta": null, "at": "2026-10-21T09:00:00+00:00", '
        '"error": "лайтсерверы: RuntimeError: no alive peers"}'
    )
    assert len(lines) == 1
    assert "неполная" in lines[0]
    assert "no alive peers" in lines[0]


def test_treasury_line_tolerates_broken_marker() -> None:
    """Битая метка не роняет отчёт: нечитаемая — молча, неполная — видна."""
    from app.ton_pay.treasury import _cold_start_lines

    assert _cold_start_lines(None) == []
    assert _cold_start_lines("не json") == []
    # Разобранная, но бессмысленная метка — это «сверка неполная», а не тишина.
    lines = _cold_start_lines('{"lite": "нет числа"}')
    assert len(lines) == 1 and "неполная" in lines[0]
