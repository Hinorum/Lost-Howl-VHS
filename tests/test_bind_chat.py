"""Ручная привязка чата: /bind закрывает дыру, которую не закрывает my_chat_member.

Единственный автоматический путь регистрации — my_chat_member, и он срабатывает
только на СМЕНУ состава: бота, добавленного в канал, пока бот стоял (Telegram
хранит апдейты сутки), система не видит никогда. Дни при этом уходят в пустоту,
а claim дня уже стоит, так что восстановитель их не досылает. /bind — ручной
путь для хранителя; здесь он обязан привязывать чат, не пускать постороннего и
работать там, где команда приходит не сообщением, а channel_post'ом (канал).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal
from app.handlers.admin import cmd_bind
from app.handlers.common import router
from app.models import Chat

KEEPER_ID = 4242
CHAT_ID = -100_777_666


def _message(
    text: str = "/bind",
    *,
    chat_type: str = "supergroup",
    chat_id: int = CHAT_ID,
    user_id: int | None = KEEPER_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id, title="Канал проверки", username=None),
        from_user=None if user_id is None else SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


async def _row(chat_id: int = CHAT_ID) -> Chat | None:
    async with SessionLocal() as db:
        return await db.get(Chat, chat_id)


async def _forget(chat_id: int = CHAT_ID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Chat).where(Chat.id == chat_id))
        await db.commit()


async def test_bind_rejects_outsider(monkeypatch) -> None:
    """Гейт хранителя стоит до любой записи в БД — как у остальных админ-команд."""
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    await _forget()
    try:
        msg = _message(user_id=1)
        await cmd_bind(msg)
        assert "только для хранителя" in msg.answer.call_args.args[0].lower()
        assert msg.answer.await_count == 1
        assert await _row() is None
    finally:
        await _forget()


async def test_bind_rejects_anonymous_author(monkeypatch) -> None:
    """Анонимный админ (from_user None) не должен привязывать чаты."""
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    await _forget()
    try:
        msg = _message(user_id=None)
        await cmd_bind(msg)
        assert "только для хранителя" in msg.answer.call_args.args[0].lower()
        assert await _row() is None
    finally:
        await _forget()


async def test_bind_in_private_says_where_to_run(monkeypatch) -> None:
    """В личке привязывать нечего: хранитель должен получить подсказку,
    а не пустую команду без объяснения."""
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    private_id = KEEPER_ID
    msg = _message(chat_type="private", chat_id=private_id)
    try:
        await cmd_bind(msg)
        reply = msg.answer.call_args.args[0]
        assert "/bind" in reply
        assert "в группе или канале" in reply
        assert await _row(private_id) is None
    finally:
        await _forget(private_id)


async def test_bind_registers_group_chat(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    await _forget()
    try:
        msg = _message(chat_type="supergroup")
        await cmd_bind(msg)
        assert "привязан" in msg.answer.call_args.args[0].lower()
        row = await _row()
        assert row is not None
        assert row.active is True
        assert row.type == "supergroup"
        assert row.title == "Канал проверки"
    finally:
        await _forget()


async def test_bind_registers_channel(monkeypatch) -> None:
    """Канал — основной пострадавший: именно он не получал новостей дня."""
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    await _forget()
    try:
        msg = _message(chat_type="channel")
        await cmd_bind(msg)
        row = await _row()
        assert row is not None and row.active is True
        assert row.type == "channel"
    finally:
        await _forget()


async def test_bind_reactivates_deactivated_chat(monkeypatch) -> None:
    """Чат уже известен, но помечен неактивным (бота выгнали / права отобрали)
    — /bind обязан вернуть его в рассылку, а не оставить старую пометку."""
    monkeypatch.setattr(settings, "admin_ids", str(KEEPER_ID))
    await _forget()
    async with SessionLocal() as db:
        db.add(Chat(id=CHAT_ID, type="channel", active=False, title="старое имя"))
        await db.commit()
    try:
        msg = _message(chat_type="channel")
        await cmd_bind(msg)
        row = await _row()
        assert row is not None and row.active is True
        assert row.title == "Канал проверки"
    finally:
        await _forget()


def test_bind_is_registered_for_messages_and_channel_posts() -> None:
    """Два observer'а: группа отвечает на message, канал — на channel_post.

    В канал Telegram приходит channel_post, а не message: без второй
    регистрации команда в канале молчала бы, и привязать его было бы
    нечем — ровно та дыра, которую закрывает /bind.
    """
    messages = [handler.callback.__name__ for handler in router.message.handlers]
    channels = [handler.callback.__name__ for handler in router.channel_post.handlers]
    assert "cmd_bind" in messages
    assert "cmd_bind" in channels
