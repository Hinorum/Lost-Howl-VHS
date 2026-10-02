"""Сверка «sent»-выплат с блокчейном: confirm_broadcast_payouts.

Метка bcast:<unix> значит лишь «лайтсервер принял запрос», а не «транзакция
в блоке». При гонке двух быстрых переводов (приз + рейк одного дня) один
может не попасть в цепочку: база остаётся с несуществующим переводом.
Джоба сверяет memo с историей казначея: найденное → реальный хеш вместо
bcast, отсутствующее дольше окна → обратно в очередь на повторную отправку.
"""

import os
from datetime import UTC, datetime, timedelta

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


async def _seed_sent_payout(kind: str = "prize", round_id: int | None = 64) -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=round_id,
            player_id=42,
            kind=kind,
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="sent",
            tx_hash="bcast:1789470019",
            sent_at=datetime.now(UTC) - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()
        return payout_id


async def _cleanup(payout_id: int) -> None:
    async with SessionLocal() as session:
        await session.delete(await session.get(Payout, payout_id))
        await session.commit()


async def test_confirm_replaces_bcast_marker_with_real_hash(monkeypatch) -> None:
    """memo найдено в истории — bcast-метка заменяется реальным хешем."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {f"way:64:prize#{payout_id}": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "0" * 64
        assert row.last_error is None
    finally:
        await _cleanup(payout_id)


async def test_confirm_requeues_lost_transfer(monkeypatch) -> None:
    """memo нет в истории, окно верификации истекло — перевод не ушёл: retry."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"way:63:prize#1": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"
        assert row.attempts >= 1
        assert "не найдено в блокчейне" in (row.last_error or "")
    finally:
        await _cleanup(payout_id)


async def test_confirm_leaves_fresh_broadcast_alone(monkeypatch) -> None:
    """Свежая вещация (окно верификации не истекло) не трогается: блокчейн
    мог просто не успеть проиндексировать перевод."""
    payout_id = await _seed_sent_payout()
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.sent_at = datetime.now(UTC) - timedelta(seconds=30)
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"another": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "bcast:1789470019"
    finally:
        await _cleanup(payout_id)


async def test_confirm_skips_when_history_empty(monkeypatch) -> None:
    """Оба провайдера молчат — «не знаем»: ни подтверждать, ни reqeue.

    Возврат в очередь при слепой сети мог бы задвоить ушедший перевод:
    memo могло успеть попасть в блок, а история ещё не отвечает."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "bcast:1789470019"
    finally:
        await _cleanup(payout_id)


async def test_confirm_uses_comment_override(monkeypatch) -> None:
    """Выплаты со свободным комментарием сверяются по нему же."""
    payout_id = await _seed_sent_payout()
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.comment_override = "техработы 12.09"
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"техработы 12.09": "1" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.tx_hash == "1" * 64
    finally:
        await _cleanup(payout_id)


async def test_confirm_ignores_other_networks(monkeypatch) -> None:
    """Сверка работает только по выплатам активной сети."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=64,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            network="testnet",
            status="sent",
            tx_hash="bcast:1789470019",
            sent_at=datetime.now(UTC) - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {f"way:64:prize#{payout_id}": "2" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
    finally:
        await _cleanup(payout_id)


async def test_confirm_skips_rows_with_real_hash(monkeypatch) -> None:
    """Уже подтверждённые реальным хешем выплаты сверка не трогает."""
    async with SessionLocal() as session:
        payout = Payout(
            round_id=64,
            kind="refund",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="sent",
            tx_hash="3" * 64,
            sent_at=datetime.now(UTC) - timedelta(hours=5),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.tx_hash == "3" * 64
    finally:
        await _cleanup(payout_id)