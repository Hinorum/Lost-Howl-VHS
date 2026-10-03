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


async def test_refund_cap_stops_sender_gas_burn(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Потолок авто-возвратов ограничивает сжигание газа казны.

    Каждый авто-возврат — это исходящая транзакция за счёт казны. Адрес
    публичен, стоимость атаки определяется только суммой присланного, поэтому
    спам переводов чуть выше refund_min_gram заставлял казну платить газ за
    каждый: 10 000 переводов по 0.06 Gram — это 10 000 исходящих tx, то есть
    расход в десятки раз больше присланного.

    Перешагнувший потолок НЕ пропадает: он уходит в ledger и ждёт ручного
    возврата, а сверка казны с БД продолжает сходиться.
    """
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)
    monkeypatch.setattr(settings, "refund_max_per_sender_day", 3)
    monkeypatch.setattr(settings, "refund_max_total_day", 100)
    now = int(datetime.now(UTC).timestamp()) - 60
    source = "0:" + "ee" * 32

    statuses = []
    for index in range(6):
        transfer = Transfer(
            tx_hash=f"spam-{index}",
            source=source,
            value_nanotons=60_000_000,  # 0.06 Gram — выше порога пыли
            comment="",
            utime=now + index,
        )
        statuses.append(await _stash_refund(session, transfer, None))

    assert statuses[:3] == ["refund_queued"] * 3
    assert statuses[3:] == ["refund_capped"] * 3, statuses
    rows = (await session.execute(select(Payout))).scalars().all()
    assert len(rows) == 3, "после потолка возвраты создаваться не должны"
    # Деньги не пропали: каждый перешагнувший перевод учтён в ledger.
    ledgered = (
        await session.execute(select(Income).where(Income.note.like("in:refund_cap%")))
    ).scalars().all()
    assert len(ledgered) == 3


async def test_refund_cap_global_limit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Общий потолок срабатывает и для разных отправителей.

    Лимит на отправителя обходится пачкой кошельков, поэтому нужен и общий
    потолок на сутки: иначе атака распределяется по тысяче адресов.
    """
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)
    monkeypatch.setattr(settings, "refund_max_per_sender_day", 0)
    monkeypatch.setattr(settings, "refund_max_total_day", 4)
    now = int(datetime.now(UTC).timestamp()) - 60

    statuses = []
    for index in range(7):
        transfer = Transfer(
            tx_hash=f"spread-{index}",
            source="0:" + f"{index:02x}" * 32,
            value_nanotons=60_000_000,
            comment="",
            utime=now + index,
        )
        statuses.append(await _stash_refund(session, transfer, None))

    assert statuses[:4] == ["refund_queued"] * 4
    assert statuses[4:] == ["refund_capped"] * 3, statuses


async def test_refund_cap_zero_disables_protection(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 = лимита нет: осознанный отказ, а не «забыли посчитать»."""
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)
    monkeypatch.setattr(settings, "refund_max_per_sender_day", 0)
    monkeypatch.setattr(settings, "refund_max_total_day", 0)
    now = int(datetime.now(UTC).timestamp()) - 60
    source = "0:" + "ab" * 32

    statuses = [
        await _stash_refund(
            session,
            Transfer(
                tx_hash=f"free-{index}",
                source=source,
                value_nanotons=60_000_000,
                comment="",
                utime=now + index,
            ),
            None,
        )
        for index in range(8)
    ]

    assert statuses == ["refund_queued"] * 8


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
