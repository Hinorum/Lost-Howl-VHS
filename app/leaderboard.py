"""Автоматические выплаты копилок лидерборда: недели и месяца.

**Неделя.** Каждый день 2% фонда капает в копилку недели (WeeklyPot). В
понедельник приз уходит трём сильнейшим игрокам прошедшей недели — доли по
WEEKLY_PRIZE_PCTS (по умолчанию 50/30/20%: первое место забирает половину).
Выплата возможна ТОЛЬКО после записи эпилога последнего дня недели (флаг
week_leaderboard_ready в watcher_state). Претендент обязан иметь привязанный
кошелёк, минимум WEEKLY_MIN_DAYS дней голосования за неделю и хотя бы одну
ставку в этой неделе — иначе фермы мультиаккаунтов собирают приз дешёвыми
голосами. Ставка-требование считается заново каждую неделю: приз не уйдёт
тому, кто в прошедшей неделе не поставил ни грамма. Ничья по верным путям
решается большим вкладом Gram за период, при равенстве ставок — кто раньше
нажал Claim в /start, далее — меньший player_id. Месту, которому не нашлось
достойного игрока, ждать нечего: его доля переносится в копилку новой недели.

**Месяц.** Выплата происходит ТОЛЬКО после записи эпилога последнего дня
месяца (флаг month_leaderboard_ready в watcher_state). Это гарантирует,
что лидерборд не сработает до завершения нарративной части дня. Сумма всех
накопленных месяцев до текущего уходит топ-K игрокам периода (обязательна
хотя бы одна ставка в этом месяце) по весам MONTHLY_PRIZE_WEIGHTS (по
умолчанию top-3, 50/30/20). Ничьи решаются в точности как в неделю: больший
вклад Gram, затем первый Claim, затем меньший player_id. Лидер без
привязанного кошелька или без ставки в периоде не блокирует остальных: горш
делится между оплачиваемыми, а месяц остаётся «незакрытым», пока хоть
кому-то нельзя заплатить и никого оплатить нельзя вовсе.

Метки «выплачено до X» живут в watcher_state и переживают рестарт.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import case, delete, func, or_, select, text

from app.config import settings
from app.core.registry import (
    MARKER_KEY,
    MONTH_CLAIM_WINDOW_KEY,
    MONTH_READY_KEY,
    WEEK_CLAIM_WINDOW_KEY,
    WEEK_READY_KEY,
    WEEKLY_MARKER_KEY,
)
from app.db import SessionLocal
from app.models import (
    LeaderboardClaim,
    LeaderboardPot,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WeeklyPot,
)
from app.stakes import split_equal
from app.ton_utils import to_nano
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds

logger = logging.getLogger(__name__)

# Приложение живёт в одном процессе, но закрытие дня (tick) и плановый
# сброс копилок могут вызывать settle-функции параллельно из разных задач
# одного цикла событий. Без взаимной блокировки два вызова прочитали бы
# метку «ещё не закрыто», построили Пэйауты и задвоили копилку. Мьютекс
# сериализует такие перекрытия внутри процесса.

_month_settle_lock: asyncio.Lock | None = None
_week_settle_lock: asyncio.Lock | None = None


def _month_settle():
    global _month_settle_lock
    if _month_settle_lock is None:
        _month_settle_lock = asyncio.Lock()
    return _month_settle_lock


def _week_settle():
    global _week_settle_lock
    if _week_settle_lock is None:
        _week_settle_lock = asyncio.Lock()
    return _week_settle_lock

_MEDALS = ("🥇", "🥈", "🥉")


async def _claim_marker_lock(session, key: str) -> WatcherState:
    """Материализует метку-замок и берёт на неё ряд-лок (псевдо-семафор).

    Гонка за пределами одного процесса (несколько реплик на Postgres): две
    копии бота могут одновременно пройти идемпотентный чек и задвоить
    копилку (два комплекта Payout). SELECT ... FOR UPDATE держит ряд до
    коммита: вторая копия блокируется на чтении и после коммита первой видит
    свежую метку «выплачено» — возвращает False и не платит.

    Ряд гарантированно есть: перед захватом выполняем INSERT ... ON CONFLICT
    DO NOTHING (сырым SQL: оба бэкенда — SQLite ≥3.24 и Postgres — понимают
    одинаково), чтобы сериализовать и первый запуск (когда метки ещё нет).

    На SQLite FOR UPDATE не поддержан (no-op) — это документированно безопасно:
    в одном процессе settle и так сериализован asyncio.Lock ниже.
    """
    await session.execute(
        text(
            "INSERT INTO watcher_state (key, value) VALUES (:k, '') "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"k": key},
    )
    row = (
        await session.execute(
            select(WatcherState).where(WatcherState.key == key).with_for_update()
        )
    ).scalar_one()
    return row


def _capped(expr, cap: int):
    """Верное-количество, обрезанное потолком, портативно (SQLite/Postgres)."""
    if not cap or cap <= 0:
        return expr
    return case((expr > cap, cap), else_=expr)


def previous_month_key(now: datetime | None = None) -> str:
    """Ключ последнего ПОЛНОСТЬЮ прошедшего месяца («YYYY-MM»)."""
    now = now or datetime.now(UTC)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


async def _top_correct_voters(session, since: datetime, until: datetime) -> list[tuple[int, int]]:
    """Игроки с максимумом верных ответов в окне [since, until).

    Нижняя граница обязательна: без неё «чемпион всех времён» забирал бы
    копилку каждого следующего месяца, даже ничего не отгадав в нём.
    """
    count_expr = _capped(func.count(), settings.leaderboard_correct_cap)
    result = await session.execute(
        select(Vote.player_id, count_expr)
        .join(Round, Round.id == Vote.round_id)
        .where(
            Vote.card_position == Round.winner_card,
            Round.status == RoundStatus.CLOSED,
            Round.tally_ends_at >= since,
            Round.tally_ends_at < until,
        )
        .group_by(Vote.player_id)
        .order_by(count_expr.desc(), Vote.player_id.asc())
    )
    rows = [(pid, int(count)) for pid, count in result.all()]
    if not rows:
        return []
    best = rows[0][1]
    return [row for row in rows if row[1] == best]


def _active_network() -> str:
    """Контур казны: вклад Gram учитывается только в живом контуре."""
    return "testnet" if settings.is_testnet else "mainnet"


async def _gram_contributions(
    session,
    period_start: datetime,
    period_end: datetime,
    by: str = "opens_at",
) -> dict[int, int]:
    """Проверенные суммы ставок игроков за период [period_start, period_end).

    Тайбрейк при равенстве верных сцен: выше тот, кто поставил больше (в
    нанотонах). Считаем только confirmed — ставки, вернённые вручную (refunded)
    исключены: денежные средства были не в риске, и такие игроки не имеют
    права претендовать на лидербордные копилки. Ставка другого контура
    (mainnet/testnet) не в зачёт.
    """
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    rows = await session.execute(
        select(Stake.player_id, func.sum(Stake.amount_nanotons))
        .join(Round, Round.id == Stake.round_id)
        .where(
            Stake.status == "confirmed",
            Stake.network == _active_network(),
            window >= period_start,
            window < period_end,
        )
        .group_by(Stake.player_id)
    )
    return {int(pid): int(total) for pid, total in rows.all()}


async def _rank_window(
    session,
    since: datetime,
    until: datetime,
    by: str = "opens_at",
    limit: int = 50,
) -> list[tuple[int, int, int, int]]:
    """Ранг окна [since, until): (player_id, верных, дней, вклад Gram).

    Порядок — ровно тот, что использует выплата: верные пути ↓, вклад Gram
    ↓, player_id ↑ (претензия Claim докладывается уже после фильтра достойных
    в _order_by_ties). Верные пути — с учётом потолка leaderboard_correct_cap.
    LIMIT применяется ПОСЛЕ пересортировки: усечение до грам-тайбрейка
    выкидывало бы претендента с большим вкладом, но большим player_id.
    """
    correct_expr = _capped(
        func.sum(case((Vote.card_position == Round.winner_card, 1), else_=0)),
        settings.leaderboard_correct_cap,
    )
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    result = await session.execute(
        select(
            Vote.player_id,
            correct_expr,
            func.count(),
        )
        .join(Round, Round.id == Vote.round_id)
        .where(
            Round.status == RoundStatus.CLOSED,
            window >= since,
            window < until,
        )
        .group_by(Vote.player_id)
    )
    rows = [(pid, int(correct or 0), int(days)) for pid, correct, days in result.all()]
    if not rows:
        return []
    grams = await _gram_contributions(session, since, until, by=by)
    rows.sort(key=lambda row: (-row[1], -grams.get(row[0], 0), row[0]))
    return [
        (pid, correct, days, grams.get(pid, 0)) for pid, correct, days in rows[:limit]
    ]


async def _claim_times(session, kind: str, periods: list[str]) -> dict[int, datetime]:
    """Самая ранняя претензия игрока среди contexts периода (kind-окно).

    Ничья по (верность, вклад Gram): выше тот, кто раньше нажал Claim в
    течение периода; не заявлявшийся — позади любого заявившегося.
    """
    if not periods:
        return {}
    rows = await session.execute(
        select(LeaderboardClaim.player_id, func.min(LeaderboardClaim.claimed_at))
        .where(LeaderboardClaim.kind == kind, LeaderboardClaim.period.in_(periods))
        .group_by(LeaderboardClaim.player_id)
    )
    claims: dict[int, datetime] = {}
    for pid, claimed in rows.all():
        moment = claimed if claimed.tzinfo else claimed.replace(tzinfo=UTC)
        claims[int(pid)] = min(claims.get(int(pid), moment), moment)
    return claims


_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=UTC)


def _prize_tied_groups(
    candidates: list[tuple[int, int, int, str]],
    top_k: int,
) -> list[list[int]]:
    """Группы связанных (tied) игроков на призовых местах.

    Кандидаты уже отсортированы _order_by_ties (correct ↓, gram ↓, claim, pid ↑).
    top_k — число призовых мест (3 для недели, monthly_prize_top_k для месяца).

    Ничья = группа ≥2 кандидатов с одинаковыми (correct, gram), чья позиция
    в отсортированном списке ≤ top_k (или группа «перекрывает» cutoff).

    Возвращает список групп (каждая группа — список player_id).
    """
    if not candidates or top_k <= 0:
        return []

    # Ничья, РЕЖУЩАЯ границу призового среза: кандидаты уже отсортированы, и
    # полное равенство (верность, вклад Gram) последнего призёра и первого
    # безпризёра — та же ничья, что и внутри призов. Без этого край среза
    # решался бы меньшим player_id молча: игрок №4, абсолютно равный №3,
    # не получал Claim-окна и не мог «перестоять» запретителя. Дотягиваем
    # tied-хвост за границу среза.
    prize_slice = list(candidates[:top_k])
    if len(candidates) > top_k:
        border = prize_slice[-1]
        for extra in candidates[top_k:]:
            if extra[1] == border[1] and extra[2] == border[2]:
                prize_slice.append(extra)
            else:
                break
    groups: list[list[int]] = []
    i = 0
    while i < len(prize_slice):
        pid, correct, gram, _w = prize_slice[i]
        j = i + 1
        while j < len(prize_slice):
            _p2, c2, g2, _w2 = prize_slice[j]
            if c2 == correct and g2 == gram:
                j += 1
            else:
                break
        if j - i >= 2:
            groups.append([p for p, _c, _g, _w in prize_slice[i:j]])
        i = j
    return groups


async def _open_claim_window(
    session, kind: str, period: str, tied_player_ids: list[int]
) -> None:
    """Открывает окно Claim: записывает JSON в watcher_state."""
    key = WEEK_CLAIM_WINDOW_KEY if kind == "week" else MONTH_CLAIM_WINDOW_KEY
    data = json.dumps({
        "period": period,
        "players": tied_player_ids,
        "opened_at": datetime.now(UTC).isoformat(),
    })
    row = await session.get(WatcherState, key)
    if row is None:
        session.add(WatcherState(key=key, value=data))
    else:
        row.value = data


async def _load_claim_window(
    session, kind: str
) -> dict | None:
    """Читает окно Claim из watcher_state. None = окна нет."""
    key = WEEK_CLAIM_WINDOW_KEY if kind == "week" else MONTH_CLAIM_WINDOW_KEY
    row = await session.get(WatcherState, key)
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return None


async def _clear_claim_window(session, kind: str) -> None:
    """Закрывает окно Claim."""
    key = WEEK_CLAIM_WINDOW_KEY if kind == "week" else MONTH_CLAIM_WINDOW_KEY
    await session.execute(delete(WatcherState).where(WatcherState.key == key))


async def _claim_window_players(
    session, kind: str
) -> tuple[list[int], str]:
    """Tied-игроки и период активного окна Claim (пусто — окна нет)."""
    window = await _load_claim_window(session, kind)
    if window is None:
        return [], ""
    return window.get("players", []) or [], window.get("period", "") or ""


async def _notify_tied_players(
    bot: Bot | None,
    kind: str,
    period: str,
    tied_player_ids: list[int],
) -> None:
    """Отправляет DM tied-игрокам с кнопкой Claim."""
    if bot is None or not tied_player_ids:
        return
    human = (
        f"недели {period}"
        if kind == "week"
        else f"месяца {period}"
    )
    text = (
        f"🏆 Ничья за призовые места leaderboard {human}!\n\n"
        "Ты и ещё кто-то набрали одинаковые верные сцены и вклад Gram. "
        "Чтобы решить, кто выше — нажми кнопку заявки ниже. "
        "Кто раньше нажал — тот выше. "
        f"Дедлайм: {settings.claim_window_hours}ч."
    )
    button_text = (
        "🗓 Заявить приз недели" if kind == "week"
        else "🗓 Заявить приз месяца"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, callback_data=f"claim:{kind}")]
    ])
    for pid in tied_player_ids:
        try:
            await bot.send_message(pid, text, reply_markup=kb)
        except Exception as exc:
            logger.warning("DM tied-игроку %s не доставлен: %s", pid, exc)


async def _resolve_claim_window(
    session,
    bot: Bot | None,
    kind: str,
    period: str,
    placed: list[tuple[int, int, int, str]],
    top_k: int,
    claims: dict[int, datetime],
    now: datetime,
) -> bool:
    """Решает, продолжать ли выплату или ждать Claim от tied-игроков.

    Возвращает True, когда можно платить дальше: ничьей на призовых местах
    нет, все tied уже заявились, или дедлайн заявок прошёл. False — окно
    Claim открыто/ещё живо и выплату надо отложить (такое окно открывается
    один раз и рассылает DM tied-игрокам).

    Ничья без заявок никогда не разрешается меньшим player_id немедленно:
    сначала tied-игроки получают шанс подать Claim (кто раньше — тот выше).
    """
    if not settings.leaderboard_claim_enabled:
        await _clear_claim_window(session, kind)
        return True

    tied_groups = _prize_tied_groups(placed, top_k=top_k)
    if not tied_groups:
        await _clear_claim_window(session, kind)
        return True

    all_tied = [pid for group in tied_groups for pid in group]
    window = await _load_claim_window(session, kind)
    if window is not None and window.get("period") != period:
        # Окно от прошлого периода — устарело, переоткрываем под текущий.
        await _clear_claim_window(session, kind)
        window = None

    claimed_ids = {pid for pid in all_tied if claims.get(pid) is not None}
    if claimed_ids >= set(all_tied):
        # Все tied заявились — ничья разрешена временем Claim (раньше — выше).
        # Закрываем окно (если было) и платим сразу.
        await _clear_claim_window(session, kind)
        return True

    if window is None:
        await _open_claim_window(session, kind, period, all_tied)
        await session.commit()
        if bot is not None:
            await _notify_tied_players(bot, kind, period, all_tied)
        logger.info("Ничья %s %s: tied %s — открыто окно Claim", kind, period, all_tied)
        return False

    opened_at_str = window.get("opened_at", "")
    try:
        opened_at = datetime.fromisoformat(opened_at_str)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        opened_at = now
    deadline_passed = (
        settings.claim_window_hours > 0
        and (now - opened_at).total_seconds() >= settings.claim_window_hours * 3600
    )
    if not deadline_passed:
        # Дедлайн не прошёл — ждём заявок.
        return False

    await _clear_claim_window(session, kind)
    logger.info("Ничья %s %s: дедлайн Claim прошёл, окно закрыто", kind, period)
    return True


def _order_by_ties(
    candidates: list[tuple[int, int, int, str]],
    claims: dict[int, datetime],
) -> list[tuple[int, int, int, str]]:
    """Места по (верность ↓, вклад Gram ↓, Claim ↓, player_id ↑).

    candidates — (player_id, верных, вклад Gram, кошелёк). Претензия решает
    только ПОЛНУЮ ничью (равны и верность, и вклад); тот, кто не заявлялся,
    уступает любому заявившемуся; дальше — меньший player_id.
    """
    def total_key(item: tuple[int, int, int, str]):
        pid, correct, gram, _wallet = item
        claimed = claims.get(pid)
        return (-correct, -gram, claimed if claimed is not None else _FAR_FUTURE, pid)

    return sorted(candidates, key=total_key)


async def _players_with_stake(
    session,
    period_start: datetime,
    period_end: datetime,
    by: str = "opens_at",
) -> set[int]:
    """Игроки, поставившие в периоде [period_start, period_end) хотя бы одну ставку.

    Требование «приз только тем, кто ставил»: бесплатные голоса не должны
    собирать копилки. Ставка — реальный TON-обязательство стаи, момент расчёта
    статусы уже разрешены: считаем только confirmed; refunded (ручной возврат
    держателем /keeper) исключён — средства были не в риске, участник не
    играл по-настоящему; rejected (нарушители) в зачёт не идут.
    Окно по колонке дня должно совпадать с окном, в котором settle-считает
    голоса ("opens_at" для недели, "tally_ends_at" для месяца).
    """
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    rows = await session.execute(
        select(Stake.player_id)
        .join(Round, Round.id == Stake.round_id)
        .where(
            Stake.status == "confirmed",
            Stake.network == _active_network(),
            window >= period_start,
            window < period_end,
        )
        .distinct()
    )
    return {int(pid) for pid in rows.scalars().all()}


def is_last_day_of_month(moment: datetime) -> bool:
    """Проверяет, является ли момент последним днём календарного месяца."""
    next_day = moment + timedelta(days=1)
    return next_day.month != moment.month


def is_last_day_of_week(moment: datetime) -> bool:
    """Проверяет, является ли момент последним днём ISO-недели (воскресенье)."""
    next_day = moment + timedelta(days=1)
    return next_day.isocalendar()[1] != moment.isocalendar()[1]


async def mark_month_leaderboard_ready(session, month_key: str) -> None:
    """Отмечает, что эпилог последнего дня месяца написан — лидерборд может выплачиваться."""
    marker = await session.get(WatcherState, MONTH_READY_KEY)
    if marker is None:
        session.add(WatcherState(key=MONTH_READY_KEY, value=month_key))
    else:
        marker.value = month_key


async def mark_week_leaderboard_ready(session, week_key: str) -> None:
    """Отмечает, что эпилог последнего дня недели написан — лидерборд может выплачиваться."""
    marker = await session.get(WatcherState, WEEK_READY_KEY)
    if marker is None:
        session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
    else:
        marker.value = week_key


async def mark_leaderboards_for_finished(session, finished) -> None:
    """Закрытый день — последний день недели/месяца? Ставим метки готовности.

    Общий шаг планировщика (_finalize_new_day_job) И ручного /advance: если
    последний день недели/месяца закрыт вручную, без меток копилки останутся
    навсегда незакрытыми (приз забрать нельзя).
    """
    if finished is None or getattr(finished, "opens_at", None) is None:
        return
    from app.weeks import iso_week_key

    day_index = getattr(finished, "day_index", None)
    if is_last_day_of_month(finished.opens_at):
        month_key = finished.opens_at.strftime("%Y-%m")
        await mark_month_leaderboard_ready(session, month_key)
        logger.info(
            "Эпилог дня %s — последний день месяца %s: лидерборд готов к выплате",
            day_index, month_key,
        )
    if is_last_day_of_week(finished.opens_at):
        week_key = iso_week_key(finished.opens_at)
        await mark_week_leaderboard_ready(session, week_key)
        logger.info(
            "Эпилог дня %s — последний день недели %s: недельный лидерборд готов к выплате",
            day_index, week_key,
        )


async def settle_month_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших месяцев (безопасно при параллельных вызовах)."""
    async with _month_settle():
        return await _settle_month_locked(bot)


