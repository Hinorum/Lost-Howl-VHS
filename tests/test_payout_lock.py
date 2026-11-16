"""Межпроцессная защита выплат: advisory lock + lease владения строкой.

`_DISPATCH_LOCK` сериализует циклы только внутри процесса. На проде webhook
ручных команд (/finalize, /refinalize, /payout retry) может обслуживать
второй инстанс: он не тикает, но диспетчер в нём жив. Два цикла на одной
очереди — это двойное вещание, а не «лишняя работа»: один возвращает
sending → pending, пока другой ещё держит строку, и оба отправляют перевод.

Здесь проверяются три слоя:
  1. advisory lock PostgreSQL — второй процесс цикл пропускает;
  2. lease перед вещанием — условный UPDATE по штампу клейма продлевает
     окно и доказывает владение строкой;
  3. сброс строки во время прохода — потерявшую клейм строку диспетчер
     не вещает, даже если она уже была выбрана в начале цикла.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import ensure_player, ensure_round

import app.payout_lock as plock
from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


def _network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


async def _seed_payouts(count: int) -> list[int]:
    await ensure_round(7)
    ids: list[int] = []
    for index in range(count):
        await ensure_player(300 + index)
        async with SessionLocal() as session:
            payout = Payout(
                round_id=7,
                player_id=300 + index,
                kind="prize",
                amount_nanotons=500_000_000,
                dest_address="0:" + os.urandom(32).hex(),
            )
            session.add(payout)
            await session.flush()
            payout.status = "pending"
            await session.commit()
            ids.append(payout.id)
    return ids


async def _drop_payouts(ids: list[int]) -> None:
    async with SessionLocal() as session:
        for payout_id in ids:
            row = await session.get(Payout, payout_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


async def _empty_markers() -> set[str]:
    return set()


# ---------- advisory lock: контракт модуля ----------


async def test_guard_is_permissive_on_sqlite() -> None:
    """SQLite advisory lock не умеет: там один процесс, guard обязан пустить."""
    from app.db import engine

    if engine.dialect.name == "postgresql":
        pytest.skip("контракт SQLite; exclusivity проверяется ниже на фейковом PG")

    assert await plock.acquire_payout_dispatch_lock() is True
    # Повторный захват того же «лока» безопасен.
    assert await plock.acquire_payout_dispatch_lock() is True
    assert plock.payout_dispatch_lock_held() is True


async def test_release_on_sqlite_is_noop_and_safe() -> None:
    await plock.release_payout_dispatch_lock()
    await plock.acquire_payout_dispatch_lock()
    await plock.release_payout_dispatch_lock()
    await plock.release_payout_dispatch_lock()


async def test_lock_key_differs_from_scheduler() -> None:
    """Один ключ на два лока — диспетчер и планировщик мешали бы друг другу."""
    from app.scheduler_lock import _LOCK_KEY as scheduler_key

    assert plock._LOCK_KEY != scheduler_key


async def test_second_instance_is_rejected(monkeypatch) -> None:
    """Второй процесс на той же базе лок не получает и цикл пропускает."""
    calls = {"got": iter([True, False, True])}

    class _Result:
        def scalar(self):
            return next(calls["got"])

    class _Conn:
        def __init__(self) -> None:
            self.closed = False
            self.queries: list[tuple[str, dict | None]] = []

        async def execute(self, stmt, params=None):
            self.queries.append((str(stmt), params))
            return _Result()

        async def close(self):
            self.closed = True

    conns: list[_Conn] = []

    async def _connect():
        conn = _Conn()
        conns.append(conn)
        return conn

    class _Engine:
        dialect = type("d", (), {"name": "postgresql"})()

        connect = staticmethod(_connect)

    monkeypatch.setattr(plock, "_lock_conn", None)
    monkeypatch.setattr(plock, "_lock_engine_instance", lambda: _Engine())
    monkeypatch.setattr("app.db.engine", _Engine())

    assert await plock.acquire_payout_dispatch_lock() is True
    assert len(conns) == 1 and conns[0].closed is False
    assert plock.payout_dispatch_lock_held() is True
    # Повторный захват в том же процессе не плодит соединения.
    assert await plock.acquire_payout_dispatch_lock() is True
    assert len(conns) == 1

    # Сбрасываем состояние модуля — играем «второй процесс».
    monkeypatch.setattr(plock, "_lock_conn", None)
    assert await plock.acquire_payout_dispatch_lock() is False
    assert len(conns) == 2 and conns[1].closed is True
    assert plock.payout_dispatch_lock_held() is False

    await plock.release_payout_dispatch_lock()
    await plock.release_payout_dispatch_lock()


async def test_verify_resets_dead_lock_connection(monkeypatch) -> None:
    """Мёртвое соединение-держатель: verify возвращает False и сбрасывает ссылку.

    Иначе процесс считал бы лок своим, хотя сервер его уже отпустил, и второй
    инстанс мог бы начать вещание параллельно.
    """

    class _DeadConn:
        closed = False

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection closed")

        async def close(self):
            self.closed = True

    dead = _DeadConn()
    monkeypatch.setattr(plock, "_lock_conn", dead)
    assert await plock.verify_payout_dispatch_lock() is False
    assert plock._lock_conn is None
    assert dead.closed is True


# ---------- диспетчер: второй процесс и сброс во время прохода ----------


class _NoopLock:
    """Разные процессы не делят asyncio.Lock — в тесте это и эмулируем."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


