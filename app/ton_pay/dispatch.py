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
причиной).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import or_, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Player, Round, RoundStatus
from app.stakes import finalize_day_payouts
from app.ton_utils import to_nano

from . import state as _state

logger = logging.getLogger(__name__)

# Доли казны без игрока: адрес получателя — OWNER_WALLET_ADDRESS.
_TREASURY_KINDS = {"rake", "leaderboard"}

# Как часто проверять появление memo в блокчейне во время _wait_for_broadcast_memo.
_BROADCAST_CONFIRM_POLL = 2.0


def _comment_cell(text: str):
    """Memo-ячейка для исходящего сообщения: 32-битный нулевой op + utf8 текст.

    Текст усекается до 120 символов: ячейка v3 стандарта TON-комментария.
    Дубликат http_channel._comment_cell — оставлен локально, чтобы диспетчер
    не тянул импорт из соседнего модуля ради одного хелпера.
    """
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


def _payout_comment_candidates(payout) -> list[str]:
    """Все комментарии, которыми эта выплата МОГЛА уйти в цепочку.

    Служебное memo «way:<день>:<тип>#<id>» уникально глобально — на нём держится
    анти-дубль. Свободный текст переопределения (возвраты при паузе) общий для
    многих выплат: если слать его как есть, два возврата с одинаковым текстом
    становятся НЕРАЗЛИЧИМЫ для сверки — таймаут вещания одной строки прочитается
    как «уже ушла» по чужому переводу, и игрок не получит деньги.

    Поэтому переопределение дополняется служебным суффиксом (уникальный ключ
    сохраняется даже у строки на 120 символов — сам текст усекается), а
    первичный кандидат идёт в цепочку. Второй кандидат — сырой текст: это
    легаси-строки, отправленные ДО введения суффикса; их сверка должна уметь
    находить их в истории, иначе вернёт в очередь уже разосланное.
    """
    unique = f"way:{payout.round_id}:{payout.kind}#{payout.id}"
    override = payout.comment_override
    if not override:
        return [unique]
    suffix = f" | {unique}"
    available = max(0, 120 - len(suffix))
    return [f"{override[:available]}{suffix}", override]


async def _send_raw_with_seqno(wallet, seqno: int, dest_address: str, amount_nanotons: int, body) -> int:
    """Один перевод с ЗАДАННЫМ seqno (без get_seqno у сети).

    Собирает внутреннее сообщение, подписывает external-сообщение кошелька
    с явным seqno и вещает через лайтсерверы. Версии контракта отличаются
    параметром wallet_id: v5 держит network_global_id в wallet_id (тестнет и
    мейннет — разные адреса), v4 — константу. Используем wallet.wallet_id,
    который кошелёк сам знает из собственного state.
    """
    from pytoniq_core import Address

    internal = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=body,
    )
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[internal],
    )
    return await wallet.send_external(body=transfer_msg)


async def send_ton_transfer(dest_address: str, amount_nanotons: int, comment: str) -> str | None:
    """Отправляет перевод с казначея. Возвращает метку вещания или None.

    None — только когда отправка невозможна в принципе (TON выключен или нет
    мнемоники): вызывающий диспетчер сам запишет понятную причину в
    payouts.last_error. Реальные ошибки (пара мнемоника/адрес, лайтсерверы,
    seqno) ПРОПАГАЦИЯТСЯ исключением — диспетчер кладёт их текст в
    last_error, и причина видна в /payouts и алертах без раскопок логов.
    Успех фиксируется лайтсервером (результат 1); фактический хеш транзакции
    смотрится в эксплорере по memo-комментарию.
    """
    if not settings.ton_enabled or not settings.active_treasury_mnemonic:
        logger.warning("TON выключен или нет мнемоники: выплата к …%s не отправлена", dest_address[-6:])
        return None
    try:
        # Вызовы идут через app.ton_pay: тесты патчат ton_pay._get_wallet /
        # ton_pay._send_ton_transfer_http / ton_pay._is_liteserver_down.
        import app.ton_pay as _tp

        wallet = await _tp._get_wallet()
        if _state._batch_seqno is not None:
            # Диспетчер держит seqno из одного get_seqno() на цикл: два подряд
            # перевода не получают одинаковый seqno (иначе один молча потеряется).
            # Инкремент — только при УСПЕХЕ вещания; при сбое батч отменяется:
            # последующие переводы получат свежий seqno из нового get_seqno().
            seqno = _state._batch_seqno
            try:
                result = await _send_raw_with_seqno(
                    wallet, seqno, dest_address, amount_nanotons, _comment_cell(comment)
                )
            except (Exception, asyncio.CancelledError):
                # Таймаут диспетчера (asyncio.wait_for) обрывает корутину через
                # CancelledError — это НЕ Exception, и без явного перехвата
                # _batch_seqno остался бы протухшим: следующий перевод батча
                # переиспользовал бы уже разосланный seqno и молча потерялся.
                _state._batch_seqno = None
                raise
            if result != 1:
                _state._batch_seqno = None
                raise RuntimeError(f"Лайтсерверы не приняли перевод (результат {result})")
            _state._batch_seqno += 1
        else:
            result = await wallet.transfer(
                destination=dest_address,
                amount=amount_nanotons,
                body=_comment_cell(comment),
            )
        if result != 1:
            raise RuntimeError(f"Лайтсерверы не приняли перевод (результат {result})")
        marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
        logger.info("Перевод %d нанотонов к …%s разослан (%s)", amount_nanotons, dest_address[-6:], comment[:40])
        return marker
    except asyncio.CancelledError:
        # Таймаут диспетчера: он сам решит, что делать со строкой. HTTP-канал
        # сюда не цепляем — рваную корутину «добивать» нельзя.
        raise
    except Exception as exc:
        import app.ton_pay as _tp

        if not _tp._is_liteserver_down(exc):
            raise
        # Лайтсерверы мертвы (ADNL/TCP режется окружением), а деньги слать
        # надо: оффлайн-подпись + HTTPS-вещание через Toncenter.
        logger.warning(
            "Лайтсерверы недоступны (%s) — переключаюсь на HTTP-канал (toncenter)",
            exc,
        )
        return await _tp._send_ton_transfer_http(dest_address, amount_nanotons, comment)


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
                    confirmed += 1
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


