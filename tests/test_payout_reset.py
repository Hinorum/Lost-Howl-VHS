"""Stale-guard _reset_retriable: sending оживает только заведомо «мёртвый».

До колонки claimed_at любой цикл диспетчера безусловно возвращал sending →
pending. Живое вещание другой копии (держит строку до
payout_send_timeout_seconds) перехватывалось и вещалось второй раз — двойной
расход казны; memo-антидубль бессилен, перевод ещё не в цепочке. Теперь
sending возвращается в очередь только когда клейм старше окна вещания + 30 с,
либо при claimed_at NULL (сирота, упавшая до мига колонки); failed оживает
по-прежнему сразу.

Предикаты — в UPDATE, а не в Python: чтобы повторный запуск цикла на строке,
которая УЖЕ в pending, оставался no-op даже на двух репликах. Это страховка
от двойной отправки на пару с FOR UPDATE SKIP LOCKED в claim_retry_loop.
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


async def test_predicate_in_update_not_in_python() -> None:
    """UPDATE без WHERE по статусу/возрасту не должен сбрасывать уже ожившую строку.

    Раньше фильтр по Python'у выбирал id'шники, а UPDATE писал
    «WHERE id IN (...)» — после двух реплик один и тот же id мог быть
    сброшен дважды без ошибки (статус ставился в «pending» повторно), а
    рядом с этим UPDATE мог зацепить строку, которую вторая реплика уже
    забрала из sending. Сейчас предикаты статуса и attempts лежат в WHERE —
    «уже не в sending/failed» означает no-op без гонки.
    """
    now = datetime.now(UTC)
    stale = _payout(
        "sending",
        claimed_at=now - timedelta(seconds=settings.payout_send_timeout_seconds + 300),
    )
    async with SessionLocal() as session:
        session.add(stale)
        await session.commit()
        stale_id = stale.id

    # Прогон: статус станет pending.
    async with SessionLocal() as session:
        await ton_pay._reset_retriable(session, _network())
        await session.commit()

    async with SessionLocal() as session:
        row = await session.get(Payout, stale_id)
        assert row.status == "pending"

    # Второй прогон подряд: pending не должен мигрировать обратно, попытки —
    # не увеличиваться. Если бы UPDATE шёл «WHERE id IN (…)» без проверки
    # статуса, attempts/artifacts могли бы дрейфовать; здесь же статус-предикат
    # в UPDATE сам стопорит повтор.
    async with SessionLocal() as session:
        await ton_pay._reset_retriable(session, _network())
        await session.commit()

    async with SessionLocal() as session:
        row = await session.get(Payout, stale_id)
        assert row.status == "pending", (
            "повторный сброс уже ожившей строки — регрессия к UPDATE без WHERE"
        )
        # Поле attempts не должно мигрировать при no-op UPDATE'е: рост счётчика
        # попыток означал бы, что цикл диспетчера засчитал себе «попытку».
        assert row.attempts == 1


async def test_predicate_protects_fresh_claim_under_race() -> None:
    """Свежий sending с живым claimed_at остаётся sending даже после двух прогонов.

    Сценарий двойной реплики, ради которого предикат переехал в UPDATE: одна
    реплика держит claimed_at=now, другая — гонит _reset_retriable параллельно.
    С Python-фильтром по id'шникам возможен сценарий «выбрали id в момент,
    когда sending был — стало pending, и другая реплика потеряла клейм,
    а memo-антидубль ещё не помог». С status='sending' в WHERE UPDATE просто
    не трогает строку, чей статус уже не тот.
    """
    now = datetime.now(UTC)
    fresh = _payout("sending", claimed_at=now)
    async with SessionLocal() as session:
        session.add(fresh)
        await session.commit()
        fresh_id = fresh.id

    async with SessionLocal() as session:
        await ton_pay._reset_retriable(session, _network())
        await ton_pay._reset_retriable(session, _network())
        await session.commit()

    async with SessionLocal() as session:
        row = await session.get(Payout, fresh_id)
        assert row.status == "sending", (
            "живой клейм перехвачен — регрессия к фильтру в Python"
        )