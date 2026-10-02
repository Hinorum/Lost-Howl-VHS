"""Закон дня на цепи: правило выводится из root_hash блока TON, а не локального RNG.

Как жребий ничьей (tie_entropy), закон дня снимает энтропию мастерчейна
(«seqno:root_hash»), и rule выводится детерминированно: root_hash % 3.

Свойства:
- _plan_and_render с энтропией даёт детерминированный закон, payload хранит
  саму энтропию — хранитель проверит закон по блоку в эксплорере;
- create_next_round_detailed снимает энтропию и сохраняет её в день
  (rule_entropy), а not локальный secrets-жребий;
- TON выключен / энтропия недоступна → локальный жребий, rule_entropy пустая;
- в анонсе дня и посте итогов закон печатается без номера блока и без ссылки —
  игрокам не нужны ни рамка превью, ни расшифровка.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models import Card, Round, RoundStatus, WinRule
from app.rounds import (
    _plan_and_render,
    create_next_round_detailed,
)

NOW = datetime.now(UTC)


def _expected_rule(root_hash: str) -> WinRule:
    rules = list(WinRule)
    return rules[int(root_hash, 16) % len(rules)]


def _round(*, rule_entropy: str | None = None) -> Round:
    round_row = Round(
        day_index=42,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="День 42",
        chapter_text="Текст.",
        opens_at=NOW - timedelta(hours=1),
        voting_ends_at=NOW + timedelta(hours=20),
        tally_ends_at=NOW + timedelta(hours=21),
        rule_entropy=rule_entropy,
    )
    for position in range(3):
        round_row.cards.append(
            Card(
                position=position,
                title=f"Тропа {position}",
                description="д",
                consequence="Канон дня.",
                image_path="",
            )
        )
    return round_row


async def test_plan_and_render_rule_derived_from_entropy(session) -> None:
    """Закон детерминирован блоком: root_hash % 3, энтропия уходит в payload."""
    cases = (("100", "3", WinRule.MAJORITY), ("101", "7", WinRule.MINORITY), ("102", "5", WinRule.MEDIAN))
    for seqno, root, expected in cases:
        payload = await _plan_and_render(session, 7, entropy=f"{seqno}:{root}")
        assert payload["rule_entropy"] == f"{seqno}:{root}"
        assert payload["rule"] == expected.value
        assert payload["rule"] == _expected_rule(root).value


async def test_plan_and_render_same_block_same_rule(session) -> None:
    a = await _plan_and_render(session, 7, entropy="100:deadbeef")
    b = await _plan_and_render(session, 7, entropy="100:deadbeef")
    assert a["rule"] == b["rule"] == _expected_rule("deadbeef").value


async def test_create_next_round_uses_masterchain_entropy(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Открытие дня снимает энтропию мастерчейна и сохраняет закон с ней."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    calls: list[str] = []

    async def fake_fetch() -> str:
        calls.append("fetch")
        return "1234567:7"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row, created = await create_next_round_detailed(session)
    try:
        assert created is True
        assert calls == ["fetch"]  # энтропия снята один раз
        assert round_row.rule_entropy == "1234567:7"
        assert round_row.win_rule == _expected_rule("7")  # MINORITY
        loaded = await session.get(Round, round_row.id)
        assert loaded.rule_entropy == "1234567:7"
        assert loaded.win_rule == WinRule.MINORITY
    finally:
        await session.rollback()


async def test_create_next_round_falls_back_without_ton(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TON выключен/энтропия недоступна: локальный жребий, rule_entropy пустая."""
    monkeypatch.setattr(settings, "ton_enabled", False)

    async def fake_fetch() -> None:
        return None

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row, created = await create_next_round_detailed(session)
    try:
        assert created is True
        assert round_row.rule_entropy is None
        assert round_row.win_rule in list(WinRule)
    finally:
        await session.rollback()


def test_rule_block_ref_removed() -> None:
    from app.rounds import rendering

    assert not hasattr(rendering, "rule_block_ref")
    assert not hasattr(rendering, "TON_EXPLORER_BLOCK_URL")


async def test_day_open_post_has_no_block_mention() -> None:
    """Анонс дня печатает закон без номера блока и без ссылок."""
    from app.broadcast import status_text

    text = await status_text(_round(rule_entropy="4711:abcd"), show_title=True)
    assert "блок TON" not in text
    assert "href=" not in text and "http" not in text


async def test_results_post_has_no_block_mention() -> None:
    """Итоги дня печатают правило без номера блока и без ссылок."""
    from app.tally import format_results

    round_row = _round(rule_entropy="4711:abcd")
    round_row.status = RoundStatus.CLOSED
    round_row.winner_card = 0
    round_row.vote_counts_json = '{"0": 2, "1": 1, "2": 2}'
    text = format_results(round_row)
    assert "Сцена дня" in text
    assert "блок TON" not in text
    assert "href=" not in text and "http" not in text