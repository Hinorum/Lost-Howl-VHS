from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    Card,
    Income,
    MemoryHit,
    Payout,
    Player,
    PreparedDay,
    RevoteGrant,
    Round,
    RoundStatus,
    Stake,
    StatusPost,
    StoryBeat,
    Vote,
    WatcherState,
)

from .materialization import _materialize_round, _stamp_day_money_mode
from .narrative import write_epilogue
from .queries import get_active_round, get_latest_round, get_round
from .rendering import _plan_and_render
from .time import _ROMAN, _now, utc_aware
from .voting import (
    _TIE_THEATER,
    _decisive_counts,
    _winner_and_tied,
    count_stakes_for_tally,
    plain_vote_counts,
    tie_seed,
    tied_positions,
)

logger = logging.getLogger(__name__)


async def create_next_round_detailed(
    session: AsyncSession, base_day_index: int | None = None
) -> tuple[Round, bool]:
    """Создаёт следующий день. Второе значение — был ли день создан сейчас.

    День рендерится целиком (план → материализация). Без LLM/арта это
    шаблонный заголовок + три дороги; метод оставляет привычную сигнатуру
    для тика планировщика и /advance.
    """
    try:
        latest = await get_latest_round(session)
    except Exception:
        logger.exception("get_latest_round (1st) упал")
        await session.rollback()
        raise
    target_day = (
        base_day_index + 1
        if base_day_index is not None
        else (1 if latest is None else latest.day_index + 1)
    )
    # Ранний выход из гонки: нужный день уже открыт — отдаём его без рендера.
    try:
        already = (
            await session.execute(select(Round).where(Round.day_index == target_day).limit(1))
        ).scalar_one_or_none()
    except Exception:
        logger.exception("Запрос Round.day_index упал (target_day=%s)", target_day)
        await session.rollback()
        raise
    if already is not None:
        return already, False
    # Устаревшая прегенерация — чистим, чтобы открытый день не перезаписался.
    try:
        stale = await session.get(PreparedDay, target_day)
    except Exception:
        logger.exception("Запрос PreparedDay упал (target_day=%s)", target_day)
        await session.rollback()
        raise
    if stale is not None:
        await session.delete(stale)
        try:
            await session.commit()
        except Exception:
            logger.exception("Commit удаления устаревшего PreparedDay упал")
            await session.rollback()
            raise

    day_index = target_day
    opens_hint = (
        max(_now(), utc_aware(latest.tally_ends_at))
        if latest is not None and latest.tally_ends_at is not None
        else None
    )
    # Закон дня выводится из честной энтропии мастерчейна TON (root_hash % 3),
    # как жребий ничьей: блок уже в цепочке до открытия дня, подогнать закон
    # задним числом нельзя. TON выключен / оба узла молчат → локальный жребий.
    from app.ton_pay import fetch_masterchain_entropy

    entropy = await fetch_masterchain_entropy()
    payload = await _plan_and_render(session, day_index, opens_hint=opens_hint, entropy=entropy)
    try:
        round_row = await _materialize_round(session, payload, latest)
        await _stamp_day_money_mode(session, round_row)
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_round(session)
        if existing is None:
            raise
        return existing, False
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_round(session)
        if existing is None:
            raise
        return existing, False
    return round_row, True


async def create_next_round(session: AsyncSession, base_day_index: int | None = None) -> Round:
    row, _created = await create_next_round_detailed(session, base_day_index=base_day_index)
    return row


