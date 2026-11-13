"""Маршрутизация колбэков: декоратор обязан жить на настоящем обработчике.

Рефакторинг однажды оставил @router.callback_query на вынесенном хелпере.
Роутер звал хелпер напрямую: голос возвращал кортеж и молча не записывался,
а кнопки пульта падали на аргументе message, который aiogram колбэкам не
подставляет. Прямые вызовы функций в тестах этого не видели — проверяем
именно роутер.
"""

from __future__ import annotations

import inspect

from app.handlers import on_panel_action, on_vote
from app.handlers import panel as panel_mod
from app.handlers import player as player_mod
from app.handlers.common import router


def _registered_callbacks() -> list:
    return [entry.callback for entry in router.callback_query.handlers]


def test_vote_and_panel_are_registered_on_real_handlers() -> None:
    callbacks = _registered_callbacks()
    assert on_vote in callbacks, "on_vote потерял декоратор — голос не фиксируется"
    assert (
        on_panel_action in callbacks
    ), "on_panel_action потерял декоратор — кнопки пульта недоступны"
    # Хелперы не должны сами перехватывать апдейты: иначе настоящий
    # обработчик никогда не вызовется.
    assert player_mod._parse_vote_selection not in callbacks
    assert panel_mod._panel_refresh not in callbacks


def test_registered_callback_handlers_do_not_require_uninjected_args() -> None:
    """Колбэк-хендлер не может требовать message: aiogram его не подставит.

    Первый позиционный аргумент — само событие, его диспетчер передаёт всегда.
    Любой другой параметр без значения по умолчанию способен уронить кнопку
    с TypeError, как это и случилось с _panel_refresh.
    """
    for callback in _registered_callbacks():
        parameters = list(inspect.signature(callback).parameters.values())[1:]
        required = [
            parameter.name
            for parameter in parameters
            if parameter.default is parameter.empty
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        assert not required, (
            f"{getattr(callback, '__name__', callback)} требует {required}: "
            "aiogram эти аргументы не подставляет"
        )
