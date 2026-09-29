"""Пульт выплат: очередь долгов, ручной возврат, касса и отчёты.

Каждая команда хранителя — это «одна кнопка, чтобы увидеть/починить чужую
ошибку». Значит, тестируем не только счастливый путь, но и то, что видит
хранитель, когда данных нет или операция не удалась: пустая очередь, битый
формат команды, отказ бизнес-логики, упавший сбор отчёта.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.handlers.payout import (
    _payouts_text,
    _refunds_panel_text,
    _revenue_text,
    _stakes_panel_text,
    cmd_blockchain,
    cmd_fundout,
    cmd_incoming,
    cmd_mirror,
    cmd_payout,
    cmd_payouts,
    cmd_return,
    cmd_revenue,
    cmd_stakes,
    cmd_treasury,
)
from app.models import (
    Income,
    PackFund,
    PackFundLedger,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    WinRule,
)
from app.stakes import current_network
from app.ton_utils import to_nano

ADMIN = 4242
WALLET = "0:" + "ab" * 32


def make_message(text: str, uid: int = ADMIN) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text=text,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )


def said(message: SimpleNamespace) -> str:
    return message.answer.call_args.args[0]


async def _wipe(*tables) -> None:
    async with SessionLocal() as db:
        for table in tables:
            await db.execute(delete(table))
        await db.commit()


def _round(day_index: int, status: RoundStatus = RoundStatus.OPEN) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=status,
        win_rule=WinRule.MAJORITY,
        chapter_title="глава",
        chapter_text="текст",
        opens_at=now,
        voting_ends_at=now + timedelta(hours=10),
        tally_ends_at=now + timedelta(hours=11),
    )


async def test_outsiders_cannot_reach_treasury_tools(monkeypatch) -> None:
    """Все десять команд казны — только для хранителя: чужие должны упираться
    в отказ ДО любой работы с БД (иначе отчёт утекает постороннему)."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    for text in (
        "/payouts",
        "/payout 1 spam",
        "/return 1",
        "/treasury",
        "/fundout 1 покупка",
        "/incoming",
        "/stakes",
        "/revenue",
        "/blockchain",
        "/mirror reset confirm",
    ):
        outsider = make_message(text, uid=1)
        await _run_command(text, outsider)
        assert "только для хранителя" in said(outsider), text


async def _run_command(text: str, message: SimpleNamespace) -> None:
    table = {
        "/payouts": cmd_payouts,
        "/payout": cmd_payout,
        "/return": cmd_return,
        "/treasury": cmd_treasury,
        "/fundout": cmd_fundout,
        "/incoming": cmd_incoming,
        "/stakes": cmd_stakes,
        "/revenue": cmd_revenue,
        "/blockchain": cmd_blockchain,
        "/mirror": cmd_mirror,
    }
    head = text.split()[0]
    await table[head](message)


async def test_message_without_sender_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    ghost = make_message("/treasury")
    ghost.from_user = None
    await cmd_treasury(ghost)
    assert "только для хранителя" in said(ghost)


