"""Реальный диспетчер собирается: роутер, обработчик сбоев, middleware.

build_dispatcher() ни разу не выполнялся в тестах — весь конвейер aiogram
(включение роутера, глобальный обработчик исключений, middleware закрытия
диалога) проверялся только по кускам. Сломанная сборка дошла бы до прода
незамеченной: без глобального обработчика aiogram отвечает Telegram'у 200
и НЕ пересылает апдейт — действие игрока (голос, оплата) теряется молча.

Особенность aiogram: один роутер нельзя присоединить к двум диспетчерам
(«Router is already attached»), поэтому диспетчер в процессе один — и
фикстура ниже переиспользует уже собранный, если он есть.
"""

from __future__ import annotations

import pytest

from app.handlers.bootstrap import build_dispatcher
from app.handlers.common import router


@pytest.fixture(scope="session")
def dispatcher():
    """Диспетчер, собранный реально (в процессе он может быть только один)."""
    parent = router.parent_router
    if parent is not None:
        return parent
    return build_dispatcher()


def test_dispatcher_includes_router(dispatcher) -> None:
    """Роутер с хендлерами обязан попасть в диспетчер."""
    assert router in dispatcher.sub_routers


def test_dispatcher_registers_global_error_handler(dispatcher) -> None:
    """Без глобального обработчика сбоев апдейты теряются молча.

    aiogram без него глотает исключение и отвечает вебхуку 200 — Telegram
    не перешлёт апдейт, и действие игрока пропадёт без следа.
    """
    assert dispatcher.errors.handlers, "глобальный обработчик сбоев не зарегистрирован"


def test_wallet_dialog_middleware_registered_once(dispatcher) -> None:
    """Middleware закрытия диалога — ровно один, даже после нескольких сборок.

    Раньше он регистрировался внутри build_dispatcher(), а роутер общий на
    процесс: каждая сборка вешала ещё одно одинаковое закрытие диалога на
    каждое обновление.
    """
    assert len(router.message.outer_middleware) == 1
