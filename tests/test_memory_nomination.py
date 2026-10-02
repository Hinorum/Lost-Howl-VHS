"""Номинация «самый памятливый пёс недели»: подсчёт в окне, фолбэки текста."""

from datetime import UTC, datetime, timedelta

from app.leaderboard import _memory_nomination
from app.models import MemoryHit, Player, Round, RoundStatus, WinRule


def _round(day_index: int, opens_at: datetime) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",

        opens_at=opens_at,
        voting_ends_at=opens_at + timedelta(hours=23),
        tally_ends_at=opens_at + timedelta(hours=24),
        vote_counts_json="{}",
    )


async def test_nomination_counts_hits_in_window(session) -> None:
    start = datetime(2026, 8, 17, tzinfo=UTC)
    end = start + timedelta(weeks=1)
    session.add(Player(id=80, username="owl"))
    for index in range(3):
        round_row = _round(1600 + index, start + timedelta(days=index, hours=11))
        session.add(round_row)
        await session.flush()
        session.add(MemoryHit(player_id=80, round_id=round_row.id))
    # Вне окна — не считается.
    outside = _round(1610, end + timedelta(days=2))
    session.add(outside)
    await session.flush()
    session.add(MemoryHit(player_id=80, round_id=outside.id))
    await session.commit()

    line = await _memory_nomination(session, start, end)
    assert line is not None
    assert "owl" in line and "3 находки" in line


async def test_no_nomination_without_hits(session) -> None:
    start = datetime(2026, 8, 17, tzinfo=UTC)
    line = await _memory_nomination(session, start, start + timedelta(weeks=1))
    assert line is None
