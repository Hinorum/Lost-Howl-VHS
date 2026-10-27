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

    votes (бесплатные голоса) — тай-брейк «равные банки → голоса»: если
    закон дня по счёту оставил несколько претендентов, среди них побеждает
    тот, кого выбирает ТОТ ЖЕ закон по голосам — Большинство берёт больше
    голосов, Меньшинство меньше, Медиана середину. Ничья на нуле у
    Меньшинства решалась голосами и раньше (сцена, где игроки голосовали
    без ставок, не равна невыбранной вовсе); остальные ничьи подключаются
    флагом tie_break_by_votes (false — прежний жребий). Если и голоса
    равны — претенденты остаются, и жребий (энтропия мастерчейна) решает
    исход, как прежде.
    """
    items = [(counts.get(i, 0), i) for i in range(3)]
    if rule is WinRule.MAJORITY:
        best = max(item[0] for item in items)
        candidates = sorted(i for total, i in items if total == best)
    elif rule is WinRule.MINORITY:
        worst = min(item[0] for item in items)
        candidates = sorted(i for total, i in items if total == worst)
        if worst == 0 and votes is not None and len(candidates) > 1:
            candidates = _narrow_by_votes(candidates, rule, votes)
    else:
        ordered = sorted(items, key=lambda item: (item[0], item[1]))
        median = ordered[1][0]
        candidates = sorted(i for total, i in items if total == median)
    if (
        getattr(settings, "tie_break_by_votes", False)
        and votes is not None
        and len(candidates) > 1
    ):
        candidates = _narrow_by_votes(candidates, rule, votes)
    return candidates


def _narrow_by_votes(
    candidates: list[int], rule: WinRule, votes: dict[int, int]
) -> list[int]:
    """Сужение претендентов голосом стаи — тем же законом дня по голосам."""
    valued = [(votes.get(i, 0), i) for i in candidates]
    if rule is WinRule.MAJORITY:
        best = max(value for value, _ in valued)
        return sorted(i for value, i in valued if value == best)
    if rule is WinRule.MINORITY:
        worst = min(value for value, _ in valued)
        return sorted(i for value, i in valued if value == worst)
    # Медиана: середина по голосам; при трёх претендентах — настоящая
    # середина, при двух — верхний из средних (голос стаи перевешивает).
    # Все голоса равны — медиана не различает: набор остаётся жребию.
    if len({value for value, _ in valued}) == 1:
        return candidates
    ordered = sorted(valued)
    return [ordered[len(ordered) // 2][1]]


def pick_winner(
    counts: dict[int, int],
    rule: WinRule,
    seed: str | None = None,
    votes: dict[int, int] | None = None,
) -> int:
    """Победитель по закону дня.

    С энтропией мастерчейна (seed = «day:law:seqno:root_hash») — по всему
    root_hash по модулю числа претендентов: любой игрок скопирует хеш блока
    из эксплорера и пересчитает исход. Используется весь 256-битный хеш, а не
    последняя hex-цифра: на трёх претендентах последняя цифра давала бы
    перекос 6/5/5 (37,5% / 31,25% / 31,25%). Остаточный перекос от деления
    256-битного пространства на три — порядка 2^-256, то есть неразличим.

    С локальной энтропией (seed = «day:law:local-…») — выбор по сохранённой в
    дне случайности: она никому не известна заранее, но повторяется при
    пересчёте. Прежний детерминированный жребий (сид день+закон) виден всем:
    исход дня можно было вычислить заранее и поставить на него.

    Без seed — fallback «меньший номер пути».
    """
    candidates = tied_positions(counts, rule, votes)
    if len(candidates) < 2:
        return candidates[0]
    if not seed:
        return candidates[0]
    parts = seed.split(":")
    if len(parts) == 4:
        try:
            return candidates[int(parts[3], 16) % len(candidates)]
        except (TypeError, ValueError) as exc:
            # Последняя часть seed не похожа на хеш блока — выбор всё равно
            # детерминирован по строке ниже, но разбор стоит увидеть.
            logger.debug("Энтропия seed %r не разобрана (%s) — выбор по строке", seed, exc)
    return random.Random(f"law:{seed}").choice(candidates)


def tie_seed(round_row: Round) -> str:
    """Сидовое значение жеребьёвки дня — с честной энтропией мастерчейна.

    Формат «{day}:{law}[:{entropy}]»: при ничьей энтропия снимается ОДИН раз
    (close_voting) и сохраняется в день, поэтому пересчёт (heal, finish_tally)
    даёт тот же победитель. Без энтропии — прежний детерминированный жребий
    (день с деньгами так не остаётся: close_voting подставляет локальную
    случайность, см. LOCAL_TIE_PREFIX).
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
