"""Пульт LOST HOWL: кнопки меню ведут на готовые колбэки и menu:-сценарии."""

from app.handlers.player import _commands_help, _menu_keyboard


def _by_data(markup):
    return {
        button.callback_data: button.text
        for row in markup.inline_keyboard
        for button in row
    }


def test_menu_keyboard_covers_daily_actions() -> None:
    kb = _by_data(_menu_keyboard())
    assert kb["menu:today"] == "▶️ Сегодня"
    assert kb["score:view"] == "⭐ Карточка Стаи"
    assert "rank:view" not in kb
    assert kb["menu:wallet"] == "💰 Кошелёк"
    assert kb["stake:view"] == "💸 Ставка"
    assert kb["menu:top"] == "🏆 Копилки"
    assert kb["menu:fund"] == "🐾 Фонд"
    assert kb["menu:invite"] == "🐾 Позвать в стаю"
    # Кнопки-дублёры /start сняты владельцем: «Итоги в личку» (Telegram
    # сам умеет отключать уведомления) и «Помощь» (дублировала /start).
    assert "menu:help" not in kb
    assert "dm:toggle" not in kb


def test_commands_help_mentions_start_and_invite() -> None:
    lines = _commands_help()
    assert any("/start" in line for line in lines)
    assert any("/invite" in line for line in lines)
    assert not any("/help" in line for line in lines)
    assert not any("/menu" in line for line in lines)
    assert not any("/referral" in line for line in lines)
