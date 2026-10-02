"""Предохранители блокчейн-контура: баланс казначея и сверка с БД.

Гарантии:
- диспетчер не вещает переводы при недостаточном балансе: статус остаётся
  pending, попытки не сгорают, причина видна в /payouts;
- фоновая сверка зовёт админа при дефиците под очередь и при расхождении
  баланса цепочки с ожиданиями БД (ручной вывод / пропажа);
- сходимость (в допуске) не шумит.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Round, RoundStatus, Stake, WinRule
from app.ton_utils import to_nano


def _closed_round(day_index: int) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",

        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
    )


def _state(balance_nanotons: int):
    """Асинхронная заглушка fetch_account_state: (баланс, статус, источник)."""

    async def inner():
        return balance_nanotons, "active", "test"

    return inner()


async def _mk_payout(db, **kwargs) -> Payout:
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


async def _cleanup(db, *day_indexes: int) -> None:
    for day in day_indexes:
        row = (
            await db.execute(select(Round).where(Round.day_index == day))
        ).scalar_one_or_none()
        if row is None:
            continue
        await db.execute(sa_delete(Payout).where(Payout.round_id == row.id))
        await db.execute(sa_delete(Stake).where(Stake.round_id == row.id))
        await db.execute(sa_delete(Income).where(Income.round_id == row.id))
        await db.execute(sa_delete(Round).where(Round.day_index == day))
    await db.commit()


@pytest.fixture(autouse=True)
def _treasury_ready(monkeypatch: pytest.MonkeyPatch):
    """Активный казначей с ключом: без этого guard и сверка честно молчат."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", "EQ" + "a" * 46)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))


async def test_dispatch_skips_broadcast_when_treasury_underfunded(monkeypatch) -> None:
    """Дефицит баланса: fail-fast без вещания, попытки и статус нетронуты."""
    monkeypatch.setattr(ton_pay, "fetch_account_state", lambda: _state(to_nano(0.01)))

    broadcasted = False

    async def must_not_broadcast(*args, **kwargs):
        nonlocal broadcasted
        broadcasted = True
        return "tx"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", must_not_broadcast)

    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="pending")
        try:
            assert await ton_pay.dispatch_pending_payouts(bot=None) == 0
            assert broadcasted is False
            await db.refresh(payout)
            assert payout.status == "pending"
            assert payout.attempts == 0  # ретрай-бюджет не сожжён
            assert "подкачан" in (payout.last_error or "")
        finally:
            await db.delete(payout)
            await db.commit()


async def test_dispatch_broadcasts_when_balance_is_enough(monkeypatch) -> None:
    """Баланса хватает — предохранитель молчит, обычный цикл вещания."""
    monkeypatch.setattr(ton_pay, "fetch_account_state", lambda: _state(to_nano(5)))

    async def fake_send(dest, amount, comment):
        return "bcast-marker"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)

    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="pending")
        try:
            assert await ton_pay.dispatch_pending_payouts(bot=None) == 1
            await db.refresh(payout)
            assert payout.status == "sent" and payout.tx_hash == "bcast-marker"
        finally:
            await db.delete(payout)
            await db.commit()


async def test_anomaly_flags_deficit_and_drift_and_stays_quiet_when_funded(
    monkeypatch,
) -> None:
    """Сверка: дефицит под очередь и расхождение ловятся; сходимость молчит."""
    from app.ops import check_anomalies

    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as db:
        try:
            db.add(_closed_round(831))
            await db.flush()
            round_row = (
                await db.execute(select(Round).where(Round.day_index == 831))
            ).scalar_one()
            db.add_all(
                [
                    Stake(round_id=round_row.id, player_id=911,
                          amount_nanotons=to_nano(5), tx_hash="g1",
                          status="confirmed", network=network),
                    # Производство: каждый входящий перевод ставки дополнительно
                    # пишет строку Income kind="ton" (_ledger_incoming), которую
                    # сверка и считает входящим — не двойной учёт ставки.
                    Income(kind="ton", amount_nanotons=to_nano(5),
                           unit_ref=f"g1-inc-{os.urandom(4).hex()}",
                           round_id=round_row.id, player_id=911,
                           note="in:stake:ok"),
                    Payout(round_id=round_row.id, player_id=911, kind="prize",
                           amount_nanotons=to_nano(4),
                           dest_address="0:" + os.urandom(16).hex(), network=network),
                ]
            )
            await db.commit()

            # Дефицит: pending 4 G при балансе 1 G.
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(to_nano(1))
            )
            problems = await check_anomalies(None)
            assert any("не хватит на очередь" in p for p in problems)

            # Очередь ушла, баланс почти в ноль → расхождение с ожиданиями БД.
            payout_row = (
                await db.execute(select(Payout).where(Payout.kind == "prize"))
            ).scalar_one()
            payout_row.status = "sent"
            await db.commit()
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(to_nano(0.001))
            )
            problems = await check_anomalies(None)
            assert any("ниже ожиданий БД" in p for p in problems)

            # Сходимость в допуске (газ учтён): сверка молчит.
            expected_left = to_nano(5) - to_nano(4) - to_nano(settings.payout_fee_gram)
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(expected_left)
            )
            problems = await check_anomalies(None)
            assert not any("казначей" in p for p in problems)
        finally:
            await _cleanup(db, 831)