async def _reset_retriable(session, network: str) -> None:
    """Оживляем зависшие sending/failed, пока не исчерпан лимит попыток.

    failed возвращается в очередь сразу (в мёртвой строке никто не «живёт»);
    sending — ТОЛЬКО если клейм заведомо «мёртв»: он старше
    payout_send_timeout_seconds + 30 c. Живое вещание (другая копия
    диспетчера держит строку до таймаута вещания) не перехватывается —
    иначе та копия на следующем цикле забрала бы строку и перевела деньги
    второй раз; memo-антидубль не поможет — перевод ещё не в цепочке.
    claimed_at IS NULL (строки, упавшие до появления колонки) считаем
    зависшими: живой клейм всегда пишет claimed_at сейчас. Сверка в naive
    UTC: Postgres вернёт aware, SQLite — naive (см. confirm_broadcast_payouts).
    """
    rows = (
        await session.execute(
            select(Payout.id, Payout.status, Payout.claimed_at).where(
                Payout.status.in_(["failed", "sending"]),
                Payout.attempts < settings.payout_max_attempts,
                Payout.dest_address != "",
                Payout.network == network,
            )
        )
    ).all()
    if not rows:
        return
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=settings.payout_send_timeout_seconds + 30
    )
    reset_ids = [payout_id for payout_id, status, _claimed in rows if status == "failed"]
    for payout_id, status, claimed_at in rows:
        if status != "sending":
            continue
        if claimed_at is None:
            reset_ids.append(payout_id)
        elif claimed_at.replace(tzinfo=None) <= cutoff:
            reset_ids.append(payout_id)
    if reset_ids:
        await session.execute(
            update(Payout).where(Payout.id.in_(reset_ids)).values(status="pending")
        )


async def _alert_http_channel_switch(bot: Bot | None, network: str) -> None:
    """Разово (не чаще раза в кулдаун) сообщает хранителю: казначей пишет
    исходящие через HTTP-канал, лайтсерверы недоступны. Без bot — тихо."""
    # Доступ к флагам через app.ton_pay — чтобы monkeypatch.setattr(ton_pay,
    # "_http_channel_engaged_at", ...) из тестов доходил до проверки.
    import app.ton_pay as _tp

    if bot is None or _tp._http_channel_engaged_at is None:
        return
    now = datetime.now(UTC)
    if _tp._last_http_channel_alert_at is not None:
        if now - _tp._last_http_channel_alert_at < _tp._HTTP_CHANNEL_ALERT_COOLDOWN:
            return
    _tp._last_http_channel_alert_at = now
    try:
        from app.ops import notify_admins  # локально: ops не импортируется наверху

        await notify_admins(
            bot,
            "⚠️ Казначей: лайтсерверы недоступны (ADNL/TCP режется окружением) — "
            f"исходящие идут через HTTP-канал (оффлайн-подпись + Toncenter sendBoc) "
            f"[{network}]. Вернусь к лайтсерверам сам, когда они оживут.",
        )
    except Exception as exc:
        logger.warning("Алерт о переключении на HTTP-канал не отправлен: %s", exc)