async def _settle_month_locked(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших месяцев, если эпилог последнего дня записан.

    Возвращает True, если выплата создана. Идемпотентно по метке в watcher_state:
    повторные вызовы в том же месяце ничего не делают.

    Выплата возможна ТОЛЬКО после установки флага month_leaderboard_ready,
    который ставится в _finalize_new_day_job() при записи эпилога последнего дня
    месяца. Это гарантирует, что лидерборд не сработает раньше завершения
    нарративной части дня.
    """
    now = datetime.now(UTC)
    prev_key = previous_month_key(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with SessionLocal() as session:
        marker = await _claim_marker_lock(session, MARKER_KEY)
        settled_through = marker.value
        if settled_through >= prev_key:
            return False  # этот месяц уже закрыт (или вообще ещё не наступил)

        # Флаг готовности: эпилог последнего дня месяца записан.
        ready_marker = await session.get(WatcherState, MONTH_READY_KEY)
        ready_month = ready_marker.value if ready_marker is not None else ""
        if ready_month < prev_key:
            # Эпилог ещё не записан — лидерборд ждёт.
            return False

        pots = (
            await session.execute(
                select(LeaderboardPot).where(LeaderboardPot.month <= prev_key).order_by(LeaderboardPot.month.asc())
            )
        ).scalars().all()
        total = sum(pot.nanotons for pot in pots)
        # Период лидерборда: от начала самого старого НЕвыплаченного месяца
        # (после простоя копилка копится за несколько месяцев сразу) до начала
        # текущего — ровно то, за что платим.
        period_start = month_start
        if pots:
            year, mon = map(int, pots[0].month.split("-"))
            period_start = datetime(year, mon, 1, tzinfo=UTC)

        # Последние дни месяца ещё не финализированы (долгий тик, сбой) —
        # их вклад в копилку может быть недолит. Как в неделе: платим только
        # когда весь период закрыт и выплаты дней финализированы.
        unfinished = await session.execute(
            select(Round.id)
            .where(
                Round.tally_ends_at >= period_start,
                Round.tally_ends_at < month_start,
                or_(
                    Round.status != RoundStatus.CLOSED,
                    Round.payouts_finalized.is_(False),
                ),
            )
            .limit(1)
        )
        if unfinished.scalar_one_or_none() is not None:
            return False

        top_k = max(1, settings.monthly_prize_top_k)
        if top_k > 1:
            # Сглаживание дисперсии: платим топ-K по верности с весами,
            # а не «забрал всё сильнейший». Веса из месячного весового списка;
            # ничьи решаются вкладом Gram, затем Claim.
            # Усекать кандидатов до top_k нельзя: tied-игрок ПОД границей среза
            # (№top_k+1, абсолютно равный №top_k) не попал бы в _resolve_claim_window
            # и лишился бы окна Claim, как в неделе. Берём широкий ранг, а места
            # для выплаты режем после разрешения ничьи временем Claim.
            ranked = await _rank_window(
                session, period_start, month_start, by="tally_ends_at", limit=50
            )
            staked = await _players_with_stake(
                session, period_start, month_start, by="tally_ends_at"
            )
            claim_months: list[str] = []
            cursor = period_start
            while cursor < month_start:
                claim_months.append(cursor.strftime("%Y-%m"))
                cursor = (cursor + timedelta(days=35)).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
            claims = (
                await _claim_times(session, "month", claim_months)
                if settings.leaderboard_claim_enabled
                else {}
            )
            candidates: list[tuple[int, int, int, str]] = []
            skipped = 0
            for pid, score, _days, gram in ranked:
                player = await session.get(Player, pid)
                if (
                    player is not None
                    and player.wallet_address
                    and player.wallet_verified
                    and pid in staked
                ):
                    candidates.append((pid, score, gram, player.wallet_address))
                else:
                    skipped += 1
            if not candidates:
                logger.warning(
                    "Копилка %d нанотонов ждёт: у топ-%d лидеров нет подтверждённого кошелька/ставки",
                    total, top_k,
                )
                return False
            placed = _order_by_ties(candidates, claims)
            if not await _resolve_claim_window(
                session, bot, "month", prev_key, placed, top_k=top_k,
                claims=claims, now=now,
            ):
                return False
            weights = parse_prize_pcts(settings.monthly_prize_weights)
            if not weights:
                # Пустой список весов (опечатка конфига) не должен сжечь горш:
                # метку не трогаем, копилка ждёт, пока конфиг не починят.
                logger.error(
                    "Копилка %d нанотонов ждёт: monthly_prize_weights не содержат "
                    "чисел — исправь конфиг, горш не тронут",
                    total,
                )
                return False
            payments: list[tuple[int, str, int]] = []
            # Места — первые top_k после разрешения ничьи (Claim, player_id):
            # tied-претендент за границей среза в выплату не попадает.
            winners = placed[:top_k]
            top_amount = _weighted_amounts(total, winners, weights)
            for (player_id, _score, _gram, wallet), amount in zip(winners, top_amount, strict=False):
                payments.append((player_id, wallet, amount))
        else:
            # Прежнее поведение: победители, набравшие максимум, делят ровно.
            winners = await _top_correct_voters(session, period_start, month_start)
            staked = await _players_with_stake(
                session, period_start, month_start, by="tally_ends_at"
            )
            payable_ids: list[int] = []
            wallets: dict[int, str] = {}
            for player_id, _count in winners:
                player = await session.get(Player, player_id)
                if (
                    player is not None
                    and player.wallet_address
                    and player.wallet_verified
                    and player_id in staked
                ):
                    wallets[player_id] = player.wallet_address
                    payable_ids.append(player_id)
            skipped = len(winners) - len(payable_ids)
            if not payable_ids:
                # Платить некому: метку НЕ двигаем, копилка ждёт следующего цикла.
                logger.warning(
                    "Копилка %d нанотонов ждёт: у лидеров (%s) нет подтверждённого кошелька "
                    "или ставки в этом месяце",
                    total,
                    [pid for pid, _ in winners] or "нет голосов",
                )
                return False
            shares = split_equal(total, payable_ids)
            payments = [
                (player_id, wallets[player_id], amount)
                for player_id, amount in shares.items()
            ]

        if not payments:
            # Страховка на любой неожиданный путь top-K: пустые выплаты не
            # двигают метку и не удаляют горш.
            logger.error(
                "Копилка %d нанотонов ждёт: нечего выплатить — метка не двигается",
                total,
            )
            return False

        # Доли ниже min_payout_gram не создают дохлые переводы (комиссия сети
        # съела бы большую часть). Неоплаченная пыль возвращается в копилку
        # ТЕКУЩЕГО месяца — старые горши ниже удаляются, деньги не теряются.
        min_payout_nano = to_nano(settings.min_payout_gram)
        payments = [
            (player_id, wallet, amount)
            for player_id, wallet, amount in payments
            if amount >= min_payout_nano
        ]
        if not payments:
            logger.warning(
                "Копилка %d нанотонов ждёт: все доли ниже min_payout_gram — ждём роста горша",
                total,
            )
            return False
        recarry = total - sum(amount for _pid, _wallet, amount in payments)
        if recarry > 0:
            current_month = now.strftime("%Y-%m")
            pot_row = (
                await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == current_month)
                )
            ).scalar_one_or_none()
            if pot_row is None:
                session.add(LeaderboardPot(month=current_month, nanotons=recarry))
            else:
                pot_row.nanotons += recarry

        network = "testnet" if settings.is_testnet else "mainnet"
        for player_id, wallet, amount in payments:
            session.add(
                Payout(
                    round_id=None,
                    player_id=player_id,
                    kind="leaderboard",
                    amount_nanotons=amount,
                    dest_address=wallet,
                    network=network,
                )
            )
        await session.execute(
            delete(LeaderboardPot).where(LeaderboardPot.month <= prev_key)
        )
        marker.value = prev_key
        await session.commit()

    logger.info(
        "Копилка месяцев ≤ %s выплачена: %s нанотонов между %s%s",
        prev_key,
        total,
        payments,
        f" (без кошелька пропущены: {skipped})" if skipped else "",
    )
    if bot is not None and settings.admin_id_set:
        text = (
            f"🏆 Копилка лидерборда за {prev_key} отправлена: "
            f"{total / 1e9:.2f} Gram между {len(payments)} лидером(ами)."
        )
        for admin_id in settings.admin_id_set:
            try:
                await bot.send_message(admin_id, text)
            except Exception as exc:
                logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)
    return True


def _week_prize_amounts(total_nanotons: int, places: int) -> tuple[list[int], int]:
    """Доли мест по WEEKLY_PRIZE_PCTS.

    Возвращает (оплачиваемые суммы, перенос в копилку новой недели).
    Целочисленная пыль достаётся последнему оплачиваемому месту; если
    достойных игроков меньше настроенных мест, лишние доли переносятся.
    """
    pcts = parse_prize_pcts(settings.weekly_prize_pcts)
    amounts = [total_nanotons * pct // 100 for pct in pcts]
    dust = total_nanotons - sum(amounts)
    paid = max(0, min(places, len(amounts)))
    if dust > 0 and paid > 0:
        amounts[paid - 1] += dust
        dust = 0
    return amounts[:paid], sum(amounts[paid:]) + dust


def _weighted_amounts(total_nanotons: int, payable: list, weights: list[int]) -> list[int]:
    """Доли месячной копилки между top-K получателями по весам.

    Веса нормируются по фактически оплачиваемым получателям: если весов
    меньше, чем получателей, последний вес повторяется до покрытия всех —
    так режим top-K не «теряет» места из-за короткого списка весов. Весь горш
    распределяется (пыль — первым получателям), незанятых мест нет.
    """
    if not payable or total_nanotons <= 0:
        return []
    used = list(weights[: len(payable)])
    if not used:
        return []
    if len(used) < len(payable):
        used += [used[-1]] * (len(payable) - len(used))
    total_w = sum(used)
    amounts = [total_nanotons * w // total_w for w in used]
    dust = total_nanotons - sum(amounts)
    for i in range(dust):
        amounts[i % len(amounts)] += 1
    return amounts


async def _memory_nomination(session, period_start: datetime, period_end: datetime) -> str | None:
    """«Самый памятливый пёс недели»: максимум находок памяти в окне недели.

    Косметика церемонии: ничего не платит и не влияет на копилки.
    """
    from app.models import MemoryHit

    row = (
        await session.execute(
            select(MemoryHit.player_id, func.count())
            .join(Round, Round.id == MemoryHit.round_id)
            .where(
                Round.opens_at >= period_start,
                Round.opens_at < period_end,
            )
            .group_by(MemoryHit.player_id)
            .order_by(func.count().desc(), MemoryHit.player_id.asc())
            .limit(1)
        )
    ).first()
    if row is None or not row[1]:
        return None
    pid, count = int(row[0]), int(row[1])
    player = await session.get(Player, pid)
    name = (
        (player.username if player and player.username else None)
        or (player.first_name if player else None)
        or f"игрок {pid}"
    )
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        word = "находка"
    elif mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
        word = "находки"
    else:
        word = "находок"
    return f"🧠 Самый памятливый пёс недели: {name} — {count} {word} памяти."


async def settle_week_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших недель (безопасно при параллельных вызовах)."""
    async with _week_settle():
        return await _settle_week_locked(bot)


async def _settle_week_locked(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших недель топ-3, если эпилог последнего дня записи.

    Возвращает True, если выплата создана. Идемпотентно по метке в
    watcher_state; платить некому — метка не двигается, копилка ждёт.

    Выплата возможна ТОЛЬКО после установки флага week_leaderboard_ready,
    который ставится в _finalize_new_day_job() при записи эпилога последнего дня
    недели (воскресенья).
    """
    now = datetime.now(UTC)
    prev_key = previous_week_key(now)

    async with SessionLocal() as session:
        marker = await _claim_marker_lock(session, WEEKLY_MARKER_KEY)
        settled_through = marker.value
        if settled_through >= prev_key:
            return False

        # Флаг готовности: эпилог последнего дня недели записан.
        ready_marker = await session.get(WatcherState, WEEK_READY_KEY)
        ready_week = ready_marker.value if ready_marker is not None else ""
        if ready_week < prev_key:
            # Эпилог ещё не записан — лидерборд ждёт.
            return False

        pots = (
            await session.execute(
                select(WeeklyPot).where(WeeklyPot.week <= prev_key).order_by(WeeklyPot.week.asc())
            )
        ).scalars().all()
        if not pots:
            marker.value = prev_key
            await session.commit()
            return False
        total = sum(pot.nanotons for pot in pots)
        period_start = week_bounds(pots[0].week)[0]
        period_end = week_bounds(prev_key)[1]

        # Неделя долита не полностью (последний день ещё считается или его
        # выплаты не финализированы) — платить рано, метку не двигаем.
        unfinished = await session.execute(
            select(Round.id)
            .where(
                Round.opens_at >= period_start,
                Round.opens_at < period_end,
                or_(
                    Round.status != RoundStatus.CLOSED,
                    Round.payouts_finalized.is_(False),
                ),
            )
            .limit(1)
        )
        if unfinished.scalar_one_or_none() is not None:
            return False

        # Короткая первая неделя забега: если сброс случился за 1–3 дня до
        # понедельника, порог WEEKLY_MIN_DAYS снимается — новичкам честно
        # дать шанс, а копилка не должна висеть вечно.
        relaxed = False
        try:
            from app.core.registry import RUN_START_KEY
            from app.rounds.anchor import parse_anchor

            anchor_row = await session.get(WatcherState, RUN_START_KEY)
            anchor = parse_anchor(anchor_row.value if anchor_row is not None else None)
            if anchor is not None:
                a_year, a_month = (int(part) for part in anchor["key"].split("-"))
                start_date = date(a_year, a_month, max(1, min(int(anchor["dom"]), 31)))
                if period_start <= start_date < period_end:
                    span_days = (period_end - start_date).days
                    if 1 <= span_days <= 3:
                        relaxed = True
                        logger.info(
                            "Неделя %s — короткий старт забега (%d дн.): порог дней снят",
                            prev_key, span_days,
                        )
        except Exception:
            logger.warning("Не удалось проверить короткую стартовую неделю", exc_info=True)

        min_days = 0 if relaxed else max(1, settings.weekly_min_days)
        rows = await _rank_window(session, period_start, period_end, by="opens_at", limit=50)
        staked = await _players_with_stake(session, period_start, period_end, by="opens_at")
        # Места недели — три сильнейших ИГРОКА по (верные пути, вклад Gram,
        # Claim, player_id), а не ступени счёта: внутри места приз не делится.
        claims = (
            await _claim_times(session, "week", [prev_key])
            if settings.leaderboard_claim_enabled
            else {}
        )
        candidates: list[tuple[int, int, int, str]] = []
        for pid, correct, days, gram in rows:
            if correct <= 0 or days < min_days or pid not in staked:
                continue
            player = await session.get(Player, pid)
            if player is None or not player.wallet_address or not player.wallet_verified:
                continue
            candidates.append((pid, correct, gram, player.wallet_address))
        # Полный порядок (с Claim-тайбрейком) нужен для поиска ничьей на
        # ГРАНИЦЕ призового среза; места для выплаты — первые top-3 поверхности.
        ordered = _order_by_ties(candidates, claims)
        places = ordered[:3]

        if not places:
            # Достойных нет: метку НЕ двигаем, копилка ждёт следующей недели.
            logger.warning(
                "Копилка недели %d нанотонов ждёт: нет игроков с подтверждённым кошельком, "
                "%s+ днями голосования и ставкой за неделю%s",
                total,
                min_days or settings.weekly_min_days,
                " (короткая стартовая неделя — порог снят)" if relaxed else "",
            )
            return False

        # --- Ничья: блокировка выплаты до Claim или дедлайма ---
        if not await _resolve_claim_window(
            session, bot, "week", prev_key, ordered, top_k=3, claims=claims, now=now
        ):
            return False

        amounts, rolled = _week_prize_amounts(total, len(places))
        min_payout_nano = to_nano(settings.min_payout_gram)
        network = _active_network()
        paid: list[tuple[str, str, int]] = []
        for place, ((pid, correct, _gram, wallet), amount) in enumerate(zip(places, amounts, strict=False), 1):
            if amount < min_payout_nano:
                # Дохлый перевод не создаём: комиссия сети съела бы большую
                # часть доли. Переносится в копилку новой недели, как пустое место.
                rolled += amount
                continue
            session.add(
                Payout(
                    round_id=None,
                    player_id=pid,
                    kind="weekly",
                    amount_nanotons=amount,
                    dest_address=wallet,
                    network=network,
                )
            )
            name_row = await session.get(Player, pid)
            name = (
                (name_row.username if name_row and name_row.username else None)
                or (name_row.first_name if name_row else None)
                or f"игрок {pid}"
            )
            paid.append((_MEDALS[place - 1], f"{name} — {correct} верных сцен", amount))

        # Места без достойного игрока переносятся в копилку новой недели.
        if rolled > 0:
            current_week = iso_week_key(now)
            week_row = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week == current_week))
            ).scalar_one_or_none()
            if week_row is None:
                session.add(WeeklyPot(week=current_week, nanotons=rolled))
            else:
                week_row.nanotons += rolled

        await session.execute(delete(WeeklyPot).where(WeeklyPot.week <= prev_key))
        marker.value = prev_key
        try:
            nomination = await _memory_nomination(session, period_start, period_end)
        except Exception:
            nomination = None
        await session.commit()

    logger.info(
        "Копилка недель ≤ %s выплачена: %s нанотонов → %s",
        prev_key,
        total,
        [(medal, name, amount) for medal, name, amount in paid],
    )
    lines = ["🏆 Итоги недели Стаи:"]
    for medal, name, amount in paid:
        lines.append(f"{medal} {name} — {amount / 1e9:.2f} Gram")
    lines.append(
        "При равенстве верных сцен Стая смотрит на вклад Gram, а затем — "
        "кто раньше всех заявил о месте."
    )
    if nomination:
        lines.append(nomination)
    if rolled > 0:
        lines.append("Незаполненные места недели перешли в копилку новой.")
    lines.append("Неделя началась заново — копилка копится с нуля. Удачи!")
    ceremony = "\n".join(lines)
    if bot is not None:
        from app.broadcast import whisper_to_chats

        try:
            await whisper_to_chats(bot, ceremony)
        except Exception as exc:
            logger.warning("Церемония недели не разослана: %s", exc)
    if bot is not None and settings.admin_id_set:
        admin_text = f"🗓 Копилка недели {prev_key} отправлена: {total / 1e9:.4f} Gram."
        for admin_id in settings.admin_id_set:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception as exc:
                logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)
    return True
