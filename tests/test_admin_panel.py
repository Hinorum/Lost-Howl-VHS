"""Пульт хранителя (/panel): состояние игры + справочник команд.

Админ должен видеть банк дня, очередь выплат и здоровье watcher'а одним
нажатием, с кнопкой обновления — без раскрытия игрокам.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.handlers import _admin_panel_text, cmd_panel, on_panel_action
from app.handlers import panel as panel_mod
from app.models import Payout, Player, Round, RoundStatus, Stake, WinRule


def make_message(uid: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text="/panel",
        answer=AsyncMock(),
    )


def make_callback(uid: int) -> SimpleNamespace:
    return SimpleNamespace(
        data="panel:view",
        from_user=SimpleNamespace(id=uid),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            chat=SimpleNamespace(type="private"),
        ),
        answer=AsyncMock(),
    )


async def test_panel_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    outsider = make_message(1)
    await cmd_panel(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]


async def test_panel_builder_contains_core_sections(session, monkeypatch) -> None:
    """Панель читает ГЛОБАЛЬНУЮ базу (как прод): сеем туда и чистим после."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    monkeypatch.setattr(settings, "ton_enabled", True)
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        db.add(Round(
            day_index=97_500,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="x",

            opens_at=now,
            voting_ends_at=now + timedelta(hours=10),
            tally_ends_at=now + timedelta(hours=11),

        ))
        db.add(Payout(
            round_id=None,
            kind="prize",
            amount_nanotons=300_000_000,
            dest_address="0:" + "ab" * 32,
            status="failed",
            attempts=5,
            last_error="have no alive peers: задай LITESERVER_CONFIG_URL"[:200],
        ))
        await db.commit()
    async with SessionLocal() as g:
        pot_round = (
            await g.execute(select(Round).where(Round.day_index == 97_500))
        ).scalar_one()
        # Банк дня живёт в БД, а не в in-memory кэше: сеем ставки напрямую.
        g.add_all(
            [
                Stake(round_id=pot_round.id, player_id=97_510, amount_nanotons=1_000_000_000, tx_hash="pn-a", status="confirmed"),
                Stake(round_id=pot_round.id, player_id=97_511, amount_nanotons=250_000_000, tx_hash="pn-b", status="confirmed"),
                Stake(round_id=pot_round.id, player_id=97_512, amount_nanotons=999_999_999, tx_hash="pn-c", status="rejected"),
            ]
        )
        await g.commit()

    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "ПУЛЬТ ХРАНИТЕЛЯ" in text
        assert "День 97500 · open" in text
        assert "Банк дня: 1.25 Gram · ставок 3" in text
        assert "Выплаты:" in text and "failed 1" in text
        assert "#1" in text or "#2" in text  # топ долгов в панели
        assert "LITESERVER_CONFIG_URL" in text  # причина видна прямо тут
        assert "/resetgame confirm" in text  # справочник команд на месте
    finally:
        from sqlalchemy import delete as _d

        async with SessionLocal() as db:
            await db.execute(_d(Payout).where(Payout.status == "failed"))
            await db.execute(_d(Stake).where(Stake.round_id == pot_round.id))
            await db.execute(_d(Round).where(Round.day_index == 97_500))
            await db.commit()


