"""Холодный старт кошелька: сверка seqno лайтсерверов и Toncenter.

Пока бот лежал, казначей мог сделать внешние переводы (Tonkeeper, руками):
лейтсерверы отстают от цепочки и отдают устаревший seqno, а подписанный
старым номером перевод уходит «в пустоту». Сверка читает тот же номер
через Toncenter (HTTP, всегда свежий) и сравнивает.

Проверка не должна ничего ломать: сбой (мёртвые лайтсерверы, молчащий
Toncenter) — предупреждение, кошелёк остаётся рабочим.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import delete

from app.core.registry import COLD_START_SEQNO_KEY
from app.db import SessionLocal
from app.models import WatcherState
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

    def __init__(self, seqno: int) -> None:
        self._seqno = seqno

    async def get_seqno(self) -> int:
        return self._seqno


def _patch_http(monkeypatch: pytest.MonkeyPatch, value: int | None) -> None:
    """Подменить чтение seqno через Toncenter: значение или исключение."""
    from app.ton_pay import http_channel

    async def fake_seqno(wallet, *, address=None) -> int:
        if isinstance(value, Exception):
            raise value
        return int(value)

    monkeypatch.setattr(http_channel, "http_get_wallet_seqno", fake_seqno)


async def _marker() -> dict | None:
    async with SessionLocal() as session:
        row = await session.get(WatcherState, COLD_START_SEQNO_KEY)
        if row is None:
            return None
        import json

        return json.loads(row.value)


async def test_cold_start_records_matching_seqno(monkeypatch, caplog) -> None:
    """Номера совпали — метка записана, тревоги нет."""
    _patch_http(monkeypatch, 25)
    with caplog.at_level(logging.INFO, logger="app.ton_pay.wallet"):
        await wallet_mod._cold_start_seqno_check(_FakeWallet(25))

    marker = await _marker()
    assert marker is not None
    assert marker["lite"] == 25 and marker["http"] == 25
    assert marker["delta"] == 0
    assert not any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_warns_on_divergence(monkeypatch, caplog) -> None:
    """Расхождение больше 1 — предупреждение и метка с дельтой."""
    _patch_http(monkeypatch, 30)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod._cold_start_seqno_check(_FakeWallet(24))

    marker = await _marker()
    assert marker is not None
    assert marker["delta"] == -6
    assert any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_tolerates_small_divergence(monkeypatch, caplog) -> None:
    """Расхождение в 1 — не тревога: гонка фиксации блока легитимна."""
    _patch_http(monkeypatch, 26)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod._cold_start_seqno_check(_FakeWallet(25))

    marker = await _marker()
    assert marker is not None and marker["delta"] == -1
    assert not any("расхождение" in record.message for record in caplog.records)


async def test_cold_start_failure_never_breaks_startup(monkeypatch, caplog) -> None:
    """Сбой проверки не выходит наружу: кошелёк остаётся рабочим."""
    _patch_http(monkeypatch, RuntimeError("toncenter молчит"))
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod._cold_start_seqno_check(_FakeWallet(25))

    assert await _marker() is None
    assert any("не удалась" in record.message for record in caplog.records)


async def test_cold_start_liteserver_failure_never_breaks_startup(
    monkeypatch, caplog
) -> None:
    """Мёртвые лайтсерверы — тоже предупреждение, а не сбой старта."""

    class _Broken:
        async def get_seqno(self) -> int:
            raise RuntimeError("no alive peers")

    _patch_http(monkeypatch, 25)
    with caplog.at_level(logging.WARNING, logger="app.ton_pay.wallet"):
        await wallet_mod._cold_start_seqno_check(_Broken())

    assert await _marker() is None
    assert any("не удалась" in record.message for record in caplog.records)


def test_treasury_line_shows_divergence() -> None:
    """Расхождение в отчёте /treasury видно и с дельтой, и временем."""
    from app.ton_pay.treasury import _cold_start_lines

    lines = _cold_start_lines(
        '{"lite": 24, "http": 30, "delta": -6, "at": "2026-10-21T09:00:00+00:00"}'
    )
    assert len(lines) == 1
    assert "24" in lines[0] and "30" in lines[0]
    assert "-6" in lines[0]
    assert "внешние переводы" in lines[0]


def test_treasury_line_reports_match() -> None:
    """Совпадение — спокойная строка без предупреждения."""
    from app.ton_pay.treasury import _cold_start_lines

    lines = _cold_start_lines(
        '{"lite": 25, "http": 25, "delta": 0, "at": "2026-10-21T09:00:00+00:00"}'
    )
    assert len(lines) == 1
    assert "совпал" in lines[0]
    assert "⚠️" not in lines[0]


def test_treasury_line_tolerates_broken_marker() -> None:
    """Битая метка не роняет отчёт — просто строки нет."""
    from app.ton_pay.treasury import _cold_start_lines

    assert _cold_start_lines(None) == []
    assert _cold_start_lines("не json") == []
    assert _cold_start_lines('{"lite": "нет числа"}') == []
