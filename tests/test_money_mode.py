"""Версия игры: рубильник хранителя (WatcherState), снимок режима на день
(Round.money_mode), скрытие банка и гейт открытого дня."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import ops, rounds
from app.broadcast import status_text
from app.config import settings
from app.db import SessionLocal
from app.handlers import _active_round_money_mode
from app.models import (
    Card,
    Income,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    WinRule,
)


def _round_row(money_mode: bool, day_index: int, _id: int, with_cards: bool = False) -> Round:
    round_row = Round(
        id=_id,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День версии",
        chapter_text="Текст.",


        money_mode=money_mode,
        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC) + timedelta(hours=23),
        tally_ends_at=datetime.now(UTC) + timedelta(hours=24),
    )
    if with_cards:
        for position in range(3):
            round_row.cards.append(
                Card(
                    round_id=_id,
                    position=position,
                    title=f"Путь {position}",
                    description="описание",
                    consequence="канон",
                    tag="care",
                    image_path="",
                )
            )
    return round_row


async def _delete_round(round_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(Round.__table__.delete().where(Round.id == round_id))
        await session.commit()


async def test_money_mode_defaults_on_and_toggles() -> None:
    """Отсутствие ключа = денежная версия; двойной тап — no-op."""
    async with SessionLocal() as session:
        assert await ops.money_mode_enabled(session) is True
        assert await ops.set_money_mode(session, False) is True
        assert await ops.money_mode_enabled(session) is False
        assert await ops.set_money_mode(session, False) is False  # тот же тап
        assert await ops.set_money_mode(session, True) is True
        assert await ops.money_mode_enabled(session) is True


async def test_stamp_persists_snapshot_on_round() -> None:
    """День снимает режим на материализацию: свободный день остаётся свободным,
    даже если рубильник потом вернули."""
    async with SessionLocal() as session:
        await ops.set_money_mode(session, False)
        free_day = _round_row(money_mode=True, day_index=6, _id=87_900)
        await rounds._stamp_day_money_mode(session, free_day)
        session.add(free_day)
        await session.commit()
    try:
        async with SessionLocal() as session:
            row = await session.get(Round, free_day.id)
        assert row is not None and row.money_mode is False
    finally:
        await _delete_round(free_day.id)


async def test_free_day_hides_bank_line(tmp_path, monkeypatch) -> None:
    """День без ставок не показывает банк, даже при настроенном TON;
    денежный день — показывает."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ton_enabled", True)
    free_day = _round_row(money_mode=False, day_index=9, _id=87_910, with_cards=True)
    money_day = _round_row(money_mode=True, day_index=10, _id=87_911, with_cards=True)
    async with SessionLocal() as db:
        db.add(Stake(round_id=87_911, player_id=9, amount_nanotons=3_000_000_000, tx_hash="mm1", status="confirmed"))
        await db.commit()
    try:
        assert "Банк дня" not in await status_text(free_day)
        assert "Банк дня: 3.00 Gram" in await status_text(money_day)
    finally:
        async with SessionLocal() as db:
            await db.execute(Stake.__table__.delete().where(Stake.round_id == 87_911))
            await db.commit()


async def test_active_day_uses_its_snapshot() -> None:
    """Открытый день живёт по своему снимку, а не по живому рубильнику."""
    async with SessionLocal() as session:
        await ops.set_money_mode(session, False)
    money_day = _round_row(money_mode=True, day_index=7, _id=87_901)
    free_day = _round_row(money_mode=False, day_index=8, _id=87_902)
    try:
        async with SessionLocal() as session:
            session.add_all([money_day, free_day])
            await session.commit()
        # Последний открытый день — свободный: гейт дня = False.
        async with SessionLocal() as session:
            row = await session.get(Round, free_day.id)
            row.day_index = 998
            await session.commit()
        assert await _active_round_money_mode() is False
        # Снимок поверх вычеркнут — денежный день открыт и ближе всего.
        async with SessionLocal() as session:
            row = await session.get(Round, money_day.id)
            row.day_index = 999
            await session.commit()
        assert await _active_round_money_mode() is True
    finally:
        await _delete_round(money_day.id)
        await _delete_round(free_day.id)


