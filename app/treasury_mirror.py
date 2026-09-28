"""Зеркало казны: независимая копия истории активного кошелька казначея.

Сверка «тютелька в тютельку» достигается построением: зеркало хранит каждую
цепочечную транзакцию (сторону казначея) со своим balance_delta, а баланс
кошелька = Σ balance_delta от генезиса до головы цепочки. Отсюда:

  * нет допуска на газ — реальный fee берётся из цепочки (total_fees), а не
    из оценки payout_fee_gram, поэтому накопленный сдвиг оценки с ростом N
    исходящих НЕ превращается в «расхождение»;
  * неопознанные входящие (пыль, переводы мимо бота) видны строками
    kind=unknown_in — их сумма аудируется отдельно, а не мажется по общему
    допуску;
  * после бутстрапа (зеркало покрыло генезис→голову) тождество проверяется
    на каждом цикле синка и падает ровно на разницу Σ vs живой баланс:
    ноль в отчёте означает «сходится ±0» без всяких оговорок.

Модуль цепляется в:
  - ton_pay.treasury_diagnostics() — блок «Зеркало казны» в /treasury;
  - планировщик — фоновый синк (treasury_mirror_interval_seconds);
  - ops.check_anomalies() — ежедневная автосверка (один раз в сутки).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.core.registry import (
    TREASURY_MIRROR_BEAT_KEY,
    TREASURY_MIRROR_BOOTSTRAP_KEY,
    TREASURY_MIRROR_BOTTOM_KEY,
    TREASURY_MIRROR_CHECK_KEY,
    TREASURY_MIRROR_CURSOR_KEY,
    TREASURY_MIRROR_SOURCE_KEY,
)
from app.db import SessionLocal
from app.http_utils import get_http_client, http_get_with_retry
from app.models import Income, Payout, Stake, TreasuryMove, WatcherState
from app.payments import parse_bank_memo, parse_revote_memo, parse_verify_memo
from app.ton_codec import api_headers, extract_comment, norm_tx_hash
from app.ton_utils import normalize_address

logger = logging.getLogger(__name__)

# Страница истории (лимит индексатора), совмещена с выбором watcher'а.
_MIRROR_PAGE_LIMIT = 100


@dataclass(frozen=True)
class MirrorMove:
    """Нормализованная транзакция казначея со стороны кошелька."""

    tx_hash: str
    network: str
    utime: int
    lt: int
    direction: str  # in | out | self | other
    value_nanotons: int
    fee_nanotons: int
    balance_delta_nanotons: int
    counterparty: str
    comment: str
    success: bool
    provider: str = ""  # tonapi | toncenter

    @property
    def is_money_move(self) -> bool:
        return self.balance_delta_nanotons != 0


def _address_of(node: Any) -> str:
    """Адрес из конверта провайдера: строка или {'address': ...} (TonAPI)."""
    if isinstance(node, dict):
        return str(node.get("address") or "")
    return str(node or "")


def _self_direction(counterparty: str, treasury: str) -> bool:
    """Самоперевод казначея самому себе: сторона совпадает с активным адресом."""
    if not treasury or not counterparty:
        return False
    try:
        return normalize_address(counterparty) == normalize_address(treasury)
    except Exception:
        return False


def _derive_balance_delta(provider_delta: Any, in_value: int, out_value: int, fee: int) -> int:
    """Сальдо аккаунта: из цепочки, если провайдер дал; иначе вычислить."""
    if provider_delta is not None:
        try:
            return int(str(provider_delta))
        except (TypeError, ValueError) as exc:
            # Сальдо из цепочки мусорное — считаем из своих чисел, но след
            # оставляем: расхождение потом всплывёт в сверке «в ноль».
            logger.debug("balance_delta %r не разобран (%s) — считаем сами", provider_delta, exc)
    return in_value - out_value - fee


def _in_value(item: dict) -> tuple[int, str, str]:
    """(сумма, отправитель, комментарий) входящего сообщения транзакции."""
    in_msg = item.get("in_msg") or {}
    if not isinstance(in_msg, dict):
        return 0, "", ""
    try:
        value = int(in_msg.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    source = _address_of(in_msg.get("source"))
    comment = extract_comment(in_msg)
    return value, source, comment


def _out_value(item: dict) -> tuple[int, str, str]:
    """(сумма, получатель, комментарий) исходящих сообщений транзакции."""
    out_msgs = item.get("out_msgs") or []
    total = 0
    dest = ""
    comment = ""
    for msg in out_msgs:
        if not isinstance(msg, dict):
            continue
        try:
            value = int(msg.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        total += value
        if value > 0 and not dest:
            dest = _address_of(msg.get("destination"))
            comment = extract_comment(msg)
    return total, dest, comment


def _out_forward_fees(item: dict) -> int:
    """Сумма fwd_fee исходящих сообщений транзакции.

    Это плата за продвижение исходящих сообщений: она списывается с баланса
    отправителя, но не входит в total_fees (TonAPI/Toncenter), поэтому зеркало
    без неё завышает баланс казны (классический «пропал газ»).
    """
    total = 0
    for msg in item.get("out_msgs") or []:
        if not isinstance(msg, dict):
            continue
        try:
            total += int(msg.get("fwd_fee") or 0)
        except (TypeError, ValueError):
            continue
    return total


def parse_tonapi_move(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Транзакция TonAPI v2 -> нормализованное движение зеркала.

    Pure-функция над примитивом индексатора (никакой сети и БД): тестируется
    на фикстурах. Схлапывает входящее/исходящее сообщения и реальную комиссию
    (total_fees) в одно движение со знаком balance_delta.
    """
    if not isinstance(item, dict):
        return None
    hash_raw = str(item.get("hash") or "")
    if not hash_raw:
        return None
    in_value, in_source, in_comment = _in_value(item)
    out_value, out_dest, out_comment = _out_value(item)
    try:
        utime = int(item.get("utime") or 0)
        lt = int(item.get("lt") or 0)
    except (TypeError, ValueError):
        utime = 0
        lt = 0
    try:
        fee = int(item.get("total_fees") or 0)
    except (TypeError, ValueError):
        fee = 0
    # fwd_fee исходящих сообщений списывается с баланса отправителя, но
    # не входит в total_fees — учтём, чтобы Σ balance_delta совпадала с цепочкой.
    delta = _derive_balance_delta(
        item.get("balance_delta"), in_value, out_value, fee + _out_forward_fees(item)
    )
    if delta == 0 and in_value == 0 and out_value == 0:
        return None
    if in_value > 0:
        direction, counterparty, value, comment = "in", in_source, in_value, in_comment
    elif out_value > 0:
        direction, counterparty, value, comment = "out", out_dest, out_value, out_comment
    else:
        direction, counterparty, value, comment = "other", "", 0, ""
    if _self_direction(counterparty, treasury):
        direction = "self"
    success = bool(item.get("success", True))
    return MirrorMove(
        tx_hash=norm_tx_hash(hash_raw),
        network=network,
        utime=utime,
        lt=lt,
        direction=direction,
        value_nanotons=value,
        fee_nanotons=fee,
        balance_delta_nanotons=delta,
        counterparty=counterparty,
        comment=comment,
        success=success,
        provider="tonapi",
    )


