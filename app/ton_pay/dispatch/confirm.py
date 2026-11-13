"""Сверка «sent» с реальным блокчейном: метка bcast:<unix> означает
лишь «запрос принят», поэтому потерявшийся перевод приходится искать
по memo в истории и возвращать в очередь."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import Payout

from .. import state as _state
from .memo import _payout_comment_candidates

logger = logging.getLogger(__name__)

_BROADCAST_CONFIRM_POLL = 2.0
# Ожиданию после вещания хватает головы истории: только что отправленный
# перевод лежит на первой странице. Полное окно сверки (12×256 tx, ~8.6 МБ
# за опрос при промахе) здесь избыточно — 2×25 покрывает пачку с запасом.
_BROADCAST_CONFIRM_PAGE_LIMIT = 25
_BROADCAST_CONFIRM_MAX_PAGES = 2

async def _sent_payout_rows(session, network: str) -> list[Payout]:
    """Sent-выплаты без реального хеша: tx_hash NULL или маркер bcast:<unix>.

    Именно они подлежат сверке: у подтверждённых строк хеш уже настоящий, и
    повторная сверка по memo им не нужна.
    """
    return (
        (
            await session.execute(
                select(Payout).where(
                    Payout.status == "sent",
                    Payout.network == network,
                    or_(Payout.tx_hash.is_(None), Payout.tx_hash.like("bcast:%")),
                )
            )
        )
        .scalars()
        .all()
    )


async def _taken_payout_hashes(session, network: str) -> set[str]:
    """Хеши, уже занятые в этой сети: пачка v5r1 не должна делить хеш с другой строкой."""
    return set(
        (
            await session.execute(
                select(Payout.tx_hash).where(
                    Payout.network == network,
                    Payout.tx_hash.is_not(None),
                    Payout.tx_hash.not_like("bcast:%"),
                )
            )
        )
        .scalars()
        .all()
    )


async def _reconcile_sent(
    session, rows: list[Payout], tx_map, taken_hashes
) -> tuple[int, int, int]:
    """Один проход сверки sent-выплат: (confirmed, requeued, window_lost).

    Семантика «sent»:
      • memo, найденное в истории казначея → пишем реальный хеш вместо bcast;
      • memo, которого НЕТ в истории дольше payout_confirm_timeout_seconds →
        строка возвращается в pending (перевод в цепочку не ушёл, анти-дубль
        при повторной отправке не сработает — мемо там нет);
      • memo, которого НЕТ в истории, но sent_at старше payout_reconcile_history_seconds
        → строка уходит в failed с сожжённым лимитом попыток (attempts = payout_max_attempts);
      • скан окна неполон (не _RECONCILE_WINDOW_COMPLETE) → «не найдено» —
        «не знаю», строки не трогаем.
    """
    confirmed = 0
    requeued = 0
    window_lost = 0
    # Сравнение в naive UTC: Postgres (timezone=True) вернёт aware,
    # SQLite — naive; снос tzinfo с обеих сторон даёт один масштаб.
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=settings.payout_confirm_timeout_seconds
    )
    # Граница «не видно в окне сверки»: если sent_at старше неё,
    # отсутствие memo в tx_map значит «не знаю, что в цепочке», а не «перевод
    # не ушёл». Такие строки в очередь НЕ возвращаем: повтор отправил бы тот же
    # memo второй раз, а деньги уже могли уйти в цепочке раньше.
    window_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=settings.payout_reconcile_history_seconds
    )
    for payout in rows:
        real_hash = next(
            (
                tx_map[candidate]
                for candidate in _payout_comment_candidates(payout)
                if candidate in tx_map
            ),
            None,
        )
        if real_hash:
            if real_hash in taken_hashes:
                # Пачка: хеш транзакции уже занят другой строкой.
                # Свой суффикс — иначе индекс уронил бы коммит.
                suffix = f"#{payout.id}"
                payout.tx_hash = f"{real_hash[: 80 - len(suffix)]}{suffix}"
            else:
                taken_hashes.add(real_hash)
                payout.tx_hash = real_hash
            confirmed += 1
            continue
        sent_at = payout.sent_at.replace(tzinfo=None) if payout.sent_at is not None else None
        if sent_at is not None and sent_at > cutoff:
            # Свежая вещация: блокчейн мог ещё не успеть — даём время.
            continue
        if sent_at is not None and sent_at <= window_cutoff:
            # За окном сверки: невозможно ни подтвердить, ни опровергнуть.
            # Возвращать в pending = риск дубля (memo может жить в цепочке
            # за пределами нашего покрытия). В failed с понятной причиной:
            # ручной разбор через /payouts → retry при подтверждённой доле.
            # Сжигаем остаток попыток: failed с attempts < max — корм гигиены
            # _reset_retriable, а она оживила бы строку уже в следующем цикле,
            # и тот же memo ушёл бы в цепочку повторно. attempts == max делает
            # решение окончательным до ручного вердикта: resolve_dead_payout
            # счётчик не сбрасывает, и ручной retry по-прежнему пройдёт
            # анти-дубль по истории.
            payout.status = "failed"
            payout.attempts = settings.payout_max_attempts
            payout.last_error = (
                f"memo «{_payout_comment_candidates(payout)[0][:40]}…» за окном сверки "
                f"{settings.payout_reconcile_history_seconds} с — нельзя подтвердить "
                "или опровергнуть, ручной разбор"
            )
            window_lost += 1
            continue
        # memo нет в истории, окно верификации истекло — перевод не ушёл.
        if not _state._RECONCILE_WINDOW_COMPLETE:
            # ...но скан мог не покрыть окно сверки целиком (упёрся в
            # payout_reconcile_max_pages): «не найдено» здесь — «не знаю». Ждём
            # полного скана; строка либо подтвердится, либо дойдёт до ветки
            # «за окном сверки» и уйдёт на ручной разбор.
            continue
        payout.status = "pending"
        payout.attempts += 1
        payout.last_error = (
            f"memo «{_payout_comment_candidates(payout)[0][:40]}» не найдено в блокчейне "
            f"за {settings.payout_confirm_timeout_seconds} с после вещания — повторная отправка"
        )
        requeued += 1
    return confirmed, requeued, window_lost


async def confirm_broadcast_payouts(bot: Bot | None = None) -> int:
    """Сверяет «sent»-выплаты с реальным блокчейном и чинит потерю перевода.

    Метка вещания bcast:<unix> фиксирует только «запрос принят лайтсером»,
    а не «транзакция в блоке»: при гонке двух быстрых переводов (приз + рейк
    одного дня) один из них может не попасть в цепочку, хотя результат=1
    вернулся. База остаётся с sent-статусом и несуществующим переводом — игрок
    не получает приз, никто не переотправит.

    Каждый цикл:
      • memo, найденное в истории казначея → пишем реальный хеш вместо bcast;
      • memo, которого НЕТ в истории дольше payout_confirm_timeout_seconds →
        строка возвращается в pending (перевод в цепочку не ушёл, анти-дубль
        при повторной отправке не сработает — мемо там нет);
      • memo, которого НЕТ в истории, но sent_at старше
        payout_reconcile_history_seconds → строка уходит в failed с сожжённым
        лимитом попыток (attempts = payout_max_attempts): за окном сверки нельзя
        ни подтвердить, ни опровергнуть, повторная отправка рискует задвоить
        перевод, а failed с attempts < max гигиена очереди оживляет сама — такая
        строка ждёт только ручного разбора через /payouts;
      • карта истории пуста (оба провайдера молчат) → НЕ трогаем строки:
        «не знаю» не имеет права ни подтвердить, ни возвращать в очередь;
      • скан окна усечён (payout_reconcile_max_pages) → «memo не найдено»
        тоже «не знаю»: строки не трогаем, пока скан не станет полным;
      • пачка v5r1 (несколько выплат в одной транзакции) → хеш транзакции
        достаётся ровно одной строке, остальные помечаются хешем с суффиксом
        «#<id>»: индекс (tx_hash, network) не даёт двум строкам хранить один
        хеш, а пачка — законный случай одной транзакции на несколько выплат.
    """
    # Локальный импорт: карта берётся через app.ton_pay, чтобы
    # monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", ...) доходил.
    import app.ton_pay as _tp

    network = settings.active_network
    async with _state._DISPATCH_LOCK:
        async with SessionLocal() as session:
            rows = await _sent_payout_rows(session, network)
            if not rows:
                return 0
            taken_hashes = await _taken_payout_hashes(session, network)
            # Сверять нужно ТОЛЬКО эти memo (sent-без-реального-хеша): ищем их,
            # а не шерстим всю историю слепо. Как только все найдены — стоп:
            # запросов в квартал провайдера минимум, а «нет в истории» остаётся
            # правдивым (отсутствующая цель дожимает скан до конца окна).
            targets = {candidate for payout in rows for candidate in _payout_comment_candidates(payout)}
            # Полнота окна — как и в диспетчере, считается ЗАНОВО на цикл:
            # реальный fetch перепишет флаг по факту прохода (усечение по
            # payout_reconcile_max_pages → False), а протухшее False прошлого
            # цикла не имеет права блокировать решения этого.
            _state._RECONCILE_WINDOW_COMPLETE = True
            tx_map = await _tp.fetch_broadcast_tx_map(targets=targets)
            if not tx_map:
                logger.warning("История казначея недоступна — сверка sent-выплат пропущена")
                return 0
            confirmed, requeued, window_lost = await _reconcile_sent(session, rows, tx_map, taken_hashes)
            await session.commit()
    if requeued or window_lost:
        logger.warning(
            "Сверка: %d подтверждены, %d возвращены в очередь, %d ушли за окно сверки (ручной разбор)",
            confirmed,
            requeued,
            window_lost,
        )
    elif confirmed:
        logger.info("Сверка: %d выплат подтверждены реальными хешами", confirmed)
    return confirmed + requeued + window_lost


async def _wait_for_broadcast_memo(candidates: set[str], seconds: float) -> bool:
    """Ждёт, пока memo перевода появится в истории исходящих казначея.

    HTTP-канал подписывает переводы пачки последовательными seqno (+1 локально
    после успеха). Следующий seqno МОЖНО использовать, только когда предыдущий
    перевод реально лёг в блок: вслепую разосланные вплотную внешние месседжи
    сражаются за место, и второй отбрасывается молча — «ok» от провайдера
    значит лишь «мемпул принял», а не «транзакция в блоке». Пауза после
    успешного вещания и до подписи следующего убирает гонку. Таймаут → False:
    вызывающий сбрасывает батч-счётчик, и следующая отправка возьмёт свежий
    живой seqno (переживший перевод дожмёт сверка confirm_broadcast_payouts).

    Опрос — целевой (targets=candidates): скан останавливается на странице,
    где найден нужный memo, а не читает до конца окно сверки (до
    payout_reconcile_max_pages страниц) на каждый опрос — с ростом истории
    казны «честный» скан сам становился бы тормозом пачки. У выплаты кандидат
    ровно один (_payout_comment_candidates), так что ранний стоп срабатывает
    на первом же найденном memo.

    Флаги полноты (_RECONCILE_*) ожидание не переписывает: их описывает скан
    цикла (маркеры диспетчера, сверка), а целевой скан с ранним стопом
    неполон по построению — он не имеет права менять вердикт «повтор или
    заморозка» у соседних строк очереди.
    """
    import app.ton_pay as _tp

    # Снапшот входа: любой выход (в т.ч. исключение) возвращает флаги как были.
    saved_ok = _state._RECONCILE_HISTORY_OK
    saved_window = _state._RECONCILE_WINDOW_COMPLETE
    # Узкое окно — только для этой задачи: ContextVar не сужает параллельную
    # сверку очереди, которой по-прежнему нужно всё окно истории.
    scan_token = _state.RECONCILE_SCAN_OVERRIDE.set(
        (_BROADCAST_CONFIRM_PAGE_LIMIT, _BROADCAST_CONFIRM_MAX_PAGES)
    )
    try:
        deadline = time.monotonic() + seconds
        while True:
            markers = set(await _tp.fetch_broadcast_tx_map(targets=candidates))
            if any(c in markers for c in candidates):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_BROADCAST_CONFIRM_POLL)
    finally:
        _state.RECONCILE_SCAN_OVERRIDE.reset(scan_token)
        _state._RECONCILE_HISTORY_OK = saved_ok
        _state._RECONCILE_WINDOW_COMPLETE = saved_window