async def test_payouts_reports_debt_with_reason(monkeypatch) -> None:
    """Очередь показывает долг с причиной последней попытки: без неё хранитель
    не отличит «ушло в сеть» от «упало на ретраях»."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Payout)
    async with SessionLocal() as db:
        db.add_all(
            [
                Payout(
                    kind="refund",
                    amount_nanotons=to_nano(1.5),
                    dest_address=WALLET,
                    status="failed",
                    attempts=5,
                    last_error="have no alive peers: LITESERVER_CONFIG_URL" * 3,
                ),
                Payout(
                    kind="prize",
                    amount_nanotons=to_nano(0.25),
                    dest_address="0:" + "cd" * 32,
                    status="pending",
                    attempts=0,
                ),
                # Отправленные и помеченные спамом долгом не считаются.
                Payout(kind="prize", amount_nanotons=1, dest_address="0:ff", status="sent"),
                Payout(kind="prize", amount_nanotons=1, dest_address="0:ff", status="dismissed"),
            ]
        )
        await db.commit()
    text = await _payouts_text()
    assert "Неотправленные выплаты" in text
    assert "1.5000 Gram" in text
    assert "попыток 5" in text
    # Хвост адреса виден, длинная причина обрезана.
    assert "…" + WALLET[-8:] in text
    assert text.count("…") <= 3
    assert "/payout <id> retry" in text
    assert "spam" in text


async def test_payouts_empty_queue_is_good_news(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Payout)
    assert "Долгов нет" in await _payouts_text()


async def test_payout_spam_and_retry_paths(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Payout)
    async with SessionLocal() as db:
        db.add(
            Payout(
                kind="refund",
                amount_nanotons=to_nano(0.5),
                dest_address=WALLET,
                status="failed",
                attempts=3,
            )
        )
        await db.commit()
        row = (await db.execute(select(Payout))).scalar_one()
        payout_id = row.id

    spam = make_message(f"/payout {payout_id} spam")
    await cmd_payout(spam)
    assert "подтверди явно" in said(spam)

    spam = make_message(f"/payout {payout_id} spam confirm")
    await cmd_payout(spam)
    assert "помечена как спам" in said(spam)

    retry = make_message(f"/payout {payout_id} RETRY")
    await cmd_payout(retry)
    assert "вернулась в очередь" in said(retry)

    async with SessionLocal() as db:
        row = await db.get(Payout, payout_id)
        assert row is not None and row.status == "pending"


async def test_payout_rejects_broken_arguments(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    for text in ("/payout", "/payout abc spam", "/payout 5 nuke", "/payout 5 6 7"):
        message = make_message(text)
        await cmd_payout(message)
        assert "Формат" in said(message), text


async def test_payout_missing_row_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    message = make_message("/payout 424242 spam confirm")
    await cmd_payout(message)
    assert "не найдена или уже отправлена" in said(message)


async def test_payout_spam_refuses_player_money(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Payout)
    async with SessionLocal() as db:
        db.add(
            Payout(
                kind="prize",
                amount_nanotons=to_nano(1),
                dest_address=WALLET,
                status="failed",
                attempts=2,
            )
        )
        await db.commit()
        payout_id = (await db.execute(select(Payout))).scalar_one().id

    message = make_message(f"/payout {payout_id} spam confirm")
    await cmd_payout(message)
    assert "только refund" in said(message)

    async with SessionLocal() as db:
        row = await db.get(Payout, payout_id)
        assert row is not None and row.status == "failed" and row.amount_nanotons == to_nano(1)


async def test_return_creates_refund_and_kicks_dispatcher(monkeypatch) -> None:
    """Ручной возврат: выплата создаётся, статус ставки меняется, а диспетчер
    сразу дёргается — иначе возврат «висит в очереди» до следующего цикла."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    ton_pay = importlib.import_module("app.ton_pay")
    dispatch = AsyncMock(return_value=None)
    monkeypatch.setattr(ton_pay, "dispatch_pending_payouts", dispatch)

    await _wipe(Payout, Stake, Round, Player, PackFund)
    async with SessionLocal() as db:
        db.add(Player(id=11, username="loser", first_name="Проигравший", wallet_address=WALLET))
        day = _round(80_001, RoundStatus.TALLYING)
        db.add(day)
        await db.commit()
        db.add(
            Stake(
                round_id=day.id,
                player_id=11,
                amount_nanotons=to_nano(2),
                tx_hash="refund-me",
                network=current_network(),
                status="pending",
            )
        )
        await db.commit()
        stake_id = (await db.execute(select(Stake.id))).scalar_one()

    message = make_message(f"/return {stake_id}")
    await cmd_return(message)
    assert "поставлен в очередь" in said(message)
    dispatch.assert_awaited_once()

    async with SessionLocal() as db:
        stake = await db.get(Stake, stake_id)
        assert stake is not None and stake.status == "refunded"
        refund = (
            await db.execute(select(Payout).where(Payout.kind == "refund"))
        ).scalar_one()
        assert refund.player_id == 11 and refund.dest_address == WALLET


