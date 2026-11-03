"""Платная смена пути (/change): продажа, Gram-вилка и стоп-кран.

Всё, что тут живёт, связано с деньгами игрока, но тестировались только модели
грантов. Покрываем слой хендлеров целиком: три рубежа отказа (выключенная
перемотка, бесплатная версия, версия без ставок), стоп-кран техработ и
инструкцию по оплате Gram.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal
from app.handlers import on_change_view, on_payton
from app.handlers import topup as topup_mod
from app.handlers.topup import cmd_change
from app.models import Card, Player, RevoteGrant, Round, RoundStatus, Vote, WinRule

TREASURY = "0:" + "ef" * 32


def _uid() -> int:
    return 750_000 + int.from_bytes(os.urandom(2), "big")


def _open_round(day_index: int, money_mode: bool = True) -> Round:
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
        money_mode=money_mode,
    )


def _user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=None, first_name="Тестовый")


def _message(
    text: str = "/change",
    chat_type: str = "private",
    uid: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=555),
        from_user=_user(uid if uid is not None else _uid()),
        text=text,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )


def _callback(data: str, uid: int, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=_user(uid),
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type, id=555), answer=AsyncMock()),
        bot=SimpleNamespace(),
        answer=AsyncMock(),
    )


async def _wipe(*tables) -> None:
    async with SessionLocal() as db:
        for table in tables:
            await db.execute(delete(table))
        await db.commit()


@pytest.fixture
async def stage(monkeypatch):
    """Открытый день со ставками, в котором у игрока уже есть голос: только
    тогда перемотка имеет смысл и только тогда её можно купить."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    uid = _uid()
    day_index = 81_000 + int.from_bytes(os.urandom(3), "big")
    async with SessionLocal() as db:
        db.add(Player(id=uid))
        day = _open_round(day_index)
        db.add(day)
        await db.commit()
        round_id = day.id
    async with SessionLocal() as db:
        db.add(Vote(round_id=round_id, player_id=uid, card_position=1))
        await db.commit()
    yield SimpleNamespace(uid=uid, round_id=round_id, day_index=day_index)
    async with SessionLocal() as db:
        await db.execute(delete(RevoteGrant).where(RevoteGrant.player_id == uid))
        await db.execute(delete(Vote).where(Vote.player_id == uid))
        await db.execute(delete(Card).where(Card.round_id == round_id))
        await db.delete(await db.get(Player, uid) or Player())
        round_db = await db.get(Round, round_id)
        if round_db is not None:
            await db.delete(round_db)
        await db.commit()


async def test_change_refuses_when_feature_disabled(monkeypatch) -> None:
    """Рубейка выключена в настройках — команда не должна даже лезть в БД."""
    monkeypatch.setattr(settings, "revote_enabled", False)
    message = _message()
    await cmd_change(message)
    assert "недоступна" in message.answer.call_args.args[0]


async def test_change_refuses_in_free_version(monkeypatch) -> None:
    """Версия без TON: платить нечем, поэтому перемотки нет вовсе."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", False)
    message = _message()
    await cmd_change(message)
    assert "бесплатной версии" in message.answer.call_args.args[0]


async def test_change_refuses_in_version_without_stakes(monkeypatch) -> None:
    """Игра идёт в режиме без ставок: платные механики не продаются, даже
    если технически TON включён."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=False))
    message = _message()
    await cmd_change(message)
    assert "без ставок" in message.answer.call_args.args[0]


async def test_change_refuses_without_treasury_address(monkeypatch) -> None:
    """Нет адреса казначея — не обещаем кнопку оплаты: переводить некуда,
    а следующий шаг всё равно отказал бы."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", "")
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    message = _message()
    await cmd_change(message)
    assert "ещё не включён" in message.answer.call_args.args[0]
    assert message.answer.call_count == 1


async def test_change_without_open_day_offers_nothing(monkeypatch) -> None:
    """Нет открытого дня — перемотать нечего, и клавиатуру с оплатой показывать
    нельзя: иначе игрок заплатит за воздух."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    await _wipe(Vote, Card, Round, Player)
    message = _message()
    await cmd_change(message)
    assert "уже записан" in message.answer.call_args.args[0]
    assert message.answer.call_count == 1