async def test_panel_shows_prizes_waiting_for_wallet(session, monkeypatch) -> None:
    """Призы без кошелька видны в пульте: ждут привязки, уйдут сами."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as db:
        db.add(Player(id=975_011, username="late", wallet_address=""))
        db.add(Payout(
            round_id=None,
            player_id=975_011,
            kind="prize",
            amount_nanotons=100_000_000,
            dest_address="",
            status="pending",
        ))
        await db.commit()

    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "Призов без кошелька: 1" in text
        assert "/payouts" in text
    finally:
        from sqlalchemy import delete as _d

        async with SessionLocal() as db:
            await db.execute(_d(Payout).where(Payout.player_id == 975_011))
            await db.execute(_d(Player).where(Player.id == 975_011))
            await db.commit()


async def test_panel_refresh_callback_edits_for_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    callback = make_callback(4242)
    callback.data = "panel:view"

    async def fake_text(session=None):
        return "🎛 ПУЛЬТ ХРАНИТЕЛЯ"

    monkeypatch.setattr(panel_mod, "_admin_panel_text", fake_text)
    await on_panel_action(callback)
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()

    # Не-хранителю обновление закрыто.
    outsider = make_callback(1)
    outsider.data = "panel:view"
    await on_panel_action(outsider)
    args, kwargs = outsider.answer.call_args
    assert kwargs.get("show_alert") is True


async def test_panel_payouts_button_sends_listing(monkeypatch) -> None:
    from sqlalchemy import delete as _d

    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as db:
        db.add(Payout(
            round_id=None,
            kind="prize",
            amount_nanotons=150_000_000,
            dest_address="0:" + "cd" * 32,
            status="pending",
        ))
        await db.commit()
    try:
        callback = make_callback(4242)
        callback.data = "panel:payouts"
        await on_panel_action(callback)
        texts = [c.args[0] for c in callback.message.answer.await_args_list if c.args]
        assert any("#" in t and "pending" in t for t in texts)
        callback.answer.assert_awaited_with("Список ниже.")
    finally:
        async with SessionLocal() as db:
            await db.execute(_d(Payout).where(Payout.status == "pending"))
            await db.commit()


async def test_panel_advance_requires_double_press(monkeypatch) -> None:
    """Досрочное закрытие дня — с подтверждением: первый тап только предупреждает."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    advance_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(panel_mod, "cmd_advance", advance_mock)

    callback = make_callback(4242)
    callback.data = "panel:advance"
    await on_panel_action(callback)
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "ещё раз" in args[0]
    assert advance_mock.await_count == 0  # действия ещё не было


