"""Сверка казны (/adjust) и стоп-кран игры (/pause, /resume).

Расхождение «баланс цепочки ↔ ожидания БД» закрывается кнопкой или командой:
ручной вывод — леджер запоминает сумму и алерт замолкает; пропажа средств —
то же плюс пауза игры с авто-возвратом входящих переводов о техработах.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, update

from app import ops, ton_pay
from app.config import settings
from app.db import SessionLocal
from app.handlers import (
    _adjust_keyboard,
    _admin_panel_text,
    _panel_keyboard,
    cmd_adjust,
    cmd_advance,
    cmd_pause,
    cmd_payouts,
    cmd_resume,
    on_adjust_action,
    on_panel_action,
    on_paystars,
    on_pre_checkout,
)
from app.handlers import panel as panel_mod
from app.models import (
    Income,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WinRule,
)
from app.stakes import register_stake
from app.ton_utils import normalize_address, to_nano
from app.ton_watch import Transfer, process_transfer

RAW = normalize_address("UQpfcexKrlNjGFPF44W9am1o75Z6fs_QBdwVNzuhHVX2L4oo")
STRANGER = "0:" + "9" * 62
ADMIN_ID = 4242
# Аутсайдер не должен совпадать ни с одним id окружения (CI ставит ADMIN_IDS=1).
OUTSIDER_ID = 777_007


@pytest.fixture()
def ton_on(monkeypatch):
    monkeypatch.setattr(settings, "ton_enabled", True)


@pytest.fixture()
def admin_only(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))


@pytest.fixture(autouse=True)
def offline_generation(monkeypatch):
    """Шаблонная генерация работает офлайн и без стабов."""


async def _wipe_pause() -> None:
    async with SessionLocal() as db:
        await db.execute(
            WatcherState.__table__.delete().where(
                WatcherState.key.in_([ops.PAUSE_KEY, ops.PAUSE_REASON_KEY])
            )
        )
        await db.commit()


async def _wipe_money_rows() -> None:
    """Корректировки, возвраты и seed-леджер не перетекают между тестами."""
    async with SessionLocal() as db:
        await db.execute(
            delete(Income).where(
                Income.kind.in_([ops.MANUAL_OUT_KIND, ops.MANUAL_IN_KIND, "ton"])
            )
        )
        await db.execute(delete(Payout).where(Payout.kind == "refund"))
        await db.commit()


def make_message(uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        bot=AsyncMock(),
        text=text,
        answer=AsyncMock(),
    )


def make_callback(uid: int, data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid),
        bot=AsyncMock(),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            chat=SimpleNamespace(type="private"),
        ),
        answer=AsyncMock(),
    )


async def _drain_background(timeout: float = 10.0) -> None:
    """Тик плодит фоновые задачи (пинок диспетчера и пр.) — даём им закрыть
    сессии БД, иначе SQLite-лок валит очистку следующих тестов."""
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if pending:
        done, pending = await asyncio.wait(pending, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# ---------- Формула сверки и корректировки ----------


async def test_drift_detected_then_manual_out_silences_alert(ton_on, monkeypatch) -> None:
    """Сценарий инцидента: ручной вывод 1.3569 Gram — алерт, после /adjust тишина."""
    monkeypatch.setattr(settings, "treasury_address", "0:" + "11" * 32)
    monkeypatch.setattr(
        ton_pay,
        "fetch_account_state",
        AsyncMock(return_value=(to_nano(12.6431), None, "tonapi")),
    )
    async with SessionLocal() as db:
        db.add(Income(kind="ton", amount_nanotons=to_nano(14), unit_ref="seed-rv-1"))
        await db.commit()
    try:
        async with SessionLocal() as session:
            note = await ops._treasury_balance_anomaly(session)
            assert note is not None and "ниже ожиданий" in note
            assert "12.6431" in note and "/adjust" in note

            state = await ops.treasury_expected_state(session)
            assert state.drift_nanotons == to_nano(14) - to_nano(12.6431)
            assert state.beyond_tolerance

            await ops.record_manual_adjustment(
                session, ops.MANUAL_OUT_KIND, state.drift_nanotons, "ручной вывод на биржу"
            )
            # Ожидания скорректированы — алерту больше не о чем кричать.
            assert await ops._treasury_balance_anomaly(session) is None
    finally:
        await _wipe_money_rows()


async def test_single_stake_not_double_counted(ton_on, monkeypatch) -> None:
    """Регрессия: один перевод 1 Gram (подтверждённая ставка + её Income-строка)
    не даёт фантомной «пропажи». Раньше ставка считалась дважды (Stake в staked
    + Income kind=ton в revotes), и ровно на её сумму появлялось ложное
    расхождение «баланс ниже ожиданий БД»."""
    monkeypatch.setattr(settings, "treasury_address", "0:" + "33" * 32)
    balance = to_nano(1)
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(balance, None, "tonapi"))
    )
    now = datetime.now(UTC)
    try:
        async with SessionLocal() as db:
            round_row = Round(
                day_index=97_960,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",

                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
            )
            player = Player(id=930_961, username="one_gram")
            db.add_all([round_row, player])
            await db.flush()
            db.add_all(
                [
                    Stake(
                        round_id=round_row.id, player_id=player.id,
                        amount_nanotons=to_nano(1), tx_hash="ok-1g",
                        status="confirmed",
                        network="testnet" if settings.is_testnet else "mainnet",
                    ),
                    Income(
                        kind="ton", amount_nanotons=to_nano(1),
                        unit_ref="ok-1g", round_id=round_row.id, player_id=player.id,
                        note="in:stake:ok",
                    ),
                ]
            )
            await db.commit()
        async with SessionLocal() as session:
            state = await ops.treasury_expected_state(session)
            assert state is not None
            # 1 Gram на цепи = 1 Gram прихода БД (ровно один учёт): дрейфа нет.
            assert state.balance_nanotons == to_nano(1)
            assert state.expected_nanotons == to_nano(1)
            assert state.drift_nanotons == 0
            assert await ops._treasury_balance_anomaly(session) is None
    finally:
        await _wipe_money_rows()
        async with SessionLocal() as db:
            await db.execute(delete(Stake).where(Stake.tx_hash == "ok-1g"))
            await db.execute(delete(Player).where(Player.id == 930_961))
            await db.execute(delete(Round).where(Round.day_index == 97_960))
            await db.commit()


async def test_record_adjustment_validates_input() -> None:
    with pytest.raises(ValueError):
        await ops.record_manual_adjustment(None, "manual_sideways", to_nano(1))
    with pytest.raises(ValueError):
        await ops.record_manual_adjustment(None, ops.MANUAL_OUT_KIND, 0)


async def test_treasury_expected_state_filters_by_network(ton_on, monkeypatch) -> None:
    """Сверка казны считает только приходы активного контура: Income других
    сетей больше не раздувают ожидания (регрессия на смешение mainnet/testnet)."""
    from app.stakes import current_network

    monkeypatch.setattr(settings, "treasury_address", "0:" + "44" * 32)
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(to_nano(3), None, "tonapi"))
    )
    active = current_network()
    other = "testnet" if active == "mainnet" else "mainnet"
    try:
        async with SessionLocal() as db:
            db.add_all(
                [
                    Income(kind="ton", amount_nanotons=to_nano(3), unit_ref="seed-net-a", network=active),
                    Income(kind="ton", amount_nanotons=to_nano(9), unit_ref="seed-net-b", network=other),
                    Income(kind="ton", amount_nanotons=to_nano(15), unit_ref="seed-net-c", network=""),
                    Income(kind=ops.MANUAL_OUT_KIND, amount_nanotons=to_nano(2), unit_ref="seed-net-d", network=other),
                    Income(kind=ops.MANUAL_IN_KIND, amount_nanotons=to_nano(5), unit_ref="seed-net-e", network=""),
                ]
            )
            await db.commit()
        async with SessionLocal() as session:
            state = await ops.treasury_expected_state(session)
            # На балансе 3 Gram, учтён только приход активной сети — дрейфа нет.
            assert state is not None
            assert state.expected_nanotons == to_nano(3)
            assert state.drift_nanotons == 0
            assert await ops._treasury_balance_anomaly(session) is None
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(Income).where(
                    Income.unit_ref.in_(["seed-net-a", "seed-net-b", "seed-net-c", "seed-net-d", "seed-net-e"])
                )
            )
            await db.commit()


async def test_manual_refund_creates_net_payout_and_is_idempotent(ton_on) -> None:
    """Ручной возврат «не засчитанной» ставки: создаётся refund-выплата (за
    вычетом газа), ставка помечается возвращённой, повторный вызов не дублирует."""
    from app.stakes import create_manual_refund

    now = datetime.now(UTC)
    try:
        async with SessionLocal() as db:
            round_row = Round(
                day_index=97_961,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",

                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
            )
            player = Player(
                id=930_962,
                username="refund_me",
                wallet_address=RAW,
                wallet_verified=True,
            )
            db.add_all([round_row, player])
            await db.flush()
            stake = Stake(
                round_id=round_row.id, player_id=player.id,
                amount_nanotons=to_nano(1), tx_hash="ref-1g",
                status="pending",
                network="testnet" if settings.is_testnet else "mainnet",
            )
            db.add(stake)
            await db.commit()
            stake_id = stake.id
        async with SessionLocal() as session:
            first = await create_manual_refund(session, stake_id)
            assert first.startswith("возврат")
            second = await create_manual_refund(session, stake_id)
            assert "уже" in second
            rows = (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all()
        refunds = [p for p in rows if p.round_id == round_row.id]
        assert len(refunds) == 1
        assert refunds[0].amount_nanotons == to_nano(1 - settings.payout_fee_gram)
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Payout).where(Payout.kind == "refund"))
            await db.execute(delete(Stake).where(Stake.tx_hash == "ref-1g"))
            await db.execute(delete(Player).where(Player.id == 930_962))
            await db.execute(delete(Round).where(Round.day_index == 97_961))
            await db.commit()


async def test_manual_refund_racing_day_finalization_pays_once(monkeypatch, ton_on) -> None:
    """Гонка /return с закрытием дня: одна ставка — одна выплата.

    Окно шире, чем кажется на взгляд: ручной возврат успевает ПРОЧИТАТЬ
    ставку как pending и пройти все проверки (dup-поиск и claim_once), а в
    это время тик закрывает день и создаёт свой refund по той же ставке.
    Обе стороны лили ставку безусловным присваиванием статуса, поэтому в
    базе оказывались ДВЕ refund-выплаты за одну ставку.

    Пауза стоит на claim_once — последнем асинхронном шаге перед записью:
    читать ставку хендлер уже успел, писать ещё не начал.
    """
    from app import stakes as stakes_mod
    from app.stakes import create_manual_refund, finalize_day_payouts

    now = datetime.now(UTC)
    round_id = None
    try:
        async with SessionLocal() as db:
            round_row = Round(
                day_index=97_964,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",
                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
            )
            player = Player(
                id=930_964,
                username="refund_race",
                wallet_address=RAW,
                wallet_verified=True,
            )
            db.add_all([round_row, player])
            await db.flush()
            stake = Stake(
                round_id=round_row.id,
                player_id=player.id,
                amount_nanotons=to_nano(1),
                tx_hash="ref-race",
                status="pending",
                network="testnet" if settings.is_testnet else "mainnet",
            )
            db.add(stake)
            await db.commit()
            stake_id = stake.id
            round_id = round_row.id

        real_claim_once = stakes_mod.claim_once
        reached = asyncio.Event()
        resume = asyncio.Event()

        async def pausing_claim_once(session, key):
            if key.startswith("manual_refund:"):
                reached.set()
                await resume.wait()
            return await real_claim_once(session, key)

        monkeypatch.setattr(stakes_mod, "claim_once", pausing_claim_once)

        async with SessionLocal() as manual_session:
            task = asyncio.create_task(create_manual_refund(manual_session, stake_id))
            await asyncio.wait_for(reached.wait(), timeout=10)

            # В это окно закрывается день — свой refund по той же ставке.
            async with SessionLocal() as fin_session:
                fin_round = await fin_session.get(Round, round_id)
                await finalize_day_payouts(fin_session, fin_round)

            resume.set()
            result = await asyncio.wait_for(task, timeout=10)

            rows = (
                await manual_session.execute(
                    select(Payout).where(
                        Payout.kind == "refund",
                        Payout.round_id == round_id,
                    )
                )
            ).scalars().all()

        assert len(rows) == 1, (
            f"одна ставка дала {len(rows)} refund-выплат (ручной: {result!r}) — двойной возврат"
        )
        async with SessionLocal() as db:
            final_stake = await db.get(Stake, stake_id)
        assert final_stake is not None and final_stake.status == "refunded"
    finally:
        async with SessionLocal() as db:
            if round_id is not None:
                await db.execute(delete(Payout).where(Payout.round_id == round_id))
            await db.execute(delete(Payout).where(Payout.player_id == 930_964))
            await db.execute(delete(Stake).where(Stake.tx_hash == "ref-race"))
            await db.execute(delete(Player).where(Player.id == 930_964))
            await db.execute(delete(Round).where(Round.day_index == 97_964))
            await db.commit()


async def test_day_finalization_refuses_stake_claimed_mid_flight(
    monkeypatch, ton_on
) -> None:
    """Финализация дня не платит ставку, разобранную после её снапшота.

    Снапшот `stuck` берётся сразу после claim'а раунда, а claim по КАЖДОЙ
    ставке стоит в конце — после подгрузки кошельков, выборки победителей и
    разбора копилок. В этом окне ручной /return успевает закоммитить свой
    refund. Флаг payouts_finalized не помогает: он защищает повтор самой
    финализации, а ручной возврат работает со ставкой, а не с раундом.

    На SQLite такая гонка невозможна в принципе — у базы один писатель и
    финализация держит write-транзакцию от claim'а раунда до конца. Поэтому
    ставка здесь переводится в refunded ВНУТРИ той же транзакции, что и
    финализация: это ровно то состояние, которое увидит условный claim на
    Postgres, где ставки ничем не залочены.
    """
    from app import stakes as stakes_mod
    from app.stakes import finalize_day_payouts

    now = datetime.now(UTC)
    round_id = None
    pending_stake_id = None
    hooked = False
    try:
        async with SessionLocal() as db:
            round_row = Round(
                day_index=97_965,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",
                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
                winner_card=0,
            )
            winner = Player(
                id=930_965, username="race_winner",
                wallet_address=RAW, wallet_verified=True,
            )
            late = Player(
                id=930_966, username="race_late",
                wallet_address=STRANGER, wallet_verified=True,
            )
            db.add_all([round_row, winner, late])
            await db.flush()
            network = "testnet" if settings.is_testnet else "mainnet"
            db.add_all(
                [
                    Vote(round_id=round_row.id, player_id=winner.id, card_position=0),
                    Stake(
                        round_id=round_row.id, player_id=winner.id,
                        amount_nanotons=to_nano(2), tx_hash="race-win",
                        status="confirmed", network=network,
                    ),
                    Stake(
                        round_id=round_row.id, player_id=late.id,
                        amount_nanotons=to_nano(1), tx_hash="race-pend",
                        status="pending", network=network,
                    ),
                ]
            )
            await db.commit()
            round_id = round_row.id
            pending_stake_id = await db.scalar(
                select(Stake.id).where(Stake.tx_hash == "race-pend")
            )

        real_claim_once = stakes_mod.claim_once

        async def poaching_claim_once(session, key):
            nonlocal hooked
            if key.startswith("pot:") and not hooked:
                hooked = True
                # Снапшот `stuck` уже сделан — «ручной возврат» забирает
                # ставку. Пишем в ту же транзакцию, что и финализация:
                # commit здесь поднял бы блокировку и сузил гонку.
                await session.execute(
                    update(Stake).where(Stake.id == pending_stake_id).values(status="refunded")
                )
            return await real_claim_once(session, key)

        monkeypatch.setattr(stakes_mod, "claim_once", poaching_claim_once)

        async with SessionLocal() as session:
            fin_round = await session.get(Round, round_id)
            created = await finalize_day_payouts(session, fin_round)
            rows = (
                await session.execute(
                    select(Payout).where(Payout.round_id == round_id)
                )
            ).scalars().all()

        assert hooked, "хук на _claim_pot не сработал — тест ничего не проверил"
        refunds = [p for p in rows if p.kind == "refund"]
        prizes = [p for p in rows if p.kind == "prize"]
        assert refunds == [], "день повторно заплатил ставку, уже разобранную параллельно"
        assert len(prizes) == 1, f"приз победителю должен быть ровно один, got {created}: {prizes}"
        async with SessionLocal() as db:
            late_stake = await db.get(Stake, pending_stake_id)
        assert late_stake.status == "refunded"
    finally:
        async with SessionLocal() as db:
            if round_id is not None:
                await db.execute(delete(Payout).where(Payout.round_id == round_id))
                await db.execute(delete(Vote).where(Vote.round_id == round_id))
            await db.execute(delete(Stake).where(Stake.tx_hash.in_(["race-win", "race-pend"])))
            await db.execute(delete(Player).where(Player.id.in_([930_965, 930_966])))
            await db.execute(delete(Round).where(Round.day_index == 97_965))
            await db.commit()


async def test_manual_refund_refuses_unverified_wallet(ton_on) -> None:
    """Адрес без bv:-подтверждения — не получатель: возврат ушёл бы в
    необработанный bounce. И отказ обязан быть незалипающим: игрок подтвердил
    кошелёк — повтор создаёт выплату."""
    from app.stakes import create_manual_refund

    now = datetime.now(UTC)
    try:
        async with SessionLocal() as db:
            round_row = Round(
                day_index=97_962,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",
                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
            )
            player = Player(id=930_963, username="refund_unv", wallet_address=RAW)
            db.add_all([round_row, player])
            await db.flush()
            stake = Stake(
                round_id=round_row.id, player_id=player.id,
                amount_nanotons=to_nano(1), tx_hash="ref-unv",
                status="pending",
                network="testnet" if settings.is_testnet else "mainnet",
            )
            db.add(stake)
            await db.commit()
            stake_id = stake.id
            player_id = player.id
        async with SessionLocal() as session:
            refused = await create_manual_refund(session, stake_id)
            assert "bv:" in refused
            assert (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all() == []
            # Ставка не тронута: возврат не оформлен.
            s2 = await session.get(Stake, stake_id)
            assert s2.status == "pending"
        async with SessionLocal() as session:
            await session.execute(
                update(Player).where(Player.id == player_id).values(wallet_verified=True)
            )
            await session.commit()
        async with SessionLocal() as session:
            again = await create_manual_refund(session, stake_id)
            assert "поставлен в очередь" in again, again
            rows = (
                await session.execute(select(Payout).where(Payout.kind == "refund"))
            ).scalars().all()
        assert len([p for p in rows if p.round_id == round_row.id]) == 1
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Payout).where(Payout.kind == "refund"))
            await db.execute(delete(Stake).where(Stake.tx_hash == "ref-unv"))
            await db.execute(delete(Player).where(Player.id == 930_963))
            await db.execute(delete(Round).where(Round.day_index == 97_962))
            await db.execute(delete(WatcherState).where(WatcherState.key == f"manual_refund:{round_row.id}:{player_id}"))
            await db.commit()


async def test_set_game_paused_is_idempotent() -> None:
    await _wipe_pause()
    try:
        async with SessionLocal() as session:
            assert await ops.set_game_paused(session, True, "техработы") is True
            assert await ops.is_game_paused(session) is True
            # Повтор — no-op: двойной тап не перезаписывает причину.
            assert await ops.set_game_paused(session, True, "другое") is False
            assert await ops.paused_reason(session) == "техработы"
            assert await ops.set_game_paused(session, False) is True
            assert await ops.is_game_paused(session) is False
            assert await ops.paused_reason(session) is None
    finally:
        await _wipe_pause()


# ---------- Команда и кнопки /adjust ----------


async def test_adjust_is_admin_only(ton_on) -> None:
    outsider = make_message(OUTSIDER_ID, "/adjust")
    await cmd_adjust(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]


async def test_adjust_menu_shows_three_buttons(ton_on, admin_only, monkeypatch) -> None:
    # Излишек 5 Gram против пустых ожиданий: меню с кнопками и подсказкой.
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(to_nano(5), None, "tonapi"))
    )
    message = make_message(ADMIN_ID, "/adjust")
    await cmd_adjust(message)
    markup = message.answer.call_args.kwargs.get("reply_markup")
    assert markup is not None
    texts = [button.text for row in markup.inline_keyboard for button in row]
    assert len(texts) == 3
    assert any("ручной вывод" in t.lower() for t in texts)
    assert any("пропажа" in t.lower() and "пауза" in t.lower() for t in texts)
    assert [row[0].callback_data for row in _adjust_keyboard().inline_keyboard] == [
        "adj:out",
        "adj:loss",
        "adj:in",
    ]
    body = message.answer.call_args.args[0]
    assert "Ожидания БД" in body and "пополнение" in body

    # Баланс недоступен — меню честно говорит об этом (кнопки остаются,
    # но нажатие сообщит то же самое).
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(None, None, "none"))
    )
    offline_menu = make_message(ADMIN_ID, "/adjust")
    await cmd_adjust(offline_menu)
    assert "недоступен" in offline_menu.answer.call_args.args[0]


async def test_adjust_button_double_press_records_manual_out(
    ton_on, admin_only, monkeypatch
) -> None:
    """Первый тап предупреждает, повторный — записывает корректировку."""
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(to_nano(2), None, "tonapi"))
    )
    async with SessionLocal() as db:
        db.add(Income(kind="ton", amount_nanotons=to_nano(3), unit_ref="seed-rv-2"))
        await db.commit()
    try:
        first = make_callback(ADMIN_ID, "adj:out")
        await on_adjust_action(first)
        kwargs = first.answer.call_args.kwargs
        assert kwargs.get("show_alert") is True
        assert "ещё раз" in first.answer.call_args.args[0]

        second = make_callback(ADMIN_ID, "adj:out")
        await on_adjust_action(second)
        second.answer.assert_awaited_with("Записано.")
        sent = [c.args[0] for c in second.message.answer.await_args_list if c.args]
        assert any("Ручной вывод" in t and "1.0000" in t for t in sent)

        async with SessionLocal() as db:
            rows = (
                await db.execute(select(Income).where(Income.kind == ops.MANUAL_OUT_KIND))
            ).scalars().all()
        assert len(rows) == 1 and rows[0].amount_nanotons == to_nano(1)

        # Чужому кнопки закрыты.
        outsider_cb = make_callback(OUTSIDER_ID, "adj:out")
        await on_adjust_action(outsider_cb)
        assert outsider_cb.answer.call_args.kwargs.get("show_alert") is True
    finally:
        await _wipe_money_rows()


async def test_loss_button_pauses_game_and_broadcasts(ton_on, admin_only, monkeypatch) -> None:
    """«Пропажа средств»: леджер + стоп-кран + объявление игрокам."""
    monkeypatch.setattr(
        ton_pay, "fetch_account_state", AsyncMock(return_value=(to_nano(1), None, "tonapi"))
    )
    whisper = AsyncMock(return_value=2)
    monkeypatch.setattr("app.broadcast.whisper_to_chats", whisper)
    await _wipe_pause()
    async with SessionLocal() as db:
        db.add(Income(kind="ton", amount_nanotons=to_nano(4), unit_ref="seed-rv-3"))
        await db.commit()
    try:
        await on_adjust_action(make_callback(ADMIN_ID, "adj:loss"))  # предупреждение
        go = make_callback(ADMIN_ID, "adj:loss")
        await on_adjust_action(go)

        async with SessionLocal() as db:
            paused = await db.get(WatcherState, ops.PAUSE_KEY)
            reason = await db.get(WatcherState, ops.PAUSE_REASON_KEY)
            lost = (
                await db.execute(
                    select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                        Income.kind == ops.MANUAL_OUT_KIND
                    )
                )
            ).scalar_one()
        assert paused is not None
        assert reason.value == "пропажа средств"
        assert lost == to_nano(3)

        whisper.assert_awaited_once()
        assert "приостановлена" in whisper.await_args.args[1]
        sent = [c.args[0] for c in go.message.answer.await_args_list if c.args]
        assert any("/resume" in t for t in sent)
    finally:
        await _wipe_pause()
        await _wipe_money_rows()


async def test_adjust_command_with_explicit_amount(ton_on, admin_only) -> None:
    """Точная сумма аргументами: подтверждение не нужно."""
    try:
        message = make_message(ADMIN_ID, "/adjust 0.75 in ручное пополнение казны")
        await cmd_adjust(message)
        async with SessionLocal() as db:
            row = (
                await db.execute(select(Income).where(Income.kind == ops.MANUAL_IN_KIND))
            ).scalar_one()
        assert row.amount_nanotons == to_nano(0.75)
        assert "пополнение казны" in row.note
        assert row.network == ("testnet" if settings.is_testnet else "mainnet")
        text = message.answer.call_args.args[0]
        assert "Ручное пополнение" in text
    finally:
        await _wipe_money_rows()


# ---------- Пауза: переводы возвращаются, игра замирает ----------


async def test_process_transfer_refunds_everyone_during_pause(ton_on) -> None:
    tx_stranger = "pause-stranger-1"
    tx_player = "pause-player-1"
    await _wipe_pause()
    async with SessionLocal() as session:
        await ops.set_game_paused(session, True, "идут технические работы")
        session.add(Player(id=930_777, username="paused_whale", wallet_address=RAW))
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    try:
        status = await process_transfer(
            Transfer(tx_hash=tx_stranger, source=STRANGER, value_nanotons=to_nano(0.3),
                     comment="", utime=int(datetime.now(UTC).timestamp()))
        )
        assert status == "paused_refund_queued"

        status2 = await process_transfer(
            Transfer(tx_hash=tx_player, source=RAW, value_nanotons=to_nano(0.7),
                     comment="", utime=int(datetime.now(UTC).timestamp())),
            bot=bot,
        )
        assert status2 == "paused_refund_queued"
        # Известному игроку объясняют причину личным сообщением.
        bot.send_message.assert_awaited_once()
        assert "возвращается" in bot.send_message.await_args.args[1]

        async with SessionLocal() as session:
            refunds = (
                await session.execute(
                    select(Payout).where(Payout.kind == "refund").order_by(Payout.id.asc())
                )
            ).scalars().all()
            notes = (
                await session.execute(
                    select(Income.note).where(Income.unit_ref.in_([tx_stranger, tx_player]))
                )
            ).scalars().all()
            stakes_left = (await session.execute(select(func.count()).select_from(Stake))).scalar_one()
            assert {p.tx_hash for p in refunds} == {tx_stranger, tx_player}
            assert all(p.comment_override and "идут технические работы" in p.comment_override for p in refunds)
            assert all("in:paused" in n for n in notes)
        assert stakes_left == 0  # ставки при паузе не создаются
    finally:
        await _wipe_pause()
        await _wipe_money_rows()
        async with SessionLocal() as session:
            player = await session.get(Player, 930_777)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_register_stake_blocked_while_paused(ton_on) -> None:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=97_901,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=10),
        tally_ends_at=now + timedelta(hours=11),
    )
    player = Player(id=930_778, username="guard_p")
    async with SessionLocal() as session:
        session.add_all([round_row, player])
        await session.commit()
    await _wipe_pause()
    try:
        async with SessionLocal() as session:
            fresh_round = (
                await session.execute(select(Round).where(Round.day_index == 97_901))
            ).scalar_one()
            fresh_player = await session.get(Player, 930_778)
            await ops.set_game_paused(session, True, "пауза")
            result = await register_stake(
                session, fresh_round, fresh_player, to_nano(1), "pause-guard-tx"
            )
            assert result == "paused"
            stakes_left = (
                await session.execute(select(func.count()).select_from(Stake))
            ).scalar_one()
            assert stakes_left == 0
    finally:
        await _wipe_pause()
        async with SessionLocal() as session:
            stale_round = (
                await session.execute(select(Round).where(Round.day_index == 97_901))
            ).scalar_one_or_none()
            if stale_round is not None:
                await session.delete(stale_round)
            stale_player = await session.get(Player, 930_778)
            if stale_player is not None:
                await session.delete(stale_player)
            await session.commit()


async def test_tick_halted_while_paused(ton_on) -> None:
    from app.scheduler import tick

    await _wipe_pause()
    try:
        async with SessionLocal() as session:
            rounds_before = (
                await session.execute(select(func.count()).select_from(Round))
            ).scalar_one()
            await ops.set_game_paused(session, True, "стоп-кран")
        await tick(None)
        async with SessionLocal() as session:
            rounds_after = (
                await session.execute(select(func.count()).select_from(Round))
            ).scalar_one()
            beat = await session.get(WatcherState, ops.TICK_KEY)
        assert rounds_after == rounds_before  # дни не открываются
        assert beat is not None  # но тик отмечается: мониторинг жив
    finally:
        await _wipe_pause()


async def test_resume_allows_tick_to_open_day(ton_on, monkeypatch) -> None:
    from sqlalchemy import delete as _delete

    from app.models import Card, PreparedDay
    from app.scheduler import tick

    monkeypatch.setattr(settings, "ton_enabled", False)
    await _wipe_pause()
    try:
        await tick(None)  # день открыт до паузы
        await _drain_background()
        async with SessionLocal() as session:
            latest_before = (
                await session.execute(
                    select(Round.day_index).order_by(Round.day_index.desc()).limit(1)
                )
            ).scalar_one()
            await ops.set_game_paused(session, True, "пауза")

        await tick(None)  # на паузе новый день НЕ открывается
        await _drain_background()
        async with SessionLocal() as session:
            latest_during = (
                await session.execute(
                    select(Round.day_index).order_by(Round.day_index.desc()).limit(1)
                )
            ).scalar_one()
            await ops.set_game_paused(session, False)
        assert latest_during == latest_before

        # Голосование и подсчёт текущего дня истекли: следующий тик
        # закрывает день и открывает новый сам, как ни в чём не бывало.
        async with SessionLocal() as session:
            current = (
                await session.execute(
                    select(Round).order_by(Round.day_index.desc()).limit(1)
                )
            ).scalar_one()
            moment = datetime.now(UTC) - timedelta(minutes=5)
            current.voting_ends_at = moment
            current.tally_ends_at = moment
            await session.commit()
        await tick(None)
        await _drain_background()
        async with SessionLocal() as session:
            latest_after = (
                await session.execute(
                    select(Round.day_index).order_by(Round.day_index.desc()).limit(1)
                )
            ).scalar_one()
        assert latest_after == latest_before + 1
    finally:
        await _wipe_pause()
        async with SessionLocal() as session:
            for round_row in (await session.execute(select(Round))).scalars().all():
                await session.delete(round_row)
            await session.execute(_delete(Card))
            await session.execute(_delete(PreparedDay))
            await session.commit()


# ---------- Пульт и команды поверх паузы ----------


async def test_pause_commands_toggle_and_guard(
    admin_only, ton_on, monkeypatch, tmp_path
) -> None:
    await _wipe_pause()
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    try:
        outsider = make_message(OUTSIDER_ID, "/pause")
        await cmd_pause(outsider)
        assert "только для хранителя" in outsider.answer.call_args.args[0]

        pause_msg = make_message(ADMIN_ID, "/pause чиним казну")
        await cmd_pause(pause_msg)
        async with SessionLocal() as session:
            assert await ops.paused_reason(session) == "чиним казну"
        assert "/resume" in pause_msg.answer.call_args.args[0]

        repeat = make_message(ADMIN_ID, "/pause ещё раз")
        await cmd_pause(repeat)
        assert "уже на паузе" in repeat.answer.call_args.args[0]

        # /advance под паузой больше не отказывает — снимает стоп-кран сам.
        advance_msg = make_message(ADMIN_ID, "/advance")
        await cmd_advance(advance_msg)
        texts = [c.args[0] for c in advance_msg.answer.await_args_list if c.args]
        joined = "\n".join(texts)
        assert "Пауза снята автоматически" in joined
        assert "сначала /resume" not in joined

        resume_msg = make_message(ADMIN_ID, "/resume")
        await cmd_resume(resume_msg)
        assert "и так идёт" in resume_msg.answer.call_args.args[0]
    finally:
        await _wipe_pause()


async def test_panel_shows_pause_line_and_toggle_button(ton_on, admin_only) -> None:
    await _wipe_pause()
    try:
        async with SessionLocal() as session:
            await ops.set_game_paused(session, True, "техработы казны")
            text = await _admin_panel_text(session)
        assert "ИГРА НА ПАУЗЕ" in text and "техработы казны" in text

        markup = await _panel_keyboard()
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "⚖️ Сверка казны" in labels
        assert "▶️ Возобновить игру" in labels

        first = make_callback(ADMIN_ID, "panel:resume")
        await on_panel_action(first)
        assert "ещё раз" in first.answer.call_args.args[0]

        second = make_callback(ADMIN_ID, "panel:resume")
        await on_panel_action(second)
        second.answer.assert_awaited_with("Готово.")
        async with SessionLocal() as session:
            assert await ops.is_game_paused(session) is False

        # На работающей игре кнопка предлагает паузу.
        labels_running = [
            b.text for row in (await _panel_keyboard()).inline_keyboard for b in row
        ]
        assert "⏸ Пауза игры" in labels_running
    finally:
        await _wipe_pause()


# ---------- Комментарий возврата уходит в перевод ----------


async def test_dispatch_sends_comment_override(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    captured: dict[str, object] = {}

    async def fake_send(dest, amount, comment):
        captured["dest"], captured["amount"], captured["comment"] = dest, amount, comment
        return "tx-pause-refund"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)
    try:
        async with SessionLocal() as db:
            payout = Payout(
                kind="refund",
                amount_nanotons=to_nano(0.4),
                dest_address=STRANGER,
                network="testnet" if settings.is_testnet else "mainnet",
                status="pending",
                comment_override="Игра приостановлена: идут технические работы",
            )
            db.add(payout)
            await db.commit()
            payout_id = payout.id
        assert await ton_pay.dispatch_pending_payouts() >= 1
        assert captured["comment"].startswith("Игра приостановлена: идут технические работы")
        # Анти-дубль опознаёт возврат по уникальному суффиксу way:…:refund#<id>
        assert captured["comment"].endswith(f"refund#{payout_id}")
        assert captured["amount"] == to_nano(0.4)
        async with SessionLocal() as db:
            sent = await db.get(Payout, payout_id)
            assert sent.status == "sent" and sent.tx_hash == "tx-pause-refund"
    finally:
        await _wipe_money_rows()


# ---------- Регрессии пульта и платных механик ----------


async def test_payouts_command_actually_answers(admin_only) -> None:
    """/payouts раньше молчал: декоратор висел на хелпере, возвращавшем
    строку (aiogram отправляет только TelegramMethod)."""
    try:
        async with SessionLocal() as db:
            db.add(Payout(
                kind="prize",
                amount_nanotons=to_nano(0.3),
                dest_address="0:" + "ef" * 32,
                status="pending",
            ))
            await db.commit()

        outsider = make_message(OUTSIDER_ID, "/payouts")
        await cmd_payouts(outsider)
        assert "только для хранителя" in outsider.answer.call_args.args[0]

        admin = make_message(ADMIN_ID, "/payouts")
        await cmd_payouts(admin)
        text = admin.answer.call_args.args[0]
        assert "#" in text and "pending" in text
    finally:
        await _wipe_money_rows()


async def test_panel_advance_double_press_now_executes(admin_only, monkeypatch) -> None:
    """Кнопка «Завершить день» раньше вечно просила «ещё раз» — второй тап
    не доходил до действия. Теперь повторный тап подтверждает."""
    calls: list = []

    async def fake_advance(shim_message):
        calls.append(shim_message)
        await shim_message.answer("День 98001 открыт.")

    monkeypatch.setattr(panel_mod, "cmd_advance", fake_advance)

    first = make_callback(ADMIN_ID, "panel:advance")
    first.message.edit_text = AsyncMock()
    await on_panel_action(first)
    args, kwargs = first.answer.call_args
    assert kwargs.get("show_alert") is True and "ещё раз" in args[0]
    assert not calls  # первый тап только предупреждает

    second = make_callback(ADMIN_ID, "panel:advance")
    await on_panel_action(second)
    assert len(calls) == 1  # повторный тап выполнил действие
    second.answer.assert_awaited_with("День переключён.")


async def test_panel_refresh_identical_content_is_not_error(admin_only) -> None:
    """Повторное «Обновить» без изменений — «Без изменений», а не алерт сбоя."""
    from aiogram.exceptions import TelegramBadRequest

    view = make_callback(ADMIN_ID, "panel:view")
    view.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: message is not modified"
        )
    )
    await on_panel_action(view)  # не падает
    view.answer.assert_awaited_with("Без изменений.")


async def test_stars_payment_blocked_while_paused(admin_only, ton_on) -> None:
    """Стоп-кран закрывает и платные механики: счёт не выдаётся,
    а выставленный до паузы счёт отклоняется ДО списания Stars."""
    await _wipe_pause()
    try:
        async with SessionLocal() as session:
            await ops.set_game_paused(session, True, "техработы")

        paystars = SimpleNamespace(
            data="paystars:1",
            from_user=SimpleNamespace(id=ADMIN_ID),
            bot=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=ADMIN_ID, type="private")),
            answer=AsyncMock(),
        )
        await on_paystars(paystars)
        paystars.bot.send_invoice.assert_not_awaited()
        kwargs = paystars.answer.call_args.kwargs
        assert kwargs.get("show_alert") is True
        assert "технические" in paystars.answer.call_args.args[0]

        from app.payments import build_revote_payload

        pre_checkout = SimpleNamespace(
            invoice_payload=build_revote_payload(1),
            answer=AsyncMock(),
        )
        await on_pre_checkout(pre_checkout)
        kwargs = pre_checkout.answer.call_args.kwargs
        assert kwargs.get("ok") is False
        assert "технические" in kwargs.get("error_message", "")
    finally:
        await _wipe_pause()
