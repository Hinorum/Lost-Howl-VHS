"""Гейт хранителя: каждый админ-хендлер обязан отшивать не-админа первым.

Обзор всех точек (команды + кнопки пульта/сверки/кассеты):
- сообщение с from_user ∉ admin_id_set получает «только для хранителя»
  и не делает ничего дальше (гейт стоит до любого доступа к БД);
- колбэк с from_user ∉ admin_id_set — то же через callback.answer(alert).

Отдельно проверяется, что админ-команды не отвечают в группах: гейт по
ADMIN_IDS от утечки не спасает, ведь автор команды — настоящий хранитель.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import F
from aiogram.filters.magic_data import MagicFilter
from aiogram.types import Chat
from aiogram.types import Message as AiogramMessage

import app.handlers.panel as panel_mod
from app.handlers.admin import (
    cmd_adjust,
    cmd_advance,
    cmd_backup,
    cmd_dispute,
    cmd_disputes,
    cmd_finalize,
    cmd_pause,
    cmd_refinalize,
    cmd_resetgame,
    cmd_resume,
    on_adjust_action,
)
from app.handlers.common import router as app_router
from app.handlers.ops_diag import cmd_ops
from app.handlers.panel import (
    cmd_cassette,
    cmd_panel,
    on_cassette_action,
    on_cassette_document,
    on_panel_action,
)
from app.handlers.payout import (
    cmd_blockchain,
    cmd_fundout,
    cmd_halt_payouts,
    cmd_incoming,
    cmd_mirror,
    cmd_payout,
    cmd_payouts,
    cmd_resume_payouts,
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
    (cmd_backup, "/backup"),
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
    (cmd_halt_payouts, "/halt-payouts"),
    (cmd_resume_payouts, "/resume-payouts"),
    (cmd_payout, "/payout"),
    (cmd_return, "/return"),
    (cmd_treasury, "/treasury"),
    (cmd_fundout, "/fundout"),
    (cmd_incoming, "/incoming"),
    (cmd_stakes, "/stakes"),
    (cmd_revenue, "/revenue"),
    (cmd_blockchain, "/blockchain"),
    (cmd_mirror, "/mirror reset confirm"),  # гейт обязан стоять до разбора аргументов
    (cmd_ops, "/ops"),
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


def _registered_filters(handler) -> list:
    """Условия, под которыми хендлер реально зарегистрирован в роутере.

    aiogram хранит каждый фильтр обёрнутым в FilterObject с единственным
    сокрытым полем magic — исходным объектом. Распаковываем обратно, иначе
    проверять тут нечего.
    """
    found: list = []
    for observer in app_router.observers.values():
        for entry in observer.handlers:
            if entry.callback is handler:
                for candidate in entry.filters:
                    inner = getattr(candidate, "magic", None)
                    found.append(inner if inner is not None else candidate)
    return found


@pytest.mark.parametrize("handler,text", COMMAND_GUARDS, ids=[h.__name__ for h, _ in COMMAND_GUARDS])
async def test_admin_commands_do_not_answer_in_groups(handler, text) -> None:
    """В группе админ-команда молчит.

    Хранитель может набрать /treasury или /panel в общем чате, и раньше ответ
    уходил туда же — то есть группа читала баланс казначея, дрейф с БД и
    входящие переводы с никами отправителей. Гейт по ADMIN_IDS от этого не
    спасает: автор команды действительно хранитель.

    Проверяем фильтры из роутера приложения, а не тело хендлера: сообщение из
    группы отсекается ДО вызова, поэтому ни БД, ни сеть, ни содержимое снимка
    не затрагиваются.
    """
    filters = _registered_filters(handler)
    assert filters, f"{handler.__name__} не найден в роутере приложения"

    group_message = AiogramMessage(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1001234567890, type="supergroup"),
        from_user=None,
        text=text,
    )

    assert not any(
        _filter_matches(candidate, group_message) for candidate in filters
    ), f"{handler.__name__} не отсекает сообщения из группы"

    # В личке те же фильтры пропускают сообщение — иначе команда стала бы
    # недоступной целиком.
    private_message = AiogramMessage(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=OUTSIDER_ID, type="private"),
        from_user=None,
        text=text,
    )
    assert any(
        _filter_matches(candidate, private_message) for candidate in filters
    ), f"{handler.__name__} отсекает и личные сообщения — команда недоступна"


def _filter_matches(candidate, message) -> bool:
    """Совпадает ли один фильтр роутера с сообщением.

    Разбираем ровно два вида условий, которые использует проект: CommandFilter
    (команда) и MagicFilter (F.chat.type == ...). Неизвестный вид считаем
    непрозрачным и НЕ совпавшим: лучше лишняя проверка в тесте, чем тихо
    пропущенная регрессия.
    """
    from aiogram.filters import Command

    if isinstance(candidate, Command):
        return message.text.split()[0] in candidate.commands
    if isinstance(candidate, MagicFilter):
        resolved = candidate.resolve(message)
        return resolved is not False
    return False


async def test_admin_id_set_predicate(monkeypatch) -> None:
    """Гейт читает settings.admin_id_set из ADMIN_IDS: не-пустой исходник,
    int-нормализация, отсутствие ложного включения postgres-дефолта."""
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242")
    assert 4242 in panel_mod.settings.admin_id_set
    assert OUTSIDER_ID not in panel_mod.settings.admin_id_set
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242, 7777")
    assert panel_mod.settings.admin_id_set == {4242, 7777}


async def test_group_filters_present_on_every_admin_command() -> None:
    """Страховка от возврата: у каждой команды есть фильтр по типу чата.

    Отдельный от предыдущих тест — он не про конкретную команду, а про наличие
    фильтра вообще. Регрессия «добавили новую админ-команду и забыли фильтр»
    ловится здесь, а не поимкой на конкретном имени.
    """
    unguarded: list[str] = []
    for handler, _text in COMMAND_GUARDS:
        filters = _registered_filters(handler)
        if not any(isinstance(f, MagicFilter) for f in filters):
            unguarded.append(handler.__name__)
    assert not unguarded, f"админ-команды без фильтра по типу чата: {unguarded}"


async def test_cassette_document_rejects_outsider(monkeypatch) -> None:
    """Документ от не-админа молча отбрасывается (гейт в теле хендлера)."""
    monkeypatch.setattr(panel_mod.settings, "admin_ids", "4242")
    msg = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=OUTSIDER_ID),
        document=SimpleNamespace(file_size=1024, file_name="x.json"),
        reply=AsyncMock(),
    )
    await on_cassette_document(msg)
    assert msg.reply.await_count == 0


def test_every_admin_gated_handler_has_chat_type_filter() -> None:
    """Мета-тест guard-полноты: любой хендлер с admin-гейтом в теле обязан
    иметь фильтр по типу чата в роутере.

    Ловит регрессию «добавили хендлер с проверкой admin_id_set, но забыли
    F.chat.type == PRIVATE» — как было с on_cassette_document: хендлер ловил
    документы в группах, проверка админа стояла только внутри.

    Проверяем не просто наличие MagicFilter (F.document — тоже MagicFilter),
    а именно фильтр по chat.type: resolve на группе обязан вернуть False.

    Не-гейты (упоминают admin_id_set, но доступны игрокам) перечислены
    явно: cmd_change снимает кулдаун для хранителя, но команда /change —
    игрокская.
    """
    import inspect

    from aiogram.types import Chat as AiogramChat
    from aiogram.types import Message as AiogramMessage

    # Хендлеры, упоминающие admin_id_set, но НЕ гейтящие по нему.
    ALLOWLIST = {"cmd_change"}
    group_msg = AiogramMessage(
        message_id=1,
        date=datetime.now(UTC),
        chat=AiogramChat(id=-1001234567890, type="supergroup"),
        from_user=None,
    )
    unguarded: list[str] = []
    seen: set[str] = set()
    for observer_name, observer in app_router.observers.items():
        # Callback-хендлеры не проверяем: колбэк приходит только из инлайн-
        # клавиатуры, которую бот сам отправил в личку. Фильтр chat.type
        # нужен message-хендлерам — их пересылают/дёргают из групп.
        if observer_name != "message":
            continue
        for entry in observer.handlers:
            name = getattr(entry.callback, "__name__", None)
            if name is None or name in seen or name in ALLOWLIST:
                continue
            seen.add(name)
            try:
                src = inspect.getsource(entry.callback)
            except (OSError, TypeError):
                continue
            if "admin_id_set" not in src and "admin_ids" not in src:
                continue
            # Нужен фильтр, который отсекает группу: chat.type == private
            # (или иной chat-type фильтр, отвергающий supergroup).
            filters = _registered_filters(entry.callback)
            blocks_group = any(
                isinstance(f, MagicFilter) and f.resolve(group_msg) is False
                for f in filters
            )
            if not blocks_group:
                unguarded.append(name)
    assert not unguarded, (
        "хендлеры с admin-гейтом без фильтра, отсекающего группу: "
        f"{unguarded}"
    )


def test_magic_filter_presence_is_detectable() -> None:
    """Фильтр по типу чата — это MagicFilter; иначе проверка выше молчит."""
    probe = F.chat.type == "private"
    assert isinstance(probe, MagicFilter)
    private_message = AiogramMessage(
        message_id=3,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=None,
        text="/panel",
    )
    group_message = AiogramMessage(
        message_id=4,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup"),
        from_user=None,
        text="/panel",
    )
    assert probe.resolve(private_message) is True
    assert probe.resolve(group_message) is False