async def test_panel_advance_go_runs_command(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    calls: list = []

    async def fake_advance(shim_message):
        calls.append(shim_message)
        await shim_message.answer("День 98001 открыт.")

    monkeypatch.setattr(panel_mod, "cmd_advance", fake_advance)

    async def fake_text(session=None):
        return "🎛 ПУЛЬТ ХРАНИТЕЛЯ"

    monkeypatch.setattr(panel_mod, "_admin_panel_text", fake_text)
    callback = make_callback(4242)
    callback.data = "panel:advance:go"
    callback.bot = AsyncMock()
    await on_panel_action(callback)
    assert len(calls) == 1
    edited = callback.message.edit_text.call_args
    assert edited is not None
    assert "День 98001 открыт." in edited.args[0]
    callback.answer.assert_awaited_with("День переключён.")


async def test_panel_shows_payout_breakdown_and_unprocessed(session, monkeypatch) -> None:
    """Пульт делит очередь по типу и показывает необработанные переводы."""
    from sqlalchemy import delete as _d

    monkeypatch.setattr(settings, "admin_ids", "4242")
    monkeypatch.setattr(settings, "ton_enabled", True)
    now = datetime.now(UTC)
    uid = 960_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + "ef" * 32
    async with SessionLocal() as db:
        db.add(Player(id=uid, username=f"u{uid}", wallet_address=wallet))
        rnd = Round(
            day_index=97_600, status=RoundStatus.OPEN, win_rule=WinRule.MAJORITY,
            chapter_title="t", chapter_text="x",
            opens_at=now, voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        db.add(rnd)
        await db.flush()
        db.add_all(
            [
                Payout(round_id=None, player_id=uid, kind="refund",
                       amount_nanotons=120_000_000, dest_address=wallet, status="pending"),
                Payout(round_id=None, player_id=uid, kind="prize",
                       amount_nanotons=200_000_000, dest_address=wallet, status="pending"),
                Stake(round_id=rnd.id, player_id=uid, amount_nanotons=90_000_000,
                      tx_hash="panel-tx", status="pending"),
            ]
        )
        await db.commit()
    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "ждёт возвратов" in text
        assert "ждёт призов" in text
        assert "Переводов не обработано: 1" in text
    finally:
        async with SessionLocal() as db:
            await db.execute(_d(Stake).where(Stake.player_id == uid))
            await db.execute(_d(Payout).where(Payout.player_id == uid))
            await db.execute(_d(Round).where(Round.day_index == 97_600))
            await db.execute(_d(Player).where(Player.id == uid))
            await db.commit()


async def test_panel_shows_tick_failures_and_problems(monkeypatch) -> None:
    """Пульт показывает падения тика и живые тревоги, а не только счётчики.

    Раньше снимок эти два поля отдавал (/health их видел), но пульт их не
    печатал: владелец смотрел на «пульт в порядке» при падающем тике и не
    знал, что именно тревожит, пока не открывал /ops. Поле называется
    last_tick_age, а не last_tick_age_seconds, — это имя снимка.
    """
    from sqlalchemy import delete as _d

    from app.core.registry import OPS_PROBLEMS_KEY, TICK_FAIL_KEY
    from app.models import WatcherState

    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as db:
        await db.execute(_d(WatcherState).where(
            WatcherState.key.in_([OPS_PROBLEMS_KEY, TICK_FAIL_KEY])
        ))
        db.add(WatcherState(key=TICK_FAIL_KEY, value="4"))
        db.add(WatcherState(
            key=OPS_PROBLEMS_KEY,
            value=json.dumps(["очередь выплат стоит 31 мин"], ensure_ascii=False),
        ))
        await db.commit()
    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "Тик падал 4 раз" in text
        assert "очередь выплат стоит 31 мин" in text
    finally:
        async with SessionLocal() as db:
            await db.execute(_d(WatcherState).where(
                WatcherState.key.in_([OPS_PROBLEMS_KEY, TICK_FAIL_KEY])
            ))
            await db.commit()


async def test_panel_truncates_long_problem_list(monkeypatch) -> None:
    """Длинный список тревог обрезается честно: сказано, сколько не показано.

    Молчаливое усечение выглядит как «проблем ровно три», и владелец
    заключает, что разобрался, не увидев остальные.
    """
    from sqlalchemy import delete as _d

    from app.core.registry import OPS_PROBLEMS_KEY
    from app.models import WatcherState

    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as db:
        await db.execute(_d(WatcherState).where(WatcherState.key == OPS_PROBLEMS_KEY))
        db.add(WatcherState(
            key=OPS_PROBLEMS_KEY,
            value=json.dumps([f"проблема номер {n}" for n in range(1, 8)]),
        ))
        await db.commit()
    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "проблема номер 1" in text
        assert "проблема номер 4" not in text
        assert "ещё 4" in text
    finally:
        async with SessionLocal() as db:
            await db.execute(_d(WatcherState).where(WatcherState.key == OPS_PROBLEMS_KEY))
            await db.commit()


async def test_panel_stakes_button_lists_unprocessed(monkeypatch) -> None:
    """Кнопка «Ставки» на пульте показывает необработанное и статусы ставок."""
    from sqlalchemy import delete as _d

    monkeypatch.setattr(settings, "admin_ids", "4242")
    now = datetime.now(UTC)
    uid = 961_000 + int.from_bytes(os.urandom(2), "big")
    async with SessionLocal() as db:
        db.add(Player(id=uid, username="staker"))
        rnd = Round(
            day_index=97_601, status=RoundStatus.OPEN, win_rule=WinRule.MAJORITY,
            chapter_title="t", chapter_text="x",
            opens_at=now, voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        db.add(rnd)
        await db.flush()
        db.add(Stake(round_id=rnd.id, player_id=uid, amount_nanotons=70_000_000,
                     tx_hash="panel-stake-tx", status="pending"))
        await db.commit()
    try:
        callback = make_callback(4242)
        callback.data = "panel:stakes"
        await on_panel_action(callback)
        texts = [c.args[0] for c in callback.message.answer.await_args_list if c.args]
        assert any("СТАВКИ ХРАНИТЕЛЮ" in t for t in texts)
        assert any("Необработанных переводов-ставок: 1" in t for t in texts)
        assert any("staker" in t for t in texts)
        callback.answer.assert_awaited_with("Ставки ниже.")
    finally:
        async with SessionLocal() as db:
            await db.execute(_d(Stake).where(Stake.player_id == uid))
            await db.execute(_d(Round).where(Round.day_index == 97_601))
            await db.execute(_d(Player).where(Player.id == uid))
            await db.commit()


def test_report_text_keeps_verdict_and_tail_when_overlong() -> None:
    """Обрезка отчёта: вердикт «✅» и финальный статус переживают, режется середина."""
    lines = [f"Замечание №{i}: " + "x" * 120 for i in range(60)]
    lines.append("Проверено — файл не изменён.")
    report = panel_mod._report_text("✅ ", lines)
    assert len(report) <= 4000
    assert report.startswith("✅ ")
    assert report.endswith("Проверено — файл не изменён.")
    assert "…" in report


def test_report_text_short_report_untouched() -> None:
    lines = ["Сценарий mel.json принят.", "Проверено — файл не изменён."]
    assert panel_mod._report_text("✅ ", lines) == "✅ " + "\n".join(lines)
