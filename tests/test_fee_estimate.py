"""Комиссия сети: рабочая оценка и тревога о подорожании.

PAYOUT_FEE_GRAM — константа, записанная при настройке. Пока сеть стоит
столько же, поведение прежнее. Если комиссия выросла, казна платит разницу
из своего остатка: из пула вычиталась оценка, а сеть брала больше. Допуск
сверки при этом растёт вместе с числом выплат, то есть расхождение может
копиться днями и не попасть в сверку.

Зеркало казны знает фактическую комиссию каждого исходящего перевода
(treasury_moves.fee_nanotons — total_fees из цепочки), поэтому рабочая
оценка сверяется с реальностью, а оператор получает тревогу.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import ops
from app.config import settings
from app.core.fees import (
    FEE_ALERT_RATIO,
    configured_fee_nano,
    fee_estimate_nano,
    realized_fee_nano,
)
from app.db import SessionLocal
from app.models import TreasuryMove
from app.ton_utils import to_nano


async def _seed_outgoing_fee(
    session, fee_nanotons: int, count: int = 5, out_msgs_count: int = 1
) -> None:
    """Несколько исходящих переводов с фактической стоимостью сети.

    Геометрия — как в зеркале: за исходящий перевод баланс казны упал на
    (сумма перевода + стоимость сети), поэтому стоимость выводится из дельты
    (−balance_delta − value). Именно её и меряет app/core/fees.py.

    out_msgs_count > 1 — пачка: стоимость транзакции общая на все сообщения.
    """
    value = 1_000_000
    for index in range(count):
        session.add(
            TreasuryMove(
                tx_hash=f"fee-seed-{index}",
                network="mainnet",
                address="0:" + "ab" * 32,
                utime=1_700_000_000 + index,
                lt=1_700_000_000 + index,
                direction="out",
                kind="payout:prize",
                value_nanotons=value,
                out_msgs_count=out_msgs_count,
                fee_nanotons=fee_nanotons,
                balance_delta_nanotons=-(value + fee_nanotons),
                counterparty="0:" + "cd" * 32,
                comment="",
                success=True,
            )
        )
    await session.commit()


async def _wipe_moves() -> None:
    async with SessionLocal() as session:
        await session.execute(
            TreasuryMove.__table__.delete().where(
                TreasuryMove.tx_hash.like("fee-seed-%")
            )
        )
        await session.commit()


async def test_estimate_falls_back_to_configured(monkeypatch) -> None:
    """Пока зеркало не набрало истории, оценка — настроенная."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    async with SessionLocal() as session:
        assert await realized_fee_nano(session) is None
        assert await fee_estimate_nano(session) == configured_fee_nano()


async def test_estimate_follows_realized_fee(monkeypatch) -> None:
    """Сеть подорожала — рабочая оценка идёт по фактической, а не по константе."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    realized = to_nano(0.002)
    try:
        async with SessionLocal() as session:
            await _seed_outgoing_fee(session, realized)
            assert await realized_fee_nano(session) == realized
            assert await fee_estimate_nano(session) == realized
    finally:
        await _wipe_moves()


async def test_estimate_never_drops_below_configured(monkeypatch) -> None:
    """Сеть подешевела — оценка не ниже настроенной: правила игры не меняем."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    realized = to_nano(0.0001)
    try:
        async with SessionLocal() as session:
            await _seed_outgoing_fee(session, realized)
            assert await fee_estimate_nano(session) == configured_fee_nano()
    finally:
        await _wipe_moves()


async def test_fee_growth_raises_alert(monkeypatch) -> None:
    """Комиссия выросла заметно — хранитель узнаёт и обновит оценку."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "ton_enabled", True)
    bot = SimpleNamespace(send_message=AsyncMock())
    realized = configured_fee_nano() * 2
    try:
        async with SessionLocal() as session:
            await _seed_outgoing_fee(session, realized)

        problems = await ops.check_anomalies(bot=bot)
        assert any("комиссия сети выше оценки" in problem for problem in problems), problems
        assert bot.send_message.await_count >= 1
    finally:
        await _wipe_moves()


async def test_small_fee_growth_stays_silent(monkeypatch) -> None:
    """Обычный разброс сети не тревожит: порог — заметный рост."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "ton_enabled", True)
    bot = SimpleNamespace(send_message=AsyncMock())
    realized = int(configured_fee_nano() * (1 + (FEE_ALERT_RATIO - 1) / 2))
    try:
        async with SessionLocal() as session:
            await _seed_outgoing_fee(session, realized)

        problems = await ops.check_anomalies(bot=bot)
        assert not any("комиссия сети выше оценки" in problem for problem in problems), problems
    finally:
        await _wipe_moves()


