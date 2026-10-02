"""Stale-guard _reset_retriable: sending оживает только заведомо «мёртвый».

До колонки claimed_at любой цикл диспетчера безусловно возвращал sending →
pending. Живое вещание другой копии (держит строку до
payout_send_timeout_seconds) перехватывалось и вещалось второй раз — двойной
расход казны; memo-антидубль бессилен, перевод ещё не в цепочке. Теперь
sending возвращается в очередь только когда клейм старше окна вещания + 30 с,
либо при claimed_at NULL (сирота, упавшая до мига колонки); failed оживает
по-прежнему сразу.
"""

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


def _network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


def _payout(status: str, attempts: int = 1, claimed_at=None) -> Payout:
    return Payout(
        kind="prize",
        amount_nanotons=5_000_000,
        dest_address="0:" + os.urandom(32).hex(),
        network=_network(),
        status=status,
        attempts=attempts,
        claimed_at=claimed_at,
    )


async def test_sending_resets_only_when_stale() -> None:
    now = datetime.now(UTC)
    fresh = _payout("sending", claimed_at=now)
    stale = _payout(
        "sending",
        claimed_at=now - timedelta(seconds=settings.payout_send_timeout_seconds + 300),
    )
    orphan = _payout("sending", claimed_at=None)
    failed = _payout("failed", claimed_at=now)
    exhausted = _payout(
        "sending",
        attempts=settings.payout_max_attempts,
        claimed_at=now - timedelta(hours=1),
    )
    rows = [fresh, stale, orphan, failed, exhausted]
    async with SessionLocal() as session:
        session.add_all(rows)
        await session.commit()
        ids = [r.id for r in rows]

    async with SessionLocal() as session:
        await ton_pay._reset_retriable(session, _network())
        await session.commit()

    async with SessionLocal() as session:
        got = {
            r.id: r.status
            for r in (
                await session.execute(select(Payout).where(Payout.id.in_(ids)))
            )
            .scalars()
            .all()
        }
    assert got[fresh.id] == "sending", "живой клейм не перехватывается"
    assert got[stale.id] == "pending", "«мёртвый» клейм возвращается в очередь"
    assert got[orphan.id] == "pending", "сирота до колонки — завис по определению"
    assert got[failed.id] == "pending", "failed оживает сразу"
    assert got[exhausted.id] == "sending", "attempts==max — не кандидат ретрая"