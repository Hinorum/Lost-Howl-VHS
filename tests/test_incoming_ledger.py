"""Журнал входящих переводов казначея: аудит «откуда деньги» в Income.

Каждый поступивший перевод watcher записывает в Income (kind=ton) с хвостом
адреса отправителя и исходом: ставка, возврат, оплата смены пути.
Идемпотентно по tx_hash — повторный проход не плодит строк.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Player, Round, RoundStatus, WinRule
from app.ton_utils import normalize_address
from app.ton_watch import Transfer, process_transfer

RAW = normalize_address("UQpfcexKrlNjGFPF44W9am1o75Z6fs_QBdwVNzuhHVX2L4oo")
STRANGER = "0:" + "9" * 62


@pytest.fixture()
def ton_on(monkeypatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)


async def _open_round(day_index: int) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        session.add(
            Round(
                day_index=day_index,
                status=RoundStatus.OPEN,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",

                opens_at=now,
                voting_ends_at=now + timedelta(hours=20),
                tally_ends_at=now + timedelta(hours=21),
            )
        )
        await session.commit()


async def _wipe(tx_hashes: list[str], day_indexes: list[int]) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Income).where(Income.unit_ref.in_(tx_hashes)))
        await session.execute(
            delete(Round).where(Round.day_index.in_(day_indexes))
        )
        await session.execute(delete(Player).where(Player.id.in_([920_001])))
        await session.commit()


async def test_unknown_sender_is_logged_with_source_tail(ton_on) -> None:
    """Перевод с непривязанного кошелька: возврат + строка журнала с адресом."""
    tx = "ledger-unknown-1"
    await _wipe([tx], [941])
    try:
        status = await process_transfer(
            Transfer(tx_hash=tx, source=STRANGER, value_nanotons=300_000_000,
                     comment="", utime=int(datetime.now(UTC).timestamp()))
        )
        assert status == "refund_queued"
        async with SessionLocal() as session:
            row = (
                await session.execute(select(Income).where(Income.unit_ref == tx))
            ).scalar_one()
            assert row.kind == "ton"
            assert row.player_id is None
            assert "in:unknown" in row.note
            assert STRANGER[-10:] in row.note  # откуда деньги — видно сразу
    finally:
        await _wipe([tx], [941])


async def test_stake_from_bound_wallet_is_logged(ton_on) -> None:
    """Ставка от привязанного игрока: журнал знает и сумму, и кто принёс."""
    tx = "ledger-stake-1"
    await _open_round(942)
    async with SessionLocal() as session:
        session.add(Player(id=920_001, username="whale",
                           wallet_address=normalize_address(RAW)))
        await session.commit()
    try:
        status = await process_transfer(
            Transfer(tx_hash=tx, source=RAW, value_nanotons=500_000_000,
                     comment="", utime=int(datetime.now(UTC).timestamp()))
        )
        assert status == "ok"
        async with SessionLocal() as session:
            row = (
                await session.execute(select(Income).where(Income.unit_ref == tx))
            ).scalar_one()
            assert row.player_id == 920_001
            assert "in:stake:ok" in row.note
    finally:
        await _wipe([tx], [942])


async def test_ledger_is_idempotent_by_tx_hash(ton_on) -> None:
    """Повторная обработка той же транзакции не плодит вторую строку."""
    tx = "ledger-dup-1"
    await _open_round(943)
    try:
        transfer = Transfer(tx_hash=tx, source=STRANGER, value_nanotons=100_000_000,
                            comment="", utime=int(datetime.now(UTC).timestamp()))
        await process_transfer(transfer)
        await process_transfer(transfer)
        async with SessionLocal() as session:
            rows = (
                await session.execute(select(Income).where(Income.unit_ref == tx))
            ).scalars().all()
        assert len(rows) == 1
    finally:
        await _wipe([tx], [943])
