"""Тесты операционного контура: ретраи выплат, курсор watcher'а,
автоматическая выплата копилки месяца и стоп-фильтр генераций."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ton_pay
from app.config import settings
from app.core.registry import CURSOR_KEY, MONTH_CLAIM_WINDOW_KEY
from app.db import SessionLocal
from app.leaderboard import MARKER_KEY, MONTH_READY_KEY, previous_month_key, settle_month_if_due
from app.models import (
    LeaderboardPot,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WinRule,
)
from app.payments import parse_revote_memo
from app.ton_utils import to_nano


def _closed_round(day_index: int, tally_at: datetime) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=tally_at - timedelta(hours=25),
        voting_ends_at=tally_at - timedelta(hours=1),
        tally_ends_at=tally_at,
        winner_card=1,
        vote_counts_json="{}",
        payouts_finalized=True,
    )


async def _seed_expired_month_window(session: AsyncSession, period: str, players: list[int]) -> None:
    """Окно месячного Claim с истёкшим дедлайном: выплата может идти сразу."""
    opened_at = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    session.add(
        WatcherState(
            key=MONTH_CLAIM_WINDOW_KEY,
            value=json.dumps({"period": period, "players": players, "opened_at": opened_at}),
        )
    )
    await session.commit()


# ---------- Ретраи выплат ----------


async def _mk_payout(db: AsyncSession, **kwargs) -> Payout:
    payout = Payout(
        round_id=None,
        player_id=None,
        kind="prize",
        amount_nanotons=to_nano(0.5),
        dest_address="0:" + os.urandom(16).hex(),
        network="testnet" if settings.is_testnet else "mainnet",
        **kwargs,
    )
    db.add(payout)
    await db.commit()
    return payout


async def test_failed_payout_is_retried_until_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_max_attempts", 3)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=1)
        try:
            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 2 and payout.status == "pending"

            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 3 and payout.status == "failed"
            # Лимит исчерпан — больше не ретраится.
            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 3 and payout.status == "failed"
        finally:
            await db.delete(payout)
            await db.commit()


async def test_successful_send_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_send(dest, amount, comment):
        return "tx-ok-1"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=4)
        try:
            assert await ton_pay.dispatch_pending_payouts() >= 1
            await db.refresh(payout)
            assert payout.status == "sent" and payout.tx_hash == "tx-ok-1" and payout.attempts == 0
        finally:
            await db.delete(payout)
            await db.commit()


async def test_stuck_sending_is_revived_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Падение сервиса между broadcast и коммитом: зависшая sending доигрывается."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_send(dest, amount, comment):
        return "tx-recovered"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="sending", attempts=2)
        try:
            assert await ton_pay.dispatch_pending_payouts() >= 1
            await db.refresh(payout)
            assert payout.status == "sent" and payout.tx_hash == "tx-recovered"
        finally:
            await db.delete(payout)
            await db.commit()


async def test_admin_alerted_once_per_payout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")  # admin_id_set — property
    bot = SimpleNamespace(send_message=AsyncMock())
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=99)
        try:
            assert await ton_pay.dispatch_pending_payouts(bot=bot) == 0
            await db.refresh(payout)
            assert payout.status == "failed"
            # Дедуп алертов живёт в БД: флаг выставлен вместе с отправкой.
            assert payout.alerted is True
            assert bot.send_message.await_count == 1
            # Повторный цикл по той же выплате алерт не дублирует.
            assert await ton_pay.dispatch_pending_payouts(bot=bot) == 0
            assert bot.send_message.await_count == 1
        finally:
            await db.delete(payout)
            await db.commit()


# ---------- Курсор watcher'а ----------


async def test_watch_once_advances_cursor_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    # Свежие метки времени: древние переводы watcher больше не возвращает.
    base = int(datetime.now(UTC).timestamp()) - 3_600
    transfers = [
        ton_watch.Transfer(f"c-{i}", "0:" + os.urandom(32).hex(), to_nano(0.2), "", base + 1 + i)
        for i in range(3)
    ]
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=(transfers, True))
    )

    # Стартовый курсор раньше всех транзакций (фолбэк «now−12ч» их новее).
    async with SessionLocal() as db:
        db.add(WatcherState(key=ton_watch.CURSOR_KEY, value=str(base - 1)))
        await db.commit()

    await ton_watch.watch_once()
    async with SessionLocal() as db:
        cursor_row = await db.get(WatcherState, ton_watch.CURSOR_KEY)
        assert cursor_row is not None and int(cursor_row.value) == base + 3
        refunds = (
            await db.execute(
                select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_([t.tx_hash for t in transfers]))
            )
        ).scalars().all()
        assert len(refunds) == 3

    # Повторный прогон по тем же транзакциям: возвраты не задваиваются.
    await ton_watch.watch_once()
    async with SessionLocal() as db:
        refunds = (
            await db.execute(
                select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_([t.tx_hash for t in transfers]))
            )
        ).scalars().all()
        assert len(refunds) == 3
        await db.execute(WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY))
        await db.execute(
            Payout.__table__.delete().where(Payout.kind == "refund").where(Payout.player_id.is_(None))
        )
        await db.commit()


async def test_watch_cursor_stops_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    bad = ton_watch.Transfer("bad-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", base)
    good = ton_watch.Transfer("good-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", base + 1)
    monkeypatch.setattr(
        ton_watch,
        "fetch_recent_transfers",
        AsyncMock(return_value=([good, bad], True)),
    )  # API отдаёт новые сверху; watch обрабатывает по возрастанию

    async def exploding(transfer):
        if transfer.tx_hash == "bad-1":
            raise RuntimeError("цепь моргнула")
        return "refund_queued"

    monkeypatch.setattr(ton_watch, "process_transfer", exploding)
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            row = await db.get(WatcherState, ton_watch.CURSOR_KEY)
            assert row is None or int(row.value) < base  # курсор застрял до сбойного перевода
    finally:
        async with SessionLocal() as db:
            await db.execute(WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY))
            await db.commit()


async def test_overlap_window_recovers_same_second_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transfer при utime == курсору: окно перекрытия читает курсор на 90 сек раньше,
    транзакция той же секунды попадает в фильтр и обрабатывается."""
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    tx_same = ton_watch.Transfer("same-1", "0:" + os.urandom(32).hex(), to_nano(0.3), "", base + 5)
    tx_later = ton_watch.Transfer("later-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", base + 6)
    fetch = AsyncMock(return_value=([tx_same, tx_later], True))
    monkeypatch.setattr(ton_watch, "fetch_recent_transfers", fetch)
    try:
        # Курсор установлен на tx_same.utime: без перекрытия транзакция была бы пропущена.
        async with SessionLocal() as db:
            db.add(WatcherState(key=ton_watch.CURSOR_KEY, value=str(base + 5)))
            await db.commit()

        await ton_watch.watch_once()
        async with SessionLocal() as db:
            refunds = (
                await db.execute(
                    select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_(["same-1", "later-1"]))
                )
            ).scalars().all()
        assert len(refunds) == 2  # обе транзакции обработаны, несмотря на utime == курсору
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY)
            )
            await db.execute(
                Payout.__table__.delete().where(
                    Payout.kind == "refund", Payout.tx_hash.in_(["same-1", "later-1"])
                )
            )
            await db.commit()