async def _alert_admin(bot: Bot | None, network: str) -> None:
    """Алерты о failed-выплатах. Дедуп — колонка payouts.alerted в БД:
    переживает рестарт и безопасен при нескольких инстансах. В текст идут
    причины из last_error — разбор начинается без открытия логов."""
    if bot is None:
        return
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Payout.id, Payout.last_error).where(
                        Payout.status == "failed",
                        Payout.alerted.is_(False),
                        Payout.network == network,
                    )
                )
            )
            .all()
        )
        if not rows:
            return
        # Условная пометка alerted=True: два процесса-диспетчера увидят одни
        # и те же failed-строки, но алерт по строке создаёт только тот, чей
        # UPDATE вернул rowcount=1 (второй уже видит alerted=True).
        claimed = []
        for payout_id, reason in rows:
            marked = (
                await session.execute(
                    update(Payout)
                    .where(Payout.id == payout_id, Payout.alerted.is_(False))
                    .values(alerted=True)
                )
            ).rowcount
            if marked:
                claimed.append((payout_id, reason))
        await session.commit()
        if not claimed:
            return
        sample = "; ".join(
            f"#{payout_id}: {reason}" if reason else f"#{payout_id}"
            for payout_id, reason in claimed[:3]
        )
        text = (
            f"⚠️ Выплаты не ушли ({len(claimed)} шт., сеть {network}). {sample}. "
            "Разбор: /payouts (причина видна у каждой строки)."
        )
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)


async def _hydrate_player_dests(session, network: str) -> int:
    """Оживляет выплаты без получателя, когда кошелёк уже привязан.

    Призы и возвраты игроков без привязанного кошелька на момент финализации
    не должны тонуть в failed (деньги спят, пока админ не разберёт вручную).
    Строка остаётся в очереди, а как только игрок привязывает адрес (/wallet),
    следующий же цикл диспетчера всталяет его в dest_address и платёж уходит
    сам — retry из /payouts не нужен. Доли казны (rake/leaderboard) без
    OWNER_WALLET_ADDRESS и выплаты без игрока (player_id пуст) оживлять нечем:
    честный failed с причиной-действием, как раньше.

    Возвращает число оживших строк (они поедут в пик этого же цикла).
    """
    from app.ton_utils import normalize_address

    rows = list(
        (
            await session.execute(
                select(Payout).where(
                    Payout.dest_address == "",
                    Payout.status == "pending",
                    Payout.network == network,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    player_ids = {p.player_id for p in rows if p.player_id is not None}
    wallet_map: dict[int, str] = {}
    verified_map: dict[int, bool] = {}
    if player_ids:
        players = await session.execute(
            select(Player.id, Player.wallet_address, Player.wallet_verified).where(Player.id.in_(player_ids))
        )
        for pid, addr, verified in players.all():
            wallet_map[pid] = addr
            verified_map[pid] = verified
    revived = 0
    for payout in rows:
        if payout.kind in _TREASURY_KINDS:
            if settings.owner_wallet_address:
                payout.dest_address = normalize_address(settings.owner_wallet_address)
                payout.last_error = None
                revived += 1
            else:
                payout.status = "failed"
                payout.last_error = "нет адреса получателя: для доли казны задай OWNER_WALLET_ADDRESS"
        elif payout.player_id is None:
            payout.status = "failed"
            payout.last_error = "нет адреса получателя (кошелёк игрока не найден)"
        else:
            addr = wallet_map.get(payout.player_id) or ""
            is_verified = verified_map.get(payout.player_id, False)
            if addr and is_verified:
                payout.dest_address = addr
                payout.last_error = None
                revived += 1
            elif addr and not is_verified:
                payout.last_error = "кошелёк привязан, но не подтверждён (игрок должен отправить bv:<код>)"
            else:
                payout.last_error = "нет адреса получателя: кошелёк игрока ещё не привязан"
    await session.commit()
    return revived


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
                    payout.last_error = (
                        "история казначея недоступна — повтор отложен (анти-дубль), "
                        "сверка с memo невозможна"
                    )
                    logger.warning("Выплата %d: повтор отложен — история казначея недоступна", payout.id)
                    continue
                if prev_memo and _tp._http_channel_engaged_at is not None:
                    # Гонка двух быстрых переводов: следующий seqno подписываем,
                    # только когда предыдущий перевод этого цикла подтверждён в
                    # блоке (HTTP-канал; лайтсерверный путь таких окон не знает).
                    if await _wait_for_broadcast_memo(
                        prev_memo, settings.payout_batch_confirm_seconds
                    ):
                        logger.debug(
                            "Выплата %d: предыдущий перевод подтверждён в блоке — seqno актуален",
                            payout.id,
                        )
                    else:
                        # За окно подтверждения перевод не пришёл: он в пути или
                        # потерян. Повторять сейчас НЕЛЬЗЯ (анти-дубль по memo),
                        # а батч-счётчик уже протух. Сбрасываем: следующая
                        # отправка заново прочитает живой seqno казначея, а судьбу
                        # этого перевода дожмёт confirm_broadcast_payouts.
                        logger.warning(
                            "Выплата %d: предыдущий перевод не подтвердился за %d с — "
                            "seqno для следующего будет взят из сети заново",
                            payout.id,
                            settings.payout_batch_confirm_seconds,
                        )
                        _state._batch_seqno = None
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
