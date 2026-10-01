"""Выдача гранта на повторное голосование: по факту пришедшего перевода и по
запросу (fallback «недоехавшего» revote по сумме)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Player, RevoteGrant, Round, RoundStatus, Stake
from app.stakes import current_network
from app.ton_utils import from_nano, to_nano
from app.ton_watch.notify import _dm_stake
from app.ton_watch.sources import Transfer

logger = logging.getLogger(__name__)

async def confirm_aged_pending(bot: Bot | None = None) -> int:
    """Свежие переводы на момент обработки младше порога и остаются pending.

    Этот проход подтверждает их, когда возраст уже точно больше
    stake_confirm_seconds, и сообщает игроку. Закрытые дни не трогаем:
    их pending-ставки финализация вернёт как «залипшие».
    """
    confirmed = 0
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.stake_confirm_seconds)
    async with SessionLocal() as session:
        rows = (
            (await session.execute(
                select(Stake).where(
                    Stake.status == "pending",
                    Stake.network == current_network(),
                    Stake.created_at <= cutoff,
                )
            ))
            .scalars()
            .all()
        )
        for stake in rows:
            round_row = await session.get(Round, stake.round_id)
            if round_row is None or round_row.status != RoundStatus.OPEN:
                continue
            # Условный UPDATE-claim: ручной возврат ставит refunded в ЯВНОЙ
            # гонке со свипом; без WHERE pending свип перезаписал бы refunded
            # на confirmed — ставка засчитана И игрок ещё получает возврат.
            claimed = await session.execute(
                update(Stake)
                .where(Stake.id == stake.id, Stake.status == "pending")
                .values(status="confirmed", confirmed_at=now)
            )
            if claimed.rowcount != 1:
                continue  # уже не pending (возврат/дубль) — не трогаем
            confirmed += 1
            await _dm_stake(
                bot,
                stake.player_id,
                f"✅ Ставка {from_nano(stake.amount_nanotons):g} Gram на день {round_row.day_index} принята.",
            )
        if confirmed:
            await session.commit()
    return confirmed

async def _grant_revote(session, transfer: Transfer, player: Player, round_row: Round, note: str) -> str:
    """Общий путь выдачи гранта: идемпотентность + грант + учёт дохода.

    Возвращает ok / duplicate_tx. Проверки раунда, суммы и голоса — на совести
    вызывающего, грант создаётся здесь один раз.
    """
    duplicate = await session.execute(
        select(RevoteGrant.id).where(RevoteGrant.unit_ref == transfer.tx_hash)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "duplicate_tx"
    session.add(
        RevoteGrant(
            round_id=round_row.id,
            player_id=player.id,
            source="ton",
            unit_ref=transfer.tx_hash,
        )
    )
    # Ledger доходов: revote-перевод — выручка казны, её надо сверять.
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=round_row.id,
            player_id=player.id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=note,
        )
    )
    await session.commit()
    return "ok"

async def _process_revote(session, transfer: Transfer, player: Player, round_id: int) -> str:
    round_row = await session.get(Round, round_id)
    if round_row is None or round_row.status != RoundStatus.OPEN:
        return "revote_closed"
    if not round_row.money_mode:
        # Бесплатный день: смена пути бесплатна, платить за неё нельзя.
        return "revote_money_off"
    if transfer.value_nanotons < to_nano(settings.revote_ton):
        return "revote_too_small"
    # Симметрично автогранту по сумме ([revote_ton, stake_min_ton)): даже с
    # rv:-мемо «ставкоподобный» перевод (>= минимума ставки) не должен тихо
    # списываться как дешёвая смена пути, а фиксироваться как полноценная
    # ставка всего баланса. Иначе большой перевод с rv:-мемо превращался бы
    # в грант без соответствующей записи ставки.
    if transfer.value_nanotons >= to_nano(settings.stake_min_ton):
        return "revote_too_large"
    # Как и в автогранте без мемо: если пути ещё нет, менять нечего — грант
    # не выдаём, иначе игрок платил бы за бесполезный жетон.
    from app.voting import get_vote

    vote = await get_vote(session, round_row.id, player.id)
    if vote is None:
        return "revote_no_vote"
    return await _grant_revote(session, transfer, player, round_row, f"rv:{round_id}")

async def _maybe_auto_grant(session, transfer: Transfer, player: Player) -> str:
    """Фолбэк «недоехавшего» revote по сумме (когда кошелёк не приложил мемо).

    Плата за смену пути (revote_ton) ниже минимума ставки (stake_min_ton), а
    сам перевод в вилке [revote_ton, stake_min_ton) ставкой быть не может
    (мал). Если игрок уже выбрал путь на открытом дне — выдаём грант по сумме.
    Абсолютную равнозначность мемо не требуется: автогрант выдаётся один раз
    за перевод (unit_ref=tx_hash).

    Возвращает revote_ok / no_vote / revote_closed / duplicate_tx.
    """
    round_result = await session.execute(
        select(Round)
        .where(Round.status == RoundStatus.OPEN)
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    round_row = round_result.scalar_one_or_none()
    if round_row is None:
        return "revote_closed"
    if not round_row.money_mode:
        # Бесплатный день: грант за смену пути не выдаётся, перевод вернём.
        return "revote_money_off"
    # Грант нужен тем, кто уже выбрал путь (иначе смена выбора бесплатна —
    # платить за неё бессмысленно).
    from app.voting import get_vote

    vote = await get_vote(session, round_row.id, player.id)
    if vote is None:
        return "no_vote"
    status = await _grant_revote(session, transfer, player, round_row, "rv:auto")
    if status == "duplicate_tx":
        return "duplicate_tx"
    return "revote_ok"
