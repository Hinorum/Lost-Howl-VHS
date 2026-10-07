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
from app.models import Income, Payout, Player, Round, RoundStatus, Stake, WatcherState, WinRule
from app.ton_utils import normalize_address
from app.ton_watch import Transfer, _stash_refund, process_transfer

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
    """Убрать строки теста вместе с детьми раунда и игрока.

    Ставки, выплаты и журнал входящих ссылаются на раунд и игрока: без их
    удаления Postgres не отдаст DELETE по rounds/players (SQLite внешние
    ключи не проверяет, поэтому порядок исторически не соблюдался).
    """
    async with SessionLocal() as session:
        await session.execute(delete(Income).where(Income.unit_ref.in_(tx_hashes)))
        round_ids = (
            (await session.execute(select(Round.id).where(Round.day_index.in_(day_indexes))))
            .scalars()
            .all()
        )
        if round_ids:
            await session.execute(delete(Stake).where(Stake.round_id.in_(round_ids)))
            await session.execute(delete(Payout).where(Payout.round_id.in_(round_ids)))
            await session.execute(delete(Income).where(Income.round_id.in_(round_ids)))
        await session.execute(delete(Payout).where(Payout.tx_hash.in_(tx_hashes)))
        await session.execute(delete(Round).where(Round.day_index.in_(day_indexes)))
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


async def test_dedupe_markers_are_redundant_after_their_row_exists(ton_on) -> None:
    """Метки `ledger:*`/`refund:*` избыточны, как только есть строка.

    Еженедельная уборка `ws-cleanup` сносит эти метки (и правильно делает: рост
    watcher_state иначе бесконечен). Это допустимо только потому, что метка —
    средство разрешения гонки на момент обработки, а настоящий дедуп живёт в
    строках Income/Payout. Если бы метка была единственной защитой, её снос тихо
    открыл бы двойной учёт, и заметить это можно было бы только по сходимости
    казны с БД.

    Проверяем ровно это: метку сносят, историю читают заново (как после сброса
    курсора) — и ни строки Income, ни выплаты не задваиваются.
    """
    tx = "marker-gone-1"
    await _open_round(944)
    try:
        transfer = Transfer(
            tx_hash=tx,
            source=STRANGER,
            value_nanotons=100_000_000,  # выше refund_min_gram -> пойдёт возврат
            comment="",
            utime=int(datetime.now(UTC).timestamp()),
        )
        first = await process_transfer(transfer)
        assert first in ("refund_queued", "ledgered")

        # Метки действительно созданы обработкой — иначе тест проверял бы пустоту.
        async with SessionLocal() as session:
            marks = (
                await session.execute(
                    select(WatcherState.key).where(
                        WatcherState.key.in_([f"ledger:{tx}", f"refund:{tx}"])
                    )
                )
            ).scalars().all()
        assert set(marks), "обработка не оставила меток — тест бессмысленен"

        # Снос меток — ровно то, что делает еженедельная уборка ws-cleanup.
        async with SessionLocal() as session:
            await session.execute(
                delete(WatcherState).where(WatcherState.key.in_([f"ledger:{tx}", f"refund:{tx}"]))
            )
            await session.commit()

        second = await process_transfer(transfer)

        assert second in ("duplicate_tx", "already_booked", "refund_duplicated"), (
            f"повтор после сноса метки дал {second!r} — идемпотентность держится на метке, "
            "а не на строке, и уборка открыла бы двойной учёт"
        )

        async with SessionLocal() as session:
            incomes = (
                await session.execute(select(Income).where(Income.unit_ref == tx))
            ).scalars().all()
            refunds = (
                await session.execute(select(Payout).where(Payout.tx_hash == tx))
            ).scalars().all()
        assert len(incomes) <= 1, "строка дохода задвоилась"
        assert len(refunds) <= 1, "возврат задвоился"
    finally:
        await _wipe([tx], [944])


async def test_refund_without_income_row_dedupes_on_payout(ton_on) -> None:
    """Возврат без строки Income держится на проверке Payout по tx_hash.

    Вторая половина инварианта. Пути `process_transfer` всегда заводят строку
    дохода, и там повтор ловится ею. Но ручной возврат (`_stash_refund` без
    ledger_result, в т.ч. разбор инцидента хранителем) строки Income не создаёт —
    там единственная защита от задвоения это поиск существующего `Payout` с тем же
    tx_hash.

    Если снести и её, то после уборки метки повторный проход создал бы вторую
    выплату, то есть вернул бы отправителю вдвое. Проверяем именно эту
    зависимость, а не «вообще идемпотентно».
    """
    tx = "refund-no-income-1"
    transfer = Transfer(
        tx_hash=tx,
        source=STRANGER,
        value_nanotons=100_000_000,
        comment="",
        utime=int(datetime.now(UTC).timestamp()),
    )
    try:
        async with SessionLocal() as session:
            first = await _stash_refund(session, transfer, None)
        assert first == "refund_queued"

        async with SessionLocal() as session:
            await session.execute(
                delete(WatcherState).where(WatcherState.key == f"refund:{tx}")
            )
            await session.commit()

        async with SessionLocal() as session:
            second = await _stash_refund(session, transfer, None)
        assert second == "refund_duplicated", (
            f"после сноса метки повтор дал {second!r}: без строки Income единственная "
            "защита от двойного возврата — поиск Payout по tx_hash"
        )

        async with SessionLocal() as session:
            refunds = (
                await session.execute(select(Payout).where(Payout.tx_hash == tx))
            ).scalars().all()
        assert len(refunds) == 1, "возврат задвоился после сноса метки"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Payout).where(Payout.tx_hash == tx))
            await session.commit()
