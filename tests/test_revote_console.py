"""Платная смена пути (/change): продажа, счёт Stars, Gram-вилка и возврат.

Всё, что тут живёт, связано с деньгами игрока, но тестировались только модели
грантов. Покрываем слой хендлеров целиком: три рубежа отказа (выключенная
перемотка, бесплатная версия, версия без ставок), стоп-кран техработ, выставление
счёта, отказ на pre-checkout, зачисление оплаты и возврат звёзд — включая самый
дорогой сценарий: грант уже потрачен, деньги вернулись, а путь у игрока сменён.
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.handlers import on_change_view, on_paystars, on_payton, on_pre_checkout
from app.handlers import topup as topup_mod
from app.handlers.topup import cmd_change, on_refunded_payment, on_successful_payment
from app.models import Card, Income, Player, RevoteGrant, Round, RoundStatus, Vote, WinRule

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
        bot=SimpleNamespace(send_invoice=AsyncMock()),
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
        await db.execute(delete(Income).where(Income.player_id == uid))
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
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=False))
    message = _message()
    await cmd_change(message)
    assert "без ставок" in message.answer.call_args.args[0]


async def test_change_without_open_day_offers_nothing(monkeypatch) -> None:
    """Нет открытого дня — перемотать нечего, и клавиатуру с оплатой показывать
    нельзя: иначе игрок заплатит за воздух."""
    monkeypatch.setattr(settings, "revote_enabled", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
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


async def test_change_private_shows_both_payment_methods(stage, monkeypatch) -> None:
    """Личка: статус + вилка Gram и кнопка Stars. Верх вилки обязан быть строго
    ниже минимальной ставки, иначе оплата перемотки превратится в ставку."""
    monkeypatch.setattr(settings, "revote_stars", 15)
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
    keyboard = payload.kwargs["reply_markup"]
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [
        f"paystars:{stage.round_id}",
        f"payton:{stage.round_id}",
    ]
    assert any("15" in b.text for b in buttons)
    # Вилка обязана быть валидной конфигом, а не «почти такой».
    assert topup_mod._revote_gram_ceiling() == 0.49
    assert topup_mod._revote_gram_ceiling() < settings.stake_min_ton


async def test_change_keyboard_without_ton_has_no_gram_button(monkeypatch) -> None:
    """TON выключен, но /change мы в бесплатной версии уже не пускаем — однако
    клавиатура строить Gram-кнопку без адреса казначея не должна."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    keyboard = topup_mod._revote_keyboard(42)
    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].callback_data == "paystars:42"


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


async def test_paystars_needs_private_chat_and_number(stage) -> None:
    """Подделка колбэка: без номера дня, без сообщения или не из лички счёт не
    выставляется — иначе оплата уйдёт за чужий round_id."""
    for callback in (
        _callback("paystars:abc", stage.uid),
        _callback(f"paystars:{stage.round_id}", stage.uid, chat_type="group"),
    ):
        await on_paystars(callback)
        assert callback.bot.send_invoice.await_count == 0
        assert "только в личке" in callback.answer.call_args.args[0]
    no_message = _callback(f"paystars:{stage.round_id}", stage.uid)
    no_message.message = None
    await on_paystars(no_message)
    assert no_message.bot.send_invoice.await_count == 0


async def test_paystars_blocked_during_maintenance(stage, monkeypatch) -> None:
    """Стоп-кран: во время техработ счёт не продаётся вовсе."""
    monkeypatch.setattr(topup_mod, "_game_paused_now", AsyncMock(return_value=True))
    callback = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(callback)
    assert callback.bot.send_invoice.await_count == 0
    assert "технические работы" in callback.answer.call_args.args[0]


async def test_paystars_blocked_in_version_without_stakes(stage, monkeypatch) -> None:
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=False))
    callback = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(callback)
    assert callback.bot.send_invoice.await_count == 0
    assert "без ставок" in callback.answer.call_args.args[0]


async def test_paystars_stale_round_is_rejected(stage, monkeypatch) -> None:
    """Счёт из прошлого дня: активный round_id не совпал — предупреждаем и не
    выставляем новый счёт (иначе игрок заплатит за несуществующий кадр)."""
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=True))
    callback = _callback(f"paystars:{stage.round_id + 500}", stage.uid)
    await on_paystars(callback)
    assert callback.bot.send_invoice.await_count == 0
    assert callback.answer.call_args.kwargs["show_alert"] is True


async def test_paystars_issues_invoice(stage, monkeypatch) -> None:
    """Счастливый путь: счёт на текущий день, цена из настроек, payload с
    номером дня — после оплаты по нему грант привяжется именно к этому дню."""
    monkeypatch.setattr(settings, "revote_stars", 21)
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=True))
    callback = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(callback)
    assert callback.bot.send_invoice.await_count == 1
    kwargs = callback.bot.send_invoice.await_args.kwargs
    assert kwargs["payload"] == f"revote:{stage.round_id}"
    assert kwargs["currency"] == "XTR"
    assert kwargs["prices"][0].amount == 21
    assert kwargs["chat_id"] == 555
    callback.answer.assert_awaited_with()


