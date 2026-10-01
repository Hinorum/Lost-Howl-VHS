"""Авто-возврат перевода, который не может стать ставкой, и учёт этого в
ledger доходов за один коммит."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import settings
from app.models import Income, Payout, Stake
from app.ops import claim_once
from app.stakes import current_network
from app.ton_utils import from_nano, to_nano
from app.ton_watch.ledger import _ledger_stuck_incoming
from app.ton_watch.sources import Transfer

logger = logging.getLogger(__name__)

async def _stash_refund(
    session,
    transfer: Transfer,
    round_id: int | None,
    comment: str | None = None,
    *,
    ledger_result: str | None = None,
    ledger_player_id: int | None = None,
    force: bool = False,
) -> str:
    """Авто-возврат перевода + запись в ledger доходов за один коммит.

    Идемпотентно по tx_hash: повторная обработка той же транзакции не плодит
    вторую выплату. Отправка — обычным порядком через dispatch_pending_payouts.

    Древние переводы (старше WATCH_REFUND_MAX_AGE_DAYS) не возвращаются: после
    сброса базы курсор обнуляется и история казны перечитывается целиком —
    без лимита старый спам вечно рождал бы новые dead-letter возвраты.
    comment — свободный текст перевода вместо служебного memo «way:…»
    (возвраты при паузе игры объясняют игроку, что идут техработы).
    ledger_result — если передан, создаётся запись Income в том же коммите.
    force — вернуть даже сумму меньше refund_min_gram («пыль»). По умолчанию
    пыль не возвращается (газ дороже), НО это верно для анонимного спама;
    известный отправитель (привязанный игрок, неудачная верификация кошелька)
    должен получить свои копейки назад — иначе деньги пропадают молча.
    """
    age_days = (datetime.now(UTC).timestamp() - transfer.utime) / 86_400
    if age_days > max(0, settings.watch_refund_max_age_days):
        logger.warning(
            "Перевод %s старше %d дн. — авто-возврат не создаётся (спам/хлам остаётся в казне)",
            transfer.tx_hash[:16],
            int(age_days),
        )
        await _ledger_stuck_incoming(session, transfer, ledger_player_id, "refund:expired")
        return "refund_expired"
    if not force and transfer.value_nanotons < to_nano(settings.refund_min_gram):
        # Газ возврата дороже самой пыли: микро-перевод остаётся в казне, а не
        # превращается в убыточный dead-letter. Игроку не пишем — это спам-боты.
        logger.info(
            "Перевод %s на %s Gram дешевле порога %s Gram — авто-возврат не создаётся",
            transfer.tx_hash[:16],
            f"{from_nano(transfer.value_nanotons):g}",
            settings.refund_min_gram,
        )
        await _ledger_stuck_incoming(session, transfer, ledger_player_id, "refund:dust")
        return "refund_dust"
    # Защита от двойной выплаты: транзакция уже учтена как ставка (Stake) или
    # как входящий доход казны (Income — ставка, revote-оплата, микро-верификация).
    # Повторный проход (сброс курсора, overlap-окно, пауза, ре-скан) НЕ должен
    # создавать второй авто-возврат: монета уже легла в банк дня (приз/Фонд)
    # или в выручку, и повторный refund — это двойной расход казны.
    booked = await session.execute(
        select(Stake.id).where(
            Stake.tx_hash == transfer.tx_hash, Stake.network == current_network()
        )
    )
    if booked.scalar_one_or_none() is not None:
        logger.warning(
            "Перевод %s уже учтён ставкой (tx в Stake) — авто-возврат отменён "
            "(защита от двойной выплаты)",
            transfer.tx_hash[:16],
        )
        return "already_booked"
    booked = await session.execute(
        select(Income.id).where(
            Income.unit_ref == transfer.tx_hash, Income.network == current_network()
        )
    )
    if booked.scalar_one_or_none() is not None:
        logger.warning(
            "Перевод %s уже учтён входящим доходом казны (Income) — авто-возврат "
            "отменён (защита от двойной выплаты)",
            transfer.tx_hash[:16],
        )
        return "already_booked"
    duplicate = await session.execute(
        select(Payout.id).where(Payout.kind == "refund", Payout.tx_hash == transfer.tx_hash).limit(1)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "refund_duplicated"
    # Cross-process маркер в той же транзакции, что и создание выплаты:
    # два watcher-процесса не создадут по своему возврату на один перевод
    # (payouts.tx_hash не уникален), проигравший уйдёт без изменения данных.
    if not await claim_once(session, f"refund:{transfer.tx_hash}"):
        return "refund_duplicated"
    session.add(
        Payout(
            round_id=round_id,
            player_id=None,
            kind="refund",
            amount_nanotons=transfer.value_nanotons,
            dest_address=transfer.source or "",
            tx_hash=transfer.tx_hash[:80],
            network=current_network(),
            comment_override=comment[:120] if comment else None,
        )
    )
    if ledger_result is not None:
        existing_income = await session.execute(
            select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
        )
        if existing_income.scalar_one_or_none() is None:
            session.add(
                Income(
                    kind="ton",
                    amount_nanotons=transfer.value_nanotons,
                    round_id=round_id,
                    player_id=ledger_player_id,
                    network=current_network(),
                    unit_ref=transfer.tx_hash,
                    note=f"in:{ledger_result};src:…{transfer.source[-10:]}"[:200],
                )
            )
    await session.commit()
    logger.info("Перевод %s возвращён отправителю", transfer.tx_hash[:16])
    return "refund_queued"
