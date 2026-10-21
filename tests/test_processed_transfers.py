"""Журнал обработанных входящих переводов: один хеш — одна обработка.

Журнал (processed_transfers) закрывает две дыры, из-за которых казна
могла заплатить дважды, а сверка — молча расходиться:

- пылевая ставка, вытесненная валидной, теряла свой tx_hash — и повторный
  проход watcher'а (overlap-окно, ре-скан после деплоя) создавал для неё
  ВТОРОЙ авто-возврат: казна отдавала игроку пыль дважды;
- строка дохода (Income) жила в другой транзакции, чем сама ставка: падение
  между ними оставляло перевод «разобранным, но невидимым» для сверки
  казны — расхождение висело молча (тревога срабатывает на нехватку, а
  тут излишек).

Главное свойство журнала: метка и денежный эффект неразделимы — они
коммитятся вместе или откатываются вместе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.core.processed import KIND_REFUND, KIND_STAKE, claim_transfer, transfer_claim_kind
from app.db import SessionLocal
from app.models import (
    Income,
    Payout,
    Player,
    ProcessedTransfer,
    Round,
    RoundStatus,
    Stake,
    WatcherState,
    WinRule,
)
from app.stakes import current_network, register_stake
from app.ton_utils import to_nano
from app.ton_watch import Transfer, _stash_refund, process_transfer

PID = 930_001
DAY = 930_001
WALLET = "0:" + "ab" * 32


@pytest.fixture()
def ton_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)


async def _open_round() -> int:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        round_row = Round(
            day_index=DAY,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="x",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=20),
            tally_ends_at=now + timedelta(hours=21),
        )
        session.add(round_row)
        session.add(Player(id=PID, username="u", wallet_address=WALLET))
        await session.commit()
        return round_row.id


async def _wipe(round_id: int, tx_hashes: list[str]) -> None:
    """Убрать строки теста: журнал, выплаты, доходы, ставки, раунд, игрока.

    Порядок — дети раньше родителей: Postgres не отдаст DELETE по
    родителю с детьми (SQLite внешние ключи не проверяет, поэтому порядок
    исторически не соблюдался — и это тоже стоит держать).
    """
    async with SessionLocal() as session:
        await session.execute(delete(Income).where(Income.unit_ref.in_(tx_hashes)))
        await session.execute(delete(Income).where(Income.round_id == round_id))
        await session.execute(delete(Payout).where(Payout.round_id == round_id))
        await session.execute(delete(Payout).where(Payout.tx_hash.in_(tx_hashes)))
        await session.execute(delete(Stake).where(Stake.round_id == round_id))
        await session.execute(
            delete(ProcessedTransfer).where(ProcessedTransfer.tx_hash.in_(tx_hashes))
        )
        await session.execute(delete(Round).where(Round.day_index == DAY))
        await session.execute(delete(Player).where(Player.id == PID))
        await session.commit()


async def test_claim_rolls_back_with_effect() -> None:
    """Метка без эффекта невозможна: откат уносит её вместе с деньгами.

    Иначе перевод «разобран», но не обработан: деньги не учтены, а повтор
    уже не сможет их вернуть.
    """
    tx = "atomic-1"
    try:
        async with SessionLocal() as session:
            assert await claim_transfer(session, tx, current_network(), KIND_STAKE, 1)
            await session.rollback()
        async with SessionLocal() as session:
            assert await transfer_claim_kind(session, tx, current_network()) is None
            # Перевод снова можно обработать — как после падения.
            assert await claim_transfer(session, tx, current_network(), KIND_STAKE, 1)
            await session.commit()
        async with SessionLocal() as session:
            assert await transfer_claim_kind(session, tx, current_network()) == KIND_STAKE
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(ProcessedTransfer).where(ProcessedTransfer.tx_hash == tx))
            await session.commit()


async def test_stake_is_processed_once(ton_on: None) -> None:
    """Один перевод — одна ставка: журнал держит второй проход."""
    round_id = await _open_round()
    tx = "stake-once-1"
    try:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            player = await session.get(Player, PID)
            assert await register_stake(session, round_row, player, to_nano(1), tx) == "ok"
            assert (
                await register_stake(session, round_row, player, to_nano(1), tx)
                == "duplicate_tx"
            )
            await session.commit()
        async with SessionLocal() as session:
            stakes = (
                await session.execute(select(Stake).where(Stake.tx_hash == tx))
            ).scalars().all()
            assert len(stakes) == 1
            assert (
                await transfer_claim_kind(session, tx, current_network()) == KIND_STAKE
            )
    finally:
        await _wipe(round_id, [tx])


async def test_refund_is_processed_once(ton_on: None) -> None:
    """Возврат тоже помечает перевод: второй возврат не создаётся."""
    tx = "refund-once-1"
    transfer = Transfer(
        tx_hash=tx,
        source="0:" + "cd" * 32,
        value_nanotons=to_nano(1),
        comment="",
        utime=int(datetime.now(UTC).timestamp()),
    )
    try:
        async with SessionLocal() as session:
            assert await _stash_refund(session, transfer, None) == "refund_queued"
            assert (
                await transfer_claim_kind(session, tx, current_network()) == KIND_REFUND
            )
        async with SessionLocal() as session:
            assert await _stash_refund(session, transfer, None) == "refund_duplicated"
        async with SessionLocal() as session:
            refunds = (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all()
            assert len(refunds) == 1
    finally:
        await _wipe(0, [tx])


async def test_dust_replaced_by_valid_stake_is_refunded_once(ton_on: None) -> None:
    """Пыль, вытесненная валидной ставкой, возвращается ОДИН раз.

    Сценарий, который раньше стоил казне денег: пылевая ставка вытесняется
    валидной, её tx_hash перезаписывается, а возврат пыли создаётся без
    следа (без tx_hash и без метки). Повторный проход watcher'а по пылевому
    переводу (overlap-окно, ре-скан после деплоя) не находил его ни по
    одной из проверок — и создавал ВТОРОЙ возврат: казна отдавала пыль
    дважды.

    Здесь же проверяется вторая дыра: строка дохода, потерянная при падении
    между коммитом ставки и коммитом журнала, чинится повторным проходом —
    иначе перевод остаётся невидимым для сверки казны, и расхождение
    висит молча.
    """
    round_id = await _open_round()
    dust_tx = "dust-930001-1"
    valid_tx = "valid-930001-1"
    # Пыль по ставке (ниже stake_min_ton), но выше порога авто-возврата
    # (refund_min_gram) и ниже вилки авто-гранта за смену пути (revote_ton):
    # иначе перевод ушёл бы по другой ветке и сценарий не сложился бы.
    dust_amount = to_nano(0.06)
    now = int(datetime.now(UTC).timestamp())
    try:
        async with SessionLocal() as session:
            dust = Transfer(dust_tx, WALLET, dust_amount, "", now)
            assert await process_transfer(dust) == "too_small"
            # Падение между коммитом ставки и коммитом журнала: строка дохода
            # и её метка не записаны, а ставка — записана.
            await session.execute(delete(Income).where(Income.unit_ref == dust_tx))
            await session.execute(
                delete(WatcherState).where(WatcherState.key == f"ledger:{dust_tx}")
            )
            await session.commit()

        async with SessionLocal() as session:
            valid = Transfer(valid_tx, WALLET, to_nano(1), "", now)
            assert await process_transfer(valid) == "ok"

        async with SessionLocal() as session:
            refunds = (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all()
            assert len(refunds) == 1, "валидная ставка должна была вернуть пыль один раз"
            # Возврат помнит, какой перевод он возвращает: по этому хешу его
            # и находит защита от повторного возврата.
            assert refunds[0].tx_hash == dust_tx

        # Повторный проход watcher'а по пылевому переводу.
        async with SessionLocal() as session:
            again = Transfer(dust_tx, WALLET, dust_amount, "", now)
            assert await process_transfer(again) == "duplicate_tx"

        async with SessionLocal() as session:
            refunds = (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all()
            assert len(refunds) == 1, "повторный проход создал второй возврат пыли"
            # Потерянная строка дохода восстановлена: перевод снова виден
            # для сверки казны.
            incomes = (
                await session.execute(select(Income).where(Income.unit_ref == dust_tx))
            ).scalars().all()
            assert len(incomes) == 1, "журнал дохода не починен повторным проходом"
    finally:
        await _wipe(round_id, [dust_tx, valid_tx])
