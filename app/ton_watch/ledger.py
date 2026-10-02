"""Журнал доходов и застрявших переводов: каждый входящий перевод казначея
обязан оставить запись в incomes либо попасть в stuck-список. Тишина здесь
означает «перевод потерялся»."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import Income
from app.ops import claim_once
from app.stakes import current_network
from app.ton_watch.sources import Transfer

logger = logging.getLogger(__name__)

async def _ledger_stuck_incoming(
    session, transfer: Transfer, player_id: int | None, result: str
) -> None:
    """Учёт входящего перевода, который НЕ возвращается (пыль/древний).

    Деньги остаются в казне навсегда — без строки Income сверка с балансом
    цепочки работала бы на «проценты пропажи» для каждой такой суммы. Пыль
    и старый хлам тоже становятся строчкой дохода: «in:refund:dust» /
    «in:refund:expired», и ожидания БД сходятся с реальностью.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    # Cross-process в той же транзакции, что и сама запись: два watcher-инстанса
    # на один перевод не гоняются по check-then-insert (Income.unit_ref
    # уникален, но проигравший поймал бы IntegrityError и ушёл в stuck-список
    # ложным «не обработано»). Проигравший против метки выходит без изменений;
    # откат транзакции снимает метку вместе с записью.
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=None,
            player_id=player_id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=f"in:{result};src:…{transfer.source[-10:]}"[:200],
        )
    )
    await session.commit()

async def _ledger_incoming(
    session, transfer: Transfer, player_id: int | None, round_id: int | None, result: str
) -> None:
    """Каждый входящий перевод казначея — в журнал доходов (/incoming).

    Аудит «откуда деньги»: сумма, момент, хеш, хвост адреса отправителя и
    чем перевод стал (ставка / возврат / оплата смены). Идемпотентно по
    unit_ref (=tx_hash): повторный проход watcher'а не плодит строк.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    # Cross-process в той же транзакции, что и строка дохода: два инстанса на
    # один перевод не дерутся по check-then-insert (см. _ledger_stuck_incoming).
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=round_id,
            player_id=player_id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=f"in:{result};src:…{transfer.source[-10:]}"[:200],
        )
    )
    await session.commit()

PAUSE_REFUND_COMMENT = "Игра приостановлена: идут технические работы"

async def _record_bank_credit(session, transfer: Transfer) -> str:
    """Пополнение казны владельцем (мемо bank:): доход без «банка дня».

    Не ставка (пот дня не растёт) и не возврат (деньги остаются в казне),
    пишется строкой входящего дохода — зеркало учитывает его в тождестве
    «в ноль». Идемпотентно по tx_hash: повторный проход (overlap-окно, сброс
    курсора) не плодит вторую строку.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return "bank_credit"
    # Cross-process в той же транзакции, что и строка дохода (см. _ledger_incoming).
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return "bank_credit"
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=None,
            player_id=None,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note="in:bank;src:…" + transfer.source[-10:][:200],
        )
    )
    await session.commit()
    return "bank_credit"
