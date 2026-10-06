"""Пульт хранителя: один ответ на вопрос «что сломано прямо сейчас».

До этого файла список тревог жил исключительно в логе планировщика, а /health
отдавал другой набор чисел. Один вопрос — два разных ответа, и ни один из них
не отличал «всё хорошо» от «проверки давно не идти, поэтому всё хорошо».
Теперь вердикт check_anomalies кэшируется sweeper'ом, и /health, и /ops
читают один и тот же снимок вместе с его возрастом.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app import broadcast as bc
from app import ops
from app.config import settings
from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY, OPS_PROBLEMS_AT_KEY, OPS_PROBLEMS_KEY
from app.db import SessionLocal
from app.handlers import ops_diag
from app.models import Payout, Round, RoundStatus, WatcherState, WinRule


@pytest.fixture(autouse=True)
async def _clean_money_tables():
    """Пульт читает очередь выплат и текущий день напрямую, поэтому чистим их.

    Общая очистка в conftest живёт на уровне модуля, а тревоги и «чистое»
    состояние сопоставимы только если каждый тест начинает с нуля.
    """
    async with SessionLocal() as db:
        await db.execute(delete(Payout))
        await db.execute(delete(Round))
        await db.commit()
    yield


async def _seed_payout(status: str, kind: str, age_minutes: int, amount: int = 1_000_000_000) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        round_row = Round(
            day_index=96_100,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="День проверки пульта",
            chapter_text="Текст дня.",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        db.add(round_row)
        await db.flush()
        db.add(
            Payout(
                round_id=round_row.id,
                player_id=1,
                kind=kind,
                status=status,
                amount_nanotons=amount,
                dest_address="UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                created_at=now - timedelta(minutes=age_minutes),
            )
        )
        await db.commit()


# ---------- Кэш вердикта ----------


async def test_check_anomalies_stores_its_verdict(monkeypatch) -> None:
    """Прогон проверок оставляет снимок: его и читают /health и /ops."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(settings, "admin_ids", "42")
    await _seed_payout("pending", "prize", age_minutes=90)

    problems = await ops.check_anomalies(bot=SimpleNamespace(send_message=AsyncMock()))

    assert any("очередь выплат стоит" in p for p in problems)
    cached, age, _details = await ops.problems_snapshot()
    assert cached == problems
    assert age is not None and age < 60


async def test_healthy_run_clears_stale_verdict(monkeypatch) -> None:
    """Проблемы ушли — снимок обязан это показать, иначе пульт врёт вечно."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    async with SessionLocal() as db:
        db.add(WatcherState(key=OPS_PROBLEMS_KEY, value=json.dumps(["очередь стоит 90 мин"])))
        await db.commit()

    assert await ops.check_anomalies(bot=None) == []

    cached, _, _details = await ops.problems_snapshot()
    assert cached == []


async def test_problems_snapshot_survives_garbage() -> None:
    """Мусор в снимке — пустой список, а не падение /health."""
    for raw in ("не json", '{"a": 1}', "[1, 2]"):
        async with SessionLocal() as db:
            await db.execute(delete_all := WatcherState.__table__.delete())
            db.add(WatcherState(key=OPS_PROBLEMS_KEY, value=raw))
            await db.commit()
        problems, _, _details = await ops.problems_snapshot()
        expected = ["1", "2"] if raw == "[1, 2]" else []
        assert problems == expected, raw
        assert delete_all is not None


async def test_snapshot_reports_cached_problems_without_recomputing(monkeypatch) -> None:
    """Опрос мониторинга не имеет права считать проверки сам (сеть, БД, 9 запросов)."""
    monkeypatch.setattr(
        ops, "check_anomalies", AsyncMock(side_effect=AssertionError("считать нельзя"))
    )
    async with SessionLocal() as db:
        db.add(
            WatcherState(
                key=OPS_PROBLEMS_KEY, value=json.dumps(["зеркало казны расходится"])
            )
        )
        db.add(
            WatcherState(key=OPS_PROBLEMS_AT_KEY, value=datetime.now(UTC).isoformat())
        )
        await db.commit()

    payload = await ops.snapshot()

    assert payload["problems"] == ["зеркало казны расходится"]
    assert payload["problems_age"] is not None
    ops.check_anomalies.assert_not_awaited()


async def test_snapshot_reports_announce_audience() -> None:
    """Аудитория рассылки — прямо в снимке.

    Раньше «кому может уйти новый день» было видно только запросом к
    прод-базе: в /health и /ops пустая аудитория и молчащая рассылка
    выглядели одинаково — как поломка кода.
    """
    payload = await ops.snapshot()

    audience = payload["announce_audience"]
    assert set(audience) == {"chats", "dm", "total", "empty_day"}
    assert audience["total"] == audience["chats"] + audience["dm"]
    assert audience["empty_day"] is None


# ---------- Пульт ----------


async def test_ops_text_lists_problems(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", False)
    await _seed_payout("failed", "prize", age_minutes=120)

    await ops.check_anomalies(bot=None)
    text = await ops_diag._ops_diag_text()

    assert "тревог — 1" in text
    assert "dead-letter выплат: 1" in text
    assert "безнадёжных 1" in text
    assert "проверки" in text


async def test_ops_text_reports_clean_state(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", False)
    await ops.check_anomalies(bot=None)

    text = await ops_diag._ops_diag_text()

    assert "тревог нет" in text
    assert "очередь пуста" in text
    assert "устарел" not in text
    assert "/treasury" in text and "/payouts" in text


async def test_ops_text_warns_when_sweep_itself_is_silent(monkeypatch) -> None:
    """Самый важный случай: «всё чисто» при мёртвой проверке — ложь."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    stale = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    async with SessionLocal() as db:
        db.add(WatcherState(key=OPS_PROBLEMS_KEY, value="[]"))
        db.add(WatcherState(key=OPS_PROBLEMS_AT_KEY, value=stale))
        await db.commit()

    text = await ops_diag._ops_diag_text()

    assert "тревог нет" in text
    assert "устарел на 30 мин" in text
    assert "неправдой" in text