async def test_return_survives_dead_dispatcher(monkeypatch) -> None:
    """Ошибка кика диспетчера не должна съесть ответ: выплата уже создана,
    и хранитель обязан увидеть, что возврат оформлен."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    ton_pay = importlib.import_module("app.ton_pay")
    monkeypatch.setattr(
        ton_pay, "dispatch_pending_payouts", AsyncMock(side_effect=RuntimeError("сеть легла"))
    )

    await _wipe(Payout, Stake, Round, Player, PackFund)
    async with SessionLocal() as db:
        db.add(Player(id=12, wallet_address=WALLET))
        day = _round(80_002, RoundStatus.TALLYING)
        db.add(day)
        await db.commit()
        db.add(
            Stake(
                round_id=day.id,
                player_id=12,
                amount_nanotons=to_nano(3),
                tx_hash="refund-kick",
                network=current_network(),
                status="rejected",
            )
        )
        await db.commit()
        stake_id = (await db.execute(select(Stake.id))).scalar_one()

    message = make_message(f"/return {stake_id}")
    await cmd_return(message)
    assert "поставлен в очередь" in said(message)


async def test_return_refuses_confirmed_and_unknown_stakes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Payout, Stake, Round, Player, PackFund)
    async with SessionLocal() as db:
        db.add(Player(id=13, wallet_address=WALLET))
        day = _round(80_003, RoundStatus.CLOSED)
        db.add(day)
        await db.commit()
        db.add(
            Stake(
                round_id=day.id,
                player_id=13,
                amount_nanotons=to_nano(1),
                tx_hash="confirmed-stake",
                network=current_network(),
                status="confirmed",
            )
        )
        await db.commit()
        confirmed_id = (await db.execute(select(Stake.id))).scalar_one()

    refused = make_message(f"/return {confirmed_id}")
    await cmd_return(refused)
    assert "уже засчитана" in said(refused)

    missing = make_message("/return 424242")
    await cmd_return(missing)
    assert "нет такой ставки" in said(missing)

    broken = make_message("/return abc")
    await cmd_return(broken)
    assert "Формат" in said(broken)


async def test_treasury_report_and_its_failure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    ton_pay = importlib.import_module("app.ton_pay")
    monkeypatch.setattr(ton_pay, "treasury_diagnostics", AsyncMock(return_value="<b>Казна</b>"))
    ok = make_message("/treasury")
    await cmd_treasury(ok)
    assert said(ok) == "<b>Казна</b>"
    assert ok.answer.call_args.kwargs["parse_mode"] is not None

    monkeypatch.setattr(
        ton_pay,
        "treasury_diagnostics",
        AsyncMock(side_effect=RuntimeError("индексатор молчит")),
    )
    failed = make_message("/treasury")
    await cmd_treasury(failed)
    assert "Отчёт не собрался" in said(failed)
    assert "индексатор молчит" in said(failed)


async def test_blockchain_report_and_its_failure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    ton_pay = importlib.import_module("app.ton_pay")
    monkeypatch.setattr(ton_pay, "blockchain_diagnostics", AsyncMock(return_value="watcher: молчит"))
    ok = make_message("/blockchain")
    await cmd_blockchain(ok)
    assert "watcher: молчит" in said(ok)

    monkeypatch.setattr(
        ton_pay, "blockchain_diagnostics", AsyncMock(side_effect=RuntimeError("таймаут"))
    )
    failed = make_message("/blockchain")
    await cmd_blockchain(failed)
    assert "Не собрал отчёт" in said(failed)


async def test_mirror_requires_explicit_confirm(monkeypatch) -> None:
    """Сброс зеркала переписывает курсоры — только со словом confirm."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    mirror = importlib.import_module("app.treasury_mirror")
    reset = AsyncMock(return_value="зеркало сброшено")
    monkeypatch.setattr(mirror, "reset_treasury_mirror", reset)

    vague = make_message("/mirror")
    await cmd_mirror(vague)
    assert "reset confirm" in said(vague)
    reset.assert_not_awaited()

    wrong = make_message("/mirror reset")
    await cmd_mirror(wrong)
    reset.assert_not_awaited()

    confirmed = make_message("/mirror reset confirm")
    await cmd_mirror(confirmed)
    assert "зеркало сброшено" in said(confirmed)
    reset.assert_awaited_once()

    monkeypatch.setattr(
        mirror, "reset_treasury_mirror", AsyncMock(side_effect=RuntimeError("сброс залочен"))
    )
    broken = make_message("/mirror reset confirm")
    await cmd_mirror(broken)
    assert "Не сбросил зеркало" in said(broken)