async def test_degraded_cycle_freezes_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неполный проход (api_ok=False): курсор должен остаться на месте,
    чтобы дыра в индексе не превратилась в вечную дыру."""
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    fetch = AsyncMock(return_value=([], False, "none", None))
    monkeypatch.setattr(ton_watch, "_collect_transfers", fetch)
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=ton_watch.CURSOR_KEY, value=str(base)))
            await db.commit()

        await ton_watch.watch_once()
        async with SessionLocal() as db:
            row = await db.get(WatcherState, ton_watch.CURSOR_KEY)
            assert int(row.value) == base  # курсор не сдвинулся
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY)
            )
            await db.commit()


async def test_scan_gap_moves_cursor_to_coverage_front_and_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проход упёрся в бюджет страниц раньше курсора — входящие не теряются.

    Раньше такой цикл считался полным: курсор уезжал по самой свежей
    обработанной транзакции, а окно между ней и границей прочитанного
    выпадало из чтения навсегда — без записи, без тревоги, при зелёном
    /health. Теперь курсор встаёт на дно прочитанного (границу покрытия),
    границы дыры записаны, бюджет страниц удвоен, админ получает алерт.
    """
    from app import ops, ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    base = int(datetime.now(UTC).timestamp()) - 3_600
    fresh = ton_watch.Transfer("fresh-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", base + 10)
    # Прочитали до base+10, но окно от старого курсора до base+5 не вычитали.
    collect = AsyncMock(return_value=([fresh], True, "tonapi", base + 5))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    monkeypatch.setattr(ton_watch, "process_transfer", AsyncMock(return_value="refund_queued"))
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=ton_watch.CURSOR_KEY, value=str(base)))
            await db.commit()

        await ton_watch.watch_once()

        # since — с учётом окна перекрытия (курсор минус 90 с), а не сам курсор.
        scanned_from = collect.await_args.args[0]
        assert scanned_from < base
        async with SessionLocal() as db:
            cursor = await db.get(WatcherState, CURSOR_KEY)
            # Курсор встал на границу покрытия: выше неё всё прочитано, ниже
            # догонится следующими циклами. Уехать выше (base+10) он не может —
            # тогда окно base+5..base+10 потерялось бы навсегда.
            assert cursor is not None and int(cursor.value) == base + 5
            assert await db.get(WatcherState, ton_watch.BEAT_KEY) is not None  # цикл при этом здоров
            gap = await ton_watch._read_scan_gap(db)
            assert gap is not None and gap["floor"] == base + 5
            assert gap["since"] == scanned_from
            assert await ton_watch._read_scan_boost(db) == 2  # бюджет страниц удвоен

        problems = await ops.check_anomalies(bot=bot)
        assert problems and "непрочитанное окно" in problems[0]
        assert bot.send_message.await_count == 1
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_(
                        [
                            CURSOR_KEY,
                            ton_watch.BEAT_KEY,
                            ton_watch.SCAN_GAP_KEY,
                            ton_watch.SCAN_BOOST_KEY,
                            ops.ALERT_SCAN_GAP_KEY,
                            ton_watch.SOURCE_KEY,
                        ]
                    )
                )
            )
            await db.commit()


