"""Платная смена выбора и постоянство расписания."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import delete, select

from app.config import settings
from app.models import Card, Player, RevoteGrant, Round, RoundStatus, Vote, WinRule
from app.payments import (
    build_revote_payload,
    parse_revote_memo,
    parse_revote_payload,
    revote_memo,
)
from app.voting import cast_vote, change_vote


def _open_round(day_index: int = 5) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )


def test_payload_and_memo_roundtrip() -> None:
    assert build_revote_payload(7) == "revote:7"
    assert parse_revote_payload("revote:7") == 7
    assert parse_revote_payload("revote:x") is None
    assert parse_revote_payload(None) is None
    assert revote_memo(12) == "rv:12"
    assert parse_revote_memo("RV:12 ") == 12
    assert parse_revote_memo("stake something") is None
    assert parse_revote_memo("rv:") is None


def test_parse_revote_memo_tolerates_wallet_noise() -> None:
    """Кошелёк не всегда доносит мемо байт-в-байт: лишние пробелы, перевод
    строки, подпись кошелька или мусор вокруг — токен rv:<цифры> всё равно
    находится."""
    assert parse_revote_memo("rv:17") == 17
    assert parse_revote_memo("  rv:17  ") == 17
    assert parse_revote_memo("RV:17") == 17
    assert parse_revote_memo("rv : 17") == 17
    assert parse_revote_memo("rv:17\nподпись кошелька") == 17
    assert parse_revote_memo("Keeper\nrv:17") == 17
    assert parse_revote_memo("смена пути\nrv:17") == 17
    assert parse_revote_memo("rv:\u00a017") == 17
    assert parse_revote_memo("\u200brv\u200d:17") == 17
    assert parse_revote_memo("без мемо") is None
    assert parse_revote_memo("xxx rv 12 yyy") is None
    assert parse_revote_memo("") is None


async def test_change_vote_requires_grant(session) -> None:
    player = Player(id=41)
    session.add(player)
    round_row = _open_round()
    session.add(round_row)
    await session.commit()
    rid = round_row.id
    await cast_vote(session, round_row, 41, 0)

    assert await change_vote(session, round_row, 41, 1) == "no_grant"
    session.add(RevoteGrant(round_id=rid, player_id=41, source="stars", unit_ref="chg-1"))
    await session.commit()

    assert await change_vote(session, round_row, 41, 1) == "ok"
    vote = (await session.execute(Vote.__table__.select())).first()._mapping
    assert vote["card_position"] == 1
    grant = (await session.execute(RevoteGrant.__table__.select())).first()._mapping
    assert grant["status"] == "used"
    # Грант одноразовый.
    assert await change_vote(session, round_row, 41, 2) == "no_grant"


async def test_change_vote_edge_cases(session) -> None:
    player = Player(id=42)
    session.add(player)
    round_row = _open_round()
    session.add(round_row)
    await session.commit()

    assert await change_vote(session, round_row, 42, 1) == "no_vote"
    assert await change_vote(session, round_row, 42, 9) == "invalid"
    # Грант есть, но голоса нет.
    session.add(RevoteGrant(round_id=round_row.id, player_id=42, source="ton", unit_ref="tx-x"))
    await session.commit()
    assert await change_vote(session, round_row, 42, 1) == "no_vote"
    # Тот же путь — грант не тратится.
    await cast_vote(session, round_row, 42, 1)
    assert await change_vote(session, round_row, 42, 1) == "same"
    assert await has_unused(session, round_row.id, 42) is True
    # Закрытый день.
    round_row.status = RoundStatus.CLOSED
    await session.commit()
    assert await change_vote(session, round_row, 42, 2) == "closed"


async def has_unused(session, round_id: int, player_id: int) -> bool:
    from sqlalchemy import select

    result = await session.execute(
        select(RevoteGrant.id).where(
            RevoteGrant.round_id == round_id,
            RevoteGrant.player_id == player_id,
            RevoteGrant.status == "granted",
        )
    )
    return result.scalar_one_or_none() is not None


async def test_round_schedule_follows_utc_grid(session, monkeypatch) -> None:
    """День открывается в момент конца подсчёта предыдущего и ложится на сетку UTC.

    Голосование закрывается в day_close_hour_utc, дальше — мгновенный подсчёт
    и итоги вместе с новым днём.
    """

    from app.rounds import create_next_round_detailed

    base_opens = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=3)
    latest = Round(
        day_index=1,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=base_opens,
        voting_ends_at=base_opens + timedelta(hours=23),
        tally_ends_at=base_opens + timedelta(hours=24),
        winner_card=0,
        vote_counts_json="{}",
    )
    session.add(latest)
    await session.commit()

    created, was_created = await create_next_round_detailed(session)
    try:
        assert was_created is True
        # Новый день открывается ровно на границе подсчёта предыдущего.
        assert created.opens_at == base_opens + timedelta(hours=24)
        # Закрытие голосования стоит на часовой сетке UTC.
        close_hour = settings.day_close_hour_utc % 24
        assert created.voting_ends_at.minute == 0
        assert created.voting_ends_at.second == 0
        assert created.voting_ends_at.hour == close_hour
        assert created.voting_ends_at > created.opens_at
        assert created.tally_ends_at == created.voting_ends_at
    finally:
        if was_created:
            for card in list(created.cards):
                await session.delete(card)
            await session.delete(created)
            await session.commit()


async def test_close_voting_expires_unused_grants(session) -> None:
    """Закрытие дня переводит неиспользованные гранты в expired; потраченные не трогает."""
    from app.rounds import close_voting

    player = Player(id=43)
    session.add(player)
    round_row = _open_round()
    session.add(round_row)
    await session.commit()
    session.add(RevoteGrant(round_id=round_row.id, player_id=43, source="stars", unit_ref="g-live"))
    session.add(
        RevoteGrant(round_id=round_row.id, player_id=43, source="stars", unit_ref="g-spent", status="used")
    )
    await session.commit()

    await cast_vote(session, round_row, 43, 0)
    await change_vote(session, round_row, 43, 1)  # тратит g-live
    session.add(RevoteGrant(round_id=round_row.id, player_id=43, source="ton", unit_ref="g-left"))
    await session.commit()

    await close_voting(session, round_row)

    rows = (
        (await session.execute(select(RevoteGrant.unit_ref, RevoteGrant.status))).all()
    )
    statuses = {unit: status for unit, status in rows}
    assert statuses["g-spent"] == "used"
    assert statuses["g-left"] == "expired"


async def test_paystars_requires_existing_vote() -> None:
    """Подделанный колбэк оплаты без голоса не открывает счёт."""
    import os

    from app.db import SessionLocal
    from app.handlers import on_paystars

    pid = 730_000 + int.from_bytes(os.urandom(2), "big")
    day_index = 78_000 + int.from_bytes(os.urandom(3), "big")
    async with SessionLocal() as db:
        db.add(Player(id=pid))
        round_row = _open_round(day_index=day_index)
        db.add(round_row)
        await db.commit()
        rid = round_row.id
        callback = SimpleNamespace(
            data=f"paystars:{rid}",
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=SimpleNamespace(id=pid, username=None, first_name="Т"),
            bot=SimpleNamespace(send_invoice=AsyncMock()),
            answer=AsyncMock(),
        )
        try:
            await on_paystars(callback)
            assert callback.bot.send_invoice.await_count == 0
            kwargs = callback.answer.call_args.kwargs
            assert kwargs.get("show_alert") is True
        finally:
            await db.execute(delete(Card).where(Card.round_id == rid))
            round_db = await db.get(Round, rid)
            if round_db is not None:
                await db.delete(round_db)
            player_db = await db.get(Player, pid)
            if player_db is not None:
                await db.delete(player_db)
            await db.commit()


async def test_paid_revote_via_card_press() -> None:
    """Полный цикл: голос → грант → нажал другую карту → путь изменён, грант списан."""
    import os

    from app.db import SessionLocal
    from app.handlers import on_vote

    pid = 740_000 + int.from_bytes(os.urandom(2), "big")
    day_index = 79_000 + int.from_bytes(os.urandom(3), "big")
    async with SessionLocal() as db:
        db.add(Player(id=pid))
        round_row = _open_round(day_index=day_index)
        db.add(round_row)
        await db.commit()
        rid = round_row.id
        await cast_vote(db, round_row, pid, 0)
        db.add(RevoteGrant(round_id=rid, player_id=pid, source="stars", unit_ref=f"card-{pid}"))
        await db.commit()
        callback = SimpleNamespace(
            data=f"vote:{rid}:2",
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=SimpleNamespace(id=pid, username=None, first_name="Т"),
            answer=AsyncMock(),
        )
        try:
            await on_vote(callback)
            assert "изменён" in callback.answer.call_args.args[0]
            vote = (
                (await db.execute(select(Vote).where(Vote.round_id == rid, Vote.player_id == pid)))
                .scalars()
                .one()
            )
            assert vote.card_position == 2
            grant = (
                (
                    await db.execute(
                        select(RevoteGrant).where(RevoteGrant.unit_ref == f"card-{pid}")
                    )
                )
                .scalars()
                .one()
            )
            assert grant.status == "used"
        finally:
            for g in (
                (
                    await db.execute(
                        select(RevoteGrant).where(RevoteGrant.unit_ref == f"card-{pid}")
                    )
                )
                .scalars()
                .all()
            ):
                await db.delete(g)
            votes = (
                (await db.execute(select(Vote).where(Vote.round_id == rid, Vote.player_id == pid)))
                .scalars()
                .all()
            )
            for v in votes:
                await db.delete(v)
            await db.execute(delete(Card).where(Card.round_id == rid))
            round_db = await db.get(Round, rid)
            if round_db is not None:
                await db.delete(round_db)
            player_db = await db.get(Player, pid)
            if player_db is not None:
                await db.delete(player_db)
            await db.commit()


async def test_successful_payment_creates_single_grant(monkeypatch) -> None:
    """Оплата Stars создаёт один грант; повтор той же транзакции игнорируется.

    Работаем с глобальной БД, как хендлер.
    """
    import os

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.handlers import on_successful_payment

    pid = 710_000 + int.from_bytes(os.urandom(2), "big")
    charge_id = f"chg-{pid}"
    day_index = 77_000 + int.from_bytes(os.urandom(3), "big")
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}"))
        round_row = _open_round(day_index=day_index)
        db.add(round_row)
        await db.commit()
        rid = round_row.id

        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            from_user=SimpleNamespace(id=pid, username=f"u{pid}", first_name="Т"),
            successful_payment=SimpleNamespace(
                invoice_payload=build_revote_payload(rid),
                total_amount=settings.revote_stars,
                telegram_payment_charge_id=charge_id,
            ),
            answer=AsyncMock(),
        )
        try:
            await on_successful_payment(message)
            grants = (
                (await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id)))
                .scalars()
                .all()
            )
            assert len(grants) == 1 and grants[0].round_id == rid and grants[0].status == "granted"
            assert "Оплачено" in message.answer.call_args.args[0]

            await on_successful_payment(message)
            grants = (
                (await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id)))
                .scalars()
                .all()
            )
            assert len(grants) == 1
            assert "уже учтена" in message.answer.call_args.args[0]
        finally:
            for g in (
                (await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id)))
                .scalars()
                .all()
            ):
                await db.delete(g)
            await db.execute(delete(Card).where(Card.round_id == rid))
            round_db = await db.get(Round, rid)
            if round_db is not None:
                await db.delete(round_db)
            player_db = await db.get(Player, pid)
            if player_db is not None:
                await db.delete(player_db)
            await db.commit()


async def test_successful_payment_for_closed_day_records_orphan() -> None:
    """Оплата за закрытый день сохраняется без раунда — возврат вручную."""
    import os

    from sqlalchemy import select as _select

    from app.db import SessionLocal
    from app.handlers import on_successful_payment

    pid = 720_000 + int.from_bytes(os.urandom(2), "big")
    charge_id = f"orphan-{pid}"
    async with SessionLocal() as db:
        db.add(Player(id=pid))
        await db.commit()
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            from_user=SimpleNamespace(id=pid, username=None, first_name="Т"),
            successful_payment=SimpleNamespace(
                invoice_payload=build_revote_payload(999_999),
                total_amount=settings.revote_stars,
                telegram_payment_charge_id=charge_id,
            ),
            answer=AsyncMock(),
        )
        try:
            await on_successful_payment(message)
            grants = (
                (await db.execute(_select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id)))
                .scalars()
                .all()
            )
            assert len(grants) == 1 and grants[0].round_id is None
            assert "возврата" in message.answer.call_args.args[0]
        finally:
            for g in (
                (await db.execute(_select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id)))
                .scalars()
                .all()
            ):
                await db.delete(g)
            player_db = await db.get(Player, pid)
            if player_db is not None:
                await db.delete(player_db)
            await db.commit()