async def reset_game(session: AsyncSession, keep_story: bool = False) -> Round:
    """Сброс игры: чистые счёты и первый день заново.

    Стираются дни, карты, голоса, ставки, выплаты; счёт игроков обнуляется.
    Кошельки, привязки чатов и копилка месяца не трогаются — это реальные
    обязательства казны, а не «результаты».

    keep_story=True — «разделить команды»: статистика и деньги обнуляются,
    но канон истории (StoryBeat) остаётся, новый первый день продолжает
    ту же «историю».

    Защита: пока в очереди есть неотправленная выплата — сброс запрещён.
    """
    from app.core.registry import RUN_START_KEY
    from app.ton_pay import pending_payout_count

    owed = await pending_payout_count(session)
    if owed:
        raise RuntimeError(
            f"Сброс запрещён: в очереди {owed} неотправленных выплат. "
            "Дождись автоплатежей или разбери зависшие вручную."
        )
    await session.execute(delete(Payout))
    await session.execute(delete(Stake))
    await session.execute(delete(Vote))
    await session.execute(delete(StatusPost))
    await session.execute(delete(RevoteGrant))
    await session.execute(delete(Income))
    await session.execute(delete(MemoryHit))
    await session.execute(delete(Card))
    await session.execute(delete(PreparedDay))
    await session.execute(
        delete(WatcherState).where(WatcherState.key.like("art_bible:%"))
    )
    if not keep_story:
        await session.execute(delete(StoryBeat))
    await session.execute(delete(Round))
    await session.execute(update(Player).values(score=0, correct_picks=0))
    # Новый забег: якорь стартует сегодня — месяц и день месяца
    # лежат в watcher_state RUN_START_KEY (совместимо с leaderboard).
    from app.rounds.anchor import default_anchor

    fresh_anchor = default_anchor(_now())
    payload = json.dumps(fresh_anchor, ensure_ascii=False)
    row = await session.get(WatcherState, RUN_START_KEY)
    if row is None:
        session.add(WatcherState(key=RUN_START_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()
    row, _created = await create_next_round_detailed(session)
    return row


async def claim_announcement(session: AsyncSession, round_row: Round) -> bool:
    """Занимает право объявить день: True только для первого вызывающего."""
    result = await session.execute(
        update(Round)
        .where(Round.id == round_row.id, Round.announced_at.is_(None))
        .values(announced_at=_now())
    )
    await session.commit()
    return result.rowcount > 0


async def ensure_current_round(session: AsyncSession) -> Round:
    current = await get_active_round(session)
    if current is not None:
        return current
    latest = await get_latest_round(session)
    if latest is not None and latest.status != RoundStatus.CLOSED:
        return latest
    if latest is not None and utc_aware(latest.tally_ends_at) > _now():
        return latest
    return await create_next_round(session)


async def heal_stale_rounds(session: AsyncSession) -> int:
    """Дочитывает дни, застрявшие не-закрытыми ПОЗАДИ актуального."""
    latest = await get_latest_round(session)
    if latest is None:
        return 0
    result = await session.execute(
        select(Round)
        .options(selectinload(Round.cards))
        .where(
            Round.day_index < latest.day_index,
            Round.status.in_([RoundStatus.OPEN, RoundStatus.TALLYING]),
        )
        .order_by(Round.day_index.asc())
    )
    stale = list(result.scalars())
    healed = 0
    from app.tally import award_points

    for round_row in stale:
        day = round_row.day_index
        try:
            if round_row.status == RoundStatus.OPEN:
                await close_voting(session, round_row)
            finished, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, finished)
                from app.stakes import finalize_day_payouts
                try:
                    await finalize_day_payouts(session, finished)
                except Exception:
                    logger.warning(
                        "Финализация ставок вылеченного дня %s упала", day,
                        exc_info=True,
                    )
                try:
                    await write_epilogue(session, finished)
                except Exception:
                    logger.warning(
                        "Эпилог вылеченного дня %s не удался", day,
                        exc_info=True,
                    )
                healed += 1
                logger.info(
                    "Вылечен застрявший день %s: подсчёт завершён, "
                    "ставки финализированы", day,
                )
        except Exception as exc:
            logger.exception("Лечение застрявшего дня %s не удалось (повторится)", day, exc)
            await session.rollback()
    return healed


def public_round_view(round_row: Round) -> dict:
    """Counts stay secret while the round is open; the law is public from the start."""
    view = {
        "day_index": round_row.day_index,
        "status": round_row.status.value,
        "title": round_row.chapter_title,
        "text": round_row.chapter_text,
        "win_rule": round_row.win_rule.value,
        "voting_ends_at": round_row.voting_ends_at,
        "tally_ends_at": round_row.tally_ends_at,
        "cards": [
            {
                "position": card.position,
                "title": card.title,
                "description": card.description,
                "image_path": card.image_path,
            }
            for card in sorted(round_row.cards, key=lambda item: item.position)
        ],
    }
    if round_row.status == RoundStatus.CLOSED:
        view["winner_card"] = round_row.winner_card
        view["vote_counts"] = json.loads(round_row.vote_counts_json or "{}")
    return view


async def close_voting(session: AsyncSession, round_row: Round) -> Round:
    if round_row.status != RoundStatus.OPEN:
        return round_row
    # Условный UPDATE: один из двух процессов переводит день OPEN → TALLYING.
    claimed = (
        await session.execute(
            update(Round)
            .where(Round.id == round_row.id, Round.status == RoundStatus.OPEN)
            .values(status=RoundStatus.TALLYING)
        )
    ).rowcount
    if not claimed:
        return round_row
    round_row.status = RoundStatus.TALLYING
    # Неиспользованные гранты смены пути сгорают вместе с днём.
    await session.execute(
        update(RevoteGrant)
        .where(RevoteGrant.round_id == round_row.id, RevoteGrant.status == "granted")
        .values(status="expired")
    )
    counts = await _tally_counts_for(session, round_row)
    round_row._tally_counts = counts
    votes = round_row._tally_votes
    # Честная жеребьёвка: при ничьей снимаем энтропию мастерчейна TON и
    # фиксируем в дне ОДИН раз. heal/пересчёт используют ту же сохранённую
    # энтропию, исход не зависит от состояния сети в момент подсчёта.
    if len(tied_positions(counts, round_row.win_rule, votes)) > 1 and not round_row.tie_entropy:
        from app.ton_pay import fetch_masterchain_entropy

        round_row.tie_entropy = await fetch_masterchain_entropy()
    seed = tie_seed(round_row)
    round_row.winner_card, _ = await _winner_and_tied(session, round_row, counts, seed, votes)
    await session.commit()
    return round_row


async def _tally_counts_for(session: AsyncSession, round_row: Round) -> dict[int, int]:
    """Решающий счёт дня без фиксации в объекте.

    Суммы подтверждённых ставок (или голоса, если граммов на день нет).
    Голоса для хранения и флаг ставок кладутся на round_row.
    """
    vote_counts = await plain_vote_counts(session, round_row.id)
    counts, used_stakes = await _decisive_counts(session, round_row, vote_counts)
    round_row._tally_votes = vote_counts
    round_row._tally_used_stakes = used_stakes
    return counts


async def finish_tally(session: AsyncSession, round_row: Round) -> tuple[Round, bool]:
    """Закрывает подсчёт атомарно. Второе значение — закрыт ли день этим вызовом.

    Условный UPDATE (status='tallying' → 'closed') защищает от гонки между
    планировщиком и /advance.
    """
    if round_row.status != RoundStatus.TALLYING:
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    counts = getattr(round_row, "_tally_counts", None)
    used_stakes = getattr(round_row, "_tally_used_stakes", None)
    staked_counts: dict[int, int] = {0: 0, 1: 0, 2: 0}
    # Голоса для хранения и отображения — всегда бесплатные, без бонуса.
    vote_counts = getattr(round_row, "_tally_votes", None) or await plain_vote_counts(
        session, round_row.id
    )
    counts = getattr(round_row, "_tally_counts", None)
    if counts is None or used_stakes is None:
        # Пересчёт (heal/другой процесс): выбор совпадает с close_voting.
        decisive, used_stakes = await _decisive_counts(session, round_row, vote_counts)
        counts = decisive
    if used_stakes:
        staked_counts = await count_stakes_for_tally(session, round_row.id)
    display_counts = vote_counts
    seed = tie_seed(round_row)
    winner, tied = await _winner_and_tied(session, round_row, counts, seed, vote_counts)
    tie_note: str | None = None
    if len(tied) > 1:
        path_names = " и ".join(_ROMAN[p] for p in tied)
        chosen = _ROMAN[winner]
        theater = _TIE_THEATER[sum(ord(c) for c in seed) % len(_TIE_THEATER)].format(
            paths=path_names,
            chosen=chosen,
        )
        head = (
            "Счёт Gram на сценах разделился" if used_stakes else "Голоса разделились"
        )
        if round_row.tie_entropy:
            try:
                seqno, root_hash = round_row.tie_entropy.split(":", 1)
                block_url = (
                    "https://testnet.tonviewer.com" if settings.is_testnet else "https://tonviewer.com"
                )
                tie_note = (
                    f"{head} ({path_names}) — жребий блока TON №{seqno} "
                    f"(хеш …{root_hash[-4:]}): выпала сцена {chosen}. "
                    f"Проверка: {block_url}/block/-1:8000000000000000:{seqno}"
                )
            except (TypeError, ValueError) as exc:
                # Жребий по блоку — бонус к проверяемости, а не основа: без него
                # ниже подставляется честная заметка без ссылки на блок.
                logger.debug("Жребий блока не собран (%s) — без ссылки на блок", exc)
        if tie_note is None:
            tie_note = f"{head} ({path_names}) — {theater}"
        tie_note = tie_note[:200]
    if not round_row.cards:
        loaded = await get_round(session, round_row.id)
        if loaded is not None:
            round_row = loaded
    cards = {card.position: card for card in round_row.cards}
    winning_card = cards.get(winner) or SimpleNamespace(
        title=f"Сцена {_ROMAN[winner]}",
        consequence="Сцена растворилась в тумане, не оставив следа.",
    )
    counts_json = json.dumps({str(key): value for key, value in display_counts.items()})
    stake_counts_json = (
        json.dumps({str(key): value for key, value in staked_counts.items()})
        if used_stakes
        else None
    )
    result = await session.execute(
        update(Round)
        .where(Round.id == round_row.id, Round.status == RoundStatus.TALLYING)
        .values(
            winner_card=winner,
            vote_counts_json=counts_json,
            stake_counts_json=stake_counts_json,
            tie_note=tie_note,
            # Канон дня — уцелевший consequence: пост итогов читает epilogue_text
            # (broadcast.results_body), выставить его надо в момент закрытия, а не
            # в write_epilogue — тот срабатывает уже после рассылки итогов.
            epilogue_text=winning_card.consequence[:700],
            status=RoundStatus.CLOSED,
        )
    )
    if result.rowcount == 0:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    round_row.winner_card = winner
    round_row.vote_counts_json = counts_json
    round_row.stake_counts_json = stake_counts_json
    round_row.tie_note = tie_note
    round_row.epilogue_text = winning_card.consequence[:700]
    round_row.status = RoundStatus.CLOSED
    # Канон дня — только победивший кадр. Крючка для следующей кассеты больше
    # нет: каждая кассета — самостоятельная история, месяц не обязан ничем.
    session.add(
        StoryBeat(
            day_index=round_row.day_index,
            winning_title=winning_card.title,
            winning_text=winning_card.consequence,
            win_rule=round_row.win_rule.value,
            vote_counts=counts_json,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        loaded = await get_round(session, round_row.id)
        return (loaded or round_row), False
    return await get_round(session, round_row.id), True  # type: ignore[return-value]