async def test_paystars_is_rate_limited(stage, monkeypatch) -> None:
    """Повторное нажатие «Оплатить» не выставляет второй счёт.

    send_invoice — это вызов Bot API, то есть расход квоты бота и риск поймать
    flood-ожидание от нажатий одного игрока (у скрипт-клиента, в отличие от
    обычного, лимита на тапы нет). Через полминуты проходит.
    """
    from app.handlers import wallet as wallet_mod

    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=True))
    first = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(first)
    assert first.bot.send_invoice.await_count == 1

    second = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(second)
    assert second.bot.send_invoice.await_count == 0, "второй счёт выставлен без паузы"
    assert "полминуты" in second.answer.await_args.args[0]

    monkeypatch.setitem(wallet_mod._ACTION_COOLDOWNS, "paystars_cd", 0.0)
    third = _callback(f"paystars:{stage.round_id}", stage.uid)
    await on_paystars(third)
    assert third.bot.send_invoice.await_count == 1, "после остывания счёт должен пройти"


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


async def test_pre_checkout_rejects_stale_invoice(monkeypatch) -> None:
    """Мусорный payload — счёт не наш: отказ ДО списания Stars."""
    query = SimpleNamespace(invoice_payload="revote:x", answer=AsyncMock())
    await on_pre_checkout(query)
    assert query.answer.await_args.kwargs["ok"] is False
    assert "устарел" in query.answer.await_args.kwargs["error_message"]


async def test_pre_checkout_blocked_during_maintenance(monkeypatch) -> None:
    """Второй рубеж стоп-крана: счёт мог быть выставлен ДО паузы — отклоняем
    оплату, не списывая Stars."""
    monkeypatch.setattr(topup_mod, "_game_paused_now", AsyncMock(return_value=True))
    query = SimpleNamespace(invoice_payload="revote:7", answer=AsyncMock())
    await on_pre_checkout(query)
    assert query.answer.await_args.kwargs["ok"] is False
    assert "технические работы" in query.answer.await_args.kwargs["error_message"]


async def test_pre_checkout_blocked_in_version_without_stakes(monkeypatch) -> None:
    """Счёт выставлен в ставках, оплата пришла после переключения — не списываем."""
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=False))
    query = SimpleNamespace(invoice_payload="revote:7", answer=AsyncMock())
    await on_pre_checkout(query)
    assert query.answer.await_args.kwargs["ok"] is False
    assert "без ставок" in query.answer.await_args.kwargs["error_message"]


async def test_pre_checkout_accepts_current_invoice(monkeypatch) -> None:
    monkeypatch.setattr(topup_mod, "_game_paused_now", AsyncMock(return_value=False))
    monkeypatch.setattr(topup_mod, "_active_round_money_mode", AsyncMock(return_value=True))
    query = SimpleNamespace(invoice_payload="revote:7", answer=AsyncMock())
    await on_pre_checkout(query)
    query.answer.assert_awaited_with(ok=True)


async def test_successful_payment_without_payload_is_ignored() -> None:
    """Подделка апдейта без successful_payment: молча, без записи в ledger."""
    await _wipe(RevoteGrant, Income)
    message = _message()
    message.successful_payment = None
    await on_successful_payment(message)
    message.answer.assert_not_awaited()
    async with SessionLocal() as db:
        assert (await db.execute(select(RevoteGrant))).scalars().all() == []


async def test_refund_of_unspent_grant_revokes_it(stage, monkeypatch) -> None:
    """Возврат неиспользованного гранта: права на перемотку отзываем, а в
    ledger-доход дописываем refunded-хвост, чтобы касса не показывала выручку,
    которой не осталось."""
    ops = importlib.import_module("app.ops")
    alerts = AsyncMock(return_value=None)
    monkeypatch.setattr(ops, "notify_admins", alerts)
    await _wipe(RevoteGrant, Income)
    charge = f"refund-{stage.uid}"
    async with SessionLocal() as db:
        db.add(
            RevoteGrant(
                round_id=stage.round_id,
                player_id=stage.uid,
                source="stars",
                unit_ref=charge,
            )
        )
        db.add(
            Income(
                kind="stars",
                amount_stars=15,
                round_id=stage.round_id,
                player_id=stage.uid,
                unit_ref=charge,
                note="revote",
            )
        )
        await db.commit()

    message = _message()
    message.from_user = _user(stage.uid)
    message.refunded_payment = SimpleNamespace(telegram_payment_charge_id=charge)
    await on_refunded_payment(message)
    assert "отозван" in message.answer.call_args.args[0]
    async with SessionLocal() as db:
        grant = (
            await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge))
        ).scalar_one()
        assert grant.status == "refunded"
        income = (await db.execute(select(Income).where(Income.unit_ref == charge))).scalar_one()
        assert income.note.startswith("revote | refunded:")
    alerts.assert_awaited_once()
    assert "УЖЕ потрачен" not in alerts.await_args.args[1]


