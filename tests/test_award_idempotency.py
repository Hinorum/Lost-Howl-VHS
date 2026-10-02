"""Идемпотентное начисление очков дня (P1-4, маркер awards_at).

award_points — claim в одной транзакции с начислением: повторный вызов
(replay тика, админское /advance, одновременные финализаторы) не удваивает
score. День, закрытый без начисления крашем между коммитом finish_tally и
award_points, догоняет award_pending_points по awards_at IS NULL.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Player, Round, RoundStatus, Vote, WinRule
from app.rounds import close_voting, finish_tally
from app.tally import award_pending_points, award_points


def _tallying_day(day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        vote_counts_json="{}",
    )
    for pos in (0, 1, 2):
        round_row.cards.append(
            Card(position=pos, title=f"t{pos}", description="d", consequence="к", image_path="")
        )
    return round_row


async def _seed_votes(session: AsyncSession, round_id: int, votes: dict[int, list[int]]) -> None:
    for path, pids in votes.items():
        for pid in pids:
            session.add(Player(id=pid))
            session.add(Vote(round_id=round_id, player_id=pid, card_position=path))
    await session.commit()


async def _scores(session: AsyncSession, ids: list[int]) -> dict[int, int]:
    rows = await session.scalars(select(Player))
    return {p.id: p.score for p in rows.all()}


async def _closed_day(
    session: AsyncSession, day_index: int, votes: dict[int, list[int]]
) -> Round:
    """Голосующий день, подсчитанный finish_tally, БЕЗ начисления очков
    (симуляция краша между итогом и награждением)."""
    round_row = _tallying_day(day_index)
    session.add(round_row)
    await session.commit()
    round_row = await session.get(Round, round_row.id)
    for path, pids in votes.items():
        for pid in pids:
            session.add(Player(id=pid))
            session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    await session.commit()
    await close_voting(session, round_row)
    closed, _here = await finish_tally(session, round_row)
    assert closed.winner_card is not None
    assert closed.awards_at is None
    return closed


async def test_award_points_repeat_call_does_not_double_score(session: AsyncSession) -> None:
    # Путь 0 побеждает большинством бесплатных голосов (игроки 1 и 2).
    closed = await _closed_day(session, 7001, {0: [1, 2], 1: [3], 2: []})

    first = await award_points(session, closed)
    assert first == 2  # оба голоса пути-победителя
    assert await _scores(session, [1, 2, 3]) == {1: 11, 2: 11, 3: 1}

    claimed = await session.get(Round, closed.id)
    assert claimed.awards_at is not None

    # Повторный вызов — ретрай тика или админское переигрывание: 0, score цел.
    replay = await award_points(session, closed)
    assert replay == 0
    assert await _scores(session, [1, 2, 3]) == {1: 11, 2: 11, 3: 1}


async def test_award_pending_points_recovers_crashed_closed_day(session: AsyncSession) -> None:
    # Разрыв между коммитом finish_tally и award_points: день CLOSED, очки нет.
    closed = await _closed_day(session, 7002, {0: [1, 2], 1: [3], 2: []})

    healed = await award_pending_points(session)
    assert healed == 2
    assert await _scores(session, [1, 2, 3]) == {1: 11, 2: 11, 3: 1}

    claimed = await session.get(Round, closed.id)
    assert claimed.awards_at is not None

    # Догон идемпотентен: повторный тик ничего не удваивает.
    assert await award_pending_points(session) == 0
    assert await _scores(session, [1, 2, 3]) == {1: 11, 2: 11, 3: 1}


async def test_award_pending_points_skips_closed_day_without_winner(session: AsyncSession) -> None:
    # Защитный фильтр: день без победителя (winner_card NULL) не трогает игроков.
    now = datetime.now(UTC)
    orphan = Round(
        day_index=7003,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now - timedelta(minutes=1),
        winner_card=None,
        vote_counts_json="{}",
    )
    session.add(orphan)
    await session.commit()
    await _seed_votes(session, orphan.id, {0: [1], 1: [2], 2: []})

    assert await award_pending_points(session) == 0
    assert await _scores(session, [1, 2]) == {1: 0, 2: 0}


async def test_award_pending_points_skips_historical_day(session: AsyncSession) -> None:
    # Регрессия прода: awards_at добавлен миграцией без бэкфилла, поэтому у
    # всей истории маркер NULL. Без recency-гарда первый же тик новой версии
    # начислил бы очки заново за ВСЕ закрытые дни — двойные очки у игроков.
    # Данные фиксированы (100 дней назад), а не выведены из настройки окна:
    # иначе тест масштабировался бы вместе с гардом и ничего не проверял бы.
    now = datetime.now(UTC)
    ancient = now - timedelta(days=100)
    stale = Round(
        day_index=7004,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=ancient,
        voting_ends_at=ancient,
        tally_ends_at=ancient,
        winner_card=0,
        vote_counts_json="{}",
    )
    session.add(stale)
    await session.commit()
    await _seed_votes(session, stale.id, {0: [1, 2], 1: [3], 2: []})

    assert await award_pending_points(session) == 0
    assert await _scores(session, [1, 2, 3]) == {1: 0, 2: 0, 3: 0}
    assert (await session.get(Round, stale.id)).awards_at is None


async def test_award_points_sets_marker_only_with_winner(session: AsyncSession) -> None:
    # День без победителя: award_points не ставит marker и не пишет очки.
    round_row = _tallying_day(7004)
    round_row.winner_card = None
    session.add(round_row)
    await session.commit()
    await _seed_votes(session, round_row.id, {0: [1], 1: [2], 2: []})

    assert await award_points(session, round_row) == 0
    assert (await session.get(Round, round_row.id)).awards_at is None
    assert await _scores(session, [1, 2]) == {1: 0, 2: 0}