async def test_scan_gap_backlog_drains_over_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дыра не копится: следующий цикл читает от границы покрытия вглубь.

    Бэклог глубже бюджета страниц не должен ни теряться, ни стоять на месте.
    Курсор при этом не откатывается (он монотонный): покрытие догоняет сам
    проход — он стартует от границы и уходит глубже, обрабатывая то, что ниже.
    """
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    base = int(datetime.now(UTC).timestamp()) - 3_600
    deeper = ton_watch.Transfer("deeper-1", "0:" + os.urandom(32).hex(), to_nano(0.1), "", base + 2)
    collect = AsyncMock(
        side_effect=[
            # Цикл 1: прочитали до base+20, курсор должен встать на base+20.
            ([], True, "tonapi", base + 20),
            # Цикл 2: следующий проход идёт уже от границы покрытия и читает глубже.
            ([deeper], True, "tonapi", base + 1),
        ]
    )
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    process = AsyncMock(return_value="refund_queued")
    monkeypatch.setattr(ton_watch, "process_transfer", process)
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=ton_watch.CURSOR_KEY, value=str(base)))
            await db.commit()

        await ton_watch.watch_once()
        async with SessionLocal() as db:
            assert int((await db.get(WatcherState, CURSOR_KEY)).value) == base + 20
            assert await ton_watch._read_scan_boost(db) == 2

        await ton_watch.watch_once()
        # Второй проход стартовал от границы покрытия минус окно перекрытия.
        assert collect.await_args_list[1].args[0] <= base + 20
        # Перевод из старого бэклога (ниже границы прошлого цикла) обработан.
        assert [t.tx_hash for t in process.await_args_list[0].args[:1]] == ["deeper-1"]
        async with SessionLocal() as db:
            # Курсор не откатился назад, но граница покрытия уехала глубже.
            assert int((await db.get(WatcherState, CURSOR_KEY)).value) == base + 20
            gap = await ton_watch._read_scan_gap(db)
            assert gap is not None and gap["floor"] == base + 1
            assert await ton_watch._read_scan_boost(db) == 4
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_(
                        [
                            CURSOR_KEY,
                            ton_watch.BEAT_KEY,
                            ton_watch.SCAN_GAP_KEY,
                            ton_watch.SCAN_BOOST_KEY,
                            ton_watch.SOURCE_KEY,
                        ]
                    )
                )
            )
            await db.commit()


async def test_full_pass_clears_scan_gap_and_resets_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно дочитано — тревога гаснет, бюджет страниц возвращается к базовому."""
    from app import ops, ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    base = int(datetime.now(UTC).timestamp()) - 3_600
    try:
        async with SessionLocal() as db:
            db.add(WatcherState(key=CURSOR_KEY, value=str(base)))
            db.add(
                WatcherState(
                    key=ton_watch.SCAN_GAP_KEY,
                    value=json.dumps({"since": base, "floor": base + 5, "at": "2024-01-01T00:00:00+00:00"}),
                )
            )
            db.add(WatcherState(key=ton_watch.SCAN_BOOST_KEY, value="4"))
            await db.commit()

        # Полный проход без остаточной дыры.
        monkeypatch.setattr(
            ton_watch,
            "_collect_transfers",
            AsyncMock(return_value=([], True, "tonapi", None)),
        )
        await ton_watch.watch_once()

        async with SessionLocal() as db:
            assert await ton_watch._read_scan_gap(db) is None
            assert await ton_watch._read_scan_boost(db) == 1

        assert await ops.check_anomalies(bot=bot) == []
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_(
                        [
                            CURSOR_KEY,
                            ton_watch.BEAT_KEY,
                            ton_watch.SCAN_GAP_KEY,
                            ton_watch.SCAN_BOOST_KEY,
                            ton_watch.SOURCE_KEY,
                        ]
                    )
                )
            )
            await db.commit()


