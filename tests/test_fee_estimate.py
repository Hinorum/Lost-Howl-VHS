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


async def _seed_outgoing_fee(session, fee_nanotons: int, count: int = 5) -> None:
    """Несколько исходящих переводов с фактической комиссией."""
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
                value_nanotons=1_000_000,
                fee_nanotons=fee_nanotons,
                balance_delta_nanotons=-1_000_000,
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
