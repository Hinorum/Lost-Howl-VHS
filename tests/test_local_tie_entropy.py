"""Жребий ничьей без сети: исход честный, но непроверяемый.

Когда оба индексатора TON молчат, энтропию блока взять неоткуда, и ничья
решается жребием. Прежний жребий был детерминированным (сид «день+закон»):
исход дня можно было вычислить заранее — и поставить на него. Теперь при
включённых деньгах подставляется ЛОКАЛЬНАЯ случайность: её никто не знает
заранее, но она сохраняется в дне, поэтому пересчёт даёт тот же исход.

Отдельно от этого оператор должен знать, что такой день был: исход не
проверяется по эксплореру (он честный, но непроверяемый) — тревога
«жребий ничьей без сети».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import ops
from app.config import settings
from app.core.registry import LOCAL_TIE_PREFIX
from app.db import SessionLocal
from app.models import Card, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.rounds import close_voting, finish_tally, pick_winner, tie_seed


def _round(day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title=f"День {day_index}",
        chapter_text="Текст дня.",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(minutes=5),
        tally_ends_at=now + timedelta(minutes=5),
    )
    for position in range(3):
        round_row.cards.append(
            Card(position=position, title=f"Сцена {position}", consequence="След сцены.")
        )
    return round_row


async def _seed_tied_day(session, day_index: int, base_pid: int = 900_021) -> Round:
    """День с ничьей: сцена 0 и 1 набрали поровну (закон MAJORITY)."""
    round_row = _round(day_index)
    session.add(round_row)
    await session.commit()
    for path, pid in ((0, base_pid), (1, base_pid + 1)):
        session.add(Player(id=pid, username=f"tie-p{pid}"))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    await session.commit()
    return round_row


async def test_tie_without_chain_gets_local_entropy(session, monkeypatch) -> None:
    """Сеть недоступна — исход всё равно не выводится из публичных данных."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def no_entropy() -> str | None:
        return None

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", no_entropy)

    round_row = await _seed_tied_day(session, 850)
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        assert loaded.tie_entropy is not None
        assert loaded.tie_entropy.startswith(LOCAL_TIE_PREFIX), loaded.tie_entropy
        # Исход выводится из СОХРАНЁННОЙ энтропии, а не из «день+закон».
        assert loaded.winner_card == pick_winner(
            {0: 1, 1: 1, 2: 0}, loaded.win_rule, tie_seed(loaded)
        )
        # Повторный подсчёт (heal) даёт того же победителя.
        first_winner = loaded.winner_card
        loaded.status = RoundStatus.TALLYING
        await finish_tally(session, loaded)
        again = await session.get(Round, round_row.id)
        assert again.winner_card == first_winner
        assert again.tie_entropy == loaded.tie_entropy
    finally:
        await session.rollback()


async def test_local_entropy_differs_between_days(session, monkeypatch) -> None:
    """Два дня получают разные локальные энтропии (это случайность, а не сид)."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def no_entropy() -> str | None:
        return None

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", no_entropy)

    first = await _seed_tied_day(session, 851, base_pid=900_031)
    second = await _seed_tied_day(session, 852, base_pid=900_041)
    try:
        await close_voting(session, first)
        await close_voting(session, second)
        loaded_first = await session.get(Round, first.id)
        loaded_second = await session.get(Round, second.id)
        assert loaded_first.tie_entropy != loaded_second.tie_entropy
    finally:
        await session.rollback()


async def test_local_tie_raises_alert(monkeypatch) -> None:
    """Хранитель узнаёт, что день решён без сети: по эксплореру не проверить."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "ton_enabled", True)
    bot = SimpleNamespace(send_message=AsyncMock())
    round_id = 0
    try:
        async with SessionLocal() as db:
            round_row = _round(853)
            round_row.tie_entropy = f"{LOCAL_TIE_PREFIX}deadbeef"
            db.add(round_row)
            await db.commit()
            round_id = round_row.id

        problems = await ops.check_anomalies(bot=bot)
        assert any("жребий ничьей без сети" in problem for problem in problems), problems
        assert bot.send_message.await_count >= 1
    finally:
        async with SessionLocal() as db:
            # Карточки дня удаляем первыми: у дня они есть по умолчанию, и
            # Postgres не даст снести раунд с живыми карточками (SQLite
            # внешние ключи не проверяет, поэтому локально это проходило).
            await db.execute(Card.__table__.delete().where(Card.round_id == round_id))
            await db.execute(Round.__table__.delete().where(Round.id == round_id))
            await db.commit()


async def test_tie_entropy_from_chain_stays_verifiable(session, monkeypatch) -> None:
    """Сеть доступна — энтропия блока, как прежде: исход проверяем по эксплореру."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_fetch() -> str:
        return "93123949:abcdef"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = await _seed_tied_day(session, 854)
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        assert loaded.tie_entropy == "93123949:abcdef"
        assert not loaded.tie_entropy.startswith(LOCAL_TIE_PREFIX)
    finally:
        await session.rollback()


async def test_money_day_stays_unpredictable_after_global_switch_off(
    session, monkeypatch
) -> None:
    """День решён банком ставок — даже если глобальный TON_ENABLED выключили.

    Ставки принимаются, пока watcher жив. Глобальный переключатель можно
    выключить деплоем уже после открытия дня — и тогда день остаётся
    денежным (его решает банк), а жребий ничьей при недоступной сети стал
    бы детерминированным: исход вычисляется заранее, и на него можно
    поставить.
    """
    monkeypatch.setattr(settings, "ton_enabled", False)

    async def no_entropy() -> str | None:
        return None

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", no_entropy)

    round_row = await _seed_tied_day(session, 855)
    round_row.money_mode = True  # день открыт со ставками
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=900_031,
            amount_nanotons=500_000_000,
            network="mainnet",
            status="confirmed",
            tx_hash="tie-stake-855",
        )
    )
    await session.commit()
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        assert loaded.tie_entropy is not None
        assert loaded.tie_entropy.startswith(LOCAL_TIE_PREFIX), loaded.tie_entropy
    finally:
        await session.rollback()
