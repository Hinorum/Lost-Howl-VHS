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

async def confirm_broadcast_payouts(bot: Bot | None = None) -> int:
    """Сверяет «sent»-выплаты с реальным блокчейном и чинит потерю перевода.

    Метка вещания bcast:<unix> фиксирует только «запрос принят лайтсервером»,
    а не «транзакция в блоке»: при гонке двух быстрых переводов (приз + рейк
    одного дня) один из них может не попасть в цепочку, хотя результат=1
    вернулся. База остаётся с sent-статусом и несуществующим переводом —
    игрок не получает приз, никто не переотправит.

    Каждый цикл:
      • memo, найденное в истории казначея → пишем реальный хеш вместо bcast;
      • memo, которого НЕТ в истории дольше payout_confirm_timeout_seconds →
        строка возвращается в pending (перевод в цепочку не ушёл, анти-дубль
        при повторной отправке не сработает — мемо там нет);
      • карта истории пуста (оба провайдера молчат) → НЕ трогаем строки:
        «не знаю» не имеет права ни подтверждать, ни возвращать в очередь.
    """
    # Локальный импорт: карта берётся через app.ton_pay, чтобы
    # monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", ...) доходил.
    import app.ton_pay as _tp

    network = "testnet" if settings.is_testnet else "mainnet"
    async with _state._DISPATCH_LOCK:
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(Payout).where(
                            Payout.status == "sent",
                            Payout.network == network,
                            Payout.confirmed.is_(False),
                            # Без confirmed=False попадали бы строки, уже
                            # прошедшие проверку N блоков (на старом
                            # payout_confirm_blocks=0 confirmed=True сразу
                            # после первого цикла). Хвост из bcast:/NULL —
                            # для mainnet это исчерпывается на первом цикле,
                            # а для обычных sent без подтверждения это и есть
                            # «нужно поднять до confirmed».
                            or_(
                                Payout.tx_hash.is_(None),
                                Payout.tx_hash.like("bcast:%"),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            # Сверять нужно ТОЛЬКО эти memo (sent-без-реального-хеша): ищем их,
            # а не шерстим всю историю слепо. Как только все найдены — стоп:
            # запросов в квартал провайдера минимум, а «нет в истории» остаётся
            # правдивым (отсутствующая цель дожимает скан до конца окна).
            targets = {
                candidate for payout in rows for candidate in _payout_comment_candidates(payout)
            }
            tx_map = await _tp.fetch_broadcast_tx_map(targets=targets)
            if not tx_map:
                logger.warning("История казначея недоступна — сверка sent-выплат пропущена")
                return 0
            confirmed = 0
            requeued = 0
            # Сравнение в naive UTC: Postgres (timezone=True) вернёт aware,
            # SQLite — naive; снос tzinfo с обеих сторон даёт один масштаб.
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=settings.payout_confirm_timeout_seconds
            )
            # Кэш head-блока на весь цикл: все «незрелые» confirmed-выплаты
            # проверяются против одного masterchain head seqno, иначе каждая
            # транзакция дёргала бы сеть отдельно и в случайные моменты (что
            # искажает число подтверждений на стыке вызовов).
            head_seqno_cache: int | None = None
            need_confirm_blocks = settings.payout_confirm_blocks > 0
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
                    payout.tx_hash = real_hash
                    if not need_confirm_blocks:
                        # Старое поведение: реальный хеш из истории = финал.
                        # Это и есть «confirmed» по смыслу, даже если флаг
                        # остался False после миграции исторических строк.
                        payout.confirmed = True
                        confirmed += 1
                        continue
                    if payout.confirmed:
                        # Уже подтверждено в прошлом цикле — не дёргаем сеть.
                        confirmed += 1
                        continue
                    import app.ton_pay as _tp_confirm

                    if head_seqno_cache is None:
                        head_seqno_cache = await _tp_confirm.fetch_masterchain_head_seqno()
                    if head_seqno_cache is None:
                        # «Не знаю» — НЕ поднимаем confirmed, цикл повторится
                        # через 120 с. Счётчик confirmed не растёт, но и
                        # requeued тоже: пустая попытка, без действий.
                        continue
                    tx_seqno = await _tp_confirm.fetch_tx_mc_seqno(real_hash)
                    if tx_seqno is None:
                        continue
                    depth = head_seqno_cache - tx_seqno
                    if depth < 0:
                        # Теоретически невозможно на mainnet, но защищаемся:
                        # пропускаем, цикл повторится.
                        continue
                    if depth >= settings.payout_confirm_blocks:
                        payout.confirmed = True
                        confirmed += 1
                    # Иначе: строка остаётся confirmed=False, ждём ещё блоков
                    # до payout_confirm_blocks. Следующий цикл в том же коде
                    # проверит снова — head_seqno_cache обновится.
                    continue
                sent_at = payout.sent_at.replace(tzinfo=None) if payout.sent_at is not None else None
                if sent_at is not None and sent_at > cutoff:
                    # Свежая вещация: блокчейн мог ещё не успеть — даём время.
                    continue
                # memo нет в истории, окно верификации истекло — перевод не ушёл.
                payout.status = "pending"
                payout.attempts += 1
                payout.last_error = (
                    f"memo «{_payout_comment_candidates(payout)[0][:40]}» не найдено в блокчейне "
                    f"за {settings.payout_confirm_timeout_seconds} с после вещания — повторная отправка"
                )
                requeued += 1
            await session.commit()
    if requeued:
        logger.warning("Сверка: %d выплат подтверждены, %d возвращены в очередь для ретрая", confirmed, requeued)
    elif confirmed:
        logger.info("Сверка: %d выплат подтверждены реальными хешами", confirmed)
    return confirmed + requeued

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
    """
    import app.ton_pay as _tp

    deadline = time.monotonic() + seconds
    while True:
        markers = await _tp.fetch_broadcast_markers()
        if any(c in markers for c in candidates):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_BROADCAST_CONFIRM_POLL)