async def test_ops_text_says_checks_never_ran() -> None:
    text = await ops_diag._ops_diag_text()

    assert "ни разу не шли" in text


async def test_ops_text_shows_tick_failures(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", False)
    async with SessionLocal() as db:
        db.add(WatcherState(key=ops.TICK_FAIL_KEY, value="4"))
        db.add(WatcherState(key=ops.TICK_KEY, value=datetime.now(UTC).isoformat()))
        await db.commit()

    text = await ops_diag._ops_diag_text()

    assert "падений тика подряд 4" in text


async def test_ops_text_hides_watcher_line_when_ton_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", False)
    await ops.check_anomalies(bot=None)

    assert "watcher выключен" in await ops_diag._ops_diag_text()


# ---------- Аудитория рассылки ----------


async def test_ops_text_warns_when_audience_is_empty(monkeypatch) -> None:
    """«Пустая аудитория» — не «всё хорошо»: пульт обязан назвать причину.

    Именно так выглядел прод-инцидент: день объявлен (claim стоит), пост не
    ушёл никуда, а пульт молчал — некому было сказать «рассылать некому».
    """
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(bc, "active_chat_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(bc, "active_player_ids", AsyncMock(return_value=[]))

    text = await ops_diag._ops_diag_text()

    assert "Аудитория рассылки: пусто" in text
    assert "/bind" in text and "/start" in text


async def test_ops_text_shows_audience_counts(monkeypatch) -> None:
    """Разбивка, а не сумма: «нет каналов» и «игроки без /start» чинятся по-разному."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(bc, "active_chat_ids", AsyncMock(return_value=[-1, -2]))
    monkeypatch.setattr(bc, "active_player_ids", AsyncMock(return_value=[7]))

    text = await ops_diag._ops_diag_text()

    assert "Аудитория рассылки: 2 чат(ов) · 1 в личку" in text


async def test_ops_text_reports_announce_that_hit_empty_audience(monkeypatch) -> None:
    """Метка дня ещё не снята — пульт показывает её отдельной строкой."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(bc, "active_chat_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(bc, "active_player_ids", AsyncMock(return_value=[]))
    async with SessionLocal() as db:
        db.add(WatcherState(key=ANNOUNCE_EMPTY_DAY_KEY, value="42"))
        await db.commit()

    text = await ops_diag._ops_diag_text()

    assert "Анонс дня 42 ушёл в пустоту" in text
    assert "тревога ещё не снята" in text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "5 с"), (90, "1 мин"), (3_600, "1 ч 0 мин"), (7_500, "2 ч 5 мин")],
)
def test_span_formats_human(seconds: int, expected: str) -> None:
    assert ops_diag._span(seconds) == expected


async def test_cmd_ops_denies_stranger() -> None:
    await ops_diag.cmd_ops(_message(999_999))
    # Сообщение отправлено, но текст — отказ, а не пульт.


def _message(uid: int, text: str = "/ops") -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        text=text,
        answer=AsyncMock(),
    )


async def test_cmd_ops_reports_failure_instead_of_stack(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "42")

    async def boom() -> None:
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(ops_diag, "snapshot", boom)
    message = _message(42)

    await ops_diag.cmd_ops(message)

    assert "не собрался" in message.answer.call_args.args[0]
    assert "RuntimeError" in message.answer.call_args.args[0]


async def test_cmd_ops_answers_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "ton_enabled", False)
    await ops.check_anomalies(bot=None)
    message = _message(42)

    await ops_diag.cmd_ops(message)

    assert "Пульт" in message.answer.call_args.args[0]
