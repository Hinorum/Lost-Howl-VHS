"""Ручной разбор выплат (спам/retry), учёт dismissed и лимит возраста возвратов."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Income, Payout
from app.stakes import current_network
from app.ton_pay import pending_payout_count, resolve_dead_payout
from app.ton_watch import Transfer, _stash_refund


async def test_pending_payout_count_ignores_dismissed(session: AsyncSession) -> None:
    session.add_all(
        [
            Payout(kind="refund", amount_nanotons=10, dest_address="a", status="sent"),
            Payout(kind="refund", amount_nanotons=20, dest_address="b", status="dismissed"),
            Payout(kind="prize", amount_nanotons=30, dest_address="c", status="pending"),
            Payout(kind="refund", amount_nanotons=40, dest_address="d", status="failed"),
        ]
    )
    await session.commit()
    # dismissed — вердикт хранителя, долгом не считается и сброс не блокирует.
    assert await pending_payout_count(session) == 2


async def test_resolve_dead_payout_actions(session: AsyncSession) -> None:
    session.add_all(
        [
            Payout(id=101, kind="refund", amount_nanotons=5, dest_address="a", status="failed", attempts=5),
            Payout(id=102, kind="refund", amount_nanotons=6, dest_address="b", status="sent"),
        ]
    )
    await session.commit()

    assert await resolve_dead_payout(session, 101, "spam") == "dismissed"
    row = await session.get(Payout, 101)
    assert row.status == "dismissed"

    assert await resolve_dead_payout(session, 101, "retry") == "pending"
    row = await session.get(Payout, 101)
    # Счётчик попыток НЕ сбрасывается: попытка могла реально уйти в цепочку,
    # и attempts >= 1 заставляет диспетчер свериться с memo перед повтором.
    assert row.status == "pending" and row.attempts == 5

    # Уже отправленную не трогаем; несуществующей и неизвестного действия нет.
    assert await resolve_dead_payout(session, 102, "spam") is None
    assert await resolve_dead_payout(session, 999, "spam") is None
    assert await resolve_dead_payout(session, 101, "nuke") is None


async def test_resolve_dead_payout_spam_guards_non_refund(session: AsyncSession) -> None:
    """Спамом гасится только refund: деньги игрока (prize/weekly/…) списать нельзя."""
    session.add(
        Payout(id=201, kind="prize", amount_nanotons=100, dest_address="0:zz", status="failed", attempts=1)
    )
    await session.commit()
    with pytest.raises(ValueError, match="только refund"):
        await resolve_dead_payout(session, 201, "spam")
    row = await session.get(Payout, 201)
    assert row.status == "failed" and row.amount_nanotons == 100


async def test_stash_refund_skips_ancient_transfers(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После сброса базы курсор обнуляется: старый спам не должен снова
    превращаться в dead-letter возвраты."""
    monkeypatch.setattr(settings, "watch_refund_max_age_days", 14)
    monkeypatch.setattr(settings, "refund_min_gram", 0)
    now = datetime.now(UTC).timestamp()
    ancient = Transfer(tx_hash="ancient", source="0:aa", value_nanotons=1_000, comment="РЕКЛАМА", utime=int(now - 40 * 86_400))
    fresh = Transfer(tx_hash="fresh", source="0:bb", value_nanotons=2_000, comment="", utime=int(now - 3_600))

    assert await _stash_refund(session, ancient, None) == "refund_expired"
    assert (await session.execute(select(Payout))).scalars().all() == []

    assert await _stash_refund(session, fresh, None) == "refund_queued"
    assert await _stash_refund(session, fresh, None) == "refund_duplicated"
    rows = (await session.execute(select(Payout))).scalars().all()
    assert len(rows) == 1 and rows[0].tx_hash == "fresh"


async def test_stash_refund_skips_dust_below_threshold(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Микро-спам дешевле refund_min_gram не порождает возврат: газ отправки
    (payout_fee_gram) дороже самой пыли."""
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)
    now = datetime.now(UTC).timestamp()
    dust = Transfer(
        tx_hash="dust-1", source="0:cc", value_nanotons=10_000,  # 0.00001 Gram
        comment="", utime=int(now - 60),
    )
    assert await _stash_refund(session, dust, None) == "refund_dust"
    assert (await session.execute(select(Payout))).scalars().all() == []


async def test_stash_refund_skips_transfer_booked_as_stake(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Двойная выплата: транзакция уже учтена ставкой (ушла в банк дня,
    разыграна победителю/в Фонд) — повторный авто-возврат той же транзакции
    (ре-скан после сброса курсора / overlap-окно) НЕ создаётся."""
    from app.models import Stake

    monkeypatch.setattr(settings, "watch_refund_max_age_days", 30)
    monkeypatch.setattr(settings, "refund_min_gram", 0)
    session.add(
        Stake(
            round_id=1,
            player_id=7,
            amount_nanotons=2_080_000_000,
            tx_hash="booked-stake",
            network=current_network(),
            status="confirmed",
        )
    )
    await session.commit()
    transfer = Transfer(
        tx_hash="booked-stake",
        source="0:bb",
        value_nanotons=2_080_000_000,
        comment="",
        utime=int(datetime.now(UTC).timestamp() - 60),
    )
    assert await _stash_refund(session, transfer, None) == "already_booked"
    assert (await session.execute(select(Payout))).scalars().all() == []


async def test_stash_refund_skips_transfer_booked_as_income(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Транзакция уже учтена входящим доходом казны (revote-оплата/ставка в
    журнале) — повторный авто-возврат не создаётся (монета уже в выручке)."""
    monkeypatch.setattr(settings, "watch_refund_max_age_days", 30)
    monkeypatch.setattr(settings, "refund_min_gram", 0)
    session.add(
        Income(
            kind="ton",
            amount_nanotons=3_000_000_000,
            unit_ref="booked-income",
            network=current_network(),
        )
    )
    await session.commit()
    transfer = Transfer(
        tx_hash="booked-income",
        source="0:bb",
        value_nanotons=3_000_000_000,
        comment="",
        utime=int(datetime.now(UTC).timestamp() - 60),
    )
    assert await _stash_refund(session, transfer, None) == "already_booked"
    assert (await session.execute(select(Payout))).scalars().all() == []