async def test_fundout_validates_arguments(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    cases = (
        ("/fundout 1", "Формат"),
        ("/fundout много причина", "числом"),
        ("/fundout 0 причина", "положительной"),
        ("/fundout -3 причина", "положительной"),
    )
    for text, hint in cases:
        message = make_message(text)
        await cmd_fundout(message)
        assert hint in said(message), text


async def test_fundout_records_dispense_and_checks_fund(monkeypatch) -> None:
    """Раздача Фонда Стаи пишется в журнал и уменьшает цифру фонда; перерасход
    и пустой фонд отклоняются, а не записываются."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(PackFundLedger, PackFund, Income)
    async with SessionLocal() as db:
        db.add(PackFund(nanotons=to_nano(10)))
        await db.commit()

    ok = make_message(f"/fundout 2,5 {'причина ' * 40}")
    await cmd_fundout(ok)
    assert "Реальный перевод" in said(ok)
    async with SessionLocal() as db:
        fund = (await db.execute(select(PackFund))).scalar_one()
        assert fund.nanotons == to_nano(7.5)
        entry = (await db.execute(select(PackFundLedger))).scalar_one()
        assert entry.entry_type == "out" and entry.amount_nanotons == to_nano(2.5)
        # Длинная причина обрезается, иначе сообщение упирается в лимит Telegram.
        assert len(entry.note) <= 180

    too_much = make_message("/fundout 9 остаток")
    await cmd_fundout(too_much)
    assert "Реальный перевод" not in said(too_much)
    assert "в фонде меньше" in said(too_much)
    async with SessionLocal() as db:
        fund = (await db.execute(select(PackFund))).scalar_one()
        assert fund.nanotons == to_nano(7.5), "отменённая раздача не должна списывать"


async def test_fundout_without_fund_row_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(PackFundLedger, PackFund, Income)
    message = make_message("/fundout 0.01 попытка")
    await cmd_fundout(message)
    assert "Реальный перевод" not in said(message)
    async with SessionLocal() as db:
        assert (await db.execute(select(PackFundLedger))).scalars().all() == []


async def test_incoming_journal_shapes(monkeypatch) -> None:
    """Журнал входящих: у кого деньги, сколько и когда. Кошелёк без игрока и
    игрок без username — оба случая обязаны читаться, иначе неизвестный
    перевод нельзя разобрать."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Income, Player)
    message = make_message("/incoming")
    await cmd_incoming(message)
    assert "пока нет" in said(message)

    naive_stamp = datetime(2026, 3, 4, 12, 30)
    async with SessionLocal() as db:
        db.add_all(
            [
                Player(id=21, username="rich", first_name="Богач"),
                Player(id=22, username=None, first_name="БезНика"),
                Income(
                    kind="ton",
                    amount_stars=0,
                    amount_nanotons=to_nano(2),
                    player_id=21,
                    unit_ref="tx-rich",
                    note="перевод",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
                Income(
                    kind="ton",
                    amount_stars=0,
                    amount_nanotons=to_nano(1),
                    player_id=22,
                    unit_ref="tx-nick",
                    note="перевод",
                    network=current_network(),
                    created_at=naive_stamp,
                ),
                Income(
                    kind="ton",
                    amount_stars=0,
                    amount_nanotons=to_nano(0.5),
                    player_id=999,
                    unit_ref="tx-ghost",
                    note="перевод",
                    network=current_network(),
                    created_at=None,
                ),
                Income(
                    kind="ton",
                    amount_stars=0,
                    amount_nanotons=to_nano(0.25),
                    player_id=None,
                    unit_ref="tx-nowallet",
                    note="перевод",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

    filled = make_message("/incoming")
    await cmd_incoming(filled)
    text = said(filled)
    assert "@rich" in text
    assert "БезНика" in text
    assert "id999" in text
    assert "неизвестный кошелёк" in text
    assert "04.03 12:30 UTC" in text
    assert "tonscan.org/tx/tx-nowallet" in text


async def test_incoming_tolerates_row_without_timestamp(monkeypatch) -> None:
    """Строка без created_at (легаси-импорт) не должна ронять весь журнал:
    в выводе такая запись помечается прочерком, а не ломает /incoming."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    payout_mod = importlib.import_module("app.handlers.payout")
    legacy = SimpleNamespace(
        id=7,
        player_id=None,
        amount_nanotons=to_nano(1.25),
        note="легаси",
        unit_ref="tx-legacy",
        created_at=None,
    )
    rows = [(legacy, None, None)]

    class _Result:
        def all(self) -> list:
            return rows

    class _Session:
        async def execute(self, _stmt) -> _Result:
            return _Result()

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_exc) -> bool:
            return False

    monkeypatch.setattr(payout_mod, "SessionLocal", _Session)
    message = make_message("/incoming")
    await cmd_incoming(message)
    text = said(message)
    assert "—" in text
    assert "легаси" in text
    assert "неизвестный кошелёк" in text
    assert "1.25 Gram" in text


async def test_stakes_listing(monkeypatch) -> None:
    """Список ставок дня: доход админа в спорном дне. Без дней и без ставок —
    разные сообщения, иначе «пусто» читается как «игра не идёт»."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Stake, Round, Player)
    no_days = make_message("/stakes")
    await cmd_stakes(no_days)
    assert "Дней ещё нет" in said(no_days)

    async with SessionLocal() as db:
        day = _round(80_010, RoundStatus.TALLYING)
        db.add(day)
        await db.commit()
        day_id = day.id
    no_stakes = make_message("/stakes")
    await cmd_stakes(no_stakes)
    assert "Ставок за день 80010 нет" in said(no_stakes)

    async with SessionLocal() as db:
        db.add_all(
            [
                Player(id=31, username="nick", first_name="Имя"),
                Player(id=32, username=None, first_name="ТолькоИмя"),
                Player(id=33, username=None, first_name=None),
                Player(id=34, first_name="Странный"),
            ]
        )
        db.add_all(
            [
                Stake(
                    round_id=day_id,
                    player_id=31,
                    amount_nanotons=to_nano(1),
                    tx_hash="s-1",
                    network=current_network(),
                    status="confirmed",
                ),
                Stake(
                    round_id=day_id,
                    player_id=32,
                    amount_nanotons=to_nano(2),
                    tx_hash="s-2",
                    network=current_network(),
                    status="pending",
                ),
                Stake(
                    round_id=day_id,
                    player_id=33,
                    amount_nanotons=to_nano(3),
                    tx_hash="s-3",
                    network=current_network(),
                    status="rejected",
                ),
                # Статус вне привычных трёх (уже возвращённая) показываем как
                # есть: молча скрывать его нельзя, это долг, который вернули.
                Stake(
                    round_id=day_id,
                    player_id=34,
                    amount_nanotons=to_nano(0.5),
                    tx_hash="s-4",
                    network=current_network(),
                    status="refunded",
                ),
            ]
        )
        await db.commit()

    listed = make_message("/stakes")
    await cmd_stakes(listed)
    text = said(listed)
    assert "Ставки дня 80010" in text and "tallying" in text
    assert "nick" in text and "✅" in text
    assert "ТолькоИмя" in text and "⏳" in text
    assert "игрок 33" in text and "↩️" in text
    assert "refunded" in text


async def test_revenue_splits_stars_and_gram(monkeypatch) -> None:
    """Касса: звёзды и Gram отдельно, ручные корректировки казны — не доход."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN))
    await _wipe(Income)
    empty = make_message("/revenue")
    await cmd_revenue(empty)
    assert "пусто" in said(empty)

    ops = importlib.import_module("app.ops")
    async with SessionLocal() as db:
        db.add_all(
            [
                Income(
                    kind="stars",
                    amount_stars=120,
                    amount_nanotons=0,
                    unit_ref="s-1",
                    note="оплата",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
                Income(
                    kind="stars",
                    amount_stars=30,
                    amount_nanotons=0,
                    unit_ref="s-2",
                    note="оплата",
                    network=current_network(),
                    created_at=datetime.now(UTC) - timedelta(days=40),
                ),
                Income(
                    kind="ton",
                    amount_stars=0,
                    amount_nanotons=to_nano(5),
                    unit_ref="t-1",
                    note="перевод",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
                Income(
                    kind=ops.MANUAL_OUT_KIND,
                    amount_stars=0,
                    amount_nanotons=to_nano(9),
                    unit_ref="m-out",
                    note="корректировка",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
                Income(
                    kind=ops.MANUAL_IN_KIND,
                    amount_stars=0,
                    amount_nanotons=to_nano(4),
                    unit_ref="m-in",
                    note="корректировка",
                    network=current_network(),
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

    text = await _revenue_text()
    assert "Месяц" in text and "Всего" in text
    assert "⭐ 120 (1 оплат)" in text
    # 150 звёзд за всё время, но за месяц только 120.
    assert "⭐ 150 (2 оплат)" in text
    assert "💎 5.0000 Gram (1 переводов)" in text
    assert "9.0000 Gram" not in text
    assert "4.0000 Gram" not in text


async def test_stakes_panel_lists_pending_days(monkeypatch) -> None:
    """Панель ставок хранителя: необработанные переводы по последним дням."""
    ops = importlib.import_module("app.ops")
    monkeypatch.setattr(ops, "snapshot", AsyncMock(return_value={"pending_stakes": 4}))
    await _wipe(Stake, Round, Player)
    empty = await _stakes_panel_text()
    assert "Необработанных переводов-ставок: 4" in empty
    assert "Ставок за последние дни нет" in empty

    async with SessionLocal() as db:
        today = _round(80_020, RoundStatus.OPEN)
        yesterday = _round(80_019, RoundStatus.CLOSED)
        idle = _round(80_018, RoundStatus.CLOSED)
        db.add_all([today, yesterday, idle])
        await db.commit()
        today_id, yesterday_id = today.id, yesterday.id
    async with SessionLocal() as db:
        db.add_all(
            [
                Player(id=41, username="pupil", first_name="Ученик"),
                Player(id=42, username=None, first_name="Аноним"),
                Player(id=43, username=None, first_name=None),
            ]
        )
        db.add_all(
            [
                Stake(
                    round_id=today_id,
                    player_id=41,
                    amount_nanotons=to_nano(1),
                    tx_hash="p-1",
                    network=current_network(),
                    status="pending",
                ),
                Stake(
                    round_id=today_id,
                    player_id=42,
                    amount_nanotons=to_nano(0.5),
                    tx_hash="p-2",
                    network=current_network(),
                    status="confirmed",
                ),
                Stake(
                    round_id=yesterday_id,
                    player_id=43,
                    amount_nanotons=to_nano(0.25),
                    tx_hash="p-3",
                    network=current_network(),
                    status="rejected",
                ),
            ]
        )
        await db.commit()

    text = await _stakes_panel_text()
    assert "День 80020 (open)" in text
    assert "pupil" in text and "Аноним" in text
    assert "День 80019 (closed)" in text
    assert "игрок 43" in text
    # День без ставок пропускается молча, а не печатает пустую секцию.
    assert "80018" not in text


async def test_refunds_panel_lists_candidates(monkeypatch) -> None:
    """Панель ручных возвратов: только незасчитанные ставки без выплаты."""
    await _wipe(Payout, Stake, Round, Player, PackFund)
    empty = await _refunds_panel_text()
    assert "Нет кандидатов" in empty

    async with SessionLocal() as db:
        db.add_all(
            [
                Player(id=51, username="back", first_name="Возврат"),
                Player(id=52, first_name="БезЮ"),
                Player(id=53, first_name="Засчитанный"),
            ]
        )
        day = _round(80_030, RoundStatus.TALLYING)
        idle_day = _round(80_029, RoundStatus.OPEN)
        db.add_all([day, idle_day])
        await db.commit()
        day_id = day.id
    async with SessionLocal() as db:
        db.add_all(
            [
                Stake(
                    round_id=day_id,
                    player_id=51,
                    amount_nanotons=to_nano(1),
                    tx_hash="r-1",
                    network=current_network(),
                    status="pending",
                ),
                Stake(
                    round_id=day_id,
                    player_id=52,
                    amount_nanotons=to_nano(2),
                    tx_hash="r-2",
                    network=current_network(),
                    status="rejected",
                ),
                # Подтверждённые в ручной возврат не попадают: их разберут итоги.
                Stake(
                    round_id=day_id,
                    player_id=53,
                    amount_nanotons=to_nano(3),
                    tx_hash="r-3",
                    network=current_network(),
                    status="confirmed",
                ),
            ]
        )
        await db.commit()
        stake_ids = list((await db.execute(select(Stake.id).order_by(Stake.id))).scalars())

    text = await _refunds_panel_text()
    assert "РУЧНОЙ ВОЗВРАТ СТАВОК" in text
    assert "back" in text
    assert "БезЮ" in text
    assert "не подтверждена" in text
    assert "отклонена" in text
    assert f"/return {stake_ids[0]}" in text
    assert f"/return {stake_ids[1]}" in text
    assert f"/return {stake_ids[2]}" not in text
    assert "день 80030" in text
    # День без ставок в панели не появляется.
    assert "80029" not in text


async def test_treasury_guard_uses_admin_id_set(monkeypatch) -> None:
    """Несколько хранителей — список в настройках, а не один id."""
    monkeypatch.setattr(settings, "admin_ids", "1, 4242 ,777")
    assert settings.admin_id_set == {1, 4242, 777}
    second = make_message("/payouts", uid=777)
    await cmd_payouts(second)
    assert "Долгов нет" in said(second) or "Неотправленные" in said(second)


@pytest.mark.parametrize("missing", [False, True])
async def test_payouts_survives_null_from_user(monkeypatch, missing: bool) -> None:
    monkeypatch.setattr(settings, "admin_ids", "")
    message = make_message("/payouts")
    if missing:
        message.from_user = None
    await cmd_payouts(message)
    assert "только для хранителя" in said(message)
