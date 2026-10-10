"""Авто-привязка чата: ручной /bind снят, регистрирует само присутствие.

Указ владельца: /bind — лишний костыль (команду нужно знать, написать в
нужном чате, а в ЛС она лишь отвечает «привязывай в нужном чате»). Чат
попадает в рассылку теперь только автоматическими путями:

- my_chat_member — смена состава (бота добавили, выгнали, повысили);
- auto_bind_chat — первая же команда или сообщение в чате: закрывает дыру
  пропущенного события добавления (Telegram хранит апдейты сутки), из-за
  которой день уходил в пустоту, а claim дня уже стоял;
- личка — через /start (dm_subscribed), привязки чата там не требуется.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import delete

from app.db import SessionLocal
from app.handlers import auto_bind_chat
from app.handlers.admin import track_chat
from app.handlers.common import router
from app.models import Chat

CHAT_ID = -100_777_666


def _message(*, chat_type: str = "supergroup", chat_id: int = CHAT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id, title="Канал проверки", username=None),
        answer=AsyncMock(),
    )


async def _row(chat_id: int = CHAT_ID) -> Chat | None:
    async with SessionLocal() as db:
        return await db.get(Chat, chat_id)


async def _forget(chat_id: int = CHAT_ID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Chat).where(Chat.id == chat_id))
        await db.commit()


async def test_traffic_binds_group_chat() -> None:
    """Любое сообщение из группы регистрирует её для рассылки."""
    await _forget()
    try:
        await auto_bind_chat(_message(chat_type="supergroup"))
        row = await _row()
        assert row is not None
        assert row.active is True
        assert row.type == "supergroup"
        assert row.title == "Канал проверки"
    finally:
        await _forget()


async def test_traffic_binds_channel() -> None:
    """Канал — основной пострадавший: именно он не получал новостей дня."""
    await _forget()
    try:
        await auto_bind_chat(_message(chat_type="channel"))
        row = await _row()
        assert row is not None and row.active is True
        assert row.type == "channel"
    finally:
        await _forget()


async def test_traffic_is_silent() -> None:
    """Авто-привязка не отвечает в чат: мусорная строка не будит стаю."""
    await _forget()
    try:
        msg = _message()
        await auto_bind_chat(msg)
        assert msg.answer.await_count == 0
    finally:
        await _forget()


async def test_private_chat_does_not_bind() -> None:
    """Личку рассылка получает через /start, а не через привязку чата."""
    private_id = 4242
    msg = _message(chat_type="private", chat_id=private_id)
    try:
        await auto_bind_chat(msg)
        assert await _row(private_id) is None
        assert msg.answer.await_count == 0
    finally:
        await _forget(private_id)


async def test_traffic_reactivates_deactivated_chat() -> None:
    """Чат помечен неактивным (бота выгнали / права отобрали) — первое же
    сообщение возвращает его в рассылку, а не оставляет старую пометку."""
    await _forget()
    async with SessionLocal() as db:
        db.add(Chat(id=CHAT_ID, type="channel", active=False, title="старое имя"))
        await db.commit()
    try:
        await auto_bind_chat(_message(chat_type="channel"))
        row = await _row()
        assert row is not None and row.active is True
        assert row.title == "Канал проверки"
    finally:
        await _forget()


async def test_my_chat_member_still_registers() -> None:
    """Автоматический путь не тронут: смена состава по-прежнему пишет строку."""
    await _forget()
    event = SimpleNamespace(
        chat=SimpleNamespace(type="group", id=CHAT_ID, title="Канал проверки", username=None),
        new_chat_member=SimpleNamespace(status="administrator"),
    )
    try:
        await track_chat(event)
        row = await _row()
        assert row is not None and row.active is True
    finally:
        await _forget()


def test_bind_command_removed_and_autobind_registered() -> None:
    """Ручной /bind снят; авто-привязка висит между игровыми хендлерами и fallback.

    До fallback — с фильтром «не ЛС»: хендлер ловит только неразобранный
    текст в группе/канале и не может перехватить ЛС (иначе молча съел бы
    сообщения хранителя, ловит же fallback). После игровых команд — чтобы
    не претендовать на их сообщения.
    """
    from app import handlers

    assert "cmd_bind" not in handlers.__all__
    assert "auto_bind_chat" in handlers.__all__
    messages = [handler.callback.__name__ for handler in router.message.handlers]
    channels = [handler.callback.__name__ for handler in router.channel_post.handlers]
    assert "cmd_bind" not in messages and "cmd_bind" not in channels
    assert "auto_bind_chat" in messages and "auto_bind_chat" in channels
    assert messages.index("auto_bind_chat") < messages.index("on_private_fallback")
