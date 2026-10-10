"""Новая механика (winner_by_stakes=True): ставки в Gram решают исход дня.

Закон дня (majority/minority/median) применяется к суммам подтверждённых
ставок на каждом пути. Путь без грамма участвует в подсчёте как 0.
День, где нет ни одного грамма, решается бесплатными голосами (fallback).
Бесплатные голоса на исход НЕ влияют — они питают только лидерборд
(score/correct_picks/стрики): верность = голос совпал с путём, выбранным
ставками. Легаси-режим (winner_by_stakes=False) проверен в
test_winrule_stake_guard.py.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Card, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.rounds import (
    close_voting,
    count_stakes_for_tally,
    count_votes_for_tally,
    finish_tally,
    pick_winner,
    tied_positions,
)
from app.tally import award_points, format_results
from app.ton_utils import to_nano


def _round_row(rule: WinRule, day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.TALLYING,
        win_rule=rule,
        chapter_title="t",
        chapter_text="text",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        vote_counts_json="{}",
    )
    for pos in (0, 1, 2):
        round_row.cards.append(
            Card(position=pos, title=f"t{pos}", consequence="к")
        )
    return round_row


async def _seed_day(
    session: AsyncSession,
    rule: WinRule,
    votes: dict[int, list[int]],
    stakes: dict[int, list[tuple[int, float]]],
    day_index: int = 1,
) -> Round:
    """День в подсчёте. votes[путь] = игроки; stakes[путь] = [(игрок, Gram)].

    Ставщик без явного голоса в votes автоматически голосует за путь своей
    ставки (ставка привязана к пути через голос игрока).
    """
    round_row = _round_row(rule, day_index)
    session.add(round_row)
    await session.commit()
    seen: set[int] = set()
    for path, pids in votes.items():
        for pid in pids:
            session.add(Player(id=pid))
            seen.add(pid)
            session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    for path, entries in stakes.items():
        for pid, gram in entries:
            if pid not in seen:
                session.add(Player(id=pid))
                seen.add(pid)
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
            session.add(
                Stake(
                    round_id=round_row.id,
                    player_id=pid,
                    amount_nanotons=to_nano(gram),
                    tx_hash=f"tx-{path}-{pid}",
                    status="confirmed",
                )
            )
    await session.commit()
    return round_row


async def test_majority_stakes_beat_free_votes(session: AsyncSession) -> None:
    # Сердце за путь 0 (5 голосов), деньги за путь 1 (3 Gram против 1 Gram).
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3, 4, 5], 1: [6], 2: []},
        stakes={0: [(1, 1.0)], 1: [(6, 3.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    # Решающий счёт ставок сохраняется; голоса — отдельно, для лидерборда.
    assert json.loads(closed.stake_counts_json) == {"0": to_nano(1), "1": to_nano(3), "2": 0}
    assert json.loads(closed.vote_counts_json) == {"0": 5, "1": 1, "2": 0}


async def test_minority_prefers_least_funded_path(session: AsyncSession) -> None:
    # Нулевой путь участвует как 0 Gram: MINORITY по ставкам → путь 2.
    round_row = await _seed_day(
        session,
        WinRule.MINORITY,
        votes={0: [1], 1: [2], 2: [3]},
        stakes={0: [(1, 2.0)], 1: [(2, 1.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 2


def test_minority_zero_tie_broken_by_fewest_votes() -> None:
    # День 23: деньги (1.00 Gram) на пути 0, пути 1 и 2 делят минимум (0 Gram).
    # Путь 1 никто не выбрал — уцелеет он, без жребия «по людям».
    stakes = {0: to_nano(1.0), 1: 0, 2: 0}
    votes = {0: 2, 1: 0, 2: 2}
    assert tied_positions(stakes, WinRule.MINORITY, votes) == [1]
    assert pick_winner(stakes, WinRule.MINORITY, "9:minority:zzz", votes) == 1


def test_minority_zero_tie_with_equal_votes_keeps_draw() -> None:
    # Оба пустых пути без голосов — настоящая ничья, жребий остаётся.
    stakes = {0: to_nano(1.0), 1: 0, 2: 0}
    votes = {0: 2, 1: 0, 2: 0}
    assert tied_positions(stakes, WinRule.MINORITY, votes) == [1, 2]


def test_minority_nonzero_tie_unaffected_by_votes() -> None:
    # Не-нулевая ничья голосами не разрешается — только жребием.
    stakes = {0: 5, 1: 3, 2: 3}
    votes = {0: 9, 1: 1, 2: 8}
    assert tied_positions(stakes, WinRule.MINORITY, votes) == [1, 2]


async def test_minority_empty_scene_wins_without_draw(session: AsyncSession) -> None:
    # Интеграция: путь 1 (0 голосов) уцелел при ничьей на нуле с путём 2
    # (2 голоса без ставок) — и жребий не кидался.
    round_row = await _seed_day(
        session,
        WinRule.MINORITY,
        votes={0: [1, 2], 2: [3, 4]},
        stakes={0: [(1, 1.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    assert (closed.tie_note or "") == ""
    assert json.loads(closed.stake_counts_json) == {"0": to_nano(1.0), "1": 0, "2": 0}
    assert json.loads(closed.vote_counts_json) == {"0": 2, "1": 0, "2": 2}


async def test_close_voting_skips_draw_when_votes_resolve_zero_tie(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ничья на нуле, разрешимая голосами, не трогает мастерчейн-энтропию."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    captured: list[str] = []

    async def fake_fetch() -> str:
        captured.append("fetch")
        return "93123949:abcdef"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = _round_row(WinRule.MINORITY, 423)
    round_row.status = RoundStatus.OPEN
    session.add(round_row)
    await session.commit()
    for pid, path in ((1, 0), (2, 0), (3, 2)):
        session.add(Player(id=pid))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=1,
            amount_nanotons=to_nano(1.0),
            tx_hash="tx-day423",
            status="confirmed",
        )
    )
    await session.commit()

    await close_voting(session, round_row)
    loaded = await session.get(Round, round_row.id)
    assert loaded.winner_card == 1
    assert captured == []  # энтропии не снимали — исхода нет ничьей
    assert loaded.tie_entropy is None


async def test_median_takes_middle_bank(session: AsyncSession) -> None:
    # Ставки: 0→1 Gram, 1→3 Gram, 2→5 Gram. MEDIAN — среднее значение 3 → путь 1.
    round_row = await _seed_day(
        session,
        WinRule.MEDIAN,
        votes={0: [1], 1: [2], 2: [3]},
        stakes={0: [(1, 1.0)], 1: [(2, 3.0)], 2: [(3, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_day_without_stakes_falls_back_to_votes(session: AsyncSession) -> None:
    # Граммов на день нет вовсе — победителя выводят голоса, без stake-счёта.
    round_row = await _seed_day(
        session, WinRule.MAJORITY, votes={0: [1], 1: [2], 2: [3, 4]}, stakes={}
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 2
    assert closed.stake_counts_json is None
    assert json.loads(closed.vote_counts_json) == {"0": 1, "1": 1, "2": 2}


async def test_pending_stakes_not_counted(session: AsyncSession) -> None:
    # Только confirmed: зависшая ставка 99 Gram пути 1 не перевешивает.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1], 1: [2]},
        stakes={0: [(1, 1.0)]},
    )
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=2,
            amount_nanotons=to_nano(99),
            tx_hash="tx-pending",
            status="pending",
        )
    )
    await session.commit()
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 0


async def test_stake_without_vote_has_no_path(session: AsyncSession) -> None:
    # Ставка без голоса ни к какому пути не привязана — в счёт путей не входит,
    # день фактически без ставок: fallback на голоса.
    round_row = await _seed_day(
        session, WinRule.MAJORITY, votes={0: [1]}, stakes={}
    )
    session.add(Player(id=7))
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=7,
            amount_nanotons=to_nano(9),
            tx_hash="tx-novote",
            status="confirmed",
        )
    )
    await session.commit()
    counts = await count_stakes_for_tally(session, round_row.id)
    assert counts == {0: 0, 1: 0, 2: 0}
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 0
    assert closed.stake_counts_json is None


async def test_revote_redirects_stake_weight(session: AsyncSession) -> None:
    # Игрок сменил голос (протокол смены выбора): его ставка усиливает новый путь.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1]},
        stakes={0: [(1, 3.0)]},
    )
    vote = (
        await session.execute(
            select(Vote).where(Vote.round_id == round_row.id, Vote.player_id == 1)
        )
    ).scalar_one()
    vote.card_position = 1
    await session.commit()
    counts = await count_stakes_for_tally(session, round_row.id)
    assert counts == {0: 0, 1: to_nano(3), 2: 0}
    # Исход тоже сместится: MAJORITY по ставкам теперь путь 1.
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_leaderboard_follows_stake_decided_winner(session: AsyncSession) -> None:
    # Сердце за путь 0 (1,2,3 голосуют), деньги (5 Gram) за путь 1 (игрок 4).
    # MAJORITY по ставкам → путь 1. Верными на лидерборде признаны те, кто
    # голосовал за путь 1, несмотря на перевес бесплатных голосов за путь 0.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3], 1: [4], 2: []},
        stakes={0: [(1, 1.0)], 1: [(4, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    await award_points(session, closed)
    rows = (await session.execute(select(Player))).scalars().all()
    res = {p.id: (p.score, p.correct_picks) for p in rows}
    # Все голосовавшие +1; верный голос (игрок 4) +10 и correct_picks=1.
    assert res[1] == (1, 0)
    assert res[2] == (1, 0)
    assert res[3] == (1, 0)
    assert res[4] == (11, 1)


async def test_vote_counts_tally_is_separate(session: AsyncSession) -> None:
    # count_votes_for_tally по-прежнему считает голоса, а count_stakes_for_tally
    # — Gram: два независимых счёта, решает только второй при winner_by_stakes.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2], 1: [3], 2: []},
        stakes={0: [(1, 0.5)], 1: [(3, 4.0)]},
    )
    assert await count_votes_for_tally(session, round_row.id) == {0: 2, 1: 1, 2: 0}
    assert await count_stakes_for_tally(session, round_row.id) == {
        0: to_nano(0.5),
        1: to_nano(4.0),
        2: 0,
    }
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_results_post_stake_decided_text(session: AsyncSession) -> None:
    # Финальный пост итогов: исход решили ставки — кадр уцелел по счёту Gram,
    # а строки «на волоске» для ставок больше нет (она осталась голосам).
    rnd = Round(
        day_index=5,
        win_rule=WinRule.MAJORITY,
        winner_card=2,
        vote_counts_json='{"0": 5, "1": 1, "2": 0}',
        stake_counts_json=json.dumps(
            {"0": 1_000_000_000, "1": 3_000_000_000, "2": 3_010_000_000}
        ),
    )
    for pos, title in ((0, "Тропа A"), (1, "Тропа B"), (2, "Тропа C")):
        rnd.cards.append(
            Card(position=pos, title=title, consequence="к")
        )
    text = format_results(
        rnd,
        path_stakes={0: 1_000_000_000, 1: 3_000_000_000, 2: 3_010_000_000},
        multiplier=None,
    )
    assert "Тропа B" in text
    assert "Кадр дня уцелел по счёту Gram" in text
    assert "на волоске" not in text
    # Строки сцен пронумерованы римскими — та же нумерация, что и в итогах
    # дня (scene_label: «I. «название»»), позиция кнопки = позиция карты.
    assert "I: Тропа A: 5" in text
    assert "II: Тропа B: 1" in text
    assert "III: Тропа C: 0" in text
    assert "← 🏆 След" in text


# ---------- Флаг winner_by_stakes ----------


async def test_winner_by_stakes_off_falls_back_to_votes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WINNER_BY_STAKES=false: исход дня решают голоса, ставки не входят.

    Флаг документирован в README, но до сих пор не существовал в коде: настройки
    не было, а исход всегда считался по ставкам. Теперь рычаг настоящий — и
    выключенный режим честно откатывает именно к прежнему «сердцу» (голосам).
    """
    monkeypatch.setattr(settings, "winner_by_stakes", False)
    # Голоса за путь 0 (1,2,3), Gram за путь 1 — при разных счётах исход разойдётся.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3], 1: [4], 2: []},
        stakes={1: [(4, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 0, "при флаге off решают голоса, а не Gram"


async def test_winner_by_stakes_on_ignores_votes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная сторона того же рычага: при true голоса на исход не влияют.

    Сцена зеркальна предыдущему тесту — тот же день, но исход уезжает в сторону
    Gram. Без этой пары тестов «флаг не работает» выглядело бы как «работает».
    """
    monkeypatch.setattr(settings, "winner_by_stakes", True)
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3], 1: [4], 2: []},
        stakes={1: [(4, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1, "при флаге on решают суммы ставок"


async def test_winner_by_stakes_off_ignores_stakes_for_text_phrase(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Выключенный флаг берёт и ФОРМУЛИРОВКУ про голоса (VOTE_RULE_PHRASES).

    Формулировка берётся по признаку «счёт пришёл из ставок», который _decisive_counts
    возвращает вторым элементом пары. Проверяем, что флаг меняет и текст дня:
    иначе игрокам говорили бы «уцелеет кадр, собравший больше всего Gram», а
    считали бы по голосам.
    """
    from app.models import VOTE_RULE_PHRASES

    monkeypatch.setattr(settings, "winner_by_stakes", False)
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3], 1: [4], 2: []},
        stakes={1: [(4, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.stake_counts_json is None, "ставки не участвовали — счёт не голосовой"
    assert VOTE_RULE_PHRASES[WinRule.MAJORITY] == "уцелеет кадр, собравший больше всех голосов (день Большинства)"


def test_results_post_scene_numbers_without_stakes() -> None:
    # Голосовой день (без ставок): номера сцен всё равно проставлены.
    rnd = Round(
        day_index=6,
        win_rule=WinRule.MAJORITY,
        winner_card=1,
        vote_counts_json='{"0": 1, "1": 4, "2": 2}',
        stake_counts_json=None,
    )
    for pos, title in ((0, "Тропа A"), (1, "Тропа B"), (2, "Тропа C")):
        rnd.cards.append(
            Card(position=pos, title=title, consequence="к")
        )
    text = format_results(rnd)
    assert "I: Тропа A: 1" in text
    assert "II: Тропа B: 4" in text
    assert "III: Тропа C: 2" in text