async def test_income_revotes_counted_in_expected_float(monkeypatch) -> None:
    """Revote-переводы (Income kind=ton) входят в ожидаемый остаток казны."""
    from app.ops import check_anomalies

    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as db:
        try:
            db.add(_closed_round(832))
            await db.flush()
            round_row = (
                await db.execute(select(Round).where(Round.day_index == 832))
            ).scalar_one()
            db.add_all(
                [
                    Stake(round_id=round_row.id, player_id=912,
                          amount_nanotons=to_nano(5), tx_hash="g2",
                          status="confirmed", network=network),
                    # Вход ставки тоже пишет Income kind="ton" (_ledger_incoming) —
                    # сверка считает его, иначе ставка была бы посчитана дважды.
                    Income(kind="ton", amount_nanotons=to_nano(5),
                           unit_ref=f"stk-tx-{os.urandom(4).hex()}",
                           round_id=round_row.id, player_id=912, note="in:stake:ok"),
                    Income(kind="ton", amount_nanotons=to_nano(0.3),
                           unit_ref=f"rv-tx-{os.urandom(4).hex()}",
                           round_id=round_row.id, player_id=912, note="rv:832"),
                    Payout(round_id=round_row.id, player_id=912, kind="prize",
                           amount_nanotons=to_nano(4),
                           dest_address="0:" + os.urandom(16).hex(), network=network),
                ]
            )
            await db.commit()

            # Ожидание с учётом ставки (5) и revote (0.3): 5 + 0.3 − 4 (pending) ≈ 1.3 G.
            expected_left = (
                to_nano(5) + to_nano(0.3) - to_nano(4) - to_nano(settings.payout_fee_gram)
            )
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(expected_left)
            )
            problems = await check_anomalies(None)
            assert not any("казначей" in p for p in problems)

            # Выплата ушла: ожидание то же ≈ 1.3 G; баланс сильно ниже → расхождение.
            payout_row = (
                await db.execute(select(Payout).where(Payout.kind == "prize"))
            ).scalar_one()
            payout_row.status = "sent"
            await db.commit()
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(to_nano(0.5))
            )
            problems = await check_anomalies(None)
            assert any("ниже ожиданий БД" in p for p in problems)

            # Сходимость в допуске: сверка молчит.
            monkeypatch.setattr(
                ton_pay, "fetch_account_state", lambda: _state(expected_left)
            )
            problems = await check_anomalies(None)
            assert not any("казначей" in p for p in problems)
        finally:
            await _cleanup(db, 832)

async def test_treasury_diag_shows_watcher_aim_and_cursor(monkeypatch) -> None:
    """/treasury отвечает: куда смотрит watcher, курсор, жив ли цикл."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from app.db import SessionLocal
    from app.models import WatcherState
    from app.ton_pay import treasury_diagnostics
    from app.ton_watch import BEAT_KEY, CURSOR_KEY, SOURCE_KEY

    async with SessionLocal() as db:
        now = _dt.now(UTC)
        db.add(WatcherState(key=CURSOR_KEY, value=str(int((now - _td(seconds=30)).timestamp()))))
        db.add(WatcherState(key=BEAT_KEY, value=(now - _td(seconds=25)).isoformat()))
        db.add(WatcherState(key=SOURCE_KEY, value="tonapi"))
        await db.commit()
        try:
            text = await treasury_diagnostics()
            assert "Watcher:" in text
            assert "смотрит на: EQaaaaaa" in text and "(mainnet)" in text
            assert "с назад" in text and "источник tonapi" in text
        finally:
            for key in (CURSOR_KEY, BEAT_KEY, SOURCE_KEY):
                row = await db.get(WatcherState, key)
                if row is not None:
                    await db.delete(row)
            await db.commit()

    # Курсор в будущем — диагностикa обязана кричать.
    async with SessionLocal() as db:
        future = int((_dt.now(UTC) + _td(minutes=10)).timestamp())
        db.add(WatcherState(key=CURSOR_KEY, value=str(future)))
        db.add(WatcherState(key=BEAT_KEY, value=(_dt.now(UTC) - _td(seconds=5)).isoformat()))
        await db.commit()
        try:
            text = await treasury_diagnostics()
            assert "курсор В БУДУЩЕМ" in text
        finally:
            row = await db.get(WatcherState, CURSOR_KEY)
            if row is not None:
                await db.delete(row)
                await db.commit()


class _AccountResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


async def test_toncenter_account_uses_account_param(monkeypatch) -> None:
    """Toncenter v3 ждёт query-параметр «account», а не «address» — иначе баланс
    казначея в фолбэке не читался (подтверждено по /api/v3/transactions)."""
    treasury = "0:" + "ee" * 32
    monkeypatch.setattr(settings, "treasury_address", treasury)
    monkeypatch.setattr(settings, "ton_enabled", True)

    calls: list[tuple] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def get(self, url, params=None, headers=None, **kwargs):
            calls.append((url, dict(params or {})))
            return _AccountResp(200, {"balance": "1234567890", "status": "active"})


    monkeypatch.setattr(ton_pay, "get_http_client", lambda: _Client())
    data = await ton_pay._toncenter_account(treasury)
    assert data["balance"] == "1234567890"
    _, params = calls[-1]
    assert "account" in params and params["account"] == treasury
    assert "address" not in params
