"""Диспетчер выплат: основной цикл очереди, анти-дубль, батч-seqno, алерты.

Это сердце казначея — здесь сходятся:
  - http_channel (лайтсервер + HTTPS-фолбэк)
  - reconcile (сверка истории казначея)
  - treasury (баланс и диагностика)
  - state (блокировки и кэши)
  - app.stakes (финализация раунда)
  - app.models (Payout lifecycle: pending → sending → sent/failed)

Главный инвариант — НЕ задвоить платёж при гонке процессов (conditional UPDATE
на pending→sending, claim_once для confirm'а, анти-дубль по memo в истории
казначея) и НЕ потерять перевод при крэше (sending→pending по таймауту,
sent подтверждается реальным хешем, failed уходит в dead-letter с видимой
причиной)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, cast

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus
from app.stakes import finalize_day_payouts
from app.ton_utils import to_nano

from .. import state as _state

logger = logging.getLogger(__name__)

# Листы пакета. Доступны и как app.ton_pay.dispatch.<имя> — тем же
# способом, как раньше был плоский модуль.
from . import alerts as alerts
from . import confirm as confirm
from . import memo as memo
from . import queue as queue
from . import send as send
from .alerts import _alert_admin, _alert_http_channel_switch
from .confirm import _BROADCAST_CONFIRM_POLL, _wait_for_broadcast_memo, confirm_broadcast_payouts  # noqa: F401
from .memo import (
    _comment_cell,  # noqa: F401
    _payout_comment_candidates,
)
from .queue import (
    _TREASURY_KINDS,  # noqa: F401
    _hydrate_player_dests,
    _reset_retriable,
)
from .send import (  # noqa: F401
    BatchRejected,
    BatchUncertain,
    _send_raw_batch_with_seqno,
    _send_raw_with_seqno,
    send_ton_transfer,
    send_ton_transfers_batch,
    wallet_supports_batch,
)


async def _balance_via_liteserver() -> int | None:
    """Баланс казначея напрямую с liteserver — третий источник.

    TonAPI и Toncenter — HTTP-индексаторы: у них своя квота, свой аптайм, свой
    прокси; читают они аккаунт, а не ту транзакцию, которую мы собираемся
    слать. Liteserver — тот же канал, которым уходит сама отправка, поэтому
    для решения «хватит ли средств» его ответ авторитетнее. И важнее другое:
    при живой сети он ровно тогда, когда отправка вообще возможна — так
    «не знаю» перестаёт означать «слать вслепую».

    Читаем ТОЛЬКО когда оба индексатора молчат, под коротким таймаутом: это
    проба перед решением, а не отправка.
    """
    if not settings.active_treasury_mnemonic or not settings.active_treasury_address:
        return None

    async def probe() -> int:
        # Через app.ton_pay — тем же способом, как в цикле ищутся
        # fetch_account_state/send_ton_transfer, чтобы тесты патчили один объект.
        import app.ton_pay as _tp

        wallet = await _tp._get_wallet()
        return int(await wallet.get_balance())

    try:
        return await asyncio.wait_for(
            probe(), timeout=settings.payout_balance_probe_timeout_seconds
        )
    except Exception as exc:
        logger.warning(
            "Баланс казначея через liteserver не прочитан: %s",
            str(exc) or type(exc).__name__,
        )
        return None


async def _wait_for_batch_memos(
    batch_rows: list[tuple[Payout, tuple[str, ...]]], seconds: float
) -> set[int]:
    """Ждёт memo пачки в истории казначея: id строк, чьи memo уже видны.

    Пачечный аналог _wait_for_broadcast_memo: там цель одна (предыдущий
    перевод пачки), здесь — кандидаты всех строк сразу, с ранним стопом по
    факту «нашли всё». Транзакция v5r1 атомарна, поэтому частично найденное —
    рассинхрон индексации провайдера, а не «половина ушла»: строки с
    найденным memo вызывающий фиксирует sent, остальным пишет причину.

    Флаги полноты (_RECONCILE_*) — снапшот/восстановление, как в
    _wait_for_broadcast_memo: целевой скан с ранним стопом неполон по
    построению и не имеет права менять вердикты соседних строк очереди.
    """
    import app.ton_pay as _tp

    saved_ok = _state._RECONCILE_HISTORY_OK
    saved_window = _state._RECONCILE_WINDOW_COMPLETE
    try:
        targets = {
            candidate for _payout, candidates in batch_rows for candidate in candidates
        }
        deadline = time.monotonic() + seconds
        while True:
            tx_map = await _tp.fetch_broadcast_tx_map(targets=targets)
            found = {
                payout.id
                for payout, candidates in batch_rows
                if any(candidate in tx_map for candidate in candidates)
            }
            if len(found) == len(batch_rows) or time.monotonic() >= deadline:
                return found
            await asyncio.sleep(_BROADCAST_CONFIRM_POLL)
    finally:
        _state._RECONCILE_HISTORY_OK = saved_ok
        _state._RECONCILE_WINDOW_COMPLETE = saved_window


async def _try_send_batch_v5(
    payouts: list[Payout], markers: set[str]
) -> tuple[int, set[int]]:
    """Одна пачка за цикл: отправляемые строки — одним сообщением v5r1.

    Возвращает (сколько строк помечено sent, id строк, которые поштучный
    проход этого цикла обязан пропустить). Исходы:
      • в пачку не берутся строки, уже найденные в маркерах (поштучный проход
        пометит их sent без повтора), и повторы с недоступной/неполной
        историей казначея (поштучный проход заморозит их с видимой причиной);
      • BatchRejected (ошибка сборки/явный отказ лайтсерверов) — ничего не
        помечаем и не держим: поштучный проход отправит строки как раньше.
        Транзакция v5r1 атомарна (одно плохое действие отменяет всю пачку),
        поэтому поштучный фолбэк — не опция, а требование корректности;
      • всё прочее (таймаут вещания, сбой обоих каналов, неклассифицированные
        ошибки) — «неясно»: вещание могло состояться, повтор в этом цикле
        рискнул бы двойным платежом. Ждём memo пачки: найденное фиксируем
        sent без повтора, остальное — pending с видимой причиной (попытку
        не возвращаем: вещание могло уйти); следующий цикл начнёт с
        анти-дубля.
    """
    history_trusted = _state._RECONCILE_HISTORY_OK and _state._RECONCILE_WINDOW_COMPLETE
    batch_rows: list[tuple[Payout, tuple[str, ...]]] = []
    for payout in payouts:
        candidates = tuple(_payout_comment_candidates(payout))
        if any(candidate in markers for candidate in candidates):
            continue
        if payout.attempts > 1 and not history_trusted:
            continue
        batch_rows.append((payout, candidates))
    if not batch_rows:
        return 0, set()

    import app.ton_pay as _tp

    payload = [
        (payout.dest_address, payout.amount_nanotons, candidates[0])
        for payout, candidates in batch_rows
    ]
    try:
        marker = await asyncio.wait_for(
            _tp.send_ton_transfers_batch(payload),
            timeout=settings.payout_send_timeout_seconds,
        )
    except BatchRejected as exc:
        logger.warning(
            "Пачка из %d переводов не принята (%s) — отправляю поштучно",
            len(batch_rows),
            exc,
        )
        return 0, set()
    except Exception as exc:
        # «Неясно», а не «не ушло»: таймаут вещания (asyncio.wait_for), сбой
        # обоих каналов после таймаута лайтсерверов (BatchUncertain) и прочие
        # неклассифицированные сбои. Внешка могла уйти в сеть — повтор в этом
        # цикле рискнул бы двойным платежом. Решаем по memo, как одиночный
        # путь при таймауте.
        if isinstance(exc, TimeoutError):
            logger.warning(
                "Пачка из %d переводов: таймаут вещания >%ss — решаю по memo",
                len(batch_rows),
                settings.payout_send_timeout_seconds,
            )
        else:
            logger.warning(
                "Пачка из %d переводов: вещание не подтвердилось (%s) — "
                "считаю судьбу неясной, повтор в цикле не шлю",
                len(batch_rows),
                exc,
            )
        found = await _wait_for_batch_memos(
            batch_rows, settings.payout_batch_confirm_seconds
        )
        confirmed = 0
        for payout, _candidates in batch_rows:
            if payout.id in found:
                payout.tx_hash = None
                payout.status = "sent"
                payout.sent_at = datetime.now(UTC)
                payout.attempts = 0
                payout.last_error = None
                confirmed += 1
                continue
            payout.last_error = (
                "судьба пачки неясна (таймаут вещания или сбой канала) — "
                "повтор отложен до сверки по memo"
            )[:200]
            if payout.attempts >= settings.payout_max_attempts:
                payout.status = "failed"
            else:
                payout.status = "pending"
        logger.warning(
            "Пачка: вещание неясно — memo найдено у %d из %d, остальные ждут сверки",
            confirmed,
            len(batch_rows),
        )
        return confirmed, {payout.id for payout, _candidates in batch_rows}

    if marker is None:
        # TON выключен или нет мнемоники: причина строк пишется поштучным
        # проходом ниже (он единственный путь с видимым last_error).
        return 0, set()
    for payout, _candidates in batch_rows:
        payout.tx_hash = marker
        payout.status = "sent"
        payout.sent_at = datetime.now(UTC)
        payout.attempts = 0
        payout.last_error = None
    logger.info("Пачка: %d переводов разошлись одним внешним сообщением", len(batch_rows))
    return len(batch_rows), {payout.id for payout, _candidates in batch_rows}


async def dispatch_pending_payouts(limit: int = 50, bot: Bot | None = None) -> int:
    """Разгребает очередь выплат. Весь цикл под _DISPATCH_LOCK: только один
    диспетчер в эвентлупе вещает, _reset_retriable не восстанавливает строки,
    которые другой цикл взял в работу (иначе двойная рассылка)."""
    async with _state._DISPATCH_LOCK:
        return await _dispatch_pending_payouts_impl(limit=limit, bot=bot)

async def _dispatch_pending_payouts_impl(limit: int, bot: Bot | None) -> int:
    sent = 0
    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as session:
        # Kill switch хранителя (/halt-payouts) — самая первая проверка цикла,
        # до клейма строк и до возврата зависших. Деньги не уходят, но очередь
        # НЕ тонет молча: каждой ожидающей строке пишем причину, чтобы /payouts
        # отвечал «почему не уходит», а не показывал устаревший текст.
        # Подтверждения уже ушедших переводов при этом продолжает вести
        # confirm_broadcast_payouts — он читает цепочь и ничего не отправляет,
        # останавливать его означало бы потерять судьбу in-flight выплат.
        from app.ops import is_payouts_halted, payout_halt_reason

        if await is_payouts_halted(session):
            halt_reason = await payout_halt_reason(session)
            note = (
                "исходящие выплаты остановлены (/halt-payouts): " + halt_reason
                if halt_reason
                else "исходящие выплаты остановлены (/halt-payouts)"
            )
            logger.warning("Диспетчер: %s — очередь не трогаем", note)
            await session.execute(
                update(Payout)
                .where(
                    Payout.status == "pending",
                    Payout.dest_address != "",
                    Payout.amount_nanotons > 0,
                    Payout.network == network,
                )
                .values(last_error=note[:200])
            )
            await session.commit()
            return 0
        # Ретрай: зависшие failed с неисчерпанным лимитом снова в очередь.
        await _reset_retriable(session, network)
        await session.commit()
        # Призы без кошелька оживают сами, когда игрок привязал адрес: это
        # отдельный проход, а НЕ статус failed, иначе строки тонули бы в
        # мёртвых письмах, а игрок терял бы деньги без веской причины.
        await _hydrate_player_dests(session, network)
        result = await session.execute(
            select(Payout)
            .where(
                Payout.status == "pending",
                # Пустые получатели в пик не берём: они либо оживают выше в этом
                # же цикле, либо ждут кошелёк. Иначе они съедали бы лимит из
                # 50 строк и голодали настоящие выплаты.
                Payout.dest_address != "",
                Payout.amount_nanotons > 0,
                Payout.network == network,
            )
            .order_by(Payout.id.asc())
            .limit(limit)
        )
        payouts = list(result.scalars().all())
        # Предохранитель баланса: не вещаем переводы, которые сеть отвергнет
        # из-за нехватки средств на казначее. Fail-fast с понятной причиной:
        # статус остаётся pending, попытки НЕ сгорают — после пополнения
        # очередь уйдёт сама, без ручного retry и без мёртвых писем.
        #
        # «Не знаю» раньше обходило предохранитель: баланс не прочитан —
        # слали всё равно, надеясь, что liteserver отвергнет сам. Логика была
        # про доступность («в очередь нельзя вечно висеть»), но плата —
        # неизвестная: по пограничному балансу сжигается газ на отвергнутые
        # внешние сообщения, попытки копятся на отправке, которой не было, и
        # строка уезжает в dead-letter с причиной «не хватает средств»,
        # хотя проверить это было нечем. Неизвестность — это не «пусто» и не
        # «хватает», это «отправить не можем решить», а значит — не шлём.
        # Третий источник (liteserver напрямую) держит такой момент редким:
        # баланс читается там же, откуда уйдёт сам перевод.
        sendable = [payout for payout in payouts if payout.dest_address]
        if (
            sendable
            and settings.active_treasury_address
            and settings.active_treasury_mnemonic
        ):
            import app.ton_pay as _tp

            try:
                balance, _status, _source = await _tp.fetch_account_state()
            except Exception as exc:
                logger.warning("Баланс казначея перед циклом не прочитан: %s", exc)
                balance = None
            if balance is None:
                balance = await _balance_via_liteserver()
            if balance is None:
                reason = (
                    "баланс казначея неизвестен: TonAPI, Toncenter и liteserver молчат — "
                    "отправка отложена до восстановления связи"
                )
                logger.warning(
                    "Диспетчер: %s (выплат в очереди: %d)", reason, len(sendable)
                )
                for payout in sendable:
                    payout.last_error = reason[:200]
                await session.commit()
                return 0
            fee_nano = to_nano(settings.payout_fee_gram)
            needed = sum(p.amount_nanotons for p in sendable) + fee_nano * len(sendable)
            if balance < needed:
                reason = (
                    f"казначей подкачан: нужно {needed / 1e9:.4f} Gram (с газом), "
                    f"есть {balance / 1e9:.4f} — пополни баланс, очередь уйдёт сама"
                )
                logger.warning("Диспетчер: %s", reason)
                for payout in sendable:
                    payout.last_error = reason[:200]
                await session.commit()
                return 0
        # Атомарный клейм «взятых в работу» ДО вещания. Условный UPDATE по
        # status='pending' — единственный процесс (одна копия диспетчера)
        # переведёт строку в sending: rowcount==1. Вторая копия (двойной
        # процесс: закрытие дня + ton-settle + ручной кик) получит 0 и НЕ
        # возьмёт строку — иначе оба вещали бы один перевод и задваивали
        # трату казны. Падение после клейма обратимо: _reset_retriable вернёт
        # sending → pending на следующем цикле, а memo-антидубль (attempts>1)
        # уберёт повтор уже ушедшего перевода.
        claimed_ids: set[int] = set()
        # Попытки ДО клейма: клейм тратит попытку на весь батч разом, а откат
        # ниже (пачка встала на неподтверждённом переводе, либо история казначея
        # недоступна) может вернуть в очередь строки, которые НИКОГДА не
        # отправлялись. Без возврата счётчика они копят потраченные попытки на
        # чужих сбоях и уходят в dead-letter, не будучи отправлены ни разу.
        prior_attempts: dict[int, int] = {}
        for payout in payouts:
            if not payout.dest_address:
                continue
            gate = await session.execute(
                update(Payout)
                .where(Payout.id == payout.id, Payout.status == "pending")
                .values(status="sending")
            )
            if cast(CursorResult[Any], gate).rowcount != 1:
                # Строку уже забрала другая копия — не трогаем и не вещаем.
                continue
            prior_attempts[payout.id] = int(payout.attempts or 0)
            payout.attempts += 1
            payout.status = "sending"
            payout.claimed_at = datetime.now(UTC)
            claimed_ids.add(payout.id)
        await session.commit()
        # Работаем только строками, что реально забрали мы: сама рассылка
        # (claimed). Строки другой копии диспетчера в эту сессию НЕ трогаем.
        payouts = [p for p in payouts if p.id in claimed_ids]
        # Сверка с историей: если комментарий уже есть среди недавних
        # исходящих казначея — перевод ушёл в прошлом цикле (краш между
        # вещанием и коммитом). Повторная отправка задвоила бы платёж.
        # Доступность и полнота истории считаются ЗАНОВО для этого цикла: сбой
        # в прошлом цикле не должен вечно замораживать повторы — история могла
        # ожить, а окно — снова стать полным. Реальный fetch ниже перепишет
        # оба флага по факту (сбой → False, усечение по max_pages → неполное
        # окно; тесты, патчащие fetch, остаются на прежнем доверии).
        _state._RECONCILE_HISTORY_OK = True
        _state._RECONCILE_WINDOW_COMPLETE = True
        markers: set[str] = set()
        if payouts:
            # Через app.ton_pay: тесты патчат ton_pay.fetch_broadcast_markers.
            import app.ton_pay as _tp

            markers = await _tp.fetch_broadcast_markers()
        # Батч: берём seqno кошелька ОДИН раз на цикл и наращиваем его локально
        # на каждую рассылку. Иначе каждый перевод делал бы свой get_seqno(),
        # и два подряд перевода (приз + рейк одного дня) получили бы ОДИН и тот
        # же seqno — в блок входил бы только один, второй тихо терялся.
        _state._batch_seqno = None
        v5_batch_capable = False
        wallet_for_batch = None
        if payouts and settings.ton_enabled and settings.active_treasury_mnemonic:
            # Через app.ton_pay: тесты патчат ton_pay._get_wallet /
            # ton_pay._http_get_wallet_seqno / ton_pay._is_liteserver_down.
            import app.ton_pay as _tp

            try:
                wallet_for_batch = await _tp._get_wallet()
                _state._batch_seqno = await wallet_for_batch.get_seqno()
                # Seqno прочитан лайтсерверами — серия сбоев канала оборвалась.
                _tp.notify_liteserver_ok()
                # Пачка одним внешним сообщением — механизм контракта v5r1
                # (out_list). Проверяем класс кошелька, которым реально
                # подписываем, а не строку версии из настроек.
                v5_batch_capable = _tp.wallet_supports_batch(wallet_for_batch)
            except Exception as exc:
                # Сбой не критичен: выродимся в старый путь, где send_ton_transfer
                # сам получает seqno (а её тред-безопасность отдельная история).
                # Исключение — сбой лайтсерверного канала (лагующая нода не
                # отдаёт стейт для seqno): читаем seqno по HTTP. Иначе пачка
                # цикла рассыпалась бы в поштучные отправки, хотя само вещание
                # тот же классификатор всё равно уведёт в HTTP-канал.
                _state._batch_seqno = None
                v5_batch_capable = False
                if wallet_for_batch is not None and _tp._is_liteserver_down(exc):
                    # Серия сбоев лайтсерверов: после порога пул пересоберётся
                    # при следующем обращении (флаг TON_PROVIDER_REBUILD).
                    _tp.notify_liteserver_down(exc)
                    try:
                        _state._batch_seqno = await _tp._http_get_wallet_seqno(wallet_for_batch)
                        v5_batch_capable = _tp.wallet_supports_batch(wallet_for_batch)
                    except Exception as http_exc:
                        _state._batch_seqno = None
                        v5_batch_capable = False
                        logger.warning(
                            "seqno не прочитан ни лайтсерверами (%s), ни по HTTP (%s) — отправлю по одному",
                            exc,
                            http_exc,
                        )
                    else:
                        logger.warning(
                            "seqno лайтсерверами не прочитан (%s) — прочитан по HTTP, пачка остаётся пачкой",
                            exc,
                        )
                else:
                    logger.warning("Не удалось получить seqno для батч-отправки — отправлю по одному", exc_info=True)
        # Известен ли уже разосланный на этом цикле перевод? Если да — ждём его
        # memo в блоке перед подписью следующего (гонка двух быстрых переводов
        # HTTP-канала: вплотную разосланные месседжи со следующими seqno
        # сражаются за место, и второй молча отбрасывается).
        prev_memo: set[str] | None = None
        # Пачка встала на паузу из-за неподтверждённого предыдущего перевода:
        # строки, которые остались в 'sending' за нами, НИКОГДА не вещаем в этом
        # цикле — иначе их seqno пришлось бы угадывать вслепую.
        deferred = False
        # Пачечный проход (флаг PAYOUT_V5_BATCH + контракт v5r1): пробует
        # увести все отправляемые строки цикла ОДНИМ внешним сообщением.
        # Строки, уже найденные в маркерах, и замороженные повторы сюда не
        # берутся — их разбирает поштучный проход ниже, как раньше.
        try:
            batch_skip_ids: set[int] = set()
            if settings.payout_v5_batch and v5_batch_capable and payouts:
                batch_sent, batch_skip_ids = await _try_send_batch_v5(payouts, markers)
                sent += batch_sent
            for payout in payouts:
                if payout.id in batch_skip_ids:
                    # Строка уже разобрана пачечным проходом: либо помечена
                    # sent одним сообщением, либо удержана после неясного
                    # вещания (повтор в этом цикле мог бы задвоить платёж).
                    continue
                # Свободный комментарий (возвраты при паузе) дополняется
                # служебным суффиксом «way:<день>:<тип>#<id>», чтобы анти-дубль
                # не спотыкался на одинаковом тексте разных возвратов.
                candidates = _payout_comment_candidates(payout)
                comment = candidates[0]
                if any(candidate in markers for candidate in candidates):
                    # Перевод уже ушёл в цепочку раньше, но статус тогда не
                    # сохранился (краш/таймаут после вещания). Повтор задвоил бы
                    # платёж — фиксируем доставку без новой отправки.
                    payout.tx_hash = None
                    payout.status = "sent"
                    payout.sent_at = datetime.now(UTC)
                    payout.last_error = None
                    sent += 1
                    logger.warning(
                        "Выплата %d уже разослана ранее (memo найдено у казначея) — помечена sent без повтора",
                        payout.id,
                    )
                    continue
                history_trusted = (
                    _state._RECONCILE_HISTORY_OK and _state._RECONCILE_WINDOW_COMPLETE
                )
                if (
                    payout.attempts > 1
                    and not any(candidate in markers for candidate in candidates)
                    and not history_trusted
                ):
                    # Повтор (>1 попытки) и история казначея НЕДОСТУПНА ИЛИ НЕПОЛНА:
                    # не знаем, не ушёл ли этот перевод тем же memo в прошлом цикле
                    # (краш между вещанием и коммитом). Пустой ответ маркеров в этом
                    # случае означает «молчат оба провайдера» либо «скан упёрся в
                    # max_pages, не дотянувшись до глубины», а НЕ «перевода нет».
                    # Переотправка в «не знаю» = реальный двойной платёж.
                    # Замораживаем строку с видимой причиной: история вернётся —
                    # сверка повторится сама, без ручного retry.
                    payout.status = "pending"
                    # Отправки не было — попытку клейма возвращаем: иначе
                    # недоступная история казначея съедала бы по попытке за
                    # цикл у каждой повторной выплаты.
                    payout.attempts = prior_attempts.get(payout.id, int(payout.attempts or 0))
                    payout.last_error = (
                        "история казначея недоступна или неполна — повтор отложен (анти-дубль), "
                        "сверка с memo невозможна"
                    )
                    logger.warning(
                        "Выплата %d: повтор отложен — история казначея недоступна/неполна",
                        payout.id,
                    )
                    continue
                if prev_memo:
                    # Гонка двух быстрых переводов: следующий seqno подписываем,
                    # только когда предыдущий перевод этого цикла подтверждён в
                    # блоке. Канал тут ни при чём — окно нужно на ЛЮБОМ пути,
                    # лайтсерверном в том числе: подписанный seqno+1 может ещё
                    # лежать в мемпуле узла, и следующий внешний месседж с
                    # seqno+2 сражается с ним за место (второй молча теряется,
                    # ловим только 2-часовой сверкой). Условие на
                    # _http_channel_engaged_at было оптимизацией, которая
                    # осталась после того, как seqno начали переиспользовать и
                    # на лайтсерверном пути (до этого каждый перевод брал
                    # свежий get_seqno и ждать было нечего).
                    if await _wait_for_broadcast_memo(
                        prev_memo, settings.payout_batch_confirm_seconds
                    ):
                        logger.debug(
                            "Выплата %d: предыдущий перевод подтверждён в блоке — seqno актуален",
                            payout.id,
                        )
                    else:
                        # За окно подтверждения перевод не пришёл: он в пути или
                        # потерян. Пачку на этом цикле останавливаем, и это не
                        # перестраховка, а требование корректности: сброс
                        # _batch_seqno заставил бы следующий перевод взять seqno
                        # из сети, а там всё ещё старая (предыдущая) величина —
                        # второй перевод подписался бы тем же номером и молча
                        # потерялся бы, как уже ушедший. Живой seqno можно
                        # прочитать только после того, как предыдущий перевод
                        # встанет в блок; до этого — пауза. Оставшиеся строки
                        # вернёт _reset_retriable следующим циклом, а судьбу
                        # неподтверждённого перевода дожмёт confirm_broadcast_payouts.
                        logger.warning(
                            "Выплата %d: предыдущий перевод не подтвердился за %d с — "
                            "пачка на этом цикле остановлена, seqno не читаем из сети, "
                            "пока предыдущий не в блоке",
                            payout.id,
                            settings.payout_batch_confirm_seconds,
                        )
                        _state._batch_seqno = None
                        deferred = True
                        break
                try:
                    # Через app.ton_pay — чтобы monkeypatch.setattr(ton_pay,
                    # "send_ton_transfer", mock) из тестов доходил до вызова.
                    import app.ton_pay as _tp

                    tx_hash = await asyncio.wait_for(
                        _tp.send_ton_transfer(
                            payout.dest_address,
                            payout.amount_nanotons,
                            comment=comment,
                        ),
                        timeout=settings.payout_send_timeout_seconds,
                    )
                except TimeoutError:
                    # Зависший лайтсервер не имеет права замораживать цикл, но
                    # «таймаут» — ещё не «перевод не ушёл»: отменённая корутина
                    # могла успеть отдать внешний месседж в сеть. Раньше строка
                    # сразу уходила в pending, и следующий цикл переотправлял тот
                    # же memo, пока первый перевод ещё не проиндексирован, —
                    # реальный двойной платёж. Ждём memo ограниченное окно (как
                    # после успешного вещания): нашёлся — перевод в цепочке,
                    # фиксируем sent без повтора (реальный хеш дожмёт
                    # confirm_broadcast_payouts); нет — обычный ретрай с видимой
                    # причиной, как раньше.
                    logger.warning("Выплата %s: таймаут вещания >%ss", payout.id, settings.payout_send_timeout_seconds)
                    payout.last_error = f"таймаут вещания (>{settings.payout_send_timeout_seconds} с)"
                    if await _wait_for_broadcast_memo(
                        set(candidates), settings.payout_batch_confirm_seconds
                    ):
                        payout.tx_hash = None
                        payout.status = "sent"
                        payout.sent_at = datetime.now(UTC)
                        payout.attempts = 0
                        payout.last_error = None
                        sent += 1
                        prev_memo = set(candidates)
                        logger.warning(
                            "Выплата %d: таймаут вещания, но memo найдено у казначея — "
                            "помечена sent без повтора",
                            payout.id,
                        )
                        continue
                    tx_hash = None
                except Exception as exc:
                    # «no alive peers» сюда уже не доходит: send_ton_transfer
                    # перехватывает сбой лайтсерверного канала и уходит в HTTP
                    # (тот при неуспехе падает текстом провайдера — он и виден).
                    reason = str(exc)
                    logger.warning("Выплата %s не ушла: %s", payout.id, exc)
                    payout.last_error = reason[:200]
                    tx_hash = None
                if tx_hash is None and payout.last_error is None:
                    # Единственный путь сюда — guard выключенного TON/мнемоники.
                    payout.last_error = "отправка недоступна: TON выключен или нет мнемоники казначея"
                if tx_hash:
                    payout.tx_hash = tx_hash
                    payout.status = "sent"
                    payout.sent_at = datetime.now(UTC)
                    payout.attempts = 0
                    payout.last_error = None
                    sent += 1
                    # Следующий перевод пачки дождётся подтверждения ЭТОГО в блоке.
                    prev_memo = set(candidates)
                elif payout.attempts >= settings.payout_max_attempts:
                    payout.status = "failed"
                else:
                    # Лимит не исчерпан — вернётся в очередь следующего цикла;
                    # last_error сохраняем: причина видна в /payouts уже сейчас.
                    payout.status = "pending"
        finally:
            _state._batch_seqno = None
        if deferred:
            # Причина одна на всю остановленную часть пачки, чтобы в /payouts
            # было видно, что дело в неподтверждённом предыдущем переводе, а не
            # в каждой строке отдельно. Строки откатываем сразу: так они не
            # висят в полусостоянии «взято в работу, но не взято».
            reason = (
                f"пачка приостановлена: предыдущий перевод не подтверждён в блоке за "
                f"{settings.payout_batch_confirm_seconds} с — ждём блок, чтобы не "
                f"подписать следующий seqno вслепую"
            )
            for rest in payouts:
                if rest.status != "sending":
                    continue
                rest.status = "pending"
                # Попытку возвращаем: эти строки клейм заняли вместе с батчем,
                # но до отправки дело не дошло — они стоят правее остановившейся
                # строки. Иначе после нескольких таких циклов (окно
                # подтверждения 15с на живом API — обычное дело) строка
                # исчерпает payout_max_attempts и уйдёт в dead-letter, не будучи
                # отправлена ни разу.
                rest.attempts = prior_attempts.get(rest.id, int(rest.attempts or 0))
                rest.last_error = reason
        await session.commit()
    dead = [p.id for p in payouts if p.status == "failed"]
    if dead:
        logger.warning("Выплаты окончательно не отправлены: %s", dead)
    # Алерт по ВСЕМ неотправленным без предупреждения (включая найденные
    # после рестарта): дедуп внутри _alert_admin по колонке alerted.
    await _alert_admin(bot, network)
    # Отдельная история: выплаты ушли, но через HTTP-канал — хранитель должен
    # знать, что лайтсерверы за стеной (дедуп по времени, см. хелпер).
    await _alert_http_channel_switch(bot, network)
    return sent

async def settle_closed_rounds(bot: Bot | None = None) -> int:
    """Финализирует фонды закрытых дней и разбирает очередь выплат.

    Финализация ловит только НЕДАВНИЕ закрытые дни (catchup_cutoff):
    finalize_day_payouts не проверяет уже созданные выплаты (идемпотентность
    только по флагу), а у исторических дней payouts_finalized=false с
    server_default=0 — без границы джоба пересоздавала бы выплаты за всю
    историю. Отправка внизу не ограничена по возрасту: уже созданные
    выплаты (kind=pending) должны уйти независимо от давности дня.
    """
    from app.rounds.time import catchup_cutoff

    async with SessionLocal() as session:
        result = await session.execute(
            select(Round.id).where(
                Round.status == RoundStatus.CLOSED,
                Round.payouts_finalized.is_(False),
                Round.voting_ends_at >= catchup_cutoff(),
            )
        )
        round_ids = [row[0] for row in result.all()]
    created = 0
    for round_id in round_ids:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            if round_row is not None:
                created += await finalize_day_payouts(session, round_row)
    await dispatch_pending_payouts(bot=bot)
    return created