async def test_watch_beats_on_quiet_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тишина в цепочке — здоровье: сердцебиение ставится и без переводов.

    Именно этот кейс раньше порождал ложный алерт «watcher ещё ни разу не
    отмечал курсор»: курсор двигался только переводами.
    """
    from app import ops, ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=([], True))
    )
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            beat = await db.get(WatcherState, ton_watch.BEAT_KEY)
            assert beat is not None  # цикл прошёл успешно — сердце бьётся
            assert await db.get(WatcherState, ton_watch.CURSOR_KEY) is None
        # Свежее сердцебиение — аномалий нет, алертов нет.
        assert await ops.check_anomalies(bot=None) == []
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ton_watch.BEAT_KEY, ton_watch.CURSOR_KEY])
                )
            )
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_WATCHER_KEY])
                )
            )
            await db.commit()


async def test_api_outage_does_not_beat_and_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """TonAPI и Toncenter лежат вместе — сердцебиения нет, админ узнает об этом."""
    from app import ops, ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=([], False))
    )
    # Фолбэк-источник тоже недоступен: успешного цикла нет ни у одного провайдера.
    monkeypatch.setattr(
        ton_watch,
        "_toncenter_page",
        AsyncMock(return_value=([], ton_watch._PAGE_DEGRADED)),
    )
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            assert await db.get(WatcherState, ton_watch.BEAT_KEY) is None
        problems = await ops.check_anomalies(bot=bot)
        assert problems and "цикл" in problems[0]
        assert bot.send_message.await_count == 1
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ton_watch.BEAT_KEY, ops.ALERT_WATCHER_KEY])
                )
            )
            await db.commit()


async def test_no_watcher_alerts_when_ton_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", False)
    assert await ops.check_anomalies(bot=None) == []


# ---------- Токены (джеттоны) против фонда ----------


def _tx_item(**overrides) -> dict:
    item = {
        "hash": "tx-" + os.urandom(8).hex(),
        "utime": 5000,
        "in_msg": {
            "source": {"address": "0:" + os.urandom(32).hex()},
            "value": str(to_nano(0.5)),
            "raw_message": "",
        },
    }
    item.update(overrides)
    return item


def test_native_ton_transaction_parses() -> None:
    from app import ton_watch

    transfer = ton_watch._parse_tx_item(_tx_item(), since_utime=1000)
    assert transfer is not None
    assert transfer.value_nanotons == to_nano(0.5)


def test_jetton_transfer_is_not_a_stake_and_not_refunded() -> None:
    """USDt/др. токены не смешиваются с фондом: уведомление джеттона
    пропускается целиком — ни ставки, ни пыльного авто-возврата."""
    from app import ton_watch

    jetton = _tx_item(
        in_msg={
            "source": {"address": "EQ" + "A" * 46},  # jetton-кошелёк отправителя
            "value": "1000000",  # копейки газа в обёртке, не сумма перевода
            "opcode": "0x7362d09c",
        }
    )
    assert ton_watch._is_jetton_notification(jetton["in_msg"])
    assert ton_watch._parse_tx_item(jetton, since_utime=1000) is None

    # Альтернативная форма сигнала от API — decoded_op.
    decoded = _tx_item(in_msg={"msg_data": {"decoded_op": "transfer_notification"}})
    assert ton_watch._is_jetton_notification(decoded["in_msg"])
    assert ton_watch._parse_tx_item(decoded, since_utime=1000) is None


def test_decode_comment_strips_invisible_wallet_noise() -> None:
    """Невидимые символы вокруг rv:-мемо (нулевые/неразрывные пробелы, BOM)
    вычищаются при декодировании, чтобы мемо дошло до разбора целым."""
    import base64 as _b64

    from app import ton_watch

    clean = ton_watch._decode_comment(
        {"msg_data": {"decoded_comment": "\u200brv:\u200d17\u00a0"}}
    )
    assert parse_revote_memo(clean) == 17
    # base64-поле «text» тоже чистится.
    b64 = _b64.b64encode("\ufeffrv:17".encode()).decode()
    assert parse_revote_memo(ton_watch._decode_comment({"msg_data": {"text": b64}})) == 17

    # Нативный TON с обычным опкодом джеттоном не считается.
    native = _tx_item(in_msg={**_tx_item()["in_msg"], "opcode": "0x00000000"})
    assert not ton_watch._is_jetton_notification(native["in_msg"])


def test_old_or_empty_transactions_are_skipped() -> None:
    from app import ton_watch

    assert ton_watch._parse_tx_item(_tx_item(utime=999), since_utime=1000) is None
    empty = _tx_item()
    empty["in_msg"]["value"] = "0"
    assert ton_watch._parse_tx_item(empty, since_utime=1000) is None


def test_previous_month_key() -> None:
    assert previous_month_key(datetime(2026, 8, 22, tzinfo=UTC)) == "2026-07"
    assert previous_month_key(datetime(2026, 1, 5, tzinfo=UTC)) == "2025-12"


# ---------- Копилка месяца ----------


async def _seed_leaderboard_month(session: AsyncSession) -> tuple[int, dict]:
    """Два закрытых дня прошлого месяца; p1 угадал дважды, p2 один раз."""
    pid1 = 910_000 + int.from_bytes(os.urandom(2), "big")
    pid2 = pid1 + 1
    wallet1 = "0:" + os.urandom(32).hex()
    session.add_all(
        [
            Player(id=pid1, username=f"u{pid1}", wallet_address=wallet1, wallet_verified=True),
            Player(id=pid2, username=f"u{pid2}"),
        ]
    )
    prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
    round_a = _closed_round(700_001, prev_month - timedelta(days=10))
    round_b = _closed_round(700_002, prev_month - timedelta(days=9))
    session.add_all([round_a, round_b])
    await session.flush()
    session.add_all(
        [
            Vote(round_id=round_a.id, player_id=pid1, card_position=1),
            Vote(round_id=round_b.id, player_id=pid1, card_position=1),
            Vote(round_id=round_b.id, player_id=pid2, card_position=2),
            # Лидер ставил в месяце (требование выплаты) — confirmed-ставка.
            Stake(
                round_id=round_b.id,
                player_id=pid1,
                amount_nanotons=to_nano(1),
                tx_hash="tx_" + os.urandom(16).hex(),
                status="confirmed",
            ),
        ]
    )
    pot_old = LeaderboardPot(month=(prev_month - timedelta(days=40)).strftime("%Y-%m"), nanotons=300)
    pot_prev = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1.7))
    session.add_all([pot_old, pot_prev])
    await session.commit()
    total = pot_old.nanotons + pot_prev.nanotons
    return total, {"p1": pid1, "p2": pid2, "w1": wallet1}


async def test_monthly_pot_paid_to_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    async with SessionLocal() as session:
        total, ids = await _seed_leaderboard_month(session)
        pots = (await session.execute(select(LeaderboardPot))).scalars().all()
        months = [p.month for p in pots]
        # Ставим флаг готовности: эпилог последнего дня месяца записан.
        from app.leaderboard import MONTH_READY_KEY
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=bot) is True
            payout = (
                await session.execute(
                    select(Payout).where(Payout.kind == "leaderboard", Payout.player_id == ids["p1"])
                )
            ).scalar_one()
            assert payout.amount_nanotons == total
            assert payout.dest_address == ids["w1"]
            assert payout.round_id is None
            assert (await session.execute(select(LeaderboardPot))).all() == []
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is not None and marker.value == previous_month_key()
            assert bot.send_message.await_count == 1

            # Идемпотентность: в том же месяце второй раз не платим.
            assert await settle_month_if_due(bot=bot) is False
            assert bot.send_message.await_count == 1
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month.in_(months)))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MONTH_READY_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([ids["p1"], ids["p2"]])))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_([ids["p1"], ids["p2"]])))
            rounds = (
                (await session.execute(select(Round).where(Round.day_index.in_([700_001, 700_002])))).scalars().all()
            )
            for round_row in rounds:
                await session.delete(round_row)
            for pid in (ids["p1"], ids["p2"]):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_split_between_tied_leaders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ничья по верным путям и вкладу Gram: места по меньшему player_id, 50/30.

    Месяц по умолчанию — топ-3 с весами 50/30/20: двое равных (верность и
    Gram совпали, никто не жал Claim) занимают первое и второе места, веса
    нормируются на двоих получателей (50/80 и 30/80 от горшка).
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_a = 930_000 + int.from_bytes(os.urandom(2), "big")
    pid_b = pid_a + 1
    wallet_a = "0:" + os.urandom(32).hex()
    wallet_b = "0:" + os.urandom(32).hex()
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a, username=f"u{pid_a}", wallet_address=wallet_a, wallet_verified=True),
                Player(id=pid_b, username=f"u{pid_b}", wallet_address=wallet_b, wallet_verified=True),
            ]
        )
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(702_001, prev_month - timedelta(days=2))
        session.add(round_row)
        await session.flush()
        session.add_all(
            [
                Vote(round_id=round_row.id, player_id=pid_a, card_position=1),
                Vote(round_id=round_row.id, player_id=pid_b, card_position=1),
                Stake(
                    round_id=round_row.id, player_id=pid_a,
                    amount_nanotons=to_nano(1), tx_hash=f"tx_{pid_a}", status="confirmed",
                ),
                Stake(
                    round_id=round_row.id, player_id=pid_b,
                    amount_nanotons=to_nano(1), tx_hash=f"tx_{pid_b}", status="confirmed",
                ),
            ]
        )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        # Двое абсолютно равных: дедлайн окна Claim истёк — места по меньшему id.
        await _seed_expired_month_window(session, month_key, [pid_a, pid_b])
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "leaderboard").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [(p.player_id, p.dest_address) for p in rows] == [
                (pid_a, wallet_a),
                (pid_b, wallet_b),
            ]
            assert sum(p.amount_nanotons for p in rows) == to_nano(1)
            # Взвешенный дележ 50/30 (нормализованы на двоих из топ-3).
            assert rows[0].amount_nanotons == 625_000_000
            assert rows[1].amount_nanotons == 375_000_000
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_a, pid_b])))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_([pid_a, pid_b])))
            await session.delete(round_row)
            for pid in (pid_a, pid_b):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_pays_verified_wallet_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Месячная копилка идёт только на подтверждённый (bv:) кошелёк лидера.

    Топ верных путей с привязанным, но неподтверждённым адресом пропускается,
    как и без кошелька: приз не должен уйти на чужой/недоказанный адрес, а
    bounce без обработки сжёг бы горш молча.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_shadow, pid_proven = 920_000, 920_001
    wallet_proven = "0:" + os.urandom(32).hex()
    prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_shadow, username="shadow", wallet_address="0:" + os.urandom(32).hex()),
                Player(
                    id=pid_proven, username="proven", wallet_address=wallet_proven, wallet_verified=True
                ),
            ]
        )
        rounds = [
            _closed_round(703_001 + i, prev_month - timedelta(days=9 + i)) for i in range(5)
        ]
        session.add_all(rounds)
        await session.flush()
        # shadow — 5 верных из 5, proven — 3 из 5: без верификации лидер мимо.
        plan = {pid_shadow: 5, pid_proven: 3}
        for i, round_row in enumerate(rounds):
            for pid, count in plan.items():
                if i < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
                session.add(
                    Stake(
                        round_id=round_row.id, player_id=pid,
                        amount_nanotons=to_nano(1), tx_hash=f"tx_{i}_{pid}", status="confirmed",
                    )
                )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "leaderboard").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [p.player_id for p in rows] == [pid_proven]
            assert rows[0].dest_address == wallet_proven and rows[0].amount_nanotons > 0
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MONTH_READY_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_shadow, pid_proven])))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_([pid_shadow, pid_proven])))
            for round_row in rounds:
                await session.delete(round_row)
            for pid in (pid_shadow, pid_proven):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()

async def test_monthly_pot_pays_top_k_by_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сглаживание дисперсии: топ-K месячной копилки делится по весам."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_top_k", 2)
    monkeypatch.setattr(settings, "monthly_prize_weights", "60,30,10")
    # Горш теста — символические 1000 нанотонов; тест проверяет РАСКЛАД ПО ВЕСАМ,
    # а не реальные суммы. Порог снимаем, чтобы пыль не блокировала выплату.
    monkeypatch.setattr(settings, "min_payout_gram", 0.0)
    pid_a = 960_000 + int.from_bytes(os.urandom(2), "big")
    pid_b = pid_a + 1
    wallet_a = "0:" + os.urandom(32).hex()
    wallet_b = "0:" + os.urandom(32).hex()
    async with SessionLocal() as session:
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        ra = _closed_round(760_001, prev_month - timedelta(days=9))
        rb = _closed_round(760_002, prev_month - timedelta(days=8))
        session.add_all(
            [
                Player(id=pid_a, username=f"a{pid_a}", wallet_address=wallet_a, wallet_verified=True),
                Player(id=pid_b, username=f"b{pid_b}", wallet_address=wallet_b, wallet_verified=True),
            ]
        )
        session.add_all([ra, rb])
        await session.flush()
        session.add_all(
            [
                Vote(round_id=ra.id, player_id=pid_a, card_position=1),
                Vote(round_id=rb.id, player_id=pid_a, card_position=1),
                Vote(round_id=rb.id, player_id=pid_b, card_position=1),
                # Оба ставили в месяце (требование выплаты).
                Stake(
                    round_id=ra.id, player_id=pid_a, amount_nanotons=to_nano(1),
                    tx_hash="tx_" + os.urandom(16).hex(), status="confirmed",
                ),
                Stake(
                    round_id=rb.id, player_id=pid_b, amount_nanotons=to_nano(1),
                    tx_hash="tx_" + os.urandom(16).hex(), status="confirmed",
                ),
            ]
        )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=1000)
        month_key = pot.month
        session.add(pot)
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                await session.execute(select(Payout).where(Payout.kind == "leaderboard"))
            ).scalars().all()
            amounts = {p.player_id: p.amount_nanotons for p in rows}
            assert set(amounts) == {pid_a, pid_b}
            # 60/30 из "60,30,10" нормируются по двум получателям: 60/90 и
            # 30/90 от 1000 → 667 и 333 (пыль — первому месту).
            assert amounts[pid_a] == 667 and amounts[pid_b] == 333
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_a, pid_b])))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_([pid_a, pid_b])))
            for round_row in (await session.execute(select(Round).where(Round.day_index.in_([760_001, 760_002])))).scalars().all():
                await session.delete(round_row)
            for pid in (pid_a, pid_b):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_carried_when_leader_has_no_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    async with SessionLocal() as session:
        # Только игрок без кошелька угадывает.
        pid = 920_000 + int.from_bytes(os.urandom(2), "big")
        session.add(Player(id=pid, username=f"u{pid}"))
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(701_001, prev_month - timedelta(days=3))
        session.add(round_row)
        await session.flush()
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(2))
        session.add(pot)
        month_key = pot.month
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (await session.scalar(select(LeaderboardPot.nanotons).where(LeaderboardPot.month == month_key))) == to_nano(2)
            assert await session.get(WatcherState, MARKER_KEY) is None
        finally:
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.delete(round_row)
            await session.execute(Vote.__table__.delete().where(Vote.player_id == pid))
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_monthly_pot_waits_when_leader_has_no_stake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Лидер с кошельком и голосами, но без ставки в месяце — приз ждёт."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    async with SessionLocal() as session:
        pid = 950_000 + int.from_bytes(os.urandom(2), "big")
        session.add(
            Player(id=pid, username=f"u{pid}", wallet_address="0:" + os.urandom(32).hex(), wallet_verified=True)
        )
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(706_001, prev_month - timedelta(days=3))
        session.add(round_row)
        await session.flush()
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(2))
        session.add(pot)
        month_key = pot.month
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (
                await session.scalar(select(LeaderboardPot.nanotons).where(LeaderboardPot.month == month_key))
            ) == to_nano(2)
            assert await session.get(WatcherState, MARKER_KEY) is None
        finally:
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.delete(round_row)
            await session.execute(Vote.__table__.delete().where(Vote.player_id == pid))
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_monthly_pot_waits_until_last_day_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Незафинализированный последний день месяца откладывает выплату целиком.

    Аналог недельного гейта: копилка может быть недолита, пока хоть один
    день периода не закрыл выплаты — метку не двигаем, горш цел.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    async with SessionLocal() as session:
        now = datetime.now(UTC)
        prev_month = now.replace(day=1) - timedelta(days=5)
        unfinished = Round(
            day_index=704_001,
            status=RoundStatus.TALLYING,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="text",

            opens_at=prev_month - timedelta(days=1, hours=1),
            voting_ends_at=prev_month - timedelta(hours=1),
            tally_ends_at=prev_month,
            winner_card=None,
            vote_counts_json="{}",
        )
        session.add(unfinished)
        player_row = Player(
            id=950_100 + int.from_bytes(os.urandom(2), "big"),
            username="u",
            wallet_address="0:" + os.urandom(32).hex(),
        )
        session.add(player_row)
        await session.flush()
        session.add(Vote(round_id=unfinished.id, player_id=player_row.id, card_position=1))
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(2))
        month_key = pot.month
        session.add(pot)
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (
                await session.scalar(select(LeaderboardPot.nanotons).where(LeaderboardPot.month == month_key))
            ) == to_nano(2)
            assert await session.get(WatcherState, MARKER_KEY) is None
        finally:
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.round_id == unfinished.id))
            await session.delete(unfinished)
            player = await session.get(Player, player_row.id)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_monthly_pot_not_burned_by_empty_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустые monthly_prize_weights (опечатка конфига) не сжигают горш.

    Раньше пустой список весов давал пустые выплаты, но копилка всё равно
    удалялась и метка двигалась — месячная казна пропадала бесследно.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "monthly_prize_weights", "")
    pid = 959_000 + int.from_bytes(os.urandom(2), "big")
    async with SessionLocal() as session:
        session.add(
            Player(id=pid, username=f"u{pid}", wallet_address="0:" + os.urandom(32).hex(), wallet_verified=True)
        )
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(705_001, prev_month - timedelta(days=3))
        session.add(round_row)
        await session.flush()
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
        session.add(
            Stake(
                round_id=round_row.id, player_id=pid, amount_nanotons=to_nano(1),
                tx_hash="tx_" + os.urandom(16).hex(), status="confirmed",
            )
        )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(2))
        month_key = pot.month
        session.add(pot)
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (
                await session.scalar(select(LeaderboardPot.nanotons).where(LeaderboardPot.month == month_key))
            ) == to_nano(2)
            assert await session.get(WatcherState, MARKER_KEY) is None
        finally:
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id == pid))
            await session.execute(Stake.__table__.delete().where(Stake.player_id == pid))
            await session.delete(round_row)
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_monthly_pot_gram_tiebreak_at_third_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Грам-лидер на границе топ-3 с бОльшим id не выпадает из призов.

    Раньше _rank_window резал SQL-LIMIT до грам-тайбрейка: при равном счёте
    на 3-м месте выбирались меньшие player_id, игрок с большим вкладом
    Gram (и большим id) терял место. Теперь LIMIT режет после грам-сортировки.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = 958_000 + int.from_bytes(os.urandom(1), "big") * 100
    pids = [base + i for i in range(5)]
    grams = [1, 2, 3, 4, 6]
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    async with SessionLocal() as session:
        session.add_all(
            Player(id=pid, username=f"p{i}", wallet_address=wallets[pid], wallet_verified=True)
            for i, pid in enumerate(pids)
        )
        prev_month = datetime.now(UTC).replace(day=1) - timedelta(days=5)
        rounds: list[Round] = []
        for i in range(5):
            round_row = _closed_round(707_001 + i, prev_month - timedelta(days=9 + i))
            session.add(round_row)
            rounds.append(round_row)
        await session.flush()
        for i, round_row in enumerate(rounds):
            for pid in pids:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
            session.add(
                Stake(
                    round_id=round_row.id, player_id=pids[i],
                    amount_nanotons=to_nano(grams[i]),
                    tx_hash="tx_" + os.urandom(16).hex(), status="confirmed",
                )
            )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            amounts = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "leaderboard"))).scalars()
            }
            # Все равны по верным путям — места решает вклад Gram: p5, p4, p3.
            assert amounts == {
                pids[4]: 500_000_000,
                pids[3]: 300_000_000,
                pids[2]: 200_000_000,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_(pids)))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_(pids)))
            for round_row in rounds:
                await session.delete(round_row)
            for pid in pids:
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_ignores_already_settled_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Чемпион позапрошлого (уже выплаченного) месяца не забирает новый горш.

    Окно лидерборда — от начала самого старого невыплатенного месяца до
    начала текущего; голоса за его пределами не считаются.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_champ = 940_000 + int.from_bytes(os.urandom(2), "big")
    pid_new = pid_champ + 1
    wallet_new = "0:" + os.urandom(32).hex()
    now = datetime.now(UTC)
    prev_first = (now.replace(day=1) - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    two_ago_key = (prev_first - timedelta(days=1)).strftime("%Y-%m")
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_champ, username=f"u{pid_champ}"),
                Player(id=pid_new, username=f"u{pid_new}", wallet_address=wallet_new, wallet_verified=True),
            ]
        )
        # Чемпион: 2 верных в позапрошлом месяце (вне окна выплат).
        champ_round_a = _closed_round(703_001, prev_first - timedelta(days=10))
        champ_round_b = _closed_round(703_003, prev_first - timedelta(days=5))
        # Новичок: 1 верный в прошлом месяце (внутри окна).
        new_round = _closed_round(703_002, prev_first + timedelta(days=5))
        session.add_all([champ_round_a, champ_round_b, new_round])
        await session.flush()
        session.add_all(
            [
                Vote(round_id=champ_round_a.id, player_id=pid_champ, card_position=1),
                Vote(round_id=champ_round_b.id, player_id=pid_champ, card_position=1),
                Vote(round_id=new_round.id, player_id=pid_new, card_position=1),
                # Победитель ставил в окне — ставка confirmed.
                Stake(
                    round_id=new_round.id, player_id=pid_new,
                    amount_nanotons=to_nano(1), tx_hash=f"tx_{pid_new}", status="confirmed",
                ),
            ]
        )
        pot = LeaderboardPot(month=prev_first.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        session.add(WatcherState(key=MONTH_READY_KEY, value=previous_month_key()))
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "leaderboard", Payout.round_id.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].player_id == pid_new
            assert rows[0].dest_address == wallet_new
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(
                LeaderboardPot.__table__.delete().where(LeaderboardPot.month.in_([month_key, two_ago_key]))
            )
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_champ, pid_new])))
            await session.execute(Stake.__table__.delete().where(Stake.player_id.in_([pid_champ, pid_new])))
            for round_row in (champ_round_a, champ_round_b, new_round):
                await session.delete(round_row)
            for pid in (pid_champ, pid_new):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


# ---------- Разбивка очереди и целевые алерты (возвраты, необработанное) ----------


def _open_round(day_index: int) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",

        opens_at=now - timedelta(hours=1),
        voting_ends_at=now + timedelta(hours=1),
        tally_ends_at=now + timedelta(hours=2),
    )


async def _fresh_watcher_marks() -> None:
    """Свежие тик и сердцебиение: фоновые алерты watcher'а не спамят в тесте."""
    from app import ops, ton_watch

    now = datetime.now(UTC).isoformat()
    async with SessionLocal() as db:
        await db.execute(
            WatcherState.__table__.delete().where(
                WatcherState.key.in_([ops.TICK_KEY, ton_watch.BEAT_KEY])
            )
        )
        db.add_all(
            [
                WatcherState(key=ops.TICK_KEY, value=now),
                WatcherState(key=ton_watch.BEAT_KEY, value=now),
            ]
        )
        await db.commit()


