"""Диагностика казначея для /treasury и /blockchain.

Глазами блокчейн-контура: баланс, пара мнемоника/адрес, очередь выплат,
watcher-состояние, зеркало казны. Этот модуль дёргается из admin-хендлеров
(`/treasury`, `/blockchain`) и не должен зависеть от логики отправки.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import func, or_, select

from app.config import settings
from app.db import SessionLocal
from app.http_utils import get_http_client, http_get_with_retry
from app.models import Income, Payout, Player, WatcherState
from app.ton_codec import api_headers
from app.ton_utils import friendly_address, from_nano, normalize_address

from . import state as _state
from . import wallet as _wallet_pkg

logger = logging.getLogger(__name__)


async def _tonapi_account_raw(address: str) -> dict:
    url = f"{settings.active_ton_api_base}/v2/accounts/{address}"
    headers = api_headers(settings.ton_api_key)
    client = get_http_client()
    response = await http_get_with_retry(client, url, headers=headers)
    response.raise_for_status()
    return response.json()


async def _toncenter_account(address: str) -> dict:
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/accountInformation"
    headers = api_headers(settings.toncenter_api_key)
    # v3 ждёт query-параметр «account», а не «address» (как в /api/v3/transactions).
    client = get_http_client()
    response = await http_get_with_retry(client, url, params={"account": address}, headers=headers)
    response.raise_for_status()
    return response.json()


async def fetch_account_state() -> tuple[int | None, str | None, str]:
    """(баланс в нанотонах | None, статус аккаунта | None, источник данных).

    TonAPI → фолбэк Toncenter v3; оба молчат — (None, None, "none").
    """
    address = settings.active_treasury_address
    try:
        data = await _tonapi_account_raw(address)
        balance = int(str(data.get("balance") or 0))
        status = str(data.get("status") or "")
        return balance, (status or None), "tonapi"
    except Exception as exc:
        logger.warning("Баланс казначея через TonAPI недоступен: %s", exc)
    try:
        data = await _toncenter_account(address)
        return int(str(data.get("balance") or 0)), None, "toncenter"
    except Exception as exc:
        logger.warning("Баланс казначея через Toncenter недоступен: %s", exc)
    return None, None, "none"


def treasury_pair_check_text() -> str:
    """Сверка пары мнемоника/адрес без выхода в сеть: v4r2/v5r1 → адрес."""
    from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

    words = settings.active_treasury_mnemonic.replace("\n", " ").split()
    if len(words) < 12:
        return f"мнемоника неполная ({len(words)} слов вместо 24) ⚠️"
    try:
        _, private_key = mnemonic_to_private_key(words)
    except Exception:
        return "мнемоника невалидна ⚠️"
    public_key = private_key_to_public_key(private_key)
    network = "testnet" if settings.is_testnet else "mainnet"
    version, _candidates = _wallet_pkg.detect_wallet_version(
        public_key, settings.active_treasury_address, _wallet_pkg.NETWORK_GLOBAL_IDS[network]
    )
    if version is not None:
        return f"{version} ✓ (детект по адресу)"
    return (
        "ни v4r2, ни v5r1 не дают настроенный адрес ⚠️ — "
        "проверь пару мнемоника/адрес или задай TREASURY_WALLET_VERSION явно"
    )


async def treasury_diagnostics() -> str:
    """Полный отчёт по казначею одной строкой-текстом для /treasury."""
    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"🏛 Казначей ({network})"]
    if not settings.ton_enabled:
        lines.append("TON выключен (TON_ENABLED=false): ставки и выплаты не работают.")
        return "\n".join(lines)
    if settings.active_treasury_address:
        shown = friendly_address(settings.active_treasury_address, testnet=settings.is_testnet)
        lines.append(f"Адрес: <code>{shown}</code>")
    else:
        lines.append("Адрес не задан ⚠️")
    lines.append("Мнемоника: " + ("задана ✓" if settings.active_treasury_mnemonic else "НЕ задана ⚠️"))
    lines.append(
        "OWNER_WALLET_ADDRESS: "
        + ("задан ✓" if settings.owner_wallet_address else "не задан — доли казны (рейк/копилка) уйти не могут ⚠️")
    )
    if (
        settings.active_treasury_address
        and settings.owner_wallet_address
        and normalize_address(settings.owner_wallet_address)
        == normalize_address(settings.active_treasury_address)
    ):
        lines.append("⚠️ OWNER_WALLET_ADDRESS совпадает с казначеем: рейк уходит «сам себе» — задай отдельный кошелёк владельца.")
    if settings.active_treasury_address and settings.active_treasury_mnemonic:
        try:
            lines.append(f"Пара мнемоника/адрес: {treasury_pair_check_text()}")
        except Exception as exc:
            lines.append(f"Пара мнемоника/адрес: не проверена ({exc})")
        balance, status, source = await fetch_account_state()
        if balance is None:
            lines.append("Баланс: недоступен (оба индексатора молчат) ⚠️")
        else:
            note = f", статус {status}" if status else ""
            lines.append(f"Баланс: {from_nano(balance):.4f} Gram{note} · источник {source}")
            if balance <= 0:
                lines.append("Баланс пуст: пополнить через @testgiver_ton_bot (testnet).")
    # Сверка с ожиданиями БД, корректировки казны и стоп-кран (/adjust, /pause).
    from app.ops import (
        MANUAL_IN_KIND,
        MANUAL_OUT_KIND,
        is_game_paused,
        paused_reason,
        treasury_expected_state,
    )

    try:
        async with SessionLocal() as session:
            drift_state = await treasury_expected_state(session)
            adjustments = (
                await session.execute(
                    select(
                        Income.kind,
                        func.count(),
                        func.coalesce(func.sum(Income.amount_nanotons), 0),
                    )
                    .where(Income.kind.in_([MANUAL_OUT_KIND, MANUAL_IN_KIND]))
                    .group_by(Income.kind)
                )
            ).all()
            paused = await is_game_paused(session)
            reason = await paused_reason(session)
    except Exception:
        logger.warning("Сверка казны для /treasury не собралась", exc_info=True)
        drift_state, adjustments, paused, reason = None, [], False, None
    if paused:
        lines.append(
            f"⏸ Игра на паузе ({reason or 'техработы'}): входящие переводы "
            "возвращаются отправителям. Снять: /resume"
        )
    if adjustments:
        parts = [
            f"{'−' if kind == MANUAL_OUT_KIND else '+'}{from_nano(total):.4f} Gram ({count})"
            for kind, count, total in adjustments
        ]
        lines.append("Корректировки казны: " + " · ".join(parts))
    if drift_state is not None:
        if drift_state.beyond_tolerance:
            lines.append(
                f"Ожидания БД: ~{drift_state.expected_nanotons / 1e9:.4f} Gram · "
                f"расхождение {drift_state.drift_nanotons / 1e9:+.4f} Gram ⚠️ — "
                "закрой: /adjust"
            )
        else:
            lines.append(
                f"Сверка с БД сходится ✓ (ожидается ~{drift_state.expected_nanotons / 1e9:.4f} Gram)"
            )
    # Зеркало казны: независимая копия истории кошелька со сверкой «в ноль».
    try:
        from app.treasury_mirror import treasury_mirror_block

        mirror_text = await treasury_mirror_block()
        if mirror_text:
            lines.append("")
            lines.append(mirror_text)
    except Exception as exc:
        logger.warning("Блок «Зеркало казны» в /treasury не собрался: %s", exc)
    async with SessionLocal() as session:
        waiting = (
            await session.execute(
                select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
            )
        ).scalar_one()
        dead = (
            await session.execute(select(func.count()).select_from(Payout).where(Payout.status == "failed"))
        ).scalar_one()
        # Глазами watcher'а: куда смотрит, когда последний раз видел цепочку
        # и где стоит курсор. Одна команда отвечает на «почему не видно пополнений».
        from app.ton_watch import BEAT_KEY, CURSOR_KEY, SOURCE_KEY

        beat_iso = None
        source = None
        cursor_raw = None
        for key, slot in ((BEAT_KEY, "b"), (SOURCE_KEY, "s"), (CURSOR_KEY, "c")):
            row = await session.get(WatcherState, key)
            if row is not None:
                if slot == "b":
                    beat_iso = row.value
                elif slot == "s":
                    source = row.value
                else:
                    cursor_raw = row.value
    lines.append(f"Очередь выплат: ожидает {waiting} · failed {dead}")
    if waiting or dead:
        lines.append("Разбор: /payouts — причина видна у каждой строки.")
    now = datetime.now(UTC)
    lines.append("Watcher:")
    if not settings.active_treasury_address:
        lines.append("  адрес не задан — смотреть не на что ⚠️")
    else:
        lines.append(f"  смотрит на: {settings.active_treasury_address[:8]}…{settings.active_treasury_address[-6:]} ({network})")
    beat_age = None
    if beat_iso:
        try:
            beat_moment = datetime.fromisoformat(beat_iso)
            if beat_moment.tzinfo is None:
                beat_moment = beat_moment.replace(tzinfo=UTC)
            beat_age = int((now - beat_moment).total_seconds())
        except ValueError:
            pass
    lines.append(
        f"  успешный цикл: {'never' if beat_age is None else f'{beat_age} с назад'}"
        + (f" · источник {source}" if source else "")
    )
    if beat_age is not None and beat_age > 180:
        lines.append("  ⚠️ циклы не проходят >3 мин: индексаторы недоступны или процесс спит")
    if cursor_raw and cursor_raw.isdigit():
        cursor_dt = datetime.fromtimestamp(int(cursor_raw), tz=UTC)
        lag = int((now - cursor_dt).total_seconds())
        lines.append(f"  курсор: {cursor_dt:%d.%m %H:%M} UTC ({lag:+d} с от текущего времени)")
        if lag < -60:
            lines.append("  ⚠️ курсор В БУДУЩЕМ: новые переводы отсекаются как «старые» — обнули ключ ton_watch_cursor_utime в watcher_state")
    elif settings.ton_enabled:
        lines.append("  курсора нет — стартует с отката 12 ч")
    return "\n".join(lines)


async def blockchain_diagnostics() -> str:
    """Аудит блокчейн-контура одной строкой-текстом для /blockchain.

    Показывает глазами всей связки watcher → очередь выплат → казначей:
    курсор и источник, stuck-список сбойных входящих, очередь по статусам,
    неопознанные «sent» (ждут сверки), глубину сверки истории и флаг её
    доступности в последнем цикле. Поиск «куда делось» начинается здесь,
    без перебора логов и запросов к индексаторам руками.
    """
    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"⛓ Блокчейн-контур ({network})"]
    if not settings.ton_enabled:
        lines.append("TON выключен (TON_ENABLED=false): ставки и выплаты не работают.")
        return "\n".join(lines)
    # Watcher-состояние одним запросом (курсор, источник, сердцебиение, stuck).
    from app.ton_watch import (  # локально: ton_watch не импортируется сверху
        BEAT_KEY,
        CURSOR_KEY,
        SOURCE_KEY,
    )
    from app.ton_watch import (
        STUCK_TX_KEY as STUCK_KEY,
    )

    cursor_raw: str | None = None
    source: str | None = None
    beat_iso: str | None = None
    stuck: dict = {}
    queue: dict[str, int] = {}
    sent_unconfirmed = 0
    waiting_dest = 0
    verified_wallets = 0
    async with SessionLocal() as session:
        for key, slot in ((BEAT_KEY, "b"), (SOURCE_KEY, "s"), (CURSOR_KEY, "c")):
            row = await session.get(WatcherState, key)
            if row is None:
                continue
            if slot == "b":
                beat_iso = row.value
            elif slot == "s":
                source = row.value
            else:
                cursor_raw = row.value
        stuck_row = await session.get(WatcherState, STUCK_KEY)
        if stuck_row is not None:
            try:
                stuck = json.loads(stuck_row.value) or {}
            except (ValueError, TypeError):
                stuck = {}
            if not isinstance(stuck, dict):
                stuck = {}
        for status in ("pending", "sending", "sent", "failed"):
            n = (
                await session.execute(
                    select(func.count()).select_from(Payout).where(
                        Payout.status == status, Payout.network == network
                    )
                )
            ).scalar_one()
            queue[status] = n
        sent_unconfirmed = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.status == "sent",
                    Payout.network == network,
                    or_(Payout.tx_hash.is_(None), Payout.tx_hash.like("bcast:%")),
                )
            )
        ).scalar_one()
        waiting_dest = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.dest_address == "", Payout.network == network
                )
            )
        ).scalar_one()
        verified_wallets = (
            await session.execute(
                select(func.count()).select_from(Player).where(Player.wallet_verified.is_(True))
            )
        ).scalar_one()
    # Курсор: лаг от текущего времени (тот же расчёт, что в /treasury).
    now = datetime.now(UTC)
    if cursor_raw and cursor_raw.isdigit():
        cursor_dt = datetime.fromtimestamp(int(cursor_raw), tz=UTC)
        lines.append(
            f"Watcher: курсор {cursor_dt:%d.%m %H:%M} UTC "
            f"({int((now - cursor_dt).total_seconds()):+d} с)"
            + (f" · источник {source}" if source else "")
        )
    else:
        lines.append("Watcher: курсора нет — стартует с отката 12 ч")
    if beat_iso:
        try:
            beat_moment = datetime.fromisoformat(beat_iso)
            if beat_moment.tzinfo is None:
                beat_moment = beat_moment.replace(tzinfo=UTC)
            beat_age = int((now - beat_moment).total_seconds())
        except ValueError:
            beat_age = None
        lines.append(f"Watcher: успешный цикл {beat_age if beat_age is not None else '?'} с назад")
        if beat_age is not None and beat_age > 180:
            lines.append("  ⚠️ циклы не проходят >3 мин: индексаторы недоступны или процесс спит")
    stuck_entries = sum(1 for rec in stuck.values() if isinstance(rec, dict) and not rec.get("reported"))
    stuck_sample = ""
    if stuck_entries:
        hashes = [h[:10] for h in list(stuck.keys())[:3]]
        stuck_sample = "· " + ", ".join(hashes) + ("…" if stuck_entries > 3 else "")
    lines.append(f"Stuck-входящих: {stuck_entries} {stuck_sample} (ключ watcher_state[{STUCK_KEY}])")
    lines.append(
        f"Очередь выплат: pending {queue.get('pending', 0)} · sending {queue.get('sending', 0)} · "
        f"sent {queue.get('sent', 0)} (сверки ждут {sent_unconfirmed}) · failed {queue.get('failed', 0)}"
    )
    if waiting_dest:
        lines.append(f"  {waiting_dest} строк ждут кошелёк игрока (dest пустой) — уйдут после /wallet")
    lines.append(f"Кошельков verified: {verified_wallets}")
    balance, _status, balance_source = await fetch_account_state()
    if balance is not None:
        lines.append(f"Баланс казначея: {balance / 1e9:.4f} Gram ({balance_source})")
    else:
        lines.append("Баланс казначея: недоступен (оба индексатора молчат)")
    lines.append(
        f"Сверка истории: {settings.payout_reconcile_history_seconds / 86400:g} сут · "
        f"до {settings.payout_reconcile_max_pages} стр · "
        f"история {'доступна' if _state._RECONCILE_HISTORY_OK else 'НЕДОСТУПНА ⚠️ повторы выплат заморожены'}"
    )
    return "\n".join(lines)
