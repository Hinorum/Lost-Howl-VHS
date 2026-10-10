"""Пульт хранителя: одна команда вместо пяти диагностических.

`/treasury`, `/payouts`, `/blockchain`, `/mirror`, `/stakes` показывают
подробности по одному узлу. Но на вопрос «что СЕЙЧАС сломано» они не отвечают
ни вместе, ни по отдельности: список тревог жил в логе планировщика, а
`/health` отдавал другой, неполный набор чисел. Хранителю приходилось
помнить, что и в каком порядке спрашивать, и не отличать «всё хорошо» от
«проверки давно не идут, поэтому всё хорошо».

Здесь — тот же список, что уходит в тревоги (снимок check_anomalies), плюс
возраст этого снимка: растущий возраст означает, что перестал идти сам sweeper.
Для затянувшихся тревог добавляется, сколько они уже держатся.
"""

from __future__ import annotations

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.ops import problems_snapshot, snapshot
from app.style import ok_mark, warn_mark

from .common import router

# Снимок старше этого — сигнатура о том, что молчит сама проверка, а не игрок.
_STALE_SWEEP_AFTER = 600.0


def _span(seconds: float) -> str:
    """Длительность человеческим языком: «12 с», «3 мин», «2 ч 14 мин»."""
    if seconds < 60:
        return f"{int(seconds)} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    return f"{int(seconds // 3600)} ч {int((seconds % 3600) // 60)} мин"


def _age_phrase(seconds: float | None) -> str:
    """Возраст как «давно»: «12 с назад», «никогда»."""
    if seconds is None:
        return "никогда"
    return f"{_span(seconds)} назад"


def _queue_line(by_kind: dict, dead_by_kind: dict, pending_stakes: int) -> str:
    parts = []
    total = sum(int(v) for v in by_kind.values())
    if total:
        detail = ", ".join(
            f"{kind} {count}" for kind, count in sorted(by_kind.items()) if count
        )
        parts.append(f"очередь выплат {total} ({detail})")
    dead = sum(int(v) for v in dead_by_kind.values())
    if dead:
        parts.append(f"безнадёжных {dead}")
    if pending_stakes:
        parts.append(f"ставок не обработано {int(pending_stakes)}")
    if not parts:
        return "Деньги: очередь пуста, необработанных ставок нет"
    return "Деньги: " + " · ".join(parts)


async def _ops_diag_text() -> str:
    """Текст пульта: вердикт, список проблем, возраст проверок и опора."""
    data = await snapshot()
    problems, age, details = await problems_snapshot()
    lines: list[str] = []

    if problems:
        lines.append(f"{warn_mark('ops')} Пульт: тревог — {len(problems)}")
        ages = {
            str(item.get("text", "")): item.get("age") for item in (details or [])
        }
        for problem in problems:
            held = ages.get(problem)
            # «Очередь выплат стоит 42 мин» и «…(третий час)» — разная срочность:
            # часы показывают, что минутный текст уже не про свежесть.
            tail = ""
            if held is not None and held >= 3600:
                tail = f" — держится {_span(held)}"
            lines.append(f"• {problem}{tail}")
    else:
        lines.append(f"{ok_mark('ops')} Пульт: тревог нет")

    if age is None:
        lines.append(
            "⚠️ Проверки аномалий ещё ни разу не шли — вердикт выше неполон "
            "(следи за логами планировщика)."
        )
    elif age > _STALE_SWEEP_AFTER:
        lines.append(
            f"⚠️ Снимок проверок устарел на {_span(age)} — сама проверка не идёт, "
            "список выше может быть неправдой. Причина обычно в главном тике "
            "(/health last_tick_age)."
        )

    beat_age = data.get("watcher_beat_age")
    schedule = (
        f"Расписание: тик {_age_phrase(data.get('last_tick_age'))} · "
        f"проверки {_age_phrase(age)} · "
        f"watcher {_age_phrase(beat_age) if data.get('ton_enabled') else 'выключен'}"
    )
    if data.get("tick_failures"):
        schedule += f" · падений тика подряд {data['tick_failures']}"
    lines.append(schedule)
    lines.append(
        _queue_line(
            data.get("payout_pending_by_kind") or {},
            data.get("payout_dead_by_kind") or {},
            int(data.get("pending_stakes") or 0),
        )
    )
    lines.append(_audience_line(data.get("announce_audience") or {}))
    lines.append(
        f"Раунд: {_round_line(data)} · аптайм {_age_phrase(data.get('uptime_seconds'))}"
    )
    lines.append("Разбор: /treasury · /payouts · /stakes · /blockchain · /mirror")
    return "\n".join(lines)


def _audience_line(audience: dict) -> str:
    """Кому вообще может уйти новость дня — одним числом и без запроса к БД.

    Ровно тот вопрос, на который раньше приходилось идти в прод-базу: пустая
    аудитория и молчащая рассылка выглядели как поломка кода. Отдельно
    показывается и день, чей анонс уже ушёл в пустоту: он остаётся в списке,
    пока тревога не снимется сама.
    """
    total = int(audience.get("total") or 0)
    if not total:
        line = (
            f"{warn_mark('ops')} Аудитория рассылки: пусто — новость дня не увидит "
            "никто (канал/группа: добавь бота в чат — первое сообщение "
            "привяжет его само, игроки: /start)."
        )
    else:
        line = (
            f"Аудитория рассылки: {int(audience.get('chats') or 0)} чат(ов) · "
            f"{int(audience.get('dm') or 0)} в личку"
        )
    empty_day = audience.get("empty_day")
    if empty_day:
        line += f"\n{warn_mark('ops')} Анонс дня {empty_day} ушёл в пустоту — тревога ещё не снята."
    return line


def _round_line(data: dict) -> str:
    round_row = data.get("round")
    if not round_row:
        return "день ещё не создан"
    status = round_row.get("status")
    return f"день {round_row.get('day_index')} ({status})"


@router.message(Command("ops"), F.chat.type == ChatType.PRIVATE)
async def cmd_ops(message: Message) -> None:
    """Пульт: что сломано прямо сейчас, одним сообщением."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Пульт — только для хранителя.")
        return
    try:
        await message.answer(await _ops_diag_text())
    except Exception as exc:
        await message.answer(f"Пульт не собрался: {type(exc).__name__}")
