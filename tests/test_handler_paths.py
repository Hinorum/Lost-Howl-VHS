"""Слепые зоны хендлеров: /finalize, /dispute, сценарии пульта, тексты кошелька.

Раньше эти ветки не выполнялись ни одним тестом: полная диагностика /finalize,
админ-глаголы /dispute (включая краш «resolve без id»), кнопки пульта
(▶️ Сегодня / 💰 Кошелёк / 🏆 Копилки / 🐾 Фонд / ❓ Помощь), сборщики
текстов фонда/копилок и внутренности пульта Хранителя (версия игры,
пауза/возобновление, кассеты, двойные тапы подтверждения).
Фейки сообщений — по образцу test_admin_guard.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from conftest import ensure_player
from sqlalchemy import delete, select

import app.handlers.panel as panel_mod
import app.handlers.player as player_mod
import app.ton_pay as ton_pay_mod
from app.config import settings
from app.db import SessionLocal
from app.disputes import open_dispute
from app.handlers.admin import cmd_dispute, cmd_finalize
from app.handlers.panel import _cassette_menu_text, cmd_panel, on_panel_action
from app.handlers.player import on_menu
from app.handlers.wallet import _fund_text, _top_text, cmd_fund
from app.models import (
    Card,
    Dispute,
    LeaderboardPot,
    PackFund,
    PackFundLedger,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WalletDialog,
    WatcherState,
    WeeklyPot,
    WinRule,
)
from app.ops import money_mode_enabled, set_money_mode
from app.story.bay import LibraryEntry
from app.story.bay import list_cassettes as real_list_cassettes
from app.ton_utils import to_nano

ADMIN_ID = 7777


def _admin_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=ADMIN_ID),
        text=text,
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


def _answers(msg: SimpleNamespace) -> list[str]:
    return [call.args[0] for call in msg.answer.await_args_list]


def _menu_callback(data: str, *, uid: int = 88001, chat_type: str = "private", with_message: bool = True):
    message = None
    if with_message:
        message = SimpleNamespace(
            answer=AsyncMock(),
            chat=SimpleNamespace(type=chat_type),
        )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid, username=None, first_name="Тест"),
        answer=AsyncMock(),
        message=message,
    )


async def _seed_day(
    round_id: int,
    *,
    finalized: bool = False,
    with_payout: bool = False,
) -> None:
    """Закрытый раунд с голосами, ставкой победителя и (опц.) старой выплатой."""
    now = datetime.now(UTC)
    await ensure_player(770011)
    await ensure_player(770012)
    async with SessionLocal() as db:
        db.add(
            Round(
                id=round_id,
                day_index=round_id,
                status=RoundStatus.CLOSED,
                payouts_finalized=finalized,
                win_rule=WinRule.MAJORITY,
                chapter_title="Тест",
                chapter_text="Текст",
                opens_at=now,
                voting_ends_at=now,
                tally_ends_at=now,
                winner_card=0,
                vote_counts_json='{"0":2,"1":1}',
            )
        )
        db.add(Vote(round_id=round_id, player_id=770011, card_position=0))
        db.add(Vote(round_id=round_id, player_id=770012, card_position=1))
        db.add(
            Stake(
                round_id=round_id,
                player_id=770011,
                amount_nanotons=to_nano(10),
                tx_hash=f"hp:{round_id}",
                status="confirmed",
            )
        )
        if with_payout:
            db.add(
                Payout(
                    round_id=round_id,
                    player_id=770011,
                    kind="prize",
                    amount_nanotons=to_nano(1),
                    dest_address="wallet-770011",
                )
            )
        await db.commit()


# ---------------------------------------------------------------- /finalize


async def test_finalize_reports_missing_day(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    msg = _admin_message("/finalize 779999")
    await cmd_finalize(msg)
    assert any("День 779999 не найден." in text for text in _answers(msg))


async def test_finalize_diagnoses_and_finalizes_day(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    monkeypatch.setattr(ton_pay_mod, "dispatch_pending_payouts", AsyncMock(return_value=0))
    await _seed_day(770001, with_payout=True)

    msg = _admin_message("/finalize 770001")
    await cmd_finalize(msg)
    text = "\n".join(_answers(msg))

    # Диагностическая таблица: голоса, ставки, победитель, старые выплаты.
    assert "День 770001 (Round#770001)" in text
    assert "Голоса:" in text
    assert "✅ПОБЕДА" in text
    assert "Победители со ставкой: 1, сумма ставок:" in text
    assert "Выплаты (" in text
    # Сама финализация и отправка.
    assert "создано выплат" in text
    assert "Отправлено: 0" in text


async def test_finalize_says_nothing_when_already_finalized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    monkeypatch.setattr(ton_pay_mod, "dispatch_pending_payouts", AsyncMock(return_value=0))
    await _seed_day(770002, finalized=True)

    msg = _admin_message("/finalize 770002")
    await cmd_finalize(msg)
    text = "\n".join(_answers(msg))
    assert "Незавершённых дней нет" in text


# ---------------------------------------------------------------- /dispute


async def test_dispute_open_validates_arguments(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    await ensure_player(88001)

    msg = _admin_message("/dispute open 779999")
    await cmd_dispute(msg)
    assert any("Формат: /dispute open" in text for text in _answers(msg))

    msg = _admin_message("/dispute open abc 88001 опоздал")
    await cmd_dispute(msg)
    assert any("Номер дня должен быть целым числом." in text for text in _answers(msg))

    msg = _admin_message("/dispute open 779999 @netakogo нет-такого")
    await cmd_dispute(msg)
    assert any("Игрок не найден (id или @ник)." in text for text in _answers(msg))


async def test_dispute_open_creates_record(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    await ensure_player(88001)

    msg = _admin_message("/dispute open 779999 88001 ставка не дошла")
    await cmd_dispute(msg)
    assert any("спор открыт и ждёт рассмотрения хранителем" in text for text in _answers(msg))
    async with SessionLocal() as db:
        row = (
            await db.execute(select(Dispute).where(Dispute.player_id == 88001))
        ).scalar_one_or_none()
    assert row is not None and row.status == "open"
    assert row.reason == "ставка не дошла"


async def test_dispute_resolve_without_id_shows_format(monkeypatch) -> None:
    """Без id раньше команда падала IndexError: guard пропускал parts[2]."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))

    msg = _admin_message("/dispute resolve")
    await cmd_dispute(msg)
    assert any("Формат: /dispute resolve <id> [примечание]" in text for text in _answers(msg))

    msg = _admin_message("/dispute resolve abc")
    await cmd_dispute(msg)
    assert any("Номер спора должен быть целым числом." in text for text in _answers(msg))

    msg = _admin_message("/dispute resolve 999999")
    await cmd_dispute(msg)
    assert any("нет такого спора" in text for text in _answers(msg))


