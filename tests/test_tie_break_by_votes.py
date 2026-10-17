"""«Равные банки → голоса» (TIE_BREAK_BY_VOTES):

если закон дня по суммам ставок оставил несколько претендентов (ничья
Gram), среди них выбирает ТОТ ЖЕ закон дня по бесплатным голосам —
Большинство больше голосов, Меньшинство меньше, Медиана середину.
Ничья на нуле у Меньшинства так решалась и до флага; флаг распространяет
правило на остальные ничьи. Если и голоса равны (а при выключенном флаге —
сразу) остаётся жребий мастерчейна, как прежде.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Card, Payout, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.rounds import close_voting, finish_tally, pick_winner, tied_positions
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
        round_row.cards.append(Card(position=pos, title=f"t{pos}", consequence="к"))
    return round_row


async def _seed_day(
    session: AsyncSession,
    rule: WinRule,
    votes: dict[int, list[int]],
    stakes: dict[int, list[tuple[int, float]]],
    day_index: int = 1,
) -> Round:
    """День в подсчёте; формат как в test_winner_by_stakes._seed_day."""
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


# ---------- Юнит: сужение претендентов голосом стаи ----------


def test_majority_tie_broken_by_more_votes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Банки 5/5/1 — ничья лидеров; голоса 2 против 7 — верх берёт путь 1.
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 5, 1: 5, 2: 1}
    votes = {0: 2, 1: 7, 2: 9}
    assert tied_positions(stakes, WinRule.MAJORITY, votes) == [1]
    assert pick_winner(stakes, WinRule.MAJORITY, "9:majority:zzz", votes) == 1


def test_majority_tie_flag_off_keeps_entropy_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tie_break_by_votes", False)
    stakes = {0: 5, 1: 5, 2: 1}
    votes = {0: 2, 1: 7, 2: 9}
    assert tied_positions(stakes, WinRule.MAJORITY, votes) == [0, 1]


def test_minority_nonzero_tie_broken_by_fewer_votes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ничья на 3 Gram; Меньшинство по голосам — путь с меньшим числом (1).
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 5, 1: 3, 2: 3}
    votes = {0: 9, 1: 1, 2: 8}
    assert tied_positions(stakes, WinRule.MINORITY, votes) == [1]


def test_minority_zero_tie_still_fewest_votes_with_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ничья на нуле работала голосами и раньше; флаг её не меняет.
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: to_nano(1.0), 1: 0, 2: 0}
    votes = {0: 2, 1: 0, 2: 2}
    assert tied_positions(stakes, WinRule.MINORITY, votes) == [1]


def test_median_tie_two_candidates_upper_vote_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Медиана банков: 0/0/5 — медиана 0, претенденты 0 и 1; голоса 1 против 4:
    # при двух претендентах верхний из средних — голос стаи перевешивает.
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 0, 1: 0, 2: 5}
    votes = {0: 1, 1: 4, 2: 2}
    assert tied_positions(stakes, WinRule.MEDIAN, votes) == [1]


def test_median_tie_three_candidates_middle_votes_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 2, 1: 2, 2: 2}
    votes = {0: 5, 1: 9, 2: 2}
    assert tied_positions(stakes, WinRule.MEDIAN, votes) == [0]


def test_median_tie_equal_votes_stays_for_entropy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Голоса равны — медиана не различает: претенденты остаются жребию.
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 0, 1: 0, 2: 5}
    votes = {0: 3, 1: 3, 2: 0}
    assert tied_positions(stakes, WinRule.MEDIAN, votes) == [0, 1]


def test_majority_tie_equal_votes_stays_for_entropy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 4, 1: 4, 2: 1}
    votes = {0: 6, 1: 6, 2: 0}
    assert tied_positions(stakes, WinRule.MAJORITY, votes) == [0, 1]


# ---------- Интеграция: итоги и энтропия ----------


async def test_finish_tally_vote_tie_note(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ничья банков решена голосами — в итогах появляется честная строка."""
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2], 1: [3, 4, 5, 6, 7]},
        stakes={0: [(1, 2.0)], 1: [(3, 2.0)]},
        day_index=701,
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    note = closed.tie_note or ""
    assert "Счёт Gram на сценах разделился" in note
    assert "голос стаи вывел сцену II" in note
    assert json.loads(closed.stake_counts_json) == {
        "0": to_nano(2.0),
        "1": to_nano(2.0),
        "2": 0,
    }
    # Голоса остались в своём счёте — по ним и решалась ничья.
    assert json.loads(closed.vote_counts_json) == {"0": 2, "1": 5, "2": 0}