async def test_register_stake_blocked_on_free_day(monkeypatch) -> None:
    """Серверный гейт денежного контура: ставка не принимается на свободном
    дне, даже если вызов пришёл мимо UI (наблюдатель, прямой ретрай)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    from app.stakes import register_stake

    free_day = _round_row(money_mode=False, day_index=11, _id=87_920)
    try:
        async with SessionLocal() as session:
            session.add(free_day)
            await session.commit()
            raw = "0:" + "dd" * 32
            player = Player()
            player.id = 920_111
            player.username = "free"
            player.wallet_address = raw
            session.add(player)
            await session.commit()
        from app.ton_utils import to_nano

        async with SessionLocal() as session:
            round_row = await session.get(Round, free_day.id)
            player = await session.get(Player, 920_111)
            assert await register_stake(
                session, round_row, player, to_nano(1), "tx-free-1"
            ) == "money_off"
            stakes = (
                await session.execute(
                    select(Stake).where(Stake.tx_hash == "tx-free-1")
                )
            ).scalars().all()
            assert stakes == []
    finally:
        await _delete_round(free_day.id)
        async with SessionLocal() as session:
            await session.execute(Stake.__table__.delete().where(Stake.player_id == 920_111))
            await session.execute(Player.__table__.delete().where(Player.id == 920_111))
            await session.commit()


async def test_process_transfer_refunds_on_free_day(monkeypatch) -> None:
    """Перевод в бесплатный день возвращается с объяснением, а не засчитывается
    как ставка: возврат виден в ledger как stake:money_off."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)
    from app.ton_utils import to_nano
    from app.ton_watch import Transfer, process_transfer

    free_day = _round_row(money_mode=False, day_index=12, _id=87_921)
    raw = "0:" + "ee" * 32
    txs = ["tx-free-stake-1", "tx-free-revote-1", "tx-free-auto-1"]
    try:
        async with SessionLocal() as session:
            session.add(free_day)
            await session.commit()
            player = Player()
            player.id = 920_222
            player.username = "free2"
            player.wallet_address = raw
            session.add(player)
            await session.commit()
        now = int(datetime.now(UTC).timestamp())

        status = await process_transfer(
            Transfer(
                tx_hash=txs[0], source=raw, value_nanotons=to_nano(1),
                comment="", utime=now,
            )
        )
        assert status == "money_off"
        # Явный rv:-мемо тоже не проходит в бесплатный день.
        status = await process_transfer(
            Transfer(
                tx_hash=txs[1], source=raw, value_nanotons=to_nano(0.1),
                comment=f"rv:{free_day.id}", utime=now,
            )
        )
        assert status == "revote_money_off"
        # Авто-грант по сумме ([revote_ton, stake_min_ton)) — тоже возврат.
        status = await process_transfer(
            Transfer(
                tx_hash=txs[2], source=raw, value_nanotons=to_nano(0.2),
                comment="", utime=now,
            )
        )
        assert status == "revote_auto_money_off"

        async with SessionLocal() as session:
            payouts = (
                await session.execute(
                    select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_(txs))
                )
            ).scalars().all()
            assert len(payouts) == 3
            notes = {
                (await session.scalar(select(Income.note).where(Income.unit_ref == p.tx_hash))) or ""
                for p in payouts
            }
            assert any("stake:money_off" in n for n in notes)
            assert any("revote:revote_money_off" in n for n in notes)
            assert any("revote_auto:money_off" in n for n in notes)
            stakes = (
                await session.execute(select(Stake).where(Stake.player_id == 920_222))
            ).scalars().all()
            assert stakes == []
    finally:
        await _delete_round(free_day.id)
        async with SessionLocal() as session:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "refund", Payout.tx_hash.in_(txs)))
            await session.execute(Income.__table__.delete().where(Income.unit_ref.in_(txs)))
            await session.execute(Stake.__table__.delete().where(Stake.player_id == 920_222))
            await session.execute(Player.__table__.delete().where(Player.id == 920_222))
            await session.commit()