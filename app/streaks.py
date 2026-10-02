"""Система стриков и титулов прогрессии.

Титулы присваиваются автоматически за серию правильных голосований.
Стрик считается из current_streak / best_streak в модели Player.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, Round, Vote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Title:
    key: str
    name: str
    emoji: str
    description: str
    correct_needed: int


# Пороги титулов: от 3 до 50 правильных подряд
TITLES: tuple[Title, ...] = (
    Title("novice", "Щенок", "🐾", "Первые шаги на сцене", 0),
    Title("tracking", "Следопыт", "🐾", "Три верные сцены подряд", 3),
    Title("scout", "Разведчик", "🦊", "Пять верных сцен подряд", 5),
    Title("ranger", "Следопыт Стаи", "🐺", "Семь верных сцен подряд", 7),
    Title("oracle", "Оракул", "🔮", "Десять верных сцен подряд — стая помнит твой нюх", 10),
    Title("sage", "Мудрец", "📜", "Пятнадцать верных сцен — ты читаешь мир как папку", 15),
    Title("elder", "Старейшина", "🏛️", "Двадцать верных сцен — стая идёт за тобой", 20),
    Title("legend", "Легенда Стаи", "⭐", "Тридцать верных сцен — твой нюх стал легендой", 30),
    Title("prophet", "Пророк", "🌟", "Пятьдесят верных сцен — ты видишь завтра", 50),
)


def title_for_streak(streak: int) -> Title:
    """Возвращает титул по текущему стрику."""
    result = TITLES[0]
    for title in TITLES:
        if streak >= title.correct_needed:
            result = title
    return result


def next_title(streak: int) -> Title | None:
    """Возвращает следующий титул, к которому стоит стремиться, или None если максимальный."""
    for title in TITLES:
        if streak < title.correct_needed:
            return title
    return None


async def update_streak(session: AsyncSession, player: Player, was_correct: bool) -> None:
    """Обновляет стрик игрока после подсчёта голосов."""
    if was_correct:
        player.current_streak += 1
        if player.current_streak > player.best_streak:
            player.best_streak = player.current_streak
    else:
        player.current_streak = 0


def streak_lines(player: Player) -> list[str]:
    """Строки серии без заголовка титула: серия, цель и память кадра."""
    current = player.current_streak
    best = player.best_streak
    nxt = next_title(current)

    lines: list[str] = []
    if current > 0:
        lines.append(f"🔥 Серия верных сцен: {current} · Лучшая: {best}")
    else:
        lines.append(f"🔥 Лучшая серия: {best}")

    if nxt:
        remaining = nxt.correct_needed - current
        lines.append(
            f"📈 До следующего титула: {nxt.emoji} {nxt.name} — "
            f"ещё {remaining} {remaining_word(remaining)}"
        )
    elif current >= TITLES[-1].correct_needed:
        lines.append("🏆 Ты достиг вершины. Стая идёт за тобой.")
    if current >= 10:
        lines.append("🧠 Ты помнишь дольше остальных — память кадра держится на тебе.")

    return lines


def streak_text(player: Player) -> str:
    """Форматирует текст стрика для /score."""
    title = title_for_streak(player.current_streak)
    return "\n".join([f"{title.emoji} <b>{title.name}</b>", *streak_lines(player)])


def remaining_word(n: int) -> str:
    """Склонение слова «сцена/сцены/сцен» для числа."""
    if n % 10 == 1 and n % 100 != 11:
        return "сцена"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "сцены"
    return "сцен"


async def calc_rank(session: AsyncSession, player_id: int) -> dict:
    """Счётчики игрока за текущую неделю и месяц.

    Возвращает количество голосов и верных выборов (Vote.card_position
    совпал с Round.winner_card) в днях недели и месяца. Лидербордов здесь
    нет — карточка Стаи показывает только личные цифры.
    """
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def _counters(since: datetime) -> tuple[int, int]:
        result = await session.execute(
            select(
                func.count(Vote.id).label("votes"),
                func.sum(case((Vote.card_position == Round.winner_card, 1), else_=0)).label(
                    "correct"
                ),
            )
            .join(Round, Vote.round_id == Round.id)
            .where(Round.opens_at >= since, Vote.player_id == player_id)
        )
        row = result.one()
        return int(row.votes or 0), int(row.correct or 0)

    week_votes, week_correct = await _counters(week_start)
    month_votes, month_correct = await _counters(month_start)

    return {
        "week_votes": week_votes,
        "week_correct": week_correct,
        "month_votes": month_votes,
        "month_correct": month_correct,
    }