async def test_finish_tally_flag_off_keeps_draw_note(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без флага та же ничья уходит в жребий, и строка — про жребий."""
    monkeypatch.setattr(settings, "tie_break_by_votes", False)
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2], 1: [3, 4, 5, 6, 7]},
        stakes={0: [(1, 2.0)], 1: [(3, 2.0)]},
        day_index=702,
    )
    closed, _ = await finish_tally(session, round_row)
    note = closed.tie_note or ""
    assert "Счёт Gram на сценах разделился" in note
    assert "голос стаи" not in note


async def test_close_voting_flag_on_skips_entropy_when_votes_resolve(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ничья, разрешённая голосами, не трогает мастерчейн-энтропию."""
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    captured: list[str] = []

    async def fake_fetch() -> str:
        captured.append("fetch")
        return "93123949:abcdef"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = _round_row(WinRule.MAJORITY, 703)
    round_row.status = RoundStatus.OPEN
    session.add(round_row)
    await session.commit()
    for pid, path in ((1, 0), (2, 0), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1)):
        session.add(Player(id=pid))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=1,
            amount_nanotons=to_nano(2.0),
            tx_hash="tx-703-a",
            status="confirmed",
        )
    )
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=3,
            amount_nanotons=to_nano(2.0),
            tx_hash="tx-703-b",
            status="confirmed",
        )
    )
    await session.commit()

    await close_voting(session, round_row)
    loaded = await session.get(Round, round_row.id)
    assert loaded.winner_card == 1
    assert captured == []  # энтропии не снимали — ничью решили голоса
    assert loaded.tie_entropy is None


# ---------- Выплаты: деньги идут строго за победившим путём ----------


def test_votes_cannot_resurrect_non_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Голоса не могут вытащить путь, проигравший по банкам.

    Сужение идёт только внутри списка кандидатов закона дня: путь III
    с 4 Gram (меньше максимума) не станет победителем даже с 999 голосами.
    """
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    stakes = {0: 5, 1: 5, 2: 4}
    votes_equal = {0: 1, 1: 1, 2: 999}
    assert tied_positions(stakes, WinRule.MAJORITY, votes_equal) == [0, 1]
    votes_decided = {0: 9, 1: 1, 2: 999}
    assert tied_positions(stakes, WinRule.MAJORITY, votes_decided) == [0]
    assert pick_winner(stakes, WinRule.MAJORITY, "9:majority:zzz", votes_decided) == 0


async def test_vote_tie_pays_stakers_of_chosen_path(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ничья банков, решённая голосами: приз получают поставившие на
    победившую сторону, а проигравшая равная сторона не получает ни приза,
    ни возврата — её ставка уходит в пул, как и при любой развязке дня."""
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={
            0: [811_001, 811_002],
            1: [811_003, 811_004, 811_005, 811_006, 811_007],
        },
        stakes={0: [(811_001, 2.0)], 1: [(811_003, 2.0)]},
        day_index=704,
    )
    closed, done = await finish_tally(session, round_row)
    assert done and closed.winner_card == 1
    assert "голос стаи" in (closed.tie_note or "")

    created = await stakes_mod.finalize_day_payouts(session, closed)
    assert created >= 1
    payouts = list((await session.execute(select(Payout))).scalars())
    prizes = {row.player_id: row.amount_nanotons for row in payouts if row.kind == "prize"}
    # Приз — только поставившему на победивший путь (II).
    assert set(prizes) == {811_003}
    assert prizes[811_003] > 0
    # Проигравшая сторона (I): ни приза, ни возврата.
    assert not [row for row in payouts if row.kind == "refund"]
    lost = (
        await session.execute(
            select(Stake).where(
                Stake.round_id == round_row.id, Stake.player_id == 811_001
            )
        )
    ).scalar_one()
    assert lost.status == "confirmed"


async def test_vote_tie_on_empty_paths_refunds_everyone(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ничья на нулях (Меньшинство): голоса указали на сцену без ставивших —
    деньги не пропадают: все подтверждённые ставки возвращаются."""
    monkeypatch.setattr(settings, "tie_break_by_votes", True)
    monkeypatch.setattr(settings, "ton_enabled", True)
    round_row = await _seed_day(
        session,
        WinRule.MINORITY,
        votes={
            0: [812_001, 812_002],
            1: [812_003],
            2: [812_004, 812_005, 812_006, 812_007, 812_008],
        },
        stakes={0: [(812_001, 5.0)]},
        day_index=705,
    )
    # Банки 5/0/0: у Меньшинства ничья нулей у путей II и III; голоса: II — 1, III — 5.
    closed, done = await finish_tally(session, round_row)
    assert done and closed.winner_card == 1
    assert "голос стаи вывел сцену II" in (closed.tie_note or "")

    created = await stakes_mod.finalize_day_payouts(session, closed)
    assert created == 1  # один возврат, призов нет
    refunds = [
        row
        for row in (await session.execute(select(Payout))).scalars()
        if row.kind == "refund"
    ]
    assert [row.player_id for row in refunds] == [812_001]
    assert refunds[0].amount_nanotons == stakes_mod.refund_net_amount(to_nano(5))
    refunded = (
        await session.execute(
            select(Stake).where(
                Stake.round_id == round_row.id, Stake.player_id == 812_001
            )
        )
    ).scalar_one()
    assert refunded.status == "refunded"
