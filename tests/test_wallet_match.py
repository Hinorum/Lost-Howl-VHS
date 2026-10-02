"""Регрессия: watcher сопоставляет отправителя транзакции с привязкой кошелька.

TonAPI отдаёт source в raw-hex, а игроки присылают user-friendly адрес.
Фикс: при привязке храним канонический raw (normalize_address), watcher
нормализует источник; старые UQ/EQ-записи разово мигрирует _migrate_wallet_formats.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Player, Round, RoundStatus, Stake, WatcherState, WinRule
from app.ton_utils import is_valid_ton_address, normalize_address, to_nano
from app.ton_watch import (
    WALLET_NORM_KEY,
    Transfer,
    _migrate_wallet_formats,
    confirm_aged_pending,
    process_transfer,
)

# Адреса уникальны в рамках прогона: players.wallet_address имеет UNIQUE,
# а глобальная тестовая БД общая для всех модулей. Оба — валидные
# user-friendly (CRC16 верный), сгенерированы friendly_address().
NEW_STYLE = "UQBVZneImaq7zN3u_wARIjNEVWZ3iJmqu8zd7v8AESIzRJBb"
OLD_STYLE = "UQCrq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq5jh"
RAW = normalize_address(NEW_STYLE)


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


async def test_new_style_binding_matches_raw_source(ton_on) -> None:
    assert is_valid_ton_address(NEW_STYLE)
    await _open_round(901)
    async with SessionLocal() as session:
        # Как теперь делает _bind_wallet: в БД лежит нормализованный raw.
        session.add(Player(id=910_001, username="bind", first_name="B", wallet_address=RAW))
        await session.commit()

    status = await process_transfer(
        Transfer(tx_hash="raw-match-1", source=RAW, value_nanotons=500_000_000, comment="", utime=int(datetime.now(UTC).timestamp()))
    )
    assert status == "ok"


async def test_old_friendly_row_would_miss_and_migration_fixes_it(ton_on) -> None:
    assert is_valid_ton_address(OLD_STYLE)
    old_raw = normalize_address(OLD_STYLE)
    assert old_raw != RAW
    assert old_raw != normalize_address(NEW_STYLE)
    async with SessionLocal() as session:
        # Старая запись: дружественная строка как есть (до фикса и миграции).
        session.add(Player(id=910_002, username="old", first_name="O", wallet_address=OLD_STYLE))
        await session.commit()

    # До миграции перевод не находит хозяина и уходит в возвраты — так выглядел баг.
    await _open_round(902)
    status_before = await process_transfer(
        Transfer(tx_hash="mig-before-1", source=old_raw, value_nanotons=300_000_000, comment="", utime=int(datetime.now(UTC).timestamp()) - 60)
    )
    assert status_before == "refund_queued"

    # Миграция переписывает запись в raw и ставит флаг. Флаг мог остаться
    # от предыдущих тестов в общей БД — имитируем свежий деплой сбросом.
    async with SessionLocal() as session:
        await session.execute(delete(WatcherState).where(WatcherState.key == WALLET_NORM_KEY))
        await session.commit()
        await _migrate_wallet_formats(session)
        player = await session.get(Player, 910_002)
        assert player.wallet_address == old_raw
        assert await session.get(WatcherState, WALLET_NORM_KEY) is not None

    # Повторный запуск — идемпотентный. Заодно гигиена общей БД: возврат
    # из «до-миграционного» промаха не должен остаться в очереди выплат.
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        await session.execute(delete(Payout).where(Payout.tx_hash == "mig-before-1"))
        await session.commit()
        stray = await session.execute(
            delete(Payout).where(Payout.tx_hash == "mig-before-1")
        )
        assert stray.rowcount == 0

class _RecorderBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


def _raw(seed: int) -> str:
    return f"0:{seed:064x}"


async def test_confirmed_stake_notifies_player(ton_on) -> None:
    await _open_round(903)
    raw = _raw(0xA1)
    async with SessionLocal() as session:
        session.add(Player(id=910_003, username="dm", first_name="D", wallet_address=raw))
        await session.commit()

    bot = _RecorderBot()
    status = await process_transfer(
        # Старше stake_confirm_seconds (в фикстуре он 10 000 с) — ставка
        # подтвердится сразу с «принята», но свежее лимита авто-возвратов.
        Transfer(tx_hash="dm-ok-1", source=raw, value_nanotons=500_000_000, comment="", utime=int(datetime.now(UTC).timestamp()) - 11_000),
        bot=bot,
    )
    assert status == "ok"
    assert [(m[0], "принята" in m[1]) for m in bot.messages] == [(910_003, True)]
    async with SessionLocal() as session:
        stake = (
            await session.execute(select(Stake).where(Stake.tx_hash == "dm-ok-1"))
        ).scalar_one()
        assert stake.status == "confirmed"


async def test_fresh_stake_waits_then_aged_pass_confirms_and_notifies(ton_on) -> None:
    await _open_round(904)
    raw = _raw(0xB2)
    async with SessionLocal() as session:
        session.add(Player(id=910_004, username="aged", first_name="A", wallet_address=raw))
        await session.commit()

    bot = _RecorderBot()
    fresh_utime = int(datetime.now(UTC).timestamp())
    status = await process_transfer(
        Transfer(tx_hash="dm-aged-1", source=raw, value_nanotons=750_000_000, comment="", utime=fresh_utime),
        bot=bot,
    )
    assert status == "ok"
    assert bot.messages == []  # слишком свежий — молча ждёт

    async with SessionLocal() as session:
        stake = (await session.execute(select(Stake).where(Stake.tx_hash == "dm-aged-1"))).scalar_one()
        stake.created_at = datetime.now(UTC) - timedelta(seconds=settings.stake_confirm_seconds + 60)
        await session.commit()

    assert await confirm_aged_pending(bot) == 1
    assert [(m[0], "принята" in m[1]) for m in bot.messages] == [(910_004, True)]
    async with SessionLocal() as session:
        stake = (await session.execute(select(Stake).where(Stake.tx_hash == "dm-aged-1"))).scalar_one()
        assert stake.status == "confirmed"


async def test_rejected_stake_tells_the_reason(ton_on) -> None:
    await _open_round(905)
    raw = _raw(0xC3)
    async with SessionLocal() as session:
        session.add(Player(id=910_005, username="small", first_name="S", wallet_address=raw))
        await session.commit()

    bot = _RecorderBot()
    status = await process_transfer(
        Transfer(tx_hash="dm-small-1", source=raw, value_nanotons=1, comment="", utime=int(datetime.now(UTC).timestamp())),
        bot=bot,
    )
    assert status == "too_small"
    assert len(bot.messages) == 1
    assert "не принята" in bot.messages[0][1]


async def test_dust_stake_does_not_block_real_stake(ton_on) -> None:
    """Регрессия: отклонённая микровставка больше не занимает слот дня —
    валидная ставка записывается на её место, пыль возвращается сразу."""
    await _open_round(933)
    raw = _raw(0x5F1D)
    async with SessionLocal() as session:
        session.add(Player(id=910_933, username="dusty", first_name="D", wallet_address=raw))
        await session.commit()

    now = int(datetime.now(UTC).timestamp())
    bot = _RecorderBot()
    assert await process_transfer(
        Transfer(tx_hash="dm-dust-1", source=raw, value_nanotons=to_nano(0.02), comment="", utime=now),
        bot=bot,
    ) == "too_small"
    assert await process_transfer(
        Transfer(tx_hash="dm-dust-2", source=raw, value_nanotons=to_nano(0.5), comment="", utime=now),
        bot=bot,
    ) == "ok"
    async with SessionLocal() as session:
        stake = (await session.execute(select(Stake).where(Stake.tx_hash == "dm-dust-2"))).scalar_one()
        assert stake.status == "pending"  # свежий — ждёт подтверждения
        refund = (
            await session.execute(
                select(Payout).where(Payout.kind == "refund", Payout.player_id == 910_933)
            )
        ).scalar_one_or_none()
        assert refund is not None
        from app.stakes import refund_net_amount

        assert refund.amount_nanotons == refund_net_amount(to_nano(0.02))

