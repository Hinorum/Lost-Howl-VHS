"""Гонка за платный грант смены пути: один грант — одна переделка.

Грант (RevoteGrant) — оплаченное право переписать свой голос. Списание
идёт условным UPDATE'ом, и защита от двойного списания держится на ВНЕШНЕМ
условии status='granted'.

Без него гонка двух процессов проходила насквозь: в Postgres подзапрос без
корреляции вычисляется один раз на оператор и при переоценке WHERE не
перечитывается, поэтому условие «id = <снимок>» оставалось истинным даже
после того, как первый процесс поставил status='used'. Второй UPDATE
списывал тот же грант: игрок платил за одну переделку, а голос переписывался
дважды.

Тесты: проверка инварианта на любой СУБД (условие во внешнем WHERE есть) и
настоящая гонка двумя сессиями — только на Postgres (SQLite сериализует
записи, см. tests/test_concurrency.py).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.models import Player, RevoteGrant, Round, RoundStatus, Vote, WinRule
from app.voting import _claim_grant_stmt, cast_vote, change_vote

PLAYER_ID = 91_001


def _open_round(day_index: int = 77) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",
        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )


def test_claim_checks_status_in_outer_where() -> None:
    """ВНЕШНЕЕ условие UPDATE'а проверяет status='granted'.

    Подзапрос выбирает грант, но его снимок не перечитывается при
    переоценке WHERE — значит, «сверить» грант должен именно внешний
    предикат. Без него гонка двух процессов списывает один грант дважды.
    """
    sql = str(
        _claim_grant_stmt(1, 2).compile(compile_kwargs={"literal_binds": True})
    )
    outer = sql[sql.rfind(")") + 1 :]
    assert "status" in outer, f"во внешнем WHERE нет проверки status: {sql}"


async def test_one_grant_buys_one_change(session) -> None:
    """Один оплаченный грант — одна переделка (последовательная семантика)."""
    session.add(Player(id=PLAYER_ID))
    round_row = _open_round()
    session.add(round_row)
    await session.commit()
    await cast_vote(session, round_row, PLAYER_ID, 0)
    session.add(
        RevoteGrant(round_id=round_row.id, player_id=PLAYER_ID, source="stars", unit_ref="g-1")
    )
    await session.commit()

    assert await change_vote(session, round_row, PLAYER_ID, 1) == "ok"
    assert await change_vote(session, round_row, PLAYER_ID, 2) == "no_grant"


async def test_concurrent_changes_spend_one_grant() -> None:
    """Два процесса одновременно переделывают голос — грант списывается один раз.

    Только на Postgres: SQLite сериализует записи в одном файле, настоящей
    параллельной записи там не бывает (см. tests/test_concurrency.py).
    """
    from app.db import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("гонка проверяется на Postgres: SQLite сериализует записи")

    async with SessionLocal() as db:
        db.add(Player(id=PLAYER_ID))
        round_row = _open_round(day_index=78)
        db.add(round_row)
        await db.commit()
        round_id = round_row.id
        await cast_vote(db, round_row, PLAYER_ID, 0)
        db.add(
            RevoteGrant(
                round_id=round_id, player_id=PLAYER_ID, source="stars", unit_ref="g-race"
            )
        )
        await db.commit()

    async def attempt(position: int) -> str:
        async with SessionLocal() as db:
            row = await db.get(Round, round_id)
            return await change_vote(db, row, PLAYER_ID, position)

    results = await asyncio.gather(attempt(1), attempt(2))
    assert results.count("ok") == 1, f"грант списан дважды: {results}"

    async with SessionLocal() as db:
        grants = (
            await db.execute(
                RevoteGrant.__table__.select().where(
                    RevoteGrant.unit_ref == "g-race"
                )
            )
        ).all()
        assert len(grants) == 1
        assert grants[0]._mapping["status"] == "used"
        votes = (await db.execute(Vote.__table__.select())).all()
        assert len(votes) == 1