async def test_second_dispatch_skips_while_first_send_in_flight(monkeypatch) -> None:
    """Два диспетчера, один медленный send: второй не шлёт и не трогает очередь.

    Без межпроцессного лока второй цикл увидел бы sending-строку, его
    `_reset_retriable` вернул бы её в pending (если клейм успел состариться),
    и оба процесса отправили бы один перевод. Advisory lock заставляет второй
    цикл вернуться сразу, не открывая сессию.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    ids = await _seed_payouts(1)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_send(dest, amount, comment):
        calls.append(comment)
        started.set()
        await release.wait()
        return "bcast:1"

    held = {"value": False}

    async def fake_acquire() -> bool:
        if held["value"]:
            return False
        held["value"] = True
        return True

    async def fake_release() -> None:
        held["value"] = False

    async def fake_verify() -> bool:
        # На PostgreSQL verify без живого соединения-держателя честно
        # возвращает False: настоящий лок здесь заменён фейком acquire.
        return held["value"]

    monkeypatch.setattr(ton_pay, "send_ton_transfer", slow_send)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", _empty_markers)
    monkeypatch.setattr("app.payout_lock.acquire_payout_dispatch_lock", fake_acquire)
    monkeypatch.setattr("app.payout_lock.release_payout_dispatch_lock", fake_release)
    monkeypatch.setattr("app.payout_lock.verify_payout_dispatch_lock", fake_verify)
    monkeypatch.setattr("app.ton_pay.state._DISPATCH_LOCK", _NoopLock())
    try:
        first = asyncio.create_task(ton_pay.dispatch_pending_payouts(bot=None))
        await asyncio.wait_for(started.wait(), 5)
        assert await asyncio.wait_for(
            ton_pay.dispatch_pending_payouts(bot=None), 5
        ) == 0, "второй процесс обязан пропустить цикл"
        release.set()
        assert await asyncio.wait_for(first, 10) == 1
        assert len(calls) == 1, "перевод отправлен ровно один раз"

        async with SessionLocal() as session:
            row = await session.get(Payout, ids[0])
        assert row is not None and row.status == "sent"
    finally:
        release.set()
        await _drop_payouts(ids)


async def test_reset_during_pass_skips_lost_claim(monkeypatch) -> None:
    """Строка, сброшенная во время прохода, не вещается повторно.

    Пока первый перевод висит в send, второй строке «состариваем» клейм и
    прогоняем _reset_retriable — ровно то, что сделал бы второй диспетчер.
    Перед вещанием диспетчер продлевает lease условным UPDATE по своему штампу:
    строка уже pending — rowcount 0, отправки нет.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    ids = await _seed_payouts(2)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_send(dest, amount, comment):
        calls.append(comment)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return "bcast:1"

    async def confirmed(memo, seconds):
        return True

    monkeypatch.setattr(ton_pay, "send_ton_transfer", slow_send)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", _empty_markers)
    monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", confirmed)
    monkeypatch.setattr("app.ton_pay.state._DISPATCH_LOCK", _NoopLock())
    try:
        task = asyncio.create_task(ton_pay.dispatch_pending_payouts(bot=None))
        await asyncio.wait_for(started.wait(), 5)

        # «Другой процесс»: клейм второй строки состарился и возвращён в pending.
        async with SessionLocal() as session:
            row = await session.get(Payout, ids[1])
            assert row is not None
            row.claimed_at = datetime.now(UTC) - timedelta(
                seconds=settings.payout_send_timeout_seconds + 300
            )
            await session.commit()
        async with SessionLocal() as session:
            await ton_pay._reset_retriable(session, _network())
            await session.commit()

        release.set()
        assert await asyncio.wait_for(task, 10) == 1
        assert len(calls) == 1, "сброшенная строка не должна вещаться повторно"

        async with SessionLocal() as session:
            first = await session.get(Payout, ids[0])
            second = await session.get(Payout, ids[1])
        assert first is not None and first.status == "sent"
        assert second is not None and second.status == "pending"
    finally:
        release.set()
        await _drop_payouts(ids)