async def test_realized_cost_includes_forward_fee(monkeypatch) -> None:
    """Стоимость перевода — полная: и комиссия получателя, и forward fee.

    Сеть берёт с отправителя две части, и в total_fees (fee_nanotons зеркала)
    forward fee не входит. Если мерять только им, стоимость занижена
    примерно на 11% (замерено по живым переводам казны 2026-10-21), и тревога
    «сеть подорожала» срабатывает поздно, а её значение предлагает хранителю
    заниженную константу.

    Здесь forward fee сидит в дельте баланса: баланс казны упал на сумму
    перевода ПЛЮС обе части комиссии.
    """
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    total_fees = 478_820  # комиссия получателя (total_fees в цепочке)
    forward_fee = 44_446  # forward fee сообщения — платит отправитель сверху
    value = 499_300_000
    try:
        async with SessionLocal() as session:
            session.add(
                TreasuryMove(
                    tx_hash="fee-seed-forward",
                    network="mainnet",
                    address="0:" + "ab" * 32,
                    utime=1_700_000_000,
                    lt=1_700_000_000,
                    direction="out",
                    kind="payout:prize",
                    value_nanotons=value,
                    fee_nanotons=total_fees,
                    balance_delta_nanotons=-(value + total_fees + forward_fee),
                    counterparty="0:" + "cd" * 32,
                    comment="",
                    success=True,
                )
            )
            await session.commit()
            realized = await realized_fee_nano(session)
            assert realized == total_fees + forward_fee
    finally:
        await _wipe_moves()


async def test_realized_cost_of_batch_is_per_message(monkeypatch) -> None:
    """Пачка v5r1: стоимость транзакции делится на все её сообщения.

    Одна строка зеркала — одна транзакция, а в пачке до 50 выплат. Если
    делить не на out_msgs_count, «стоимость перевода» становится стоимостью
    всей пачки, и app.stakes вычитает газ за 50 переводов 50 раз.
    """
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    per_message = to_nano(0.002)
    messages = 50
    value = 100_000_000 * messages
    total_cost = per_message * messages
    try:
        async with SessionLocal() as session:
            session.add(
                TreasuryMove(
                    tx_hash="fee-seed-batch",
                    network="mainnet",
                    address="0:" + "ab" * 32,
                    utime=1_700_000_000,
                    lt=1_700_000_000,
                    direction="out",
                    kind="payout:prize",
                    value_nanotons=value,
                    out_msgs_count=messages,
                    fee_nanotons=total_cost,
                    balance_delta_nanotons=-(value + total_cost),
                    counterparty="0:" + "cd" * 32,
                    comment="",
                    success=True,
                )
            )
            await session.commit()
            assert await realized_fee_nano(session) == per_message
            assert await fee_estimate_nano(session) == per_message
    finally:
        await _wipe_moves()


async def test_batch_does_not_raise_false_fee_alert(monkeypatch) -> None:
    """Пачка не выглядит как подорожание сети: тревога меряется за перевод."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "ton_enabled", True)
    bot = SimpleNamespace(send_message=AsyncMock())
    per_message = int(configured_fee_nano() * (1 + (FEE_ALERT_RATIO - 1) / 2))
    messages = 20
    total_cost = per_message * messages
    value = 10_000_000 * messages
    try:
        async with SessionLocal() as session:
            session.add(
                TreasuryMove(
                    tx_hash="fee-seed-batch-alert",
                    network="mainnet",
                    address="0:" + "ab" * 32,
                    utime=1_700_000_000,
                    lt=1_700_000_000,
                    direction="out",
                    kind="payout:prize",
                    value_nanotons=value,
                    out_msgs_count=messages,
                    fee_nanotons=total_cost,
                    balance_delta_nanotons=-(value + total_cost),
                    counterparty="0:" + "cd" * 32,
                    comment="",
                    success=True,
                )
            )
            await session.commit()

        problems = await ops.check_anomalies(bot=bot)
        assert not any("комиссия сети выше оценки" in problem for problem in problems), problems
    finally:
        await _wipe_moves()


async def test_realized_cost_ignores_failed_transfer(monkeypatch) -> None:
    """Неудавшийся перевод (баланс не изменился) в выборку не попадает."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    value = 1_000_000
    try:
        async with SessionLocal() as session:
            session.add(
                TreasuryMove(
                    tx_hash="fee-seed-failed",
                    network="mainnet",
                    address="0:" + "ab" * 32,
                    utime=1_700_000_000,
                    lt=1_700_000_000,
                    direction="out",
                    kind="payout:prize",
                    value_nanotons=value,
                    fee_nanotons=0,
                    balance_delta_nanotons=0,
                    counterparty="0:" + "cd" * 32,
                    comment="",
                    success=False,
                )
            )
            await session.commit()
            assert await realized_fee_nano(session) is None
    finally:
        await _wipe_moves()
