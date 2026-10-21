"""Рассылки: запрет после флуд-ретрая больше не теряется.

Копии кода рассылки разъехались, и в двух из них TelegramForbiddenError,
прилетевший ПОСЛЕ флуд-ретрая, тонул в общем except: получатель, который
заблокировал бота между первой попыткой и ретраем, оставался в базе навсегда
— его пробовали доставить в каждой рассылке. В личных итогах дня запрета не
было вовсе: там заблокировавший игрок оставался подписчиком и получал
попытки каждый день.

Теперь у всех рассылок один общий помощник: запрет обрабатывается на
обоих путях (до ретрая и после), а получатель отключается.
"""

from __future__ import annotations

import pytest

from app import broadcast as bc
from app.config import settings
from app.db import SessionLocal
from app.models import Chat, Player


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(bc.asyncio, "sleep", instant)


def _bot_that_forbids_on_retry(targets: set[int]):
    """Бот: первая попытка — флуд-контроль, вторая — запрет."""

    class _Bot:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.calls.append(chat_id)
            if chat_id in targets and self.calls.count(chat_id) > 1:
                raise bc.TelegramForbiddenError(
                    "sendMessage", "bot was blocked by the user"
                )
            raise bc.TelegramRetryAfter(
                retry_after=1, method="sendMessage", message="flood"
            )

    return _Bot()


async def test_forbidden_after_retry_deactivates_chat(monkeypatch) -> None:
    """Запрет, прилетевший после флуд-ретрая, отключает чат.

    Раньше этот случай тонул в общем except: чат оставался активным, и
    каждая последующая рассылка снова в него стучалась.
    """
    monkeypatch.setattr(settings, "player_dm", False)
    chat_id = 777_701
    async with SessionLocal() as db:
        db.add(Chat(id=chat_id, type="group", active=True))
        await db.commit()
    try:
        bot = _bot_that_forbids_on_retry({chat_id})
        delivered = await bc.whisper_to_chats(bot, "шёпот")
        assert delivered == 0
        async with SessionLocal() as db:
            assert (await db.get(Chat, chat_id)).active is False
    finally:
        async with SessionLocal() as db:
            await db.execute(Chat.__table__.delete().where(Chat.id == chat_id))
            await db.commit()


async def test_forbidden_after_retry_deactivates_player(monkeypatch) -> None:
    """То же для личных сообщений: заблокировавший игрок отключается."""
    monkeypatch.setattr(settings, "player_dm", True)
    player_id = 777_702
    async with SessionLocal() as db:
        db.add(Player(id=player_id, username="blocked", dm_subscribed=True))
        await db.commit()
    try:
        bot = _bot_that_forbids_on_retry({player_id})
        delivered = await bc._dm_send_all(
            bot,
            lambda pid: bot.send_message(pid, "личное"),
            "проверка запрета",
        )
        assert delivered == 0
        async with SessionLocal() as db:
            assert (await db.get(Player, player_id)).dm_subscribed is False
    finally:
        async with SessionLocal() as db:
            await db.execute(Player.__table__.delete().where(Player.id == player_id))
            await db.commit()


async def test_fanout_counts_reasons() -> None:
    """Причины отказов собираются в счётчик — их видно в метке доставки."""
    async def deliver(target: int) -> None:
        raise RuntimeError("сеть легла")

    delivered, reasons = await bc._fanout([1, 2], deliver, label="проверка")
    assert delivered == 0
    assert sum(reasons.values()) == 2
    assert all("RuntimeError" in reason for reason in reasons)


async def test_fanout_calls_on_forbidden_hook() -> None:
    """Запрет вызывает хук отключения — ровно один раз на получателя."""
    blocked: list[int] = []

    async def deliver(target: int) -> None:
        raise bc.TelegramForbiddenError("sendMessage", "kicked")

    async def on_forbidden(target: int) -> None:
        blocked.append(target)

    delivered, reasons = await bc._fanout([5], deliver, on_forbidden=on_forbidden)
    assert delivered == 0
    assert blocked == [5]
    assert sum(reasons.values()) == 1


async def test_fanout_delivers_to_all() -> None:
    """Успешная рассылка: все получатели получили."""
    got: list[int] = []

    async def deliver(target: int) -> None:
        got.append(target)

    delivered, reasons = await bc._fanout([1, 2, 3], deliver)
    assert delivered == 3
    assert sorted(got) == [1, 2, 3]
    assert not reasons


async def test_fanout_survives_broken_hook() -> None:
    """Хук, который сам падает, не должен ронять рассылку."""
    async def deliver(target: int) -> None:
        raise bc.TelegramForbiddenError("sendMessage", "kicked")

    async def on_forbidden(target: int) -> None:
        raise RuntimeError("хук сломан")

    delivered, _reasons = await bc._fanout([1], deliver, on_forbidden=on_forbidden)
    assert delivered == 0
