from __future__ import annotations

import json
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    RULE_PHRASES,
    VOTE_RULE_PHRASES,
    LeaderboardPot,
    PackFund,
    Payout,
    Player,
    Round,
    Stake,
    Vote,
    WeeklyPot,
)
from app.rounds import pick_winner
from app.rounds.time import _ROMAN
from app.stakes import current_network
from app.ton_utils import from_nano
from app.weeks import iso_week_key

logger = logging.getLogger(__name__)


_CHUNK = 500  # лимит параметров IN(...): большие дни чанкуются


def _tg_escape(text: str) -> str:
    """Экранирование спецсимволов HTML для Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunks(ids: list[int], size: int = _CHUNK):
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


async def award_points(session: AsyncSession, round_row: Round) -> int:
    if round_row.winner_card is None:
        return 0
    voters = await session.execute(select(Vote.player_id).where(Vote.round_id == round_row.id))
    voter_ids = [row[0] for row in voters.all()]
    for chunk in _chunks(voter_ids):
        await session.execute(
            update(Player).where(Player.id.in_(chunk)).values(score=Player.score + 1)
        )
    winners = await session.execute(
        select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
    )
    winner_ids = [row[0] for row in winners.all()]
    for chunk in _chunks(winner_ids):
        await session.execute(
            update(Player)
            .where(Player.id.in_(chunk))
            .values(score=Player.score + 10, correct_picks=Player.correct_picks + 1)
        )
    # Обновление стриков: победители увеличивают, проигравшие сбрасывают
    from app.streaks import update_streak

    # Все голосовавшие
    all_voters_result = await session.execute(
        select(Player).where(Player.id.in_(voter_ids))
    )
    all_voters = {p.id: p for p in all_voters_result.scalars().all()}

    winner_set = set(winner_ids)
    for voter_id in voter_ids:
        player = all_voters.get(voter_id)
        if player:
            was_correct = voter_id in winner_set
            await update_streak(session, player, was_correct)

    await session.commit()
    return len(winner_ids)


_FLIP_SEARCH_CAP = 15  # отрыв больше этого уже не «на волоске» — строку не пишем


def _votes_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "голос"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "голоса"
    return "голосов"


def flip_margin(counts: dict[int, int], win_rule, winner_card: int | None) -> tuple[int, int] | None:
    """«Канон на волоске»: минимальное число переносов голосов, меняющее исход.

    Перенос = один голос меняет путь. Ищем минимальный k, при котором
    существует перераспределение k голосов, дающее другого победителя по
    закону дня. Возвращает (k, альтернативный победитель) или None: развязка
    была плотной, но перебор ограничен капом — разгромные дни недраматичны.
    """
    if winner_card is None or win_rule is None:
        return None
    total = sum(counts.values())
    if total < 2:
        return None
    base = [counts.get(i, 0) for i in range(3)]
    cap = min(total, _FLIP_SEARCH_CAP)
    for k in range(1, cap + 1):
        # Все способы снять k голосов с трёх путей.
        takings = [
            (d0, d1, k - d0 - d1)
            for d0 in range(min(k, base[0]) + 1)
            for d1 in range(min(k - d0, base[1]) + 1)
            if 0 <= k - d0 - d1 <= base[2]
        ]
        # Все способы положить k голосов обратно (куда угодно).
        gives = [
            (g0, g1, k - g0 - g1)
            for g0 in range(k + 1)
            for g1 in range(k - g0 + 1)
        ]
        for taking in takings:
            for giving in gives:
                fresh = {i: base[i] - taking[i] + giving[i] for i in range(3)}
                alt = pick_winner(fresh, win_rule)
                if alt != winner_card:
                    return k, alt
    return None


def format_results(
    round_row: Round,
    path_stakes: dict[int, int] | None = None,
    multiplier: float | None = None,
) -> str:
    import json

    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    stake_raw = json.loads(round_row.stake_counts_json or "{}")
    stake_counts = {int(key): int(value) for key, value in stake_raw.items()} if stake_raw else None
    names = {card.position: _tg_escape(card.title) for card in round_row.cards}
    lines = [f"📼 День {round_row.day_index} — кадр записан"]
    if not stake_counts:
        # «Запись на волоске»: сколько голосов отделяло мир от другого исхода.
        margin = flip_margin(counts, getattr(round_row, "win_rule", None), round_row.winner_card)
        if margin is not None:
            k, alt = margin
            alt_name = names.get(alt)
            if alt_name:
                word = _votes_word(k)
                lines.append(
                    f"🩸 на волоске: ещё {k} {word} за «{alt_name}» — "
                    "и сцена повела бы иначе."
                )
    day_phrases = RULE_PHRASES if stake_counts else VOTE_RULE_PHRASES
    lines.append(f"🎬 Сцена дня: {day_phrases[round_row.win_rule]}")
    if stake_counts:
        lines.append("💰 Кадр дня уцелел по счёту Gram — голоса ведут лидерборд")
    lines.append("")
    stakes = path_stakes or {}
    for position in range(3):
        mark = " ← 🏆 След" if position == round_row.winner_card else ""
        stake_nano = stakes.get(position, 0)
        stake_str = f" ({from_nano(stake_nano):.2f} Gram)" if stake_nano > 0 else ""

        lines.append(
            f"{_ROMAN[position]}: {names[position]}: {counts.get(position, 0)}{stake_str}{mark}"
        )
    # Коэффициент: если есть ставки на победивший путь
    if multiplier is not None and multiplier > 0:
        lines.append(f"🎯 Коэффициент: ×{multiplier:.2f}")
    if getattr(round_row, "tie_note", None):
        lines.append(f"🤝 {round_row.tie_note}")
    return "\n".join(lines)


async def format_plugin_results(
    round_row: Round, session: AsyncSession | None = None
) -> str:
    """Плагиновые строки итогов отключены (слой сюжета снят)."""
    return ""


async def day_economics(session: AsyncSession, round_row: Round) -> dict:
    """Цифры дня для поста итогов: банк, проценты, коэффициент, копилка.

    Читает уже созданные финализацией выплаты, поэтому вызывать стоит после
    finalize_day_payouts; при пустом банке деньги просто не попадут в пост.
    """
    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    players = sum(counts.values())
    # Ставки по путям: сколько денег стояло за каждый вариант (по голосам
    # игроков). Победившему пути — отдельная строка с долей банка.
    path_stakes_rows = await session.execute(
        select(Vote.card_position, func.coalesce(func.sum(Stake.amount_nanotons), 0))
        .join(Stake, Stake.player_id == Vote.player_id)
        .where(
            Vote.round_id == round_row.id,
            Stake.round_id == round_row.id,
            Stake.status == "confirmed",
            Stake.network == current_network(),
        )
        .group_by(Vote.card_position)
    )
    stats: dict = {
        "players": players,
        "counts": counts,
        "pot": round_row.pot_nanotons or 0,
        "multiplier": None,
        "week_today": round_row.weekly_nanotons or 0,
        "referral_today": round_row.referral_nanotons or 0,
        "week_total": 0,
        "board_today": 0,
        "bank_total": 0,
        "fund_total": 0,
        "refunded": False,
        "path_stakes": {int(p): int(v) for p, v in path_stakes_rows.all()},
    }
    stats["winner_stake"] = (
        stats["path_stakes"].get(round_row.winner_card, 0)
        if round_row.winner_card is not None
        else 0
    )
    stats["winner_share_pct"] = (
        round(stats["winner_stake"] * 100 / stats["pot"]) if stats["pot"] else None
    )
    if not players and not stats["pot"]:
        return stats

    # Combine leaderboard + prize sums into one query
    kind_rows = await session.execute(
        select(Payout.kind, func.coalesce(func.sum(Payout.amount_nanotons), 0))
        .where(
            Payout.round_id == round_row.id,
            Payout.kind.in_(["leaderboard", "prize"]),
        )
        .group_by(Payout.kind)
    )
    kind_map = {k: int(v) for k, v in kind_rows.all()}
    board_today = kind_map.get("leaderboard", 0)
    prize_sum = kind_map.get("prize", 0)
    bank_row = await session.execute(select(func.coalesce(func.sum(LeaderboardPot.nanotons), 0)))
    stats["bank_total"] = int(bank_row.scalar_one())
    week_row = await session.execute(
        select(func.coalesce(func.sum(WeeklyPot.nanotons), 0)).where(
            WeeklyPot.week == iso_week_key(round_row.opens_at)
        )
    )
    stats["week_total"] = int(week_row.scalar_one())
    # Фонд Стаи — единое накопление без периода: показываем общий баланс,
    # а не «сегодня», т.к. разыгрывается вручную, а не по расписанию.
    fund_row = await session.execute(
        select(func.coalesce(func.sum(PackFund.nanotons), 0))
    )
    stats["fund_total"] = int(fund_row.scalar_one())
    if board_today or stats["bank_total"]:
        stats["board_today"] = board_today

    if round_row.winner_card is not None:
        winners_subq = select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
        staked_winners = await session.execute(
            select(func.coalesce(func.sum(Stake.amount_nanotons), 0)).where(
                Stake.round_id == round_row.id,
                Stake.network == current_network(),
                Stake.status == "confirmed",
                Stake.player_id.in_(winners_subq),
            )
        )
        winning_total = int(staked_winners.scalar_one())
        if winning_total > 0 and prize_sum > 0:
            stats["multiplier"] = prize_sum / winning_total
        elif stats["pot"] > 0 and prize_sum == 0 and stats["week_today"] == 0:
            # Настоящий возврат: на верный путь не ставил никто. Если же
            # week_today > 0, призовые сгорели в газ сети и ушли в копилку
            # недели — это не возврат, и строка про возврат не показывается.
            stats["refunded"] = True
    return stats


def format_economics(stats: dict) -> str:
    """Текстовый блок экономики дня; без банка показывает только ставки.

    Явка и счёт путей уже есть в основном тексте итогов — здесь только
    деньги, чтобы одна цифра не встречалась в посте дважды. Неделю и месяц
    не вываливаем: их балансы видны по выплатам, а перегруз цифр съедает
    внимание от сюжета.
    """
    lines: list[str] = []
    if stats["pot"] <= 0:
        return "\n".join(lines)
    ton = from_nano
    lines.insert(0, f"💰 Банк дня: {ton(stats['pot']):.2f} Gram")
    if stats["refunded"]:
        lines.append("🎯 На верную сцену не поставил никто — все ставки возвращены игрокам")
    if stats["fund_total"] > 0:
        lines.append(f"🐾 В Фонде Стаи: {ton(stats['fund_total']):.2f} Gram")
    if stats.get("referral_today", 0) > 0:
        lines.append(f"🎁 Реферальная награда пригласивших: {ton(stats['referral_today']):.2f} Gram")
    return "\n".join(lines)
