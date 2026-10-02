"""Гейт хранителя: каждый админ-хендлер обязан отшивать не-админа первым.

Обзззод всех точек (команды + кнопки пульта/сверки/кассеты):
- сообщение с from_user ∉ admin_id_set получает «только для хранителя»
  и не делает ничего дальше (гейт стоит до любого доступа к БД);
- колбэк с from_user ∉ admin_id_set — то же через callback.answer(alert).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.panel as panel_mod
from app.handlers.admin import (
    cmd_adjust,
    cmd_advance,
    cmd_dispute,
    cmd_disputes,
    cmd_finalize,
    cmd_pause,
    cmd_refinalize,
    cmd_resetgame,
    cmd_resume,
    on_adjust_action,
)
from app.handlers.panel import cmd_cassette, cmd_panel, on_cassette_action, on_panel_action
from app.handlers.payout import (
    cmd_blockchain,
    cmd_fundout,
    cmd_incoming,
    cmd_payout,
    cmd_payouts,
    cmd_return,
    cmd_revenue,
    cmd_stakes,
    cmd_treasury,
)

OUTSIDER_ID = 1
# фрагмент общий для всех текстов гейта (в т.ч. заглавная «Только...»)
ADMIN_TEXT = "только для хранителя"

COMMAND_GUARDS = [
    (cmd_advance, "/advance"),
    (cmd_resetgame, "/resetgame"),
    (cmd_dispute, "/dispute open 3 1 x"),  # admin-глагол open — гейт срабатывает до разбора
    (cmd_disputes, "/disputes"),
    (cmd_adjust, "/adjust"),
    (cmd_finalize, "/finalize"),
    (cmd_refinalize, "/refinalize"),
    (cmd_pause, "/pause"),
    (cmd_resume, "/resume"),
    (cmd_panel, "/panel"),
    (cmd_cassette, "/cassette"),
    (cmd_payouts, "/payouts"),
    (cmd_payout, "/payout"),
    (cmd_return, "/return"),
    (cmd_treasury, "/treasury"),
    (cmd_fundout, "/fundout"),
    (cmd_incoming, "/incoming"),
    (cmd_stakes, "/stakes"),
    (cmd_revenue, "/revenue"),
    (cmd_blockchain, "/blockchain"),
]

CALLBACK_GUARDS = [
    (on_adjust_action, "adj:out"),
    (on_panel_action, "panel:view"),
    (on_cassette_action, "cassette:set:next"),
]


def make_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=OUTSIDER_ID),
        text=text,
        answer=AsyncMock(),
    )


def make_callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=OUTSIDER_ID),
        answer=AsyncMock(),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            chat=SimpleNamespace(type="private"),
        ),
    )


@pytest.mark.parametrize("handler,text", COMMAND_GUARDS, ids=[h.__name__ for h, _ in COMMAND_GUARDS])
async def test_admin_commands_reject_outsider(monkeypatch, handler, text) -> None:
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242")
    msg = make_message(text)
    await handler(msg)
    reply = msg.answer.call_args.args[0]
    assert ADMIN_TEXT in reply.lower()
    assert msg.answer.await_count == 1


@pytest.mark.parametrize("handler,data", CALLBACK_GUARDS, ids=[h.__name__ for h, _ in CALLBACK_GUARDS])
async def test_admin_callbacks_reject_outsider(monkeypatch, handler, data) -> None:
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242")
    callback = make_callback(data)
    await handler(callback)
    args, kwargs = callback.answer.call_args
    assert ADMIN_TEXT in args[0].lower()
    assert kwargs.get("show_alert") is True


async def test_admin_id_set_predicate(monkeypatch) -> None:
    """Гейт читает settings.admin_id_set из ADMIN_IDS: не-пустой исходник,
    int-нормализация, отсутствие ложного включения postgres-дефолта."""
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242")
    assert 4242 in panel_mod.settings.admin_id_set
    assert OUTSIDER_ID not in panel_mod.settings.admin_id_set
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242, 7777")
    assert panel_mod.settings.admin_id_set == {4242, 7777}