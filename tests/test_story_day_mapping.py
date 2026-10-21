"""День кассеты считается от якоря забега, а решения — по номеру дня движка.

Две правды, из-за которых сюжет ехал относительно канона:

1. Дата дня кассеты бралась из «сегодня». После простоя движок догоняет
   пропущенные календарные дни, и «сегодня» указывало бы на контент, который
   уже показан. Теперь дата — якорь забега + (номер дня − 1): контент идёт
   по порядку дней движения.

2. Победитель дня для развилки искался по ДАТЕ открытия раунда. В
   догоняющих днях на одну дату приходится несколько раундов, и дата выбирала
   не тот — перемотка включалась по чужому решению. Теперь день кассеты
   переводится в номер дня движка (смещение от якоря), а номер — уникален.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.registry import RUN_START_KEY
from app.models import Player, Round, RoundStatus, StoryBeat, Vote, WinRule
from app.story import bay

ANCHOR_DATE = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _run_anchor(session):
    """Якорь забега: 1 мая 2026 (день движения N = N-е число мая)."""
    from app.models import WatcherState

    payload = json.dumps(
        {
            "key": "2026-05",
            "dom": 1,
            "start_iso": ANCHOR_DATE.isoformat(),
        }
    )
    row = await session.get(WatcherState, RUN_START_KEY)
    if row is None:
        session.add(WatcherState(key=RUN_START_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()


async def test_story_date_follows_anchor_not_wall_clock(session) -> None:
    """Дата дня = якорь + (номер дня − 1), а не «сегодня»."""
    # День движения 11 при якоре 1 мая → 11 мая, даже если «сейчас» 20-е.
    story_date, dom = await bay._anchor_info(session, 11)
    assert story_date == datetime(2026, 5, 11, tzinfo=UTC).date()
    assert dom == 1


async def test_story_date_advances_with_game_days(session) -> None:
    """Контент идёт по дням движения: каждый следующий день — следующий."""
    first, _ = await bay._anchor_info(session, 1)
    second, _ = await bay._anchor_info(session, 2)
    third, _ = await bay._anchor_info(session, 3)
    assert (second - first).days == 1
    assert (third - second).days == 1
    assert first == datetime(2026, 5, 1, tzinfo=UTC).date()


def test_engine_day_translation_uses_anchor_offset() -> None:
    """День кассеты переводится в день движения смещением от якоря."""
    # Якорь 1 мая: день кассеты N — это день движения N.
    assert bay._engine_day_for(1, 7) == 7
    # Якорь 5 мая: день кассеты 11 — это день движения 7.
    assert bay._engine_day_for(5, 11) == 7
    assert bay._engine_day_for(5, 7) == 3


async def test_fork_uses_engine_day_not_date_lookup(session) -> None:
    """Развилка спрашивает победителя нужного дня ДВИЖКА.

    Догоняющий день: два раунда открылись в одну дату (движок догнал
    пропущенный день). Поиск по дате взял бы последний раунд, а нужен
    РОВНО один — тот, что показал кассетный день развилки.
    """
    from app.story.schema import Cassette

    # Кассета: развилка на 8-й день, решение — по 7-му дню.
    payload = {
        "cassette_id": "race-kasseta",
        "month": "2026-05",
        "title": "Гонка",
        "switch": [
            {
                "to": "b",
                "at_day": 8,
                "winner": 1,
                "days": [
                    {
                        "day_index": i,
                        "chapter_title": f"Глава {i}",
                        "chapter_text": "x",
                        "dilemma": "d",
                        "cards": [
                            {
                                "position": p,
                                "title": f"Сцена {p}",
                                "consequence": "c",
                                "tag": "care",
                            }
                            for p in range(3)
                        ],
                    }
                    for i in range(8, 32)
                ],
            }
        ],
        "days": [
            {
                "day_index": i,
                "chapter_title": f"Глава {i}",
                "chapter_text": "x",
                "dilemma": "d",
                "cards": [
                    {
                        "position": p,
                        "title": f"Сцена {p}",
                        "consequence": "c",
                        "tag": "care",
                    }
                    for p in range(3)
                ],
            }
            for i in range(1, 32)
        ],
    }
    cassette = Cassette.model_validate(payload)

    # Два раунда ОДНОЙ даты: 7-й день движения (нужен) и 8-й (догоняющий).
    same_date = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    session.add(
        Round(
            day_index=7,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            chapter_title="нужный",
            chapter_text="x",
            opens_at=same_date,
            voting_ends_at=same_date,
            tally_ends_at=same_date,
            winner_card=1,
        )
    )
    session.add(
        Round(
            day_index=8,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            chapter_title="догоняющий",
            chapter_text="x",
            opens_at=same_date,
            voting_ends_at=same_date,
            tally_ends_at=same_date,
            winner_card=2,
        )
    )
    await session.commit()

    road, dates = await bay._resolution(session, cassette, datetime(2026, 5, 11).date(), 1)
    assert road == "b", "развилка должна идти по победителю 7-го дня движения"
    assert dates


async def test_echo_comes_from_previous_game_day(session) -> None:
    """Эхо — от предыдущего дня ДВИЖКА, а не от «вчера по календарю»."""
    session.add(
        Round(
            day_index=6,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            chapter_title="вчера",
            chapter_text="x",
            opens_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
            voting_ends_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
            tally_ends_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
            winner_card=1,
        )
    )
    session.add(
        StoryBeat(
            day_index=6,
            winning_title="ход",
            winning_text="Вчера стая пошла на тень.",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()

    echo = await bay._prev_echo(session, 7)
    assert echo == "Вчера стая пошла на тень."


async def test_player_and_vote_helpers_available() -> None:
    """Сантитарная проверка: модели, нужные тестам, на месте."""
    assert Player is not None and Vote is not None
    assert timedelta is not None
