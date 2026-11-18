"""Гигиена очереди выплат: возврат зависших sending/failed в очередь
и оживление выплат, чей кошелёк привязали уже после финализации дня."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from app.config import settings
from app.models import Payout, Player

logger = logging.getLogger(__name__)

_TREASURY_KINDS = {"rake", "leaderboard"}

def _stale_send_cutoff(session) -> datetime:
    """Граница «мёртвого» клейма: окно вещания + 30 с в TZ, понятной драйверу."""
    cutoff_dt = datetime.now(UTC) - timedelta(
        seconds=settings.payout_send_timeout_seconds + 30
    )
    # На Postgres колонка timezone=True сравнивается с aware-значением; naive
    # cutoff там трактуется в TZ сессии, и на сервере не в UTC «мёртвый» клейм
    # выглядел бы живым. SQLite хранит naive UTC — ему aware-параметр не годится.
    if session.get_bind().dialect.name == "postgresql":
        return cutoff_dt
    return cutoff_dt.replace(tzinfo=None)


def _retriable_predicates(network: str):
    """Базовые условия оживления: сеть, неисчерпанный лимит, есть получатель."""
    return (
        Payout.network == network,
        Payout.attempts < settings.payout_max_attempts,
        Payout.dest_address != "",
    )


def _dead_claim_predicate(cutoff):
    """Клейм мёртв: сирота до claimed_at либо старше окна вещания + 30 с."""
    return or_(
        Payout.claimed_at.is_(None),
        Payout.claimed_at <= cutoff,
    )


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

    Предикаты — В САМОМ UPDATE, а не в Python. Два раздельных UPDATE'а
    вместо одного «выбрать по id, перевести в pending»:
    - на двух репликах UPDATE без предикатов делал массовую перезапись
      статусов: первая реплика могла сбросить строку в pending, пока
      вторая уже держала её в sending — без WHERE status='sending'
      вторая реплика не отличила бы «уже сброшено» от «моё», и дубль
      сбрасывался по кругу. С предикатом в UPDATE строка, ушедшая в
      pending, не подходит под WHERE — повторный запуск no-op;
    - предикат attempts<max — тоже в UPDATE: лимит попыток проверяется
      гонке двух UPDATE'ов атомарно (один пишет, второй уже не видит).
    Это страховка на пару с условным UPDATE-клеймом в dispatch_pending_payouts
    (WHERE status='pending' перед переводом в sending): двойная отправка
    закрывается целиком (атомарный клейм + атомарный reset).
    """
    cutoff = _stale_send_cutoff(session)
    base_predicates = _retriable_predicates(network)
    # failed оживает сразу — никакого среза по времени: «мёртвая» строка.
    await session.execute(
        update(Payout)
        .where(Payout.status == "failed", *base_predicates)
        .values(status="pending")
    )
    # sending — только заведомо мёртвая (либо сирота до claimed_at,
    # либо клейм старше окна вещания + 30 с).
    await session.execute(
        update(Payout)
        .where(
            Payout.status == "sending",
            *base_predicates,
            _dead_claim_predicate(cutoff),
        )
        .values(status="pending")
    )
    # Мёртвый клейм с ИСЧЕРПАННЫМ лимитом попыток — рестарт/краш ровно на
    # последней попытке. Оживить его гигиена не имеет права (attempts == max),
    # а сверка sent такие строки не видит вовсе: без этого UPDATE строка висит
    # в sending вечно — деньги не двигаются, долг блокирует /resetgame, алерта
    # нет. В dead-letter с видимой причиной: судьбу решает ручной разбор
    # (/payouts → retry; повтор всё равно пройдёт анти-дубль по memo).
    await session.execute(
        update(Payout)
        .where(
            Payout.status == "sending",
            Payout.network == network,
            Payout.attempts >= settings.payout_max_attempts,
            Payout.dest_address != "",
            _dead_claim_predicate(cutoff),
        )
        .values(
            status="failed",
            last_error=(
                "вещание оборвалось на последней попытке (рестарт процесса?) — "
                "судьба перевода неизвестна, нужен ручной разбор"
            ),
        )
    )


async def _unaddressed_pending_rows(session, network: str) -> list[Payout]:
    """Pending-выплаты без получателя: их и оживляем при появлении адреса."""
    return list(
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


async def _player_wallet_maps(
    session, player_ids: set[int]
) -> tuple[dict[int, str], dict[int, bool]]:
    """Карты «игрок → адрес» и «игрок → подтверждён» одним запросом."""
    wallet_map: dict[int, str] = {}
    verified_map: dict[int, bool] = {}
    if not player_ids:
        return wallet_map, verified_map
    players = await session.execute(
        select(Player.id, Player.wallet_address, Player.wallet_verified).where(
            Player.id.in_(player_ids)
        )
    )
    for pid, addr, verified in players.all():
        wallet_map[pid] = addr
        verified_map[pid] = verified
    return wallet_map, verified_map


def _hydrate_treasury_dest(payout: Payout) -> bool:
    """Доля казны без игрока: адрес хранителя либо честный failed."""
    from app.ton_utils import normalize_address

    if settings.owner_wallet_address:
        payout.dest_address = normalize_address(settings.owner_wallet_address)
        payout.last_error = None
        return True
    payout.status = "failed"
    payout.last_error = "нет адреса получателя: для доли казны задай OWNER_WALLET_ADDRESS"
    return False


def _hydrate_player_dest(
    payout: Payout,
    player_id: int,
    wallet_map: dict[int, str],
    verified_map: dict[int, bool],
) -> bool:
    """Выплата игроку: адрес подставляется только подтверждённого кошелька."""
    addr = wallet_map.get(player_id) or ""
    is_verified = verified_map.get(player_id, False)
    if addr and is_verified:
        payout.dest_address = addr
        payout.last_error = None
        return True
    if addr and not is_verified:
        payout.last_error = "кошелёк привязан, но не подтверждён (игрок должен отправить bv:<код>)"
    else:
        payout.last_error = "нет адреса получателя: кошелёк игрока ещё не привязан"
    return False


def _hydrate_payout_dest(
    payout: Payout, wallet_map: dict[int, str], verified_map: dict[int, bool]
) -> bool:
    """Оживляет одну строку; False — ждёт действия игрока или админа."""
    # Доля казны — ТОЛЬКО строки без игрока: kind="leaderboard" с
    # player_id — это приз игрока из месячного топа, его кошелёк
    # подставляется по карте игроков, а не адресом хранителя.
    if payout.player_id is None and payout.kind in _TREASURY_KINDS:
        return _hydrate_treasury_dest(payout)
    player_id = payout.player_id
    if player_id is None:
        payout.status = "failed"
        payout.last_error = "нет адреса получателя (кошелёк игрока не найден)"
        return False
    return _hydrate_player_dest(payout, player_id, wallet_map, verified_map)


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
    rows = await _unaddressed_pending_rows(session, network)
    if not rows:
        return 0
    player_ids = {p.player_id for p in rows if p.player_id is not None}
    wallet_map, verified_map = await _player_wallet_maps(session, player_ids)
    revived = sum(
        _hydrate_payout_dest(payout, wallet_map, verified_map) for payout in rows
    )
    await session.commit()
    return revived
