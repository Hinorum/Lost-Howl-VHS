"""Порядок хендлеров: catch-all ЛС не должен глотать команды."""

from app.handlers.common import router
from app.handlers.fallback import on_private_fallback


def _message_handler_names() -> list[str]:
    return [handler.callback.__name__ for handler in router.message.handlers]


def test_private_fallback_is_last_message_handler() -> None:
    names = _message_handler_names()
    assert names[-1] == on_private_fallback.__name__
    for command in ("cmd_help", "cmd_start", "cmd_today", "cmd_menu", "cmd_wallet"):
        assert command in names
        assert names.index(command) < names.index(on_private_fallback.__name__)