async def test_dispute_resolve_and_reject_settle(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    await ensure_player(88002)
    async with SessionLocal() as db:
        await open_dispute(db, 779999, 88002, "первый спор")
        dispute_id = (
            await db.execute(select(Dispute.id).where(Dispute.player_id == 88002))
        ).scalar_one()

    msg = _admin_message(f"/dispute resolve {dispute_id} разобрано")
    await cmd_dispute(msg)
    assert any(f"спор #{dispute_id} resolved" in text for text in _answers(msg))

    # Повторный resolve — спор уже закрыт.
    msg = _admin_message(f"/dispute resolve {dispute_id}")
    await cmd_dispute(msg)
    assert any("уже закрыт" in text for text in _answers(msg))


async def test_dispute_compensate_requires_verified_wallet(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    await ensure_player(88003)
    async with SessionLocal() as db:
        await open_dispute(db, 779999, 88003, "без кошелька")
        dispute_id = (
            await db.execute(select(Dispute.id).where(Dispute.player_id == 88003))
        ).scalar_one()

    msg = _admin_message(f"/dispute compensate {dispute_id}")
    await cmd_dispute(msg)
    assert any("Формат: /dispute compensate" in text for text in _answers(msg))

    msg = _admin_message("/dispute compensate abc 1.5")
    await cmd_dispute(msg)
    assert any("Номер спора должен быть целым числом." in text for text in _answers(msg))

    msg = _admin_message(f"/dispute compensate {dispute_id} 1.5")
    await cmd_dispute(msg)
    assert any("у игрока не привязан кошелёк" in text for text in _answers(msg))


async def test_dispute_compensate_pays_out(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    await ensure_player(88004)
    async with SessionLocal() as db:
        player = await db.get(Player, 88004)
        player.wallet_address = "UQ_test_wallet"
        player.wallet_verified = True
        await open_dispute(db, 779999, 88004, "с кошельком")
        dispute_id = (
            await db.execute(select(Dispute.id).where(Dispute.player_id == 88004))
        ).scalar_one()
        await db.commit()

    # Нечисловая сумма при живом кошельке: спор остаётся открытым.
    msg = _admin_message(f"/dispute compensate {dispute_id} abc")
    await cmd_dispute(msg)
    assert any("сумма должна быть числом в Gram" in text for text in _answers(msg))

    msg = _admin_message(f"/dispute compensate {dispute_id} 1.5 за хвост")
    await cmd_dispute(msg)
    assert any(
        f"компенсация 1.5 Gram поставлена в очередь (спор #{dispute_id})" in text
        for text in _answers(msg)
    )
    async with SessionLocal() as db:
        payout = (
            await db.execute(select(Payout).where(Payout.player_id == 88004))
        ).scalar_one()
    assert payout.kind == "dispute"
    assert payout.amount_nanotons == to_nano(1.5)


# ---------------------------------------------------------------- пульт (меню)


async def test_menu_today_reposts_day_with_buttons() -> None:
    callback = _menu_callback("menu:today")
    await on_menu(callback)
    callback.message.answer.assert_awaited_once()
    (text,), kwargs = callback.message.answer.await_args
    assert "Голосование" in text
    assert kwargs.get("parse_mode") == "HTML"
    assert kwargs.get("reply_markup") is not None
    callback.answer.assert_awaited_once()


async def test_menu_today_without_message_stays_silent() -> None:
    callback = _menu_callback("menu:today", with_message=False)
    await on_menu(callback)
    callback.answer.assert_awaited_once()


async def test_menu_wallet_in_group_points_to_dm() -> None:
    callback = _menu_callback("menu:wallet", chat_type="supergroup")
    await on_menu(callback)
    args, kwargs = callback.answer.await_args
    assert "Кошелёк — личное" in args[0]
    assert kwargs.get("show_alert") is True
    callback.message.answer.assert_not_awaited()


async def test_menu_wallet_private_starts_bind_dialog() -> None:
    callback = _menu_callback("menu:wallet", uid=88101)
    await on_menu(callback)
    callback.message.answer.assert_awaited_once()
    async with SessionLocal() as db:
        dialog = await db.get(WalletDialog, 88101)
    assert dialog is not None


async def test_menu_wallet_private_bound_shows_view(monkeypatch) -> None:
    await ensure_player(88102)
    async with SessionLocal() as db:
        player = await db.get(Player, 88102)
        player.wallet_address = "UQ_menu_wallet_88102"
        player.wallet_verified = True
        await db.commit()
    callback = _menu_callback("menu:wallet", uid=88102)
    await on_menu(callback)
    callback.message.answer.assert_awaited_once()
    (text,), kwargs = callback.message.answer.await_args
    # Адрес показывается через friendly_address (маска), сверяем канонику вида.
    assert "Привязанный кошелёк" in text
    assert "Кошелёк подтверждён" in text
    assert kwargs.get("parse_mode") == "HTML"


async def _wipe_rounds() -> None:
    """Чистый игровой стейт: голоса/ставки/выплаты/раунды/копилки.

    Нужен детерминизм «пустой недели» в текстах копилок: голоса из
    предыдущих тестов модуля попадают в текущую неделю ранга.
    """
    async with SessionLocal() as db:
        for model in (Vote, Stake, Payout, Card, Round, WeeklyPot, LeaderboardPot):
            await db.execute(delete(model))
        await db.commit()


async def test_menu_top_fund_help_render() -> None:
    await _wipe_rounds()
    callback = _menu_callback("menu:top")
    await on_menu(callback)
    (top_text,), _ = callback.message.answer.await_args
    assert "Копилка недели" in top_text
    assert "Верных сцен на этой неделе ещё нет" in top_text

    callback = _menu_callback("menu:fund")
    await on_menu(callback)
    (fund_text,), kwargs = callback.message.answer.await_args
    assert "Фонд Стаи" in fund_text
    assert kwargs.get("parse_mode") == "HTML"

    callback = _menu_callback("menu:help")
    await on_menu(callback)
    (help_text,), kwargs = callback.message.answer.await_args
    assert settings.world_name in help_text
    assert kwargs.get("reply_markup") is not None


async def test_menu_unknown_action_is_acknowledged() -> None:
    callback = _menu_callback("menu:bogus")
    await on_menu(callback)
    callback.answer.assert_awaited_once()
    callback.message.answer.assert_not_awaited()


async def test_menu_crash_shows_retry_alert(monkeypatch) -> None:
    monkeypatch.setattr(player_mod, "_menu_top", AsyncMock(side_effect=RuntimeError("boom")))
    callback = _menu_callback("menu:top")
    await on_menu(callback)
    args, kwargs = callback.answer.await_args
    assert "Что-то щёлкнуло" in args[0]
    assert kwargs.get("show_alert") is True


# ---------------------------------------------------------------- тексты кошелька


async def test_fund_text_empty_journal() -> None:
    async with SessionLocal() as db:
        await db.execute(delete(PackFundLedger))
        await db.execute(delete(PackFund))
        await db.commit()
    text = await _fund_text()
    assert "Фонд Стаи на плёнке" in text
    assert "— пока пусто —" in text


async def test_fund_text_lists_journal_entries() -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await db.execute(delete(PackFundLedger))
        await db.execute(delete(PackFund))
        db.add(PackFund(nanotons=to_nano(12.5)))
        db.add(
            PackFundLedger(
                entry_type="in",
                amount_nanotons=to_nano(2),
                round_id=7,
                note="дневное начисление",
                created_at=now,
            )
        )
        db.add(
            PackFundLedger(
                entry_type="out",
                amount_nanotons=to_nano(1.5),
                round_id=None,
                note="разыграно в чате",
                created_at=now,
            )
        )
        await db.commit()
    text = await _fund_text()
    assert "12.50 Gram" in text
    assert "+2 Gram" in text
    assert "−1.5 Gram" in text
    assert "день 7" in text


async def test_cmd_fund_answers_html_text() -> None:
    msg = SimpleNamespace(answer=AsyncMock())
    await cmd_fund(msg)
    msg.answer.assert_awaited_once()
    (text,), kwargs = msg.answer.await_args
    assert "Фонд Стаи" in text
    assert kwargs.get("parse_mode") == "HTML"


async def test_top_text_empty_state() -> None:
    await _wipe_rounds()
    text = await _top_text()
    assert "Копилка недели" in text
    assert "Верных сцен на этой неделе ещё нет" in text
    assert "Копилка месяца" in text


# ---------------------------------------------------------------- пульт Хранителя


def _panel_callback(data: str, *, uid: int = ADMIN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid, username=None, first_name="Хранитель"),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        bot=AsyncMock(),
    )


async def _clear_confirm_keys() -> None:
    async with SessionLocal() as db:
        await db.execute(
            delete(WatcherState).where(
                WatcherState.key.like(f"panel_confirm:{ADMIN_ID}:%")
            )
        )
        await db.commit()


async def test_cmd_panel_renders_for_keeper(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    msg = _admin_message("/panel")
    await cmd_panel(msg)
    msg.answer.assert_awaited_once()
    (text,), kwargs = msg.answer.await_args
    assert text
    assert kwargs.get("parse_mode") == "HTML"
    assert kwargs.get("reply_markup") is not None


async def test_panel_refunds_and_adjust_buttons(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    cb = _panel_callback("panel:refunds")
    await on_panel_action(cb)
    cb.message.answer.assert_awaited_once()
    cb.answer.assert_awaited_with("Возвраты ниже.")

    cb = _panel_callback("panel:adjust")
    await on_panel_action(cb)
    (text,), kwargs = cb.message.answer.await_args
    assert kwargs.get("parse_mode") == "HTML"
    assert kwargs.get("reply_markup") is not None
    cb.answer.assert_awaited_once()


async def test_panel_treasury_and_revenue_buttons(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    cb = _panel_callback("panel:treasury")
    await on_panel_action(cb)
    (text,), kwargs = cb.message.answer.await_args
    assert kwargs.get("parse_mode") == "HTML"
    cb.answer.assert_awaited_once()

    cb = _panel_callback("panel:revenue")
    await on_panel_action(cb)
    cb.message.answer.assert_awaited_once()
    cb.answer.assert_awaited_once()


async def test_panel_cassettes_menu_real_library(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    monkeypatch.setattr(
        panel_mod, "get_next_cassette", AsyncMock(return_value=None)
    )
    cb = _panel_callback("panel:cassettes")
    await on_panel_action(cb)
    (text,), kwargs = cb.message.answer.await_args
    assert "📼 <b>КАССЕТЫ</b>" in text
    # Текущий месяц в библиотеке — октябрьская кассета, она помечена играющей.
    assert "▶ играется" in text
    assert kwargs.get("reply_markup") is not None
    cb.answer.assert_awaited_once()


async def test_panel_cassette_menu_empty_and_invalid(monkeypatch) -> None:
    monkeypatch.setattr(panel_mod, "list_cassettes", lambda: [])
    async with SessionLocal() as session:
        text = await _cassette_menu_text(session)
    assert "Библиотека пуста: валидных кассет в каталоге нет." in text

    monkeypatch.setattr(
        panel_mod,
        "list_cassettes",
        lambda: [LibraryEntry("битая.json", None, ["нет ни одного дня"], [])],
    )
    async with SessionLocal() as session:
        text = await _cassette_menu_text(session)
    assert "❌ <b>битая.json</b> — невалидна: нет ни одного дня" in text


async def test_panel_cassette_menu_marks_assigned(monkeypatch) -> None:
    entry = next(e for e in real_list_cassettes() if e.cassette is not None)
    monkeypatch.setattr(panel_mod, "list_cassettes", lambda: [entry])
    monkeypatch.setattr(
        panel_mod, "get_next_cassette", AsyncMock(return_value=entry.file_name)
    )
    async with SessionLocal() as session:
        text = await _cassette_menu_text(session)
    assert "🟢 назначена" in text


async def test_panel_money_version_double_tap(monkeypatch) -> None:
    """Версия игры: первый тап предупреждает, второй меняет; правка-сбой — не краш."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    monkeypatch.setattr(
        panel_mod, "_admin_panel_text", AsyncMock(return_value="🎛 ПУЛЬТ")
    )
    await _clear_confirm_keys()
    async with SessionLocal() as session:
        original = await money_mode_enabled(session)

    # A: меняющее направление; edit падает «message is not modified» → Готово.
    action_a = "now" if original else "money"
    cb = _panel_callback(f"panel:{action_a}")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True and "ещё раз" in args[0]

    cb = _panel_callback(f"panel:{action_a}")
    cb.message.edit_text.side_effect = TelegramBadRequest(
        None, "Bad Request: message is not modified"
    )
    await on_panel_action(cb)
    cb.answer.assert_awaited_with("Готово.")

    # B: то же направление повторно → состояние уже стоит.
    cb = _panel_callback(f"panel:{action_a}")
    await on_panel_action(cb)
    cb = _panel_callback(f"panel:{action_a}")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "Версия уже" in args[0]

    # C: обратное направление; иная ошибка правки всплывает в обработчик.
    action_c = "money" if action_a == "now" else "now"
    cb = _panel_callback(f"panel:{action_c}")
    await on_panel_action(cb)
    cb = _panel_callback(f"panel:{action_c}")
    cb.message.edit_text.side_effect = TelegramBadRequest(None, "chat not found")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert "Не получилось" in args[0]

    # D: меняющее направление с успешной правкой текста.
    cb = _panel_callback(f"panel:{action_a}")
    await on_panel_action(cb)
    cb = _panel_callback(f"panel:{action_a}")
    await on_panel_action(cb)
    cb.answer.assert_awaited_with("Готово.")
    cb.message.edit_text.assert_awaited_once()

    async with SessionLocal() as session:
        # D перевёл состояние в противоположное — возвращаем исходное.
        assert await set_money_mode(session, original)


async def test_panel_pause_resume_double_tap(monkeypatch) -> None:
    """Стоп-кран: предупреждение, смена состояния, протухание и сбои правки."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    monkeypatch.setattr(
        panel_mod, "_admin_panel_text", AsyncMock(return_value="🎛 ПУЛЬТ")
    )
    broadcast = AsyncMock(return_value=(True, 0))
    monkeypatch.setattr(panel_mod, "_set_paused_and_broadcast", broadcast)
    await _clear_confirm_keys()

    # пауза: первый тап — только предупреждение
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "ещё раз" in args[0]
    broadcast.assert_not_awaited()

    # второй тап: правка падает «message is not modified» — молчаливое Готово
    cb = _panel_callback("panel:pause")
    cb.message.edit_text.side_effect = TelegramBadRequest(
        None, "Bad Request: message is not modified"
    )
    await on_panel_action(cb)
    cb.answer.assert_awaited_with("Готово.")
    broadcast.assert_awaited_with(cb.bot, True, "технические работы")

    # состояние уже стоит → короткий ответ без правки
    broadcast.return_value = (False, None)
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "Игра уже на паузе." in args[0]

    # иная ошибка правки всплывает в общий обработчик
    broadcast.return_value = (True, 0)
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    cb = _panel_callback("panel:pause")
    cb.message.edit_text.side_effect = TelegramBadRequest(None, "chat not found")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert "Не получилось" in args[0]

    # возобновление: успешная правка со сводкой
    cb = _panel_callback("panel:resume")
    await on_panel_action(cb)
    cb = _panel_callback("panel:resume")
    await on_panel_action(cb)
    cb.answer.assert_awaited_with("Готово.")
    (text,), _ = cb.message.edit_text.await_args
    assert "Игра возобновляется" in text

    # и так идёт → только ответ
    broadcast.return_value = (False, None)
    cb = _panel_callback("panel:resume")
    await on_panel_action(cb)
    cb = _panel_callback("panel:resume")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert "Игра и так идёт." in args[0]


async def test_panel_confirm_survives_corrupt_marker(monkeypatch) -> None:
    """Битая/наивная отметка подтверждения не ломает первый тап."""
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    key = f"panel_confirm:{ADMIN_ID}:pause"
    async with SessionLocal() as db:
        await db.execute(delete(WatcherState).where(WatcherState.key == key))
        db.add(WatcherState(key=key, value='{"нет_даты": 1}'))
        await db.commit()
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True  # KeyError по created_at — не краш

    async with SessionLocal() as db:
        row = await db.get(WatcherState, key)
        row.value = '{"created_at": "2020-01-01T00:00:00"}'
        await db.commit()
    cb = _panel_callback("panel:pause")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert kwargs.get("show_alert") is True  # наивная дата: tzinfo + протухание


async def test_panel_view_other_edit_error_bubbles(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    cb = _panel_callback("panel:view")
    cb.message.edit_text.side_effect = TelegramBadRequest(None, "chat not found")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert "Не получилось" in args[0]
    assert kwargs.get("show_alert") is True


async def test_panel_unknown_action_answers_alert(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    cb = _panel_callback("panel:bogus")
    await on_panel_action(cb)
    args, kwargs = cb.answer.await_args
    assert "Неизвестное действие." in args[0]
    assert kwargs.get("show_alert") is True
