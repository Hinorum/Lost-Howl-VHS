"""Тесты претензий на места лидерборда: окно Claim, клавиатура, ничьи.

Кнопка Claim видна только tied-игрокам, пока окно заявок открыто (приз не
распределён из-за ничьи в закрытом периоде). Период заявки берётся из окна,
запись идемпотентна (unique player+kind+period), а при равенстве верных путей
и вклада Gram решает момент претензии.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.core.registry import (
    MONTH_CLAIM_WINDOW_KEY,
    WEEK_CLAIM_WINDOW_KEY,
)
from app.db import SessionLocal
from app.handlers import _start_keyboard, on_claim_month, on_claim_week
from app.leaderboard import _order_by_ties, _prize_tied_groups
from app.models import LeaderboardClaim, Player, WatcherState


def test_prize_tied_groups_detects_tie_at_cutoff() -> None:
    pts = "0:" + os.urandom(32).hex()
    # Позиции 1-3: лидер 6 верных, затем двое равных (5 верных, 100 Gram) —
    # ничья прямо на призовой границе.
    candidates = [
        (1, 6, 100, pts),
        (2, 5, 100, pts),
        (3, 5, 100, pts),
        (4, 5, 90, pts),  # меньше Gram — вне связки
        (5, 4, 100, pts),  # ниже по верным — вне топ-3
    ]
    assert _prize_tied_groups(candidates, top_k=3) == [[2, 3]]


def test_prize_tied_groups_full_top_k_and_no_tie() -> None:
    pts = "0:" + os.urandom(32).hex()
    # Полностью связанные топ-3.
    candidates = [(1, 5, 100, pts), (2, 5, 100, pts), (3, 5, 100, pts)]
    assert _prize_tied_groups(candidates, top_k=3) == [[1, 2, 3]]
    # Ничьей нет — все места однозначны.
    candidates = [(1, 5, 100, pts), (2, 4, 100, pts), (3, 3, 100, pts)]
    assert _prize_tied_groups(candidates, top_k=3) == []
    # Связка ниже призовой границы не спорит за места.
    candidates = [(1, 5, 100, pts), (2, 5, 100, pts), (3, 4, 100, pts), (4, 3, 100, pts), (5, 3, 100, pts)]
    assert _prize_tied_groups(candidates, top_k=3) == [[1, 2]]


def test_order_by_ties_claim_time_beats_id_and_silence() -> None:
    pts = "0:" + os.urandom(32).hex()
    candidates = [
        (1, 5, 100, pts),
        (2, 5, 100, pts),
        (3, 5, 100, pts),
        (4, 4, 100, pts),  # ниже по верным — позади любой пятёрки
    ]
    base = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    # Кто раньше нажал Claim — выше; молчаливый уступает обоим заявившимся.
    rows = _order_by_ties(candidates, {2: base + timedelta(hours=5), 1: base + timedelta(hours=1)})
    assert [r[0] for r in rows] == [1, 2, 3, 4]
    # Без претензий — меньший player_id.
    rows = _order_by_ties(candidates[:3], {})
    assert [r[0] for r in rows] == [1, 2, 3]
    # Заявившийся с большим id опережает равных молчунов.
    rows = _order_by_ties(candidates[:3], {3: base + timedelta(hours=2)})
    assert [r[0] for r in rows] == [3, 1, 2]


async def _mk_player(pid: int, wallet: str | None = None) -> None:
    async with SessionLocal() as db:
        player = Player(id=pid, username=f"u{pid}", first_name="P")
        if wallet is not None:
            player.wallet_address = wallet
        db.add(player)
        await db.commit()


async def _clean_player(pid: int) -> None:
    async with SessionLocal() as db:
        await db.execute(LeaderboardClaim.__table__.delete().where(LeaderboardClaim.player_id == pid))
        await db.execute(Player.__table__.delete().where(Player.id == pid))
        await db.commit()


async def _seed_claim_window(kind: str, period: str, players: list[int], opened_at: str | None = None) -> None:
    """Открывает окно Claim в watcher_state, как это делает settlement."""
    if opened_at is None:
        opened_at = datetime.now(UTC).isoformat()
    key = WEEK_CLAIM_WINDOW_KEY if kind == "week" else MONTH_CLAIM_WINDOW_KEY
    async with SessionLocal() as db:
        db.add(WatcherState(key=key, value=json.dumps({"period": period, "players": players, "opened_at": opened_at})))
        await db.commit()


async def _clear_claim_windows() -> None:
    async with SessionLocal() as db:
        await db.execute(WatcherState.__table__.delete().where(WatcherState.key.in_(
            [WEEK_CLAIM_WINDOW_KEY, MONTH_CLAIM_WINDOW_KEY]
        )))
        await db.commit()


def _callback(pid: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=pid, username=None, first_name="P"),
        message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_claim_week_inserts_one_row_and_is_idempotent() -> None:
    pid = 990_001
    period = "2026-W35"
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    await _seed_claim_window("week", period, [pid])
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        await on_claim_week(cb)  # повторный тап — не вторая запись
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(LeaderboardClaim).where(LeaderboardClaim.player_id == pid)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].kind == "week"
        assert rows[0].period == period  # период из окна, а не текущей даты
        assert rows[0].claimed_at is not None
        texts = [c.args[0] for c in cb.answer.await_args_list]
        assert any("принята" in t for t in texts)
        assert any("уже заявлено" in t for t in texts)
    finally:
        await _clean_player(pid)
        await _clear_claim_windows()


async def test_claim_month_and_week_are_separate() -> None:
    pid = 990_002
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    await _seed_claim_window("week", "2026-W35", [pid])
    await _seed_claim_window("month", "2026-08", [pid])
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        await on_claim_month(cb)
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(LeaderboardClaim).where(LeaderboardClaim.player_id == pid)))
                .scalars()
                .all()
            )
        assert {r.kind for r in rows} == {"week", "month"}
        assert {r.period for r in rows} == {"2026-W35", "2026-08"}
    finally:
        await _clean_player(pid)
        await _clear_claim_windows()


async def test_claim_requires_wallet() -> None:
    pid = 990_003
    await _mk_player(pid)  # кошелька нет
    await _seed_claim_window("week", "2026-W35", [pid])
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        async with SessionLocal() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(LeaderboardClaim)
                .where(LeaderboardClaim.player_id == pid)
            )
        assert count == 0
        alert = cb.answer.await_args.kwargs.get("show_alert")
        assert alert is True
        assert "кошелёк" in cb.answer.await_args.args[0]
    finally:
        await _clean_player(pid)
        await _clear_claim_windows()


async def test_claim_rejected_without_open_window() -> None:
    pid = 990_004
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        async with SessionLocal() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(LeaderboardClaim)
                .where(LeaderboardClaim.player_id == pid)
            )
        assert count == 0
        alert = cb.answer.await_args.kwargs.get("show_alert")
        assert alert is True
        assert "нет открытых заявок" in cb.answer.await_args.args[0]
    finally:
        await _clean_player(pid)


async def test_claim_rejected_for_non_tied_player() -> None:
    pid = 990_005
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    await _seed_claim_window("week", "2026-W35", [pid + 1])  # tied — другой игрок
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        assert "нет открытых заявок" in cb.answer.await_args.args[0]
        async with SessionLocal() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(LeaderboardClaim)
                .where(LeaderboardClaim.player_id == pid)
            )
        assert count == 0
    finally:
        await _clean_player(pid)
        await _clear_claim_windows()


async def test_start_keyboard_claim_buttons_for_tied_only() -> None:
    """Кнопка Claim видна только tied-игрокам активного окна: без окна — пусто."""
    pid_week, pid_month, pid_neither = 990_011, 990_012, 990_013
    for pid in (pid_week, pid_month, pid_neither):
        await _mk_player(pid, "0:" + os.urandom(32).hex())
    await _seed_claim_window("week", "2026-W35", [pid_week])
    await _seed_claim_window("month", "2026-08", [pid_month])
    try:
        async with SessionLocal() as session:
            for pid in (pid_week, pid_month, pid_neither):
                player = await session.get(Player, pid)
                kb = await _start_keyboard(session, player)
                labels = [b.text for row in kb.inline_keyboard for b in row]
                if pid == pid_week:
                    assert any("приз недели" in l for l in labels)
                    assert not any("приз месяца" in l for l in labels)
                elif pid == pid_month:
                    assert any("приз месяца" in l for l in labels)
                    assert not any("приз недели" in l for l in labels)
                else:
                    assert not any("приз" in l or l.startswith("🗓") for l in labels)
    finally:
        for pid in (pid_week, pid_month, pid_neither):
            await _clean_player(pid)
        await _clear_claim_windows()


async def test_start_keyboard_hides_buttons_without_window() -> None:
    """Окно Claim закрыто — никто не видит кнопки, даже имея ставку и кошелёк."""
    pid = 990_014
    await _mk_player(pid, "0:" + os.urandom(32).hex())
    try:
        async with SessionLocal() as session:
            player = await session.get(Player, pid)
            kb = await _start_keyboard(session, player)
            labels = [b.text for row in kb.inline_keyboard for b in row]
            assert not any("приз" in l or l.startswith("🗓") for l in labels)
    finally:
        await _clean_player(pid)
        await _clear_claim_windows()