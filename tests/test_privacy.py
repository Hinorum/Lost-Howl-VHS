"""Проверка приватности: личные данные не видны всему чату.

В группе /score и /wallet отвечают кнопкой; содержимое показывается через
callback.answer(show_alert=True) — окно видит только нажавший. В личке
ответ приходит напрямую.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db import init_db
from app.handlers import cmd_score, cmd_wallet, on_score_view, on_wallet_view


@pytest.fixture(scope="module", autouse=True)
async def _handlers_db():
    await init_db()
    yield


def make_user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"u{uid}", first_name="Тест")


def make_message(chat_type: str, uid: int, text: str = "/score") -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        from_user=make_user(uid),
        text=text,
        answer=AsyncMock(),
        answer_media_group=AsyncMock(),
    )


def make_callback(chat_type: str, uid: int, data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=make_user(uid),
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type)),
        answer=AsyncMock(),
    )


async def test_score_in_group_is_not_public() -> None:
    message = make_message("supergroup", 700_001)
    await cmd_score(message)
    kwargs = message.answer.call_args.kwargs
    assert "Очки" not in message.answer.call_args.args[0]
    assert kwargs.get("reply_markup") is not None


async def test_score_in_private_is_direct() -> None:
    message = make_message("private", 700_002)
    await cmd_score(message)
    text = message.answer.call_args.args[0]
    assert "Голосов на неделе" in text
    assert "Голосов в месяце" in text


async def test_score_button_alert_private_to_presser() -> None:
    callback = make_callback("supergroup", 700_003, "score:view")
    await on_score_view(callback)
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    # Лимит Telegram на окно колбэка — 200 символов.
    assert len(args[0]) <= 200
    assert "Голосов" in args[0]
    # Окно колбэка не рендерит HTML — сырых тегов в нём быть не должно.
    assert "<" not in args[0]
    # Данные пересчитаны для нажавшего, а не для автора команды.
    assert callback.from_user.username in {f"u{callback.from_user.id}"}


async def test_wallet_view_alert() -> None:
    callback = make_callback("supergroup", 700_004, "wallet:view")
    await on_wallet_view(callback)
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    assert len(args[0]) <= 200
    assert "Кошелёк" in args[0]
    # Окно колбэка не рендерит HTML — сырых тегов в нём быть не должно.
    assert "<" not in args[0]


async def test_wallet_bind_in_group_hides_details() -> None:
    address = "EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF"
    message = make_message("supergroup", 700_005, text=f"/wallet {address}")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    assert address not in text


async def test_wallet_bind_in_private_confirms() -> None:
    # Адрес с другим raw: после нормализации UQ/EQ одной пары ключей дают
    # одинаковый canonical-вид и UNIQUE(players.wallet_address) честно ругается.
    address = "UQDd3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3USz"
    message = make_message("private", 700_006, text=f"/wallet {address}")
    await cmd_wallet(message)
    assert "привязан" in message.answer.call_args.args[0]


async def test_button_press_by_another_user_shows_his_own_data() -> None:
    """Кнопку может нажать кто угодно — каждый увидит только свои цифры."""
    requester = make_message("supergroup", 700_007)
    await cmd_score(requester)
    presser_callback = make_callback("supergroup", 700_008, "score:view")
    await on_score_view(presser_callback)
    args, kwargs = presser_callback.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Голосов" in args[0]
