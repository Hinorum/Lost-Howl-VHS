"""Гигиена очереди выплат: возврат зависших sending/failed в очередь
и оживление выплат, чей кошелёк привязали уже после финализации дня."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config import settings
from app.models import Payout, Player

logger = logging.getLogger(__name__)

_TREASURY_KINDS = {"rake", "leaderboard"}

async def _reset_retriable(session, network: str) -> None:
    """Оживляем зависшие sending/failed, пока не исчерпан лимит попыток.

    failed возвращается в очередь сразу (в мёртвой строке никто не «живёт»);
    sending — ТОЛЬКО если клейм заведомо «мёртв»: он старше
    payout_send_timeout_seconds + 30 c. Живое вещание (другая копия
    диспетчера держит строку до таймаута вещания) не перехватывается —
    иначе та копия на следующем цикле забрала бы строку и перевела деньги
    второй раз; memo-антидубль не поможет — перевод ещё не в цепочке.
    claimed_at IS NULL (строки, упавшие до появления колонки) считаем
    зависшими: живой клейм всегда пишет claimed_at сейчас. Сверка в naive
    UTC: Postgres вернёт aware, SQLite — naive (см. confirm_broadcast_payouts).
    """
    rows = (
        await session.execute(
            select(Payout.id, Payout.status, Payout.claimed_at).where(
                Payout.status.in_(["failed", "sending"]),
                Payout.attempts < settings.payout_max_attempts,
                Payout.dest_address != "",
                Payout.network == network,
            )
        )
    ).all()
    if not rows:
        return
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=settings.payout_send_timeout_seconds + 30
    )
    reset_ids = [payout_id for payout_id, status, _claimed in rows if status == "failed"]
    for payout_id, status, claimed_at in rows:
        if status != "sending":
            continue
        if claimed_at is None:
            reset_ids.append(payout_id)
        elif claimed_at.replace(tzinfo=None) <= cutoff:
            reset_ids.append(payout_id)
    if reset_ids:
        await session.execute(
            update(Payout).where(Payout.id.in_(reset_ids)).values(status="pending")
        )

async def _hydrate_player_dests(session, network: str) -> int:
    """Оживляет выплаты без получателя, когда кошелёк уже привязан.

    Призы и возвраты игроков без привязанного кошелька на момент финализации
    не должны тонуть в failed (деньги спят, пока админ не разберёт вручную).
    Строка остаётся в очереди, а как только игрок привязывает адрес (/wallet),
    следующий же цикл диспетчера всталяет его в dest_address и платёж уходит
    сам — retry из /payouts не нужен. Доли казны (rake/leaderboard) без
    OWNER_WALLET_ADDRESS и выплаты без игрока (player_id пуст) оживлять нечем:
    честный failed с причиной-действием, как раньше.

    Возвращает число оживших строк (они поедут в пик этого же цикла).
    """
    from app.ton_utils import normalize_address

    rows = list(
        (
            await session.execute(
                select(Payout).where(
                    Payout.dest_address == "",
                    Payout.status == "pending",
                    Payout.network == network,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    player_ids = {p.player_id for p in rows if p.player_id is not None}
    wallet_map: dict[int, str] = {}
    verified_map: dict[int, bool] = {}
    if player_ids:
        players = await session.execute(
            select(Player.id, Player.wallet_address, Player.wallet_verified).where(Player.id.in_(player_ids))
        )
        for pid, addr, verified in players.all():
            wallet_map[pid] = addr
            verified_map[pid] = verified
    revived = 0
    for payout in rows:
        if payout.kind in _TREASURY_KINDS:
            if settings.owner_wallet_address:
                payout.dest_address = normalize_address(settings.owner_wallet_address)
                payout.last_error = None
                revived += 1
            else:
                payout.status = "failed"
                payout.last_error = "нет адреса получателя: для доли казны задай OWNER_WALLET_ADDRESS"
        elif payout.player_id is None:
            payout.status = "failed"
            payout.last_error = "нет адреса получателя (кошелёк игрока не найден)"
        else:
            addr = wallet_map.get(payout.player_id) or ""
            is_verified = verified_map.get(payout.player_id, False)
            if addr and is_verified:
                payout.dest_address = addr
                payout.last_error = None
                revived += 1
            elif addr and not is_verified:
                payout.last_error = "кошелёк привязан, но не подтверждён (игрок должен отправить bv:<код>)"
            else:
                payout.last_error = "нет адреса получателя: кошелёк игрока ещё не привязан"
    await session.commit()
    return revived