async def test_snapshot_reports_unprocessed_and_payout_by_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health и пульт видят: сколько необработанных ставок и сколько выплат
    по типу ждёт возврата / приза против безнадёжных (failed)."""
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", True)
    uid = 950_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(16).hex()
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    async with SessionLocal() as db:
        db.add(Player(id=uid, username=f"u{uid}", wallet_address=wallet))
        rnd = _open_round(704_001)
        db.add(rnd)
        await db.flush()
        db.add_all(
            [
                Payout(round_id=None, player_id=uid, kind="refund",
                       amount_nanotons=to_nano(0.3), dest_address=wallet,
                       status="pending", created_at=old),
                Payout(round_id=None, player_id=uid, kind="prize",
                       amount_nanotons=to_nano(0.5), dest_address=wallet,
                       status="pending", created_at=old),
                Payout(round_id=None, player_id=uid, kind="refund",
                       amount_nanotons=to_nano(0.1), dest_address=wallet,
                       status="failed", created_at=old),
                Stake(round_id=rnd.id, player_id=uid, amount_nanotons=to_nano(0.2),
                      tx_hash="snap-tx", status="pending", created_at=old),
            ]
        )
        await db.commit()
    try:
        snap = await ops.snapshot()
        assert snap["pending_stakes"] >= 1
        assert snap["payout_pending_by_kind"].get("refund", 0) >= 1
        assert snap["payout_pending_by_kind"].get("prize", 0) >= 1
        assert snap["payout_dead_by_kind"].get("refund", 0) >= 1
    finally:
        async with SessionLocal() as db:
            await db.execute(Payout.__table__.delete().where(Payout.player_id == uid))
            await db.execute(Stake.__table__.delete().where(Stake.player_id == uid))
            await db.execute(Round.__table__.delete().where(Round.day_index == 704_001))
            player = await db.get(Player, uid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_snapshot_status_is_honest_about_problems(monkeypatch) -> None:
    """/health не врёт «ok» при живых проблемах или серии падений тика."""
    from app import ops

    async with SessionLocal() as db:
        await ops._store_problems(db, ["очередь выплат стоит"])
    try:
        payload = await ops.snapshot()
        assert payload["status"] == "degraded"
        assert payload["problems"]  # сам вердикт тоже виден
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.OPS_PROBLEMS_KEY, ops.OPS_PROBLEMS_AT_KEY])
                )
            )
            await db.commit()
    # Чистое состояние — снова честный «ok».
    payload = await ops.snapshot()
    assert payload["status"] == "ok"


async def test_stuck_refund_alert_is_targeted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Возврат ставки, что так и не ушёл, будит хранителя конкретным алертом."""
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    uid = 951_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(16).hex()
    async with SessionLocal() as db:
        db.add(Player(id=uid, username=f"u{uid}", wallet_address=wallet))
        db.add(
            Payout(round_id=None, player_id=uid, kind="refund",
                   amount_nanotons=to_nano(0.4), dest_address=wallet,
                   status="pending",
                   created_at=datetime.now(UTC) - timedelta(hours=3))
        )
        await db.commit()
    try:
        await _fresh_watcher_marks()
        problems = await ops.check_anomalies(bot=bot)
        assert any("возврат ставки" in p for p in problems)
        sent = [c.args[1] if len(c.args) > 1 else "" for c in bot.send_message.await_args_list]
        assert any("Возврат ставки" in t for t in sent)
    finally:
        async with SessionLocal() as db:
            await db.execute(Payout.__table__.delete().where(Payout.player_id == uid))
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_REFUND_KEY, ops.TICK_KEY])
                )
            )
            player = await db.get(Player, uid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_unprocessed_pending_stakes_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переводы-ставки, что висят без подтверждения дольше окна, — алерт админу."""
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 40)  # порог = 80 с
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    uid = 952_000 + int.from_bytes(os.urandom(2), "big")
    async with SessionLocal() as db:
        db.add(Player(id=uid, username=f"u{uid}"))
        rnd = _open_round(705_001)
        db.add(rnd)
        await db.flush()
        db.add(
            Stake(round_id=rnd.id, player_id=uid, amount_nanotons=to_nano(0.2),
                  tx_hash="pending-tx", status="pending",
                  created_at=datetime.now(UTC) - timedelta(minutes=15))
        )
        await db.commit()
    try:
        await _fresh_watcher_marks()
        problems = await ops.check_anomalies(bot=bot)
        assert any("ставок не обработано" in p for p in problems)
        sent = [c.args[1] if len(c.args) > 1 else "" for c in bot.send_message.await_args_list]
        assert any("Переводы-ставки не обрабатываются" in t for t in sent)
    finally:
        async with SessionLocal() as db:
            await db.execute(Stake.__table__.delete().where(Stake.player_id == uid))
            await db.execute(Round.__table__.delete().where(Round.day_index == 705_001))
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_STAKE_KEY, ops.TICK_KEY])
                )
            )
            player = await db.get(Player, uid)
            if player is not None:
                await db.delete(player)
            await db.commit()


# ---------- Честность жребия: закон дня без энтропии сети ----------


async def test_day_without_entropy_raises_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """День, открытый без энтропии мастерчейна, поднимает тревогу.

    Фолбэк на локальный secrets-жребий нужен, чтобы день не завис при молчащем
    TonAPI, — но он не проверяем игроком по эксплореру. Раньше это уходило
    в лог и оставалось невидимым: оператор узнавал о непроверяемом исходе
    только из вопросов игроков в чате.
    """
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    day = 706_001
    async with SessionLocal() as db:
        db.add(_open_round(day))  # rule_entropy не задан → жребий локальный
        await db.commit()
    try:
        await _fresh_watcher_marks()
        problems = await ops.check_anomalies(bot=bot)
        assert any(str(day) in p and "энтропии" in p for p in problems)
        sent = [c.args[1] if len(c.args) > 1 else "" for c in bot.send_message.await_args_list]
        assert any("без блока TON" in t for t in sent)
    finally:
        async with SessionLocal() as db:
            await db.execute(Round.__table__.delete().where(Round.day_index == day))
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_ENTROPY_KEY, ops.TICK_KEY])
                )
            )
            await db.commit()


async def test_day_with_entropy_stays_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обратная сторона: день на честном блоке TON тревоги не поднимает."""
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    day = 706_002
    async with SessionLocal() as db:
        rnd = _open_round(day)
        rnd.rule_entropy = "96545285:" + "a" * 64
        db.add(rnd)
        await db.commit()
    try:
        await _fresh_watcher_marks()
        problems = await ops.check_anomalies(bot=bot)
        assert not any("энтропии" in p for p in problems)
    finally:
        async with SessionLocal() as db:
            await db.execute(Round.__table__.delete().where(Round.day_index == day))
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_ENTROPY_KEY, ops.TICK_KEY])
                )
            )
            await db.commit()