async def test_change_without_vote_is_free_and_hidden(monkeypatch) -> None:
    """Первый выбор дня бесплатный: платить за перемотку не нужно, поэтому
    даже счёт не выставляем."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    uid = _uid()
    async with SessionLocal() as db:
        db.add(Player(id=uid))
        day = _open_round(81_500)
        db.add(day)
        await db.commit()
        round_id = day.id
    try:
        message = _message()
        message.from_user = _user(uid)
        await cmd_change(message)
        assert "первая запись бесплатная" in message.answer.call_args.args[0]
        assert message.answer.call_count == 1
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Vote).where(Vote.player_id == uid))
            await db.execute(delete(Card).where(Card.round_id == round_id))
            await db.delete(await db.get(Player, uid))
            await db.delete(await db.get(Round, round_id))
            await db.commit()


async def test_change_private_shows_gram_payment(stage, monkeypatch) -> None:
    """Личка: статус + вилка Gram и единственная кнопка оплаты. Верх вилки
    обязан быть строго ниже минимальной ставки, иначе оплата перемотки
    превратится в ставку."""
    monkeypatch.setattr(settings, "revote_ton", 0.4)
    monkeypatch.setattr(settings, "stake_min_ton", 0.5)
    message = _message()
    message.from_user = _user(stage.uid)
    await cmd_change(message)
    assert message.answer.await_count == 2
    status = message.answer.await_args_list[0].args[0]
    assert "твой выбор: II" in status
    payload = message.answer.await_args_list[1]
    assert "0.4" in payload.args[0] and "0.49" in payload.args[0]
    assert "Stars" not in payload.args[0] and "⭐" not in payload.args[0]
    keyboard = payload.kwargs["reply_markup"]
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [f"payton:{stage.round_id}"]
    # Вилка обязана быть валидной конфигом, а не «почти такой».
    assert topup_mod._revote_gram_ceiling() == 0.49
    assert topup_mod._revote_gram_ceiling() < settings.stake_min_ton


async def test_change_keyboard_has_gram_button() -> None:
    """Единственный способ оплаты перемотки — Gram-перевод казначею."""
    keyboard = topup_mod._revote_keyboard(42)
    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].callback_data == "payton:42"


async def test_change_in_group_offers_private_button(stage) -> None:
    """В группе деталей не выдаём: только кнопка «открой бота в личке»."""
    message = _message(chat_type="group")
    message.from_user = _user(stage.uid)
    await cmd_change(message)
    text = message.answer.call_args.args[0]
    assert "только тебе" in text
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "change:view"


async def test_change_view_callback_explains_and_points_to_dm(stage) -> None:
    """Кнопка из группы: короткий ответ с тем же статусом и подсказкой /change."""
    callback = SimpleNamespace(from_user=_user(stage.uid), answer=AsyncMock())
    await on_change_view(callback)
    assert callback.answer.call_args.kwargs["show_alert"] is True
    text = callback.answer.call_args.args[0]
    assert "твой выбор: II" in text
    assert "/change" in text
    assert len(text) <= 200


async def test_change_is_rate_limited(stage, monkeypatch) -> None:
    """/change подряд не перебирает игрока и раунд впустую."""
    from app.handlers import wallet as wallet_mod

    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=True))

    first = _message("/change", "private", stage.uid)
    await cmd_change(first)

    second = _message("/change", "private", stage.uid)
    await cmd_change(second)
    assert "Не так часто" in second.answer.call_args.args[0]

    monkeypatch.setitem(wallet_mod._ACTION_COOLDOWNS, "change_cd", 0.0)
    third = _message("/change", "private", stage.uid)
    await cmd_change(third)
    assert "Не так часто" not in third.answer.call_args.args[0]


async def test_payton_requires_private_chat(stage) -> None:
    callback = _callback(f"payton:{stage.round_id}", stage.uid, chat_type="group")
    await on_payton(callback)
    assert "только в личке" in callback.answer.call_args.args[0]
    no_message = _callback(f"payton:{stage.round_id}", stage.uid)
    no_message.message = None
    await on_payton(no_message)
    assert "только в личке" in no_message.answer.call_args.args[0]


async def test_payton_disabled_by_feature_flag(stage, monkeypatch) -> None:
    """Подделанный или сохранённый колбэк при REVOTE_ENABLED=false не должен
    выдавать реквизиты: флаг закрывает продажу, а не только команду."""
    monkeypatch.setattr(settings, "revote_enabled", False)
    callback = _callback(f"payton:{stage.round_id}", stage.uid)
    await on_payton(callback)
    assert "недоступна" in callback.answer.call_args.args[0]
    callback.message.answer.assert_not_awaited()


async def test_payton_stale_round_is_rejected(stage) -> None:
    """Счёт из прошлого дня: активный round_id не совпал — реквизиты не
    выдаём, иначе игрок заплатит за несуществующий кадр."""
    callback = _callback(f"payton:{stage.round_id + 500}", stage.uid)
    await on_payton(callback)
    callback.message.answer.assert_not_awaited()
    assert callback.answer.call_args.kwargs["show_alert"] is True


async def test_payton_blocked_in_version_without_stakes(stage, monkeypatch) -> None:
    """В версии без ставок реквизиты не выдаются даже по подделанному колбэку."""
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=False))
    callback = _callback(f"payton:{stage.round_id}", stage.uid)
    await on_payton(callback)
    assert "без ставок" in callback.answer.call_args.args[0]
    callback.message.answer.assert_not_awaited()


async def test_payton_refuses_without_treasury_address(stage, monkeypatch) -> None:
    """Нет адреса казначея — нечего показать: молчаливый перевод в никуда."""
    monkeypatch.setattr(settings, "treasury_address", "")
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    callback = _callback(f"payton:{stage.round_id}", stage.uid)
    await on_payton(callback)
    assert "ещё не включён" in callback.answer.call_args.args[0]
    callback.message.answer.assert_not_awaited()


async def test_payton_refuses_broken_round_id(stage) -> None:
    callback = _callback("payton:abc", stage.uid)
    await on_payton(callback)
    assert "Некорректный счёт" in callback.answer.call_args.args[0]


async def test_payton_shows_fork_and_memo(stage, monkeypatch) -> None:
    """Инструкция по Gram: вилка, потолок ниже ставки, memo и предупреждение,
    что ровно минимальная ставка — это ставка дня, а не оплата перемотки."""
    monkeypatch.setattr(settings, "revote_ton", 0.4)
    monkeypatch.setattr(settings, "stake_min_ton", 0.5)
    callback = _callback(f"payton:{stage.round_id}", stage.uid)
    await on_payton(callback)
    text = callback.message.answer.call_args.args[0]
    assert TREASURY in text
    assert "0.4" in text and "0.49" in text
    assert f"rv:{stage.round_id}" in text
    assert "не оплата перемотки" in text
    assert "`" not in text
    callback.answer.assert_awaited_with()
