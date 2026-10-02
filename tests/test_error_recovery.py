"""Реакция на сбои апдейтов и устойчивость /today.

«Сеть мира дрогнула» — последний рубеж: игрок должен получить честное
«не получилось», кнопка не должна висеть со спиннером, а хранитель —
узнать причину в личку без раскопок логов. /today доставляет текст дня
даже когда медиа-группа не ушла.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.db import SessionLocal
from app.handlers import bootstrap as bootstrap_mod
from app.handlers import cmd_today, handle_update_error
from app.handlers import player as player_mod
from app.models import Card, Round, RoundStatus, WinRule


def _callback_event(chat_id: int = 555_001) -> SimpleNamespace:
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type="private")),
        answer=AsyncMock(),
        data="vote:1:0",
    )
    update = SimpleNamespace(
        message=None,
        callback_query=callback,
        from_user=SimpleNamespace(id=555_900),
        update_id=777_001,
    )
    return SimpleNamespace(update=update, exception=ValueError("boom"), bot=AsyncMock())


def _message_event(chat_id: int = 555_002) -> SimpleNamespace:
    update = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type="group")),
        callback_query=None,
        from_user=SimpleNamespace(id=555_901),
        update_id=777_002,
    )
    return SimpleNamespace(update=update, exception=RuntimeError("db hiccup"), bot=AsyncMock())


def _bare_event() -> SimpleNamespace:
    """Апдейт без message/callback — kind=update, идентификаторы не выдумываем."""
    update = SimpleNamespace(message=None, callback_query=None, update_id=777_003)
    return SimpleNamespace(update=update, exception=KeyError("k"), bot=AsyncMock())


async def test_callback_error_answers_spinner_and_notifies_player(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {"ts": 0.0})
    # Алерт хранителю идёт отдельным каналом: не мешаем счётчикам игрока.
    # Хранитель должен быть задан явно: тест не зависит от ADMIN_IDS машины.
    monkeypatch.setattr(settings, "admin_ids", "42")
    sent_admin: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent_admin.append(text)

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    event = _callback_event()
    await handle_update_error(event.bot, event)
    # Кнопке сняли спиннер, игроку ушло человеческое «не получилось».
    event.update.callback_query.answer.assert_awaited_once()
    assert "Плёнка заело" in event.update.callback_query.answer.call_args.args[0]
    event.bot.send_message.assert_awaited_once()
    assert event.bot.send_message.call_args.args[0] == 555_001
    assert len(sent_admin) == 1
    assert "ValueError" in sent_admin[0]


async def test_message_error_skips_callback_answer(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {"ts": 0.0})
    async def fake_notify(bot, text) -> None:
        pass

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    event = _message_event()
    await handle_update_error(event.bot, event)
    event.bot.send_message.assert_awaited_once()
    assert "шаг не засчитан" in event.bot.send_message.call_args.args[1]


async def test_admin_alert_throttled_per_kind(monkeypatch) -> None:
    """Причина сбоя доходит хранителю, но не чаще раза в час на КИД сбоя.

    Регрессия: троттлинг на monotonic() лгал при аптайме процесса меньше
    часа (свежая перезагрузка, свежий CI-раннер) — алерты молча исчезали.
    Регресс вторая: счётчик был один на все сбои, и падение кнопки на час
    затыкало тревогу о падении сообщения — второй инцидент выглядел тишиной.
    """
    monkeypatch.setattr(settings, "admin_ids", "4242")
    sent_to_admin: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent_to_admin.append(text)

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})

    await handle_update_error(AsyncMock(), _message_event())
    assert len(sent_to_admin) == 1
    assert "RuntimeError" in sent_to_admin[0]
    assert "message" in sent_to_admin[0]

    # Второй сбой того же вида сразу же — тревога подавлена кулдауном.
    await handle_update_error(AsyncMock(), _message_event())
    assert len(sent_to_admin) == 1

    # Сбой другого вида — отдельная тревога: он не затыкается предыдущей.
    await handle_update_error(AsyncMock(), _callback_event())
    assert len(sent_to_admin) == 2
    assert "callback" in sent_to_admin[1]


async def test_admin_alert_fires_on_fresh_process_uptime(monkeypatch) -> None:
    """Регрессия «молодого» monotonic: аптайм 100 с < кулдауна не глушит алерт."""
    import time as _time

    monkeypatch.setattr(settings, "admin_ids", "4242")
    sent_to_admin: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent_to_admin.append(text)

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})
    # Аптайм-подобное маленькое значение monotonic: стеночные часы от этого
    # не зависят, поэтому алерт обязан уйти.
    monkeypatch.setattr(_time, "monotonic", lambda: 100.0)

    await handle_update_error(AsyncMock(), _message_event())
    assert len(sent_to_admin) == 1


async def test_error_log_and_alert_carry_identifiers(monkeypatch, caplog) -> None:
    """Главное: сбой опознаётся и в логе, и в тревоге — одинаковыми метками.

    Прежде строка лога была «Ошибка обработки апдейта» без единого
    идентификатора, а тревога обещала найти по ней нужный стек: при двух
    разных сбоях строки выглядели одинаково, и найти ничего нельзя.
    """
    monkeypatch.setattr(settings, "admin_ids", "42")
    sent_admin: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent_admin.append(text)

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})

    with caplog.at_level("ERROR", logger="app.handlers.bootstrap"):
        await handle_update_error(AsyncMock(), _callback_event())

    logged = [r.getMessage() for r in caplog.records if "Ошибка обработки" in r.getMessage()]
    assert logged, "сбой не попал в лог"
    for expected in ("uid=555900", "chat=555001", "update_id=777001", "kind=callback"):
        assert expected in logged[0], logged[0]
    assert len(sent_admin) == 1
    for expected in ("uid=555900", "chat=555001", "update_id=777001"):
        assert expected in sent_admin[0], sent_admin[0]
    # Тот же маркер в логе и в тревоге: ищем ровно то, что показали.
    assert "uid=555900" in logged[0] and "uid=555900" in sent_admin[0]


async def test_alert_and_log_never_agree_on_nothing(monkeypatch) -> None:
    """Апдейт без message/callback: kind=update, чат не выдумывается."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    sent_admin: list[str] = []

    async def fake_notify(bot, text) -> None:
        sent_admin.append(text)

    monkeypatch.setattr("app.ops.notify_admins", fake_notify)
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})

    await handle_update_error(AsyncMock(), _bare_event())

    assert len(sent_admin) == 1
    assert "kind=update" in sent_admin[0]
    assert "chat=?" in sent_admin[0]
    assert "update_id=777003" in sent_admin[0]