async def test_refund_after_grant_was_used_warns_keeper(stage, monkeypatch) -> None:
    """Самая дорогая рассинхронизация: грант потрачен, путь уже сменён, деньги
    вернулись. Игроку говорим честно, хранителю — отдельной строкой."""
    ops = importlib.import_module("app.ops")
    alerts = AsyncMock(return_value=None)
    monkeypatch.setattr(ops, "notify_admins", alerts)
    await _wipe(RevoteGrant, Income)
    charge = f"spent-{stage.uid}"
    async with SessionLocal() as db:
        db.add(
            RevoteGrant(
                round_id=stage.round_id,
                player_id=stage.uid,
                source="stars",
                unit_ref=charge,
                status="used",
            )
        )
        db.add(
            Income(
                kind="stars",
                amount_stars=15,
                round_id=stage.round_id,
                player_id=stage.uid,
                unit_ref=charge,
                note="",
            )
        )
        await db.commit()

    message = _message()
    message.from_user = _user(stage.uid)
    message.refunded_payment = SimpleNamespace(telegram_payment_charge_id=charge)
    await on_refunded_payment(message)
    assert "уже была использована" in message.answer.call_args.args[0]
    async with SessionLocal() as db:
        grant = (
            await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge))
        ).scalar_one()
        assert grant.status == "used", "потраченный грант нельзя отозвать задним числом"
        income = (await db.execute(select(Income).where(Income.unit_ref == charge))).scalar_one()
        # Пустая заметка не должна превращаться в « | refunded:…».
        assert income.note.startswith("refunded:")
    assert "УЖЕ потрачен" in alerts.await_args.args[1]


async def test_refund_of_unknown_charge_still_notifies(monkeypatch) -> None:
    """Возврат по незнакомому charge_id (наш грант не нашёлся) — молча
    проглатывать нельзя: хранитель должен узнать о расхождении."""
    ops = importlib.import_module("app.ops")
    alerts = AsyncMock(return_value=None)
    monkeypatch.setattr(ops, "notify_admins", alerts)
    await _wipe(RevoteGrant, Income)
    message = _message()
    message.refunded_payment = SimpleNamespace(telegram_payment_charge_id="ghost-charge")
    await on_refunded_payment(message)
    assert "отозван" in message.answer.call_args.args[0]
    assert "ghost-charge" in alerts.await_args.args[1]


async def test_refund_survives_dead_message_channel(monkeypatch) -> None:
    """Упавший ответ игроку не должен превратить возврат в потерю алерта."""
    ops = importlib.import_module("app.ops")
    alerts = AsyncMock(return_value=None)
    monkeypatch.setattr(ops, "notify_admins", alerts)
    await _wipe(RevoteGrant, Income)
    message = _message()
    message.answer = AsyncMock(side_effect=RuntimeError("message thread closed"))
    message.refunded_payment = SimpleNamespace(telegram_payment_charge_id="dead-channel")
    await on_refunded_payment(message)
    alerts.assert_awaited_once()


async def test_refund_without_payload_is_ignored(monkeypatch) -> None:
    ops = importlib.import_module("app.ops")
    alerts = AsyncMock(return_value=None)
    monkeypatch.setattr(ops, "notify_admins", alerts)
    message = _message()
    message.refunded_payment = None
    await on_refunded_payment(message)
    message.answer.assert_not_awaited()
    alerts.assert_not_awaited()


async def test_payton_requires_private_chat(stage) -> None:
    callback = _callback(f"payton:{stage.round_id}", stage.uid, chat_type="group")
    await on_payton(callback)
    assert "только в личке" in callback.answer.call_args.args[0]
    no_message = _callback(f"payton:{stage.round_id}", stage.uid)
    no_message.message = None
    await on_payton(no_message)
    assert "только в личке" in no_message.answer.call_args.args[0]


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
    что ровно минимальная ставка будет засчитана как ставка дня."""
    monkeypatch.setattr(settings, "revote_ton", 0.4)
    monkeypatch.setattr(settings, "stake_min_ton", 0.5)
    callback = _callback(f"payton:{stage.round_id}", stage.uid)
    await on_payton(callback)
    text = callback.message.answer.call_args.args[0]
    assert TREASURY in text
    assert "0.4" in text and "0.49" in text
    assert f"rv:{stage.round_id}" in text
    assert "не может" in text
    callback.answer.assert_awaited_with()
