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
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus
from app.stakes import finalize_day_payouts
from app.ton_utils import to_nano

from .. import state as _state

logger = logging.getLogger(__name__)

# Листы пакета. Доступны и как app.ton_pay.dispatch.<имя> — тем же
# способом, как раньше был плоский модуль.
from . import alerts as alerts  # noqa: F401
from . import confirm as confirm  # noqa: F401
from . import memo as memo  # noqa: F401
from . import queue as queue  # noqa: F401
from . import send as send  # noqa: F401
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
from .send import _send_raw_with_seqno, send_ton_transfer  # noqa: F401


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
        # Если баланс недоступен (оба индексатора молчат) — логируем, но
        # ПРОБУЕМ отправить: liteclient работает через прямое TCP-соединение
        # к liteserver, а не через HTTP API. Пусть liteserver отвергнет сам,
        # если средств мало — это надёжнее, чем висеть в очереди навсегда.
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
                logger.warning(
                    "Баланс казначея недоступен (оба индексатора молчат) — "
                    "попытка отправки через liteclient напрямую (%d выплат)",
                    len(sendable),
                )
            if balance is not None:
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
            if gate.rowcount != 1:
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
        # Доступность истории считается ЗАНОВО для этого цикла: сбой в прошлом
        # цикле не должен вечно замораживать повторы — история могла ожить.
        # Реальный сбой fetch_broadcast_markers ниже снова выставит False.
        _state._RECONCILE_HISTORY_OK = True
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
        if payouts and settings.ton_enabled and settings.active_treasury_mnemonic:
            try:
                import app.ton_pay as _tp

                wallet_for_batch = await _tp._get_wallet()
                _state._batch_seqno = await wallet_for_batch.get_seqno()
            except Exception:
                # Сбой не критичен: выродимся в старый путь, где send_ton_transfer
                # сам получает seqno (а её тред-безопасность отдельная история).
                _state._batch_seqno = None
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
        try:
            for payout in payouts:
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
                if (
                    payout.attempts > 1
                    and not any(candidate in markers for candidate in candidates)
                    and not _state._RECONCILE_HISTORY_OK
                ):
                    # Повтор (>1 попытки) и история казначея НЕДОСТУПНА: не знаем,
                    # не ушёл ли этот перевод тем же memo в прошлом цикле (краш
                    # между вещанием и коммитом). Пустой ответ маркеров в этом
                    # случае означает «молчат оба провайдера», а НЕ «перевода
                    # нет». Переотправка в «не знаю» = реальный двойной платёж.
                    # Замораживаем строку с видимой причиной: история вернётся —
                    # сверка повторится сама, без ручного retry.
                    payout.status = "pending"
                    # Отправки не было — попытку клейма возвращаем: иначе
                    # недоступная история казначея съедала бы по попытке за
                    # цикл у каждой повторной выплаты.
                    payout.attempts = prior_attempts.get(payout.id, int(payout.attempts or 0))
                    payout.last_error = (
                        "история казначея недоступна — повтор отложен (анти-дубль), "
                        "сверка с memo невозможна"
                    )
                    logger.warning("Выплата %d: повтор отложен — история казначея недоступна", payout.id)
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
                    # Зависший лайтсервер не имеет права замораживать цикл:
                    # таймаут — обычный ретрай с видимой причиной.
                    logger.warning("Выплата %s: таймаут вещания >%ss", payout.id, settings.payout_send_timeout_seconds)
                    payout.last_error = f"таймаут вещания (>{settings.payout_send_timeout_seconds} с)"
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