async def test_failed_player_notice_is_logged_not_swallowed(monkeypatch, caplog) -> None:
    """Неудача «сказать игроку» больше не исчезает молча — это тоже диагноз."""
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})
    event = _message_event()
    event.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

    with caplog.at_level("DEBUG", logger="app.handlers.bootstrap"):
        await handle_update_error(event.bot, event)

    assert any("сообщили о сбое" in r.getMessage() for r in caplog.records)


async def test_failed_admin_alert_is_logged_not_swallowed(monkeypatch, caplog) -> None:
    """Тревога, потерянная молча, — инцидент без следа: логируем с идентификаторами."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(
        "app.ops.notify_admins", AsyncMock(side_effect=RuntimeError("бот недоступен"))
    )
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {})

    with caplog.at_level("ERROR", logger="app.handlers.bootstrap"):
        await handle_update_error(AsyncMock(), _message_event())

    assert any("не доставлена" in r.getMessage() for r in caplog.records)
    assert any("uid=555901" in r.getMessage() for r in caplog.records)


def _transient_round(tmp_path) -> Round:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"x")
    round_row = Round(
        day_index=87_000,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День проверки /today",
        chapter_text="Текст.",


        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC) + timedelta(hours=23),
        tally_ends_at=datetime.now(UTC) + timedelta(hours=24),
    )
    for position in range(3):
        image = tmp_path / f"card{position}.jpg"
        image.write_bytes(b"x")
        round_row.cards.append(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"Путь {position}",
                description="описание",
                consequence="канон",
                tag="care",
                image_path=str(image),
            )
        )
    return round_row


async def test_today_delivers_status_text_without_media(monkeypatch, tmp_path) -> None:
    """Шаблонный день: /today шлёт текст статуса, медиа не трогает даже при сбоях."""
    round_row = _transient_round(tmp_path)

    async def fake_round():
        return round_row

    monkeypatch.setattr(player_mod, "_ensure_round", fake_round)
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=87_001, username=None, first_name="Т"),
        text="/today",
        answer_media_group=AsyncMock(side_effect=Exception("telegram media down")),
        answer=AsyncMock(),
    )
    await cmd_today(message)
    message.answer_media_group.assert_not_awaited()
    assert message.answer.await_count == 1
    status_text_sent = message.answer.call_args.args[0]
    assert "I. Путь 0" in status_text_sent


async def test_today_normal_path_sends_status(monkeypatch, tmp_path) -> None:
    round_row = _transient_round(tmp_path)

    async def fake_round():
        return round_row

    monkeypatch.setattr(player_mod, "_ensure_round", fake_round)
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=87_002, username=None, first_name="Т"),
        text="/today",
        answer_media_group=AsyncMock(),
        answer=AsyncMock(),
    )
    await cmd_today(message)
    message.answer_media_group.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_handlers_db_reachable() -> None:
    """Санити: глобальная тестовая БД жива (нужна остальным тестам модуля)."""
    async with SessionLocal() as session:
        assert session is not None