async def test_renew_claims_extends_only_owned_rows(monkeypatch) -> None:
    """Lease пачки: чужой/сброшенный клейм не продлевается и не вещается."""
    from app.ton_pay.dispatch import _claim_pending_payouts, _renew_claims

    # Продление требует живого advisory-лока. На PostgreSQL без настоящего
    # соединения-держателя verify возвращает False, поэтому лок эмулируем:
    # предмет теста — условный UPDATE по штампу, а не сам замок.
    monkeypatch.setattr(
        "app.payout_lock.verify_payout_dispatch_lock", AsyncMock(return_value=True)
    )

    ids = await _seed_payouts(2)
    try:
        async with SessionLocal() as session:
            rows = [
                await session.get(Payout, payout_id) for payout_id in ids
            ]
            claimed, _prior = await _claim_pending_payouts(session, rows)  # type: ignore[arg-type]
            assert claimed == set(ids)

        # Вторую строку «забрал другой цикл» — статус уже не sending.
        async with SessionLocal() as session:
            second = await session.get(Payout, ids[1])
            assert second is not None
            second.status = "pending"
            await session.commit()

        async with SessionLocal() as session:
            rows = [await session.get(Payout, payout_id) for payout_id in ids]
            renewed = await _renew_claims([row for row in rows if row is not None])

        assert renewed == {ids[0]}, "продлён только свой клейм"
        async with SessionLocal() as session:
            first = await session.get(Payout, ids[0])
            second = await session.get(Payout, ids[1])
        assert first is not None and first.status == "sending"
        assert first.claimed_at is not None
        assert second is not None and second.status == "pending"
    finally:
        await _drop_payouts(ids)


async def test_dispatch_does_not_send_when_lock_busy(monkeypatch) -> None:
    """Busy advisory lock: цикл выходит до открытия сессии и не трогает строки."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    ids = await _seed_payouts(1)
    transfer = AsyncMock(return_value="bcast:1")
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(
        "app.payout_lock.acquire_payout_dispatch_lock", AsyncMock(return_value=False)
    )
    try:
        assert await ton_pay.dispatch_pending_payouts(bot=None) == 0
        transfer.assert_not_awaited()
        async with SessionLocal() as session:
            row = await session.get(Payout, ids[0])
        assert row is not None and row.status == "pending"
        assert row.attempts == 0
    finally:
        await _drop_payouts(ids)
