"""Порядок хендлеров: catch-all ЛС не должен глотать команды.

Catch-all живёт в отдельном роутере (app/handlers/fallback.py), который
подключается к диспетчеру ПОСЛЕДНИМ (build_dispatcher). Раньше он
регистрировался декоратором в общем роутере, и его позиция зависела от
порядка импортов модулей: isort поднимал fallback выше player/wallet, и
первый подходящий хендлер выигрывал — /start уходил в диалог привязки
молча.

Тест проверяет и структуру (catch-all в отдельном роутере, подключённом
последним), и результат (команды стоят раньше catch-all).
"""

from app.handlers.bootstrap import build_dispatcher
from app.handlers.common import router
from app.handlers.fallback import fallback_router, on_private_fallback


def _message_handler_names() -> list[str]:
    return [handler.callback.__name__ for handler in router.message.handlers]


def test_private_fallback_is_not_in_the_command_router() -> None:
    """Catch-all живёт отдельно от команд — иначе порядок решает импорт."""
    names = _message_handler_names()
    assert on_private_fallback.__name__ not in names
    for command in ("cmd_start", "cmd_today", "cmd_wallet", "cmd_invite"):
        assert command in names


def test_fallback_lives_in_its_own_router() -> None:
    """Catch-all не должен делить роутер с командами."""
    assert fallback_router is not router
    assert fallback_router not in router.sub_routers


def test_dispatcher_includes_fallback_router_last() -> None:
    """Порядок подключения задан в коде, а не порядком импортов."""
    # Диспетчер в процессе может быть только один (aiogram не даёт присоединить
    # один роутер к двум), поэтому берём уже собранный, а если его нет —
    # собираем.
    dispatcher = router.parent_router
    if dispatcher is None:
        dispatcher = build_dispatcher()
    assert dispatcher.sub_routers[-1] is fallback_router
    assert router in dispatcher.sub_routers
