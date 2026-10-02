"""Пульт LOST HOWL: кнопки меню ведут на готовые колбэки и menu:-сценарии."""

from app.handlers.player import _commands_help, _menu_keyboard


def _by_data(markup):
    return {
        button.callback_data: button.text
        for row in markup.inline_keyboard
        for button in row
    }


def test_menu_keyboard_covers_daily_actions() -> None:
    kb = _by_data(_menu_keyboard("🔔 Итоги в личку: ВКЛ"))
    assert kb["menu:today"] == "▶️ Сегодня"
    assert kb["score:view"] == "⭐ Карточка Стаи"
    assert "rank:view" not in kb
    assert kb["menu:wallet"] == "💰 Кошелёк"
    assert kb["stake:view"] == "💸 Ставка"
    assert kb["menu:top"] == "🏆 Копилки"
    assert kb["menu:fund"] == "🐾 Фонд"
    assert kb["menu:help"] == "❓ Помощь"
    label = kb["dm:toggle"]
    assert label.startswith("🔔") or label.startswith("🔕")


def test_menu_keyboard_toggle_label_flows_through() -> None:
    kb = _by_data(_menu_keyboard("🔕 Итоги в личку: ВЫКЛ"))
    assert kb["dm:toggle"] == "🔕 Итоги в личку: ВЫКЛ"


def test_commands_help_mentions_remote_control() -> None:
    assert any("/menu" in line for line in _commands_help())