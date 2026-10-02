from __future__ import annotations

import logging
import random

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Round, Stake, Vote, WinRule
from app.stakes import current_network

logger = logging.getLogger(__name__)

# Театр жребия: реплики к честному броску при ничьей (детерминированы сидом).
_TIE_THEATER = (
    "Котёл булькнул дважды — жребий лёг на {chosen}.",
    "Жребий дня лёг на {paths} — и указал {chosen}.",
    "Часы Вокзала пробили полночь лишний раз: выпало {chosen}.",
)


async def count_votes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Счёт голосов для подведения итога дня.

    Совместимая обёртка: единственная реализация GROUP BY по голосам —
    plain_vote_counts (хранимый счёт и fallback-исход при пустом фонде).
    Имя сохранено — по нему ходят tally-джоба и тесты
    (test_winner_by_stakes).
    """
    return await plain_vote_counts(session, round_id)


async def plain_vote_counts(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Бесплатные голоса без легаси-бонуса ставок.

    Используется как хранимый счёт (vote_counts_json) и как fallback-исход,
    когда winner_by_stakes включён, но на день не поставлено ни одного грамма.
    """
    result = await session.execute(
        select(Vote.card_position, func.count())
        .where(Vote.round_id == round_id)
        .group_by(Vote.card_position)
    )
    counts = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        counts[int(position)] = int(total)
    return counts


async def count_stakes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Суммы подтверждённых ставок (нанотоны Gram) по путям дня.

    Путь привязан к ставке через голос игрока (Stake не хранит позицию):
    ставка игрока усиливает путь, за который он проголосовал. Только
    confirmed (деньги реально заблокированы) и активной сети (счёт должен
    совпадать с тем, по чему проходят выплаты). Пути без грамма = 0.
    """
    result = await session.execute(
        select(Vote.card_position, func.coalesce(func.sum(Stake.amount_nanotons), 0))
        .join(
            Stake,
            (Stake.round_id == Vote.round_id) & (Stake.player_id == Vote.player_id),
        )
        .where(
            Vote.round_id == round_id,
            Stake.status == "confirmed",
            Stake.network == current_network(),
        )
        .group_by(Vote.card_position)
    )
    sums = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        sums[int(position)] = int(total)
    return sums


async def _decisive_counts(
    session: AsyncSession,
    round_row: Round,
    vote_counts: dict[int, int],
) -> tuple[dict[int, int], bool]:
    """(решающий счёт, были ли это суммы ставок) для winner_by_stakes.

    Исход определяют суммы подтверждённых ставок; день, где
    нет ни одного грамма, решается бесплатными голосами (fallback).

    При settings.winner_by_stakes=false ставки в исход не входят вовсе:
    счёт всегда голосовой, и второй элемент пары — False, чтобы тексты
    дня взяли формулировку про голоса (RULE_PHRASES vs VOTE_RULE_PHRASES).
    """
    if not settings.winner_by_stakes:
        return vote_counts, False
    stakes = await count_stakes_for_tally(session, round_row.id)
    if any(value > 0 for value in stakes.values()):
        return stakes, True
    return vote_counts, False


def tied_positions(
    counts: dict[int, int],
    rule: WinRule,
    votes: dict[int, int] | None = None,
) -> list[int]:
    """Все пути, претендующие на победу по закону дня (без учёта позиций).

    votes (бесплатные голоса) — только для тай-брейка «ничьей на нуле»
    Меньшинства: пути с 0 Gram делят минимум, но сцена, где игроки
    голосовали без ставок, не равна невыбранной вовсе — уцелеет путь с
    меньшим числом голосов. Не-нулевые и прочие ничьи решаются жребием.
    """
    items = [(counts.get(i, 0), i) for i in range(3)]
    if rule is WinRule.MAJORITY:
        best = max(item[0] for item in items)
        return sorted(i for total, i in items if total == best)
    if rule is WinRule.MINORITY:
        worst = min(item[0] for item in items)
        candidates = sorted(i for total, i in items if total == worst)
        # Ничья на нуле: пустые пути (0 Gram) разделили минимум — решают голоса.
        if worst == 0 and votes is not None and len(candidates) > 1:
            fewest = min(votes.get(i, 0) for i in candidates)
            candidates = sorted(i for i in candidates if votes.get(i, 0) == fewest)
        return candidates
    ordered = sorted(items, key=lambda item: (item[0], item[1]))
    median = ordered[1][0]
    return sorted(i for total, i in items if total == median)


def pick_winner(
    counts: dict[int, int],
    rule: WinRule,
    seed: str | None = None,
    votes: dict[int, int] | None = None,
) -> int:
    """Победитель по закону дня.

    С энтропией мастерчейна (seed = «day:law:seqno:root_hash») — по последней
    цифре root_hash по модулю числа претендентов: любой игрок скопирует хеш
    блока из эксплорера и пересчитает исход в уме. Без энтропии — прежний
    детерминированный жребий (сид день+закон), чтобы ничья не «зависала» на
    недоступной сети. Без seed — fallback «меньший номер пути».
    """
    candidates = tied_positions(counts, rule, votes)
    if len(candidates) < 2:
        return candidates[0]
    if not seed:
        return candidates[0]
    parts = seed.split(":")
    if len(parts) == 4:
        try:
            return candidates[int(parts[3][-1], 16) % len(candidates)]
        except (TypeError, ValueError) as exc:
            # Хвост seed не похож на хеш блока — выбор всё равно детерминирован
            # по строке ниже, но разбор стоит увидеть.
            logger.debug("Хвост seed %r не разобран (%s) — выбор по строке", seed, exc)
    return random.Random(f"law:{seed}").choice(candidates)


def tie_seed(round_row: Round) -> str:
    """Сидовое значение жеребьёвки дня — с честной энтропией мастерчейна.

    Формат «{day}:{law}[:{entropy}]»: при ничьей энтропия снимается ОДИН раз
    (close_voting) и сохраняется в день, поэтому пересчёт (heal, finish_tally)
    даёт тот же победитель. Без энтропии — прежний детерминированный жребий.
    """
    base = f"{round_row.day_index}:{round_row.win_rule.value}"
    entropy = getattr(round_row, "tie_entropy", None)
    if entropy:
        return f"{base}:{entropy}"
    return base


async def _winner_and_tied(
    session: AsyncSession,
    round_row: Round,
    counts: dict[int, int],
    seed: str,
    votes: dict[int, int] | None = None,
) -> tuple[int, list[int]]:
    """Выбор победителя по закону дня.

    counts уже решающие (суммы ставок или голоса) — закон дня
    применяется к ним напрямую.
    """
    return pick_winner(counts, round_row.win_rule, seed=seed, votes=votes), tied_positions(
        counts, round_row.win_rule, votes
    )
