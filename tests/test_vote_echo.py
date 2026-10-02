"""Эхо выбора дня в личку: голос по кнопке — подтверждение в ЛС игрока.

Раньше личное сообщение «Твой выбор этого дня» уходило только из приватного
чата; кнопка на групповом посте отвечала одним мимолётным алертом. Здесь
фиксируем, что подтверждение уходит в личку при любом месте голоса, и что
недоступный диалог (403/выключенные DM) не валит хендлер.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.handlers import on_vote
from app.models import Player, Round, RoundStatus, Vote, WinRule


def _open_round(day_index: int) -> Round:
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


def _callback(*, pid: int, chat_type: str, bot=None) -> SimpleNamespace:
    return SimpleNamespace(
        data="",
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type)),
        from_user=SimpleNamespace(id=pid, username="u", first_name="Т"),
        bot=bot,
        answer=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _dm_on(monkeypatch):
    monkeypatch.setattr(settings, "player_dm", True)


async def _seed_round(day_index: int) -> tuple[int, int]:
    """Игрок + открытый день в глобальной БД; возвращает (rid, pid)."""
    pid = 810_000 + day_index * 7
    async with SessionLocal() as db:
        db.add(Player(id=pid, username="u", first_name="Т"))
        round_row = _open_round(day_index=day_index)
        db.add(round_row)
        await db.commit()
    return round_row.id, pid


async def test_group_vote_echoes_dm(monkeypatch) -> None:
    """Нажатие «Сцена…» на групповом посте приносит сообщение в личку."""
    rid, pid = await _seed_round(8101)
    send_dm = AsyncMock()
    callback = _callback(pid=pid, chat_type="supergroup", bot=Mock(send_message=send_dm))
    callback.data = f"vote:{rid}:1"
    try:
        await on_vote(callback)
        send_dm.assert_awaited_once()
        args, kwargs = send_dm.await_args
        assert args[0] == pid
        assert "Твой выбор этого дня" in args[1]
        assert "II" in args[1]
        alert = callback.answer.await_args
        assert alert is not None and alert.args[0]
    finally:
        await _wipe(rid, pid)


async def test_private_vote_echoes_dm(monkeypatch) -> None:
    """Эхо в личке идёт тем же отдельным сообщением (из диалога с ботом)."""
    rid, pid = await _seed_round(8102)
    send_dm = AsyncMock()
    callback = _callback(pid=pid, chat_type="private", bot=Mock(send_message=send_dm))
    callback.data = f"vote:{rid}:0"
    try:
        await on_vote(callback)
        send_dm.assert_awaited_once()
        assert send_dm.await_args.args[0] == pid
    finally:
        await _wipe(rid, pid)


async def test_group_vote_dm_disabled_still_alerts(monkeypatch) -> None:
    """Выключенные DM не ломают выбор: алерт по кнопке всё равно приходит."""
    monkeypatch.setattr(settings, "player_dm", False)
    rid, pid = await _seed_round(8103)
    send_dm = AsyncMock()
    callback = _callback(pid=pid, chat_type="supergroup", bot=Mock(send_message=send_dm))
    callback.data = f"vote:{rid}:2"
    try:
        await on_vote(callback)
        send_dm.assert_not_awaited()
        callback.answer.assert_awaited_once()
    finally:
        await _wipe(rid, pid)


async def test_group_vote_dm_blocked_does_not_crash(monkeypatch) -> None:
    """Игрок не открывал диалог (403): тихий скип, голос и алерт в порядке."""
    from aiogram.exceptions import TelegramForbiddenError

    rid, pid = await _seed_round(8104)
    send_dm = AsyncMock(side_effect=TelegramForbiddenError(None, "blocked"))
    callback = _callback(pid=pid, chat_type="supergroup", bot=Mock(send_message=send_dm))
    callback.data = f"vote:{rid}:1"
    try:
        await on_vote(callback)
        callback.answer.assert_awaited_once()
        async with SessionLocal() as db:
            vote = await db.execute(
                select(Vote).where(Vote.round_id == rid, Vote.player_id == pid)
            )
            assert vote.scalar_one().card_position == 1
    finally:
        await _wipe(rid, pid)


async def _wipe(rid: int, pid: int) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Vote).where(Vote.round_id == rid))
        await db.execute(delete(Round).where(Round.id == rid))
        await db.execute(delete(Player).where(Player.id == pid))
        await db.commit()