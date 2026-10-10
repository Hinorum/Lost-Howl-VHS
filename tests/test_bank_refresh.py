"""Авто-актуализация банка дня в постах-статусах без повторного /today.

Пост дня уходит один раз при анонсе — с суммой подтверждённых ставок на тот
момент (status_text читает банк живьём через round_pot). Когда watcher
подтверждает новые ставки, банк в УЖЕ отправленном посте устаревает, и игроку
приходится звать /today. Здесь: точка доставки запоминается (_deliver_day →
status_post), а refresh_day_bank правит те же посты только при росте суммы
(дедуп по last_pot_nanotons) и только для открытого денежного дня.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from conftest import ensure_player
from sqlalchemy import delete, select

from app import broadcast as bc
from app import ton_watch
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Player, Round, RoundStatus, Stake, StatusPost, WinRule
from app.rounds import get_round
from app.stakes import current_network


async def _persist_round(day_index: int, *, status: RoundStatus = RoundStatus.OPEN) -> int:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        round_row = Round(
            day_index=day_index,
            status=status,
            win_rule=WinRule.MAJORITY,
            chapter_title="День банка",
            chapter_text="Текст дня.",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=20),
            tally_ends_at=now + timedelta(hours=21),
        )
        for position in range(3):
            round_row.cards.append(
                Card(
                    position=position,
                    title=f"Путь {position}",
                    consequence="канон",
                    tag="care",
                )
            )
        session.add(round_row)
        await session.commit()
        return round_row.id


async def _open_round_row(round_id: int) -> Round:
    async with SessionLocal() as session:
        return await get_round(session, round_id)


async def _stake(round_id: int, amount_nanotons: int, player_id: int = 700_001) -> None:
    # Родитель до ставки: БД проверяет ссылку player_id, а игрока 700_001
    # никто из тестов этого модуля не создаёт.
    await ensure_player(player_id)
    async with SessionLocal() as session:
        session.add(
            Stake(
                round_id=round_id,
                player_id=player_id,
                amount_nanotons=amount_nanotons,
                tx_hash=f"bx-{round_id}-{amount_nanotons}",
                memo=f"m{round_id}",
                network=current_network(),
                status="confirmed",
            )
        )
        await session.commit()


async def _status_row(round_id: int, chat_id: int, message_id: int, *, pot: int | None = None) -> None:
    async with SessionLocal() as session:
        session.add(
            StatusPost(
                round_id=round_id,
                chat_id=chat_id,
                message_id=message_id,
                is_dm=chat_id > 0,
                last_pot_nanotons=pot,
            )
        )
        await session.commit()


async def _rows_for(round_id: int) -> list[StatusPost]:
    async with SessionLocal() as session:
        return list(
            (await session.execute(select(StatusPost).where(StatusPost.round_id == round_id))).scalars()
        )


async def _delete_round(round_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(StatusPost).where(StatusPost.round_id == round_id))
        await session.execute(delete(Stake).where(Stake.round_id == round_id))
        await session.commit()


# ── запись точки доставки при анонсе ─────────────────────────────────────────


async def test_deliver_day_records_group_and_dm_posts() -> None:
    round_id = await _persist_round(9801)
    round_row = await _open_round_row(round_id)

    class _Bot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=4000 + self.calls)

    bot = _Bot()
    await bc._deliver_day(bot, -100_9801, round_row, finished=None)
    await bc._deliver_day(bot, 555_001, round_row, finished=None, is_dm=True)

    rows = await _rows_for(round_id)
    by_chat = {row.chat_id: row for row in rows}
    group = by_chat[-100_9801]
    assert group.message_id == 4001
    assert group.is_dm is False
    dm = by_chat[555_001]
    assert dm.message_id == 4002
    assert dm.is_dm is True
    assert dm.last_pot_nanotons is None
    await _delete_round(round_id)


async def test_deliver_day_upserts_existing_chat_row() -> None:
    round_id = await _persist_round(9802)
    round_row = await _open_round_row(round_id)

    class _Bot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=4000 + self.calls)

    bot = _Bot()
    await bc._deliver_day(bot, -100_9802, round_row, finished=None)
    await bc._deliver_day(bot, -100_9802, round_row, finished=None)

    rows = await _rows_for(round_id)
    assert len(rows) == 1
    assert rows[0].message_id == 4002
    await _delete_round(round_id)


async def test_deliver_day_new_round_prunes_old_round_posts() -> None:
    old_id = await _persist_round(9803)
    await _status_row(old_id, -100_9803, 111)
    new_id = await _persist_round(9804)
    round_row = await _open_round_row(new_id)

    class _Bot:
        async def send_message(self, chat_id, *args, **kwargs):
            return SimpleNamespace(message_id=222)

    await bc._deliver_day(_Bot(), -100_9804, round_row, finished=None)

    assert await _rows_for(old_id) == []
    assert len(await _rows_for(new_id)) == 1
    await _delete_round(old_id)
    await _delete_round(new_id)


async def test_deliver_day_without_message_id_skips_recording() -> None:
    round_id = await _persist_round(9805)
    round_row = await _open_round_row(round_id)
    # Фейк-бот без message_id (легаси/тестовая заглушка) не должен ронять день.
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace()))
    await bc._deliver_day(bot, -100_9805, round_row, finished=None)
    assert await _rows_for(round_id) == []
    await _delete_round(round_id)


# ── refresh: правки существующих постов с актуальным банком ──────────────────


async def test_refresh_edits_only_chats_with_changed_bank(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9806)
    await _stake(round_id, 1_500_000_000)
    # Первый чат — банк «протух» (показывал ноль), второй — уже актуальный.
    await _status_row(round_id, -100_9806, 5001, pot=0)
    await _status_row(round_id, 555_006, 5002, pot=1_500_000_000)

    edited: list[tuple] = []
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=lambda *args, **kwargs: edited.append((kwargs["chat_id"], kwargs["message_id"]))
        )
    )
    await bc.refresh_day_bank(bot)

    assert edited == [(-100_9806, 5001)]
    rows = {row.chat_id: row for row in await _rows_for(round_id)}
    assert rows[-100_9806].last_pot_nanotons == 1_500_000_000
    assert rows[555_006].last_pot_nanotons == 1_500_000_000
    await _delete_round(round_id)


async def test_refresh_renders_bank_line_in_edit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9807)
    await _stake(round_id, 2_250_000_000)
    await _status_row(round_id, -100_9807, 6001, pot=0)

    edited_text: list[str] = []
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=lambda text, **kwargs: edited_text.append(text))
    )
    await bc.refresh_day_bank(bot)
    assert edited_text and "Банк дня: 2.25 Gram" in edited_text[0]
    await _delete_round(round_id)


async def test_refresh_no_edits_when_bank_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9808)
    await _stake(round_id, 900_000_000)
    await _status_row(round_id, -100_9808, 7001, pot=900_000_000)

    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await bc.refresh_day_bank(bot)
    bot.edit_message_text.assert_not_awaited()
    await _delete_round(round_id)


async def test_refresh_sets_pot_on_not_modified(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9809)
    await _stake(round_id, 300_000_000)
    await _status_row(round_id, -100_9809, 8001, pot=0)  # 0 != 300M

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=TelegramBadRequest(None, "Bad Request: message is not modified"))
    )
    await bc.refresh_day_bank(bot)
    rows = await _rows_for(round_id)
    assert len(rows) == 1
    assert rows[0].last_pot_nanotons == 300_000_000
    await _delete_round(round_id)


async def test_refresh_deletes_dead_chat_on_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9810)
    await _stake(round_id, 100_000_000)
    await _status_row(round_id, -100_9810, 9001, pot=0)

    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=TelegramForbiddenError(None, "bot kicked")))
    await bc.refresh_day_bank(bot)
    assert await _rows_for(round_id) == []
    await _delete_round(round_id)


async def test_refresh_deletes_dead_chat_on_bad_request(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9811)
    await _stake(round_id, 100_000_000)
    await _status_row(round_id, -100_9811, 9002, pot=0)

    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=TelegramBadRequest(None, "chat not found")))
    await bc.refresh_day_bank(bot)
    assert await _rows_for(round_id) == []
    await _delete_round(round_id)


async def test_refresh_skips_when_bot_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9812)
    await _stake(round_id, 42_000_000)
    await _status_row(round_id, -100_9812, 9003, pot=0)
    await bc.refresh_day_bank(None)  # headless-режим: никого не редактируем
    assert len(await _rows_for(round_id)) == 1
    await _delete_round(round_id)


async def test_refresh_skips_money_off_day(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        round_row = Round(
            day_index=9813,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="День без денег",
            chapter_text="x",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=20),
            tally_ends_at=now + timedelta(hours=21),
            money_mode=False,
        )
        session.add(round_row)
        await session.commit()
        round_id = round_row.id
    await _status_row(round_id, -100_9813, 9004, pot=0)

    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await bc.refresh_day_bank(bot)
    bot.edit_message_text.assert_not_awaited()
    await _delete_round(round_id)


async def test_refresh_skips_tallying_round(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_id = await _persist_round(9814, status=RoundStatus.TALLYING)
    await _status_row(round_id, -100_9814, 9005, pot=0)

    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await bc.refresh_day_bank(bot)
    bot.edit_message_text.assert_not_awaited()
    await _delete_round(round_id)


# ── watcher: ставки подтвердились → банк в постах обновился ──────────────────


async def test_watch_once_refreshes_banks_after_aged_confirms(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)
    round_id = await _persist_round(9815)
    await _status_row(round_id, -100_9815, 9501, pot=0)
    async with SessionLocal() as session:
        session.add(Player(id=555_015, username="aged", first_name="A"))
        session.add(
            Stake(
                round_id=round_id,
                player_id=555_015,
                amount_nanotons=3_000_000_000,
                tx_hash="aged-bank-1",
                memo="m9815",
                network=current_network(),
                status="pending",
                created_at=datetime.now(UTC) - timedelta(seconds=settings.stake_confirm_seconds + 60),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        ton_watch, "_collect_transfers", AsyncMock(return_value=([], True, "none", None))
    )
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_message=AsyncMock(),
    )
    try:
        await ton_watch.watch_once(bot=bot)
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Stake).where(Stake.round_id == round_id))
            await session.execute(delete(Player).where(Player.id == 555_015))
            await session.commit()

    rows = await _rows_for(round_id)
    assert len(rows) == 1
    assert rows[0].last_pot_nanotons == 3_000_000_000
    bot.edit_message_text.assert_awaited_once()
    await _delete_round(round_id)