def parse_toncenter_move(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Транзакция Toncenter v3 -> нормализованное движение зеркала.

    У Toncenter нет total_fees/balance_delta, как у TonAPI: комиссия лежит в
    поле fee, сальдо вычисляется самостоятельно (в − скидка исходящих входящих
    по этой транзакции). Это честный фолбэк для бутстрапа при молчащем TonAPI.
    """
    if not isinstance(item, dict):
        return None
    hash_raw = str(item.get("hash") or "")
    if not hash_raw:
        return None
    in_value, in_source, in_comment = _in_value(item)
    out_value, out_dest, out_comment = _out_value(item)
    try:
        utime = int(item.get("now") or 0)
        lt = int(item.get("lt") or 0)
    except (TypeError, ValueError):
        utime = 0
        lt = 0
    try:
        fee = int(item.get("total_fees") or item.get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0
    delta = _derive_balance_delta(
        item.get("balance_delta"), in_value, out_value, fee + _out_forward_fees(item)
    )
    if delta == 0 and in_value == 0 and out_value == 0:
        return None
    if in_value > 0:
        direction, counterparty, value, comment = "in", in_source, in_value, in_comment
    elif out_value > 0:
        direction, counterparty, value, comment = "out", out_dest, out_value, out_comment
    else:
        direction, counterparty, value, comment = "other", "", 0, ""
    if _self_direction(counterparty, treasury):
        direction = "self"
    return MirrorMove(
        tx_hash=norm_tx_hash(hash_raw),
        network=network,
        utime=utime,
        lt=lt,
        direction=direction,
        value_nanotons=value,
        fee_nanotons=fee,
        balance_delta_nanotons=delta,
        counterparty=counterparty,
        comment=comment,
        success=True,
        provider="toncenter",
    )


def parse_mirror_item(item: dict, network: str, treasury: str = "") -> MirrorMove | None:
    """Движение из транзакции любого провайдера (автодетект по полям)."""
    if not isinstance(item, dict):
        return None
    if "now" in item or "fee" in item:
        return parse_toncenter_move(item, network, treasury)
    return parse_tonapi_move(item, network, treasury)


# ---------- Классификация по мемо (чистые предикаты) ----------


def parse_way_memo(comment: str) -> tuple[str, int] | None:
    """(kind, id Payout) из служебного мемо исходящего «way:<день>:<kind>#<id>».

    Мемо — глобально уникальный ключ выплаты (анти-дубль диспетчера). Возвраты
    при паузе несут тот же ключ суффиксом после свободного текста (rfind),
    поэтому легаси-строки без ключа остаются None и связываются по tx_hash.
    """
    text = (comment or "").replace("\u200b", "")
    idx = text.rfind("way:")
    if idx == -1:
        return None
    payload = text[idx + len("way:") :]
    marker = payload.rsplit("#", 1)
    if len(marker) != 2 or not marker[1].isdigit():
        return None
    kind_part = marker[0].split(":", 1)
    if len(kind_part) < 2 or not kind_part[0] or not kind_part[1]:
        return None
    return kind_part[1], int(marker[1])


def classify_incoming(comment: str) -> str:
    """Базовый тег входящего движения по мемо: bank / revote / walletverify / stake."""
    if parse_bank_memo(comment):
        return "bank"
    if parse_revote_memo(comment) is not None:
        return "revote"
    if parse_verify_memo(comment) is not None:
        return "walletverify"
    return "stake"


def classify_outgoing(comment: str) -> str:
    """Базовый тег исходящего движения по мемо: refund / payout:<kind>."""
    parsed = parse_way_memo(comment)
    if parsed is None:
        return "unknown_out"
    kind, _payout_id = parsed
    return "refund" if kind == "refund" else f"payout:{kind}"


# ---------- Связка с БД (диспетчерская классификация) ----------


def _incoming_kind_from_income(income: Income) -> str:
    """Точный тег входящего из note watcher'а («in:<result>;src:…»)."""
    note = income.note or ""
    for marker, tag in (
        ("in:stake", "stake"),
        ("in:revote", "revote"),
        ("in:walletverify", "walletverify"),
        ("in:bank", "bank"),
        ("in:paused", "paused"),
        ("in:unknown", "unknown_in"),
    ):
        if marker in note:
            return tag
    if income.kind == "ton":
        return "income"
    return "unknown_in"


async def resolve_kind(session, move: MirrorMove) -> tuple[str, int | None]:
    """Полная классификация движения по БД: (kind, linked_id).

    Входящее: watcher уже создал Income (unit_ref=tx_hash) и, если это ставка,
    строку Stake (tx_hash+network) — берём точный тег из note. Чужие/пыльный
    приход без строк БД остаётся unknown_in. Исходящее: связываем по
    служебному мемо (way:…:kind#id) или, для легаси-строк, по tx_hash выплаты.
    Самопереводы казначея проходят как self.
    """
    if move.direction == "self":
        return "self", None
    if move.direction == "other":
        return "other", None
    if move.direction == "in":
        income = (
            await session.execute(
                select(Income).where(Income.unit_ref == move.tx_hash).limit(1)
            )
        ).scalar_one_or_none()
        if income is not None:
            return _incoming_kind_from_income(income), income.id
        stake = (
            await session.execute(
                select(Stake).where(
                    Stake.tx_hash == move.tx_hash,
                    Stake.network == move.network,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if stake is not None:
            return "stake", stake.id
        return "unknown_in", None
    # Исходящее: сначала служебное memo (уникальный ключ выплаты), потом хеш.
    parsed = parse_way_memo(move.comment)
    if parsed is not None:
        kind, payout_id = parsed
        payout = await session.get(Payout, payout_id)
        if payout is not None:
            tag = "refund" if kind == "refund" else f"payout:{kind}"
            return tag, payout.id
    payout = (
        await session.execute(
            select(Payout)
            .where(Payout.tx_hash == move.tx_hash)
            .order_by(Payout.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if payout is not None:
        tag = "refund" if payout.kind == "refund" else f"payout:{payout.kind}"
        return tag, payout.id
    return "unknown_out", None


# ---------- Состояние зеркала в watcher_state ----------


async def _state_int(session, key: str) -> int | None:
    row = await session.get(WatcherState, key)
    if row is None or not (row.value or "").isdigit():
        return None
    return int(row.value)


async def _set_state(session, key: str, value: str) -> None:
    row = await session.get(WatcherState, key)
    if row is None:
        session.add(WatcherState(key=key, value=value))
    else:
        row.value = value


# ---------- Чтение истории (TonAPI → фолбэк Toncenter) ----------


async def _fetch_page(before_lt: int | None = None) -> tuple[list[MirrorMove], str, bool]:
    """Страница истории казначея (новые сверху): (движения, источник, ok).

    TonAPI — основной источник; при его сбое отдаём страницу Toncenter v3.
    ok=False — оба провайдера молчат: цикл не двигает состояние зеркала
    (курсор не тронут, сердцебиение не ставится).
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], "none", False
    network = "testnet" if settings.is_testnet else "mainnet"
    treasury = settings.active_treasury_address
    client = get_http_client()
    candidates = (
        (
            "tonapi",
            f"{settings.active_ton_api_base.rstrip('/')}/v2/blockchain/accounts/{treasury}/transactions",
            settings.ton_api_key,
        ),
        (
            "toncenter",
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions",
            settings.toncenter_api_key,
        ),
    )
    for kind, url, api_key in candidates:
        try:
            params: dict[str, Any] = {"limit": _MIRROR_PAGE_LIMIT, "sort_order": "desc"}
            if kind == "toncenter":
                params["account"] = treasury
                params["sort"] = "desc"
            if before_lt is not None:
                params["before_lt"] = str(before_lt)
            response = await http_get_with_retry(
                client, url, params=params, headers=api_headers(api_key)
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("transactions") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                # 200 БЕЗ списка транзакций — это не «история пуста», а сбой формы
                # ответа: честный пустой кошелёк приходит ключом с пустым
                # списком. Иначе один битый ответ навсегда объявил бы зеркало
                # выстроенным на нулевых строках, тождество показало бы
                # расхождение на всю казну и ежедневная автосверка подняла бы
                # тревогу до ручного /mirror reset. Считаем провайдера
                # недоступным и пробуем следующего.
                raise ValueError("в ответе нет списка transactions")
            moves: list[MirrorMove] = []
            for item in items:
                move = (
                    parse_toncenter_move(item, network, treasury)
                    if kind == "toncenter"
                    else parse_tonapi_move(item, network, treasury)
                )
                if move is not None:
                    moves.append(move)
            return moves, kind, True
        except Exception as exc:
            logger.warning("Зеркало: история (%s) недоступна: %s", kind, exc)
    return [], "none", False


# ---------- Применение движений (идемпотентный upsert по tx_hash) ----------


async def _resolve_kinds_batch(session, moves: list[MirrorMove]) -> dict[str, tuple[str, int | None]]:
    """Классификация пачки движений батчем: tx_hash → (kind, linked_id).

    По одному запросу на таблицу, а не N запросов на движение: страница в
    сто транзакций обрабатывается без сотни round-trip'ов к SQLite/Postgres.
    """
    result: dict[str, tuple[str, int | None]] = {}
    in_moves = [m for m in moves if m.direction == "in"]
    out_moves = [m for m in moves if m.direction == "out"]
    income_by_ref: dict[str, Income] = {}
    stake_by_hash: dict[str, Stake] = {}
    if in_moves:
        hashes = [m.tx_hash for m in in_moves]
        for income in (
            await session.execute(select(Income).where(Income.unit_ref.in_(hashes)))
        ).scalars():
            income_by_ref[income.unit_ref] = income
        for stake in (
            await session.execute(select(Stake).where(Stake.tx_hash.in_(hashes)))
        ).scalars():
            stake_by_hash.setdefault(stake.tx_hash, stake)
    payout_by_id: dict[int, Payout] = {}
    payout_by_hash: dict[str, Payout] = {}
    wanted_ids: set[int] = set()
    leftover: set[str] = set()
    for move in out_moves:
        parsed = parse_way_memo(move.comment)
        if parsed is not None:
            wanted_ids.add(parsed[1])
        else:
            leftover.add(move.tx_hash)
    if wanted_ids:
        for payout in (
            await session.execute(select(Payout).where(Payout.id.in_(wanted_ids)))
        ).scalars():
            payout_by_id[payout.id] = payout
    if leftover:
        for payout in (
            await session.execute(select(Payout).where(Payout.tx_hash.in_(leftover)))
        ).scalars():
            payout_by_hash.setdefault(payout.tx_hash, payout)
    for move in moves:
        if move.direction == "self":
            result[move.tx_hash] = ("self", None)
        elif move.direction == "other":
            result[move.tx_hash] = ("other", None)
        elif move.direction == "in":
            income = income_by_ref.get(move.tx_hash)
            if income is not None:
                result[move.tx_hash] = (_incoming_kind_from_income(income), income.id)
                continue
            stake = stake_by_hash.get(move.tx_hash)
            if stake is not None:
                result[move.tx_hash] = ("stake", stake.id)
                continue
            result[move.tx_hash] = ("unknown_in", None)
        else:
            parsed = parse_way_memo(move.comment)
            if parsed is not None and parsed[1] in payout_by_id:
                payout = payout_by_id[parsed[1]]
                tag = "refund" if parsed[0] == "refund" else f"payout:{parsed[0]}"
                result[move.tx_hash] = (tag, payout.id)
                continue
            payout = payout_by_hash.get(move.tx_hash)
            if payout is not None:
                tag = "refund" if payout.kind == "refund" else f"payout:{payout.kind}"
                result[move.tx_hash] = (tag, payout.id)
                continue
            result[move.tx_hash] = ("unknown_out", None)
    return result


async def _apply_page(
    session, moves: list[MirrorMove], kinds: dict[str, tuple[str, int | None]]
) -> tuple[int, int]:
    """Запись/обновление пачки движений. Возвращает (added, updated).

    Идемпотентно по tx_hash: повторный проход окна (перекрытие курсора или
    реорганизация) не плодит строк — существующая строка обновляется под
    текущее состояние цепочки (лёгкая перезапись при reorg).
    """
    if not moves:
        return 0, 0
    hashes = [m.tx_hash for m in moves]
    rows = (
        await session.execute(select(TreasuryMove).where(TreasuryMove.tx_hash.in_(hashes)))
    ).scalars().all()
    by_hash: dict[str, TreasuryMove] = {row.tx_hash: row for row in rows}
    added = updated = 0
    for move in moves:
        kind, linked = kinds[move.tx_hash]
        row = by_hash.get(move.tx_hash)
        if row is None:
            session.add(
                TreasuryMove(
                    tx_hash=move.tx_hash,
                    network=move.network,
                    utime=move.utime,
                    lt=move.lt,
                    direction=move.direction,
                    kind=kind,
                    value_nanotons=move.value_nanotons,
                    fee_nanotons=move.fee_nanotons,
                    balance_delta_nanotons=move.balance_delta_nanotons,
                    counterparty=move.counterparty,
                    comment=move.comment[:200],
                    linked_id=linked,
                    success=move.success,
                )
            )
            added += 1
            continue
        changed = (
            row.lt != move.lt
            or row.utime != move.utime
            or row.balance_delta_nanotons != move.balance_delta_nanotons
            or row.direction != move.direction
        )
        row.lt = move.lt
        row.utime = move.utime
        row.balance_delta_nanotons = move.balance_delta_nanotons
        row.direction = move.direction
        row.kind = kind
        row.linked_id = linked
        row.counterparty = move.counterparty or row.counterparty
        row.comment = move.comment[:200] or row.comment
        row.success = move.success
        if move.value_nanotons:
            row.value_nanotons = move.value_nanotons
        if move.fee_nanotons:
            row.fee_nanotons = move.fee_nanotons
        updated += int(changed)
    await session.flush()
    return added, updated


# ---------- Синк: бутстрап от генезиса и инкремент к голове ----------


def _active_network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


async def sync_treasury_mirror() -> dict:
    """Один цикл синка зеркала. Возвращает сводку для лога/отчёта.

    Два режима:
      * бутстрап — спуск от головы вглубь (страницами по before_lt) до дна
        истории провайдера; дно фиксируется в BOTTOM, при пустой странице
        зеркало объявляется выстроенным (BOOTSTRAPPED) и тождество измеряется;
      * инкремент — новые транзакции выше головы (CURSOR), до пересечения
        известной границы; при реорганизации монтируется перезапись строк.

    Каждая страница коммитится отдельно: краш между страницами не теряет
    наработанного прогресса. Когда зеркало выстроено, цикл дополнительно
    проверяет тождество «Σ balance_delta = живой баланс» и кладет результат
    в CHECK (читается автосверкой без лишнего запроса к индексатору).
    """
    summary = {
        "pages": 0,
        "added": 0,
        "updated": 0,
        "source": "none",
        "bootstrapped": False,
        "exact": None,
        "diff_nanotons": None,
        "mirror_balance": None,
        "chain_balance": None,
    }
    if not settings.ton_enabled or not settings.active_treasury_address:
        return summary
    max_pages = max(1, settings.treasury_mirror_max_pages_per_sync)
    network = _active_network()

    async with SessionLocal() as session:
        bootstrapped = (
            await session.get(WatcherState, TREASURY_MIRROR_BOOTSTRAP_KEY)
        ) is not None
        head_lt = await _state_int(session, TREASURY_MIRROR_CURSOR_KEY)
        bottom_lt = await _state_int(session, TREASURY_MIRROR_BOTTOM_KEY)
        pages = added = updated = 0
        source = "none"
        page_ok = False

        if not bootstrapped:
            # Спуск к генезису: продолжаем с достигнутого дна либо с головы.
            before_lt = bottom_lt
            for _ in range(max_pages):
                moves, source, ok = await _fetch_page(before_lt)
                if not ok:
                    break
                page_ok = True
                pages += 1
                if not moves:
                    bootstrapped = True
                    await _set_state(session, TREASURY_MIRROR_BOOTSTRAP_KEY, "1")
                    await session.commit()
                    break
                if head_lt is None:
                    head_lt = moves[0].lt
                kinds = await _resolve_kinds_batch(session, moves)
                part_added, part_updated = await _apply_page(session, moves, kinds)
                added += part_added
                updated += part_updated
                before_lt = min(move.lt for move in moves)
                await _set_state(session, TREASURY_MIRROR_BOTTOM_KEY, str(before_lt))
                if head_lt is not None:
                    await _set_state(session, TREASURY_MIRROR_CURSOR_KEY, str(head_lt))
                await session.commit()
        else:
            # Новое поверх головы; первая страница — самая свежая.
            before_lt = None
            for _ in range(max_pages):
                moves, source, ok = await _fetch_page(before_lt)
                if not ok:
                    break
                page_ok = True
                pages += 1
                if not moves:
                    break
                page_max = max(move.lt for move in moves)
                if head_lt is not None and page_max <= head_lt:
                    break  # самая свежая уже учтена — ничего нового
                kinds = await _resolve_kinds_batch(session, moves)
                part_added, part_updated = await _apply_page(session, moves, kinds)
                added += part_added
                updated += part_updated
                prev_head = head_lt
                head_lt = max(head_lt or 0, page_max)
                await _set_state(session, TREASURY_MIRROR_CURSOR_KEY, str(head_lt))
                await session.commit()
                before_lt = min(move.lt for move in moves)
                if prev_head is not None and before_lt <= prev_head:
                    # Страница пересекла известную границу — хвост под меткой,
                    # новые транзакции выше головы все учтены.
                    break

        if page_ok:
            await _set_state(session, TREASURY_MIRROR_SOURCE_KEY, source)
        summary.update(
            pages=pages,
            added=added,
            updated=updated,
            source=source,
            bootstrapped=bootstrapped,
        )

        if not bootstrapped:
            return summary

        mirror_sum = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(TreasuryMove.balance_delta_nanotons), 0))
                    .where(TreasuryMove.network == network)
                )
            ).scalar_one()
        )
        # Безопасно: ton_pay импортируется локально — ton_pay загружает тяжёлые
        # pytoniq-зависимости и держит циклический импорт с этим модулем.
        from app.ton_pay import fetch_account_state

        try:
            chain_balance, _status, _src = await fetch_account_state()
        except Exception as exc:
            logger.warning("Зеркало: живой баланс для сверки не прочитан: %s", exc)
            chain_balance = None
        exact = None
        diff = None
        if chain_balance is not None:
            diff = mirror_sum - chain_balance
            exact = diff == 0
            await _set_state(
                session,
                TREASURY_MIRROR_CHECK_KEY,
                json.dumps(
                    {
                        "exact": exact,
                        "diff_nanotons": diff,
                        "mirror_balance": mirror_sum,
                        "chain_balance": chain_balance,
                        "checked_at": datetime.now(UTC).isoformat(),
                        "source": source,
                    }
                ),
            )
        summary.update(
            exact=exact,
            diff_nanotons=diff,
            mirror_balance=mirror_sum,
            chain_balance=chain_balance,
        )
        await _set_state(session, TREASURY_MIRROR_BEAT_KEY, datetime.now(UTC).isoformat())
        await session.commit()
        logger.info(
            "Зеркало казны: страниц %d, +%d/%d, источник %s, бутстрап %s, "
            "тождество %s (diff %.4f Gram)",
            pages, added, updated, source, "да" if bootstrapped else "нет",
            "±0" if exact else "N/A", (diff or 0) / 1e9,
        )
    return summary


async def reset_treasury_mirror() -> str:
    """Полный re-bootstrap зеркала по команде хранителя (/mirror reset confirm).

    Сбрасывает состояние синка (голова/дно/флаг выстроенности/сверка), но
    НЕ трогает строки treasury_moves: следующий цикл синка пересканирует
    историю от головы к генезису и идемпотентно перезапишет движения под
    текущую цепочку. Именно так лечатся глубокие реорганизации — строки,
    изменившие баланс, перезапишутся, новые добавятся, а удалённые реоргом
    транзакции останутся висеть как «фантом», если не подчищать их отдельно.
    """
    async with SessionLocal() as session:
        for key in (
            TREASURY_MIRROR_BOOTSTRAP_KEY,
            TREASURY_MIRROR_BOTTOM_KEY,
            TREASURY_MIRROR_CURSOR_KEY,
            TREASURY_MIRROR_CHECK_KEY,
            TREASURY_MIRROR_BEAT_KEY,
            TREASURY_MIRROR_SOURCE_KEY,
        ):
            row = await session.get(WatcherState, key)
            if row is not None:
                await session.delete(row)
        await session.commit()
    return (
        "Зеркало казны: состояние сброшено — следующий цикл синка перестроит "
        "историю от головы к генезису и сверит тождество заново"
    )


async def mirror_balance(session, network: str) -> int:
    """Сумма сальдо всех движений зеркала активного контура."""
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(TreasuryMove.balance_delta_nanotons), 0))
                .where(TreasuryMove.network == network)
            )
        ).scalar_one()
    )


# ---------- Отчёт для хранителя (/treasury) ----------

_KIND_LABELS = {
    "stake": "ставки",
    "revote": "смена пути",
    "walletverify": "проверка кошелька",
    "paused": "на паузе (возврат)",
    "income": "прочий приход",
    "unknown_in": "пыль/чужие ⚠️",
    "refund": "возвраты",
    "self": "self-переводы",
    "other": "служебные",
    "unknown_out": "вывод мимо бота ⚠️",
}


def _kind_label(kind: str) -> str:
    if kind.startswith("payout:"):
        return f"выплаты:{kind.split(':', 1)[1]}"
    return _KIND_LABELS.get(kind, kind)


async def treasury_mirror_stats(session) -> dict:
    """Сводка зеркала для отчёта: покрытие, балансы, трафик по типам."""
    network = _active_network()
    bootstrapped = (
        await session.get(WatcherState, TREASURY_MIRROR_BOOTSTRAP_KEY)
    ) is not None
    head_lt = await _state_int(session, TREASURY_MIRROR_CURSOR_KEY)
    bottom_lt = await _state_int(session, TREASURY_MIRROR_BOTTOM_KEY)
    beat_row = await session.get(WatcherState, TREASURY_MIRROR_BEAT_KEY)
    source_row = await session.get(WatcherState, TREASURY_MIRROR_SOURCE_KEY)
    check_row = await session.get(WatcherState, TREASURY_MIRROR_CHECK_KEY)
    move_count = int(
        (
            await session.execute(
                select(func.count()).select_from(TreasuryMove).where(TreasuryMove.network == network)
            )
        ).scalar_one()
    )
    last_utime = (
        await session.execute(
            select(func.max(TreasuryMove.utime)).where(TreasuryMove.network == network)
        )
    ).scalar_one()
    groups = {
        kind: {
            "count": count,
            "delta": int(delta or 0),
            "value": int(value or 0),
            "fee": int(fee or 0),
        }
        for kind, count, delta, value, fee in (
            await session.execute(
                select(
                    TreasuryMove.kind,
                    func.count(),
                    func.sum(TreasuryMove.balance_delta_nanotons),
                    func.sum(TreasuryMove.value_nanotons),
                    func.sum(TreasuryMove.fee_nanotons),
                )
                .where(TreasuryMove.network == network)
                .group_by(TreasuryMove.kind)
            )
        ).all()
    }
    check: dict = {}
    if check_row is not None:
        try:
            check = json.loads(check_row.value or "{}")
        except (ValueError, TypeError):
            check = {}
    return {
        "bootstrapped": bootstrapped,
        "head_lt": head_lt,
        "bottom_lt": bottom_lt,
        "beat_iso": beat_row.value if beat_row is not None else None,
        "source": source_row.value if source_row is not None else None,
        "move_count": move_count,
        "last_utime": last_utime,
        "groups": groups,
        "check": check if isinstance(check, dict) else {},
    }


def _exact_line(check: dict, bootstrapped: bool) -> str:
    """Строка тождества: ±0 при точной сверке, иначе честный диагноз."""
    if not bootstrapped:
        return "тождество: недоступно — бутстрап ещё идёт (сверка стартует после покрытия истории)"
    if not check:
        return "тождество: пока не измерено"
    exact = check.get("exact")
    if exact is True:
        mirror = int(check.get("mirror_balance") or 0)
        return (
            f"тождество: сходится ±0 ✓ (Σ {mirror / 1e9:.4f} Gram = баланс цепочки)"
        )
    diff = int(check.get("diff_nanotons") or 0)
    return (
        f"тождество: расходится на {diff / 1e9:+.4f} Gram ⚠️ — "
        "зеркало не совпадает с цепочкой, смотри /blockchain и логи зеркала"
    )


async def treasury_mirror_block() -> str:
    """Текстовый блок «Зеркало казны» для /treasury (без доступа к сети)."""
    from datetime import UTC as _UTC

    async with SessionLocal() as session:
        stats = await treasury_mirror_stats(session)
    network = _active_network()
    lines = [f"Зеркало казны ({network}):"]
    if not settings.ton_enabled or not settings.active_treasury_address:
        lines.append("  не активно (TON выключен или адрес казначея не задан)")
        return "\n".join(lines)
    beat_age = None
    if stats["beat_iso"]:
        try:
            moment = datetime.fromisoformat(stats["beat_iso"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=_UTC)
            beat_age = max(0, int((datetime.now(_UTC) - moment).total_seconds()))
        except ValueError as exc:
            # Метка зеркала битая: возраст неизвестен, и /treasury так и скажет.
            logger.debug("Метка времени зеркала не разобрана (%r): %s", stats.get("beat_iso"), exc)
    lag = None
    if stats["last_utime"] is not None:
        lag = max(0, int(datetime.now(_UTC).timestamp()) - int(stats["last_utime"]))
    moves = stats["move_count"]
    if moves == 0 and not stats["bootstrapped"]:
        lines.append("  история кошелька пуста либо бутстрап ещё не начался")
    else:
        cov = f"{moves:,}".replace(",", " ")
        lag_part = "—"
        if lag is not None and lag >= 0:
            lag_part = f"{lag // 60} мин назад" if lag >= 60 else "только что"
        src = f" · источник {stats['source']}" if stats["source"] else ""
        lines.append(f"  покрытие: {cov} транзакций · актуально {lag_part}{src}")
    if beat_age is not None:
        lines.append(f"  последний цикл: {beat_age} с назад")
    elif moves:
        lines.append("  последний цикл: не завершался (индексаторы молчат) ⚠️")
    if stats["bootstrapped"]:
        lines.append(f"  {_exact_line(stats['check'], True)}")
        groups = stats["groups"]
        traffic = [g for g in groups if g not in ("self", "other")]
        if traffic:
            shown = ", ".join(
                f"{_kind_label(kind)} {groups[kind]['count']} "
                f"({groups[kind]['value'] / 1e9:.2f} Gram)"
                for kind in sorted(traffic)
            )
            lines.append(f"  трафик: {shown}")
        fees = sum(g["fee"] for g in groups.values())
        if fees:
            lines.append(
                f"  газ (реальный, из цепочки): {fees / 1e9:.4f} Gram суммарно"
            )
        unknown = (
            groups.get("unknown_in", {}).get("value", 0)
            + groups.get("unknown_out", {}).get("value", 0)
        )
        if unknown:
            lines.append(
                f"  ☑️ непонятых переводов на {unknown / 1e9:.3f} Gram — "
                "разбери вручную (пыль/jetton/вывод мимо бота)"
            )
    else:
        remaining_note = ""
        if stats["bottom_lt"] is not None:
            remaining_note = " — история дотягивается от головы к генезису"
        lines.append(f"  бутстрап: идёт{remaining_note}, сверка станет возможной после покрытия")
    return "\n".join(lines)