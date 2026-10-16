"""Вещание перевода с казначея: батч-seqno на цикл, лайтсерверы
и уход в HTTP-канал (оффлайн-подпись + Toncenter), когда ADNL/TCP
режется окружением."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import settings

from .. import state as _state
from .memo import _comment_cell

logger = logging.getLogger(__name__)

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
        if _state._http_channel_engaged_at is not None:
            # Лайтсерверы снова принимают вещание — канал откатился, флаг
            # снимается. Иначе алерт «лайтсерверы недоступны» повторялся бы
            # каждые 6 ч до самого рестарта, обещая возврат, который уже
            # случился (флаг ставит только уход в HTTP-канал).
            _state._http_channel_engaged_at = None
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


def wallet_supports_batch(wallet) -> bool:
    """Умеет ли контракт нести пачку переводов в одном внешнем сообщении.

    Пачка — механизм v5r1 (out_list действий внутри контрактной транзакции).
    v4r2 упаковывает максимум 4 перевода, а очередь цикла может быть длиннее,
    поэтому для него остаётся поштучный путь. Проверка по КЛАССУ кошелька, а
    не по строке версии из настроек: источник истины — то, чем реально
    подписываем.
    """
    from pytoniq.contract.wallets.wallet_v5 import WalletV5R1

    return isinstance(wallet, WalletV5R1)


async def _send_raw_batch_with_seqno(
    wallet, seqno: int, transfers: list[tuple[str, int, str]]
) -> int:
    """Пачка переводов с ЗАДАННЫМ seqno — ОДНИМ внешним сообщением кошелька.

    transfers — [(адрес, нанотоны, комментарий)]. v5r1 пакует все действия в
    out_list одной транзакции: один seqno, один внешний месседж, атомарный
    результат (одно плохое действие отменяет ВСЮ пачку — поэтому у
    вызывающего обязан быть поштучный фолбэк). Порядок переводов сохраняется.
    """
    from pytoniq_core import Address

    internals = [
        wallet.create_wallet_internal_message(
            destination=Address(dest_address),
            value=amount_nanotons,
            body=_comment_cell(comment),
        )
        for dest_address, amount_nanotons, comment in transfers
    ]
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=internals,
    )
    return await wallet.send_external(body=transfer_msg)


async def send_ton_transfers_batch(transfers: list[tuple[str, int, str]]) -> str | None:
    """Пачка переводов ОДНИМ внешним сообщением v5r1. Метка вещания или None.

    None — только когда отправка невозможна в принципе (TON выключен или нет
    мнемоники) — как у send_ton_transfer. Реальные ошибки ПРОПАГАЦИЯТСЯ:
    вызывающий сам решает, уходить ли на поштучный фолбэк. Успех фиксируется
    лайтсервером (результат 1); «принято» ≠ «выполнено» — контрактная
    транзакция атомарна, а отмену действия дожмёт сверка
    confirm_broadcast_payouts.

    Батч-счётчик _batch_seqno держится тем же инвариантом, что у одиночного
    пути: один seqno на пачку, локальный инкремент только при УСПЕХЕ вещания.
    Вызывается из цикла диспетчера (seqno уже взят на цикл) и из смоука
    /payout test (вне цикла seqno читается у сети один раз на всю пачку).
    """
    if not transfers:
        raise ValueError("пустая пачка переводов")
    if not settings.ton_enabled or not settings.active_treasury_mnemonic:
        logger.warning(
            "TON выключен или нет мнемоники: пачка из %d переводов не отправлена",
            len(transfers),
        )
        return None
    try:
        # Вызовы идут через app.ton_pay: тесты патчат ton_pay._get_wallet /
        # ton_pay._send_ton_transfers_http / ton_pay._is_liteserver_down.
        import app.ton_pay as _tp

        wallet = await _tp._get_wallet()
        if _state._batch_seqno is not None:
            # Диспетчер держит seqno из одного get_seqno() на цикл: вся пачка
            # подписывается одним номером, гонки следующих seqno внутри неё нет.
            # Инкремент — только при УСПЕХЕ вещания; при сбое батч отменяется.
            seqno = _state._batch_seqno
            try:
                result = await _send_raw_batch_with_seqno(wallet, seqno, transfers)
            except (Exception, asyncio.CancelledError):
                # Таймаут диспетчера (asyncio.wait_for) обрывает корутину через
                # CancelledError — это НЕ Exception, и без явного перехвата
                # _batch_seqno остался бы протухшим.
                _state._batch_seqno = None
                raise
            if result != 1:
                _state._batch_seqno = None
                raise RuntimeError(f"Лайтсерверы не приняли пачку (результат {result})")
            _state._batch_seqno += 1
        else:
            # Вне цикла диспетчера (смоук /payout test): свежий seqno на всю
            # пачку; устаревший номер контракт отвергнет сам, потерять
            # перевод нельзя.
            result = await _send_raw_batch_with_seqno(
                wallet, await wallet.get_seqno(), transfers
            )
        if result != 1:
            raise RuntimeError(f"Лайтсерверы не приняли пачку (результат {result})")
        if _state._http_channel_engaged_at is not None:
            # Лайтсерверы снова принимают вещание — флаг HTTP-канала снимается
            # (симметрично одиночному пути, иначе алерт «недоступны» врал бы
            # после возврата канала).
            _state._http_channel_engaged_at = None
        marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
        logger.info("Пачка из %d переводов разослана (%s)", len(transfers), marker)
        return marker
    except asyncio.CancelledError:
        # Таймаут диспетчера: он сам решит, что делать со строками. HTTP-канал
        # сюда не цепляем — рваную корутину «добивать» нельзя.
        raise
    except Exception as exc:
        import app.ton_pay as _tp

        if not _tp._is_liteserver_down(exc):
            raise
        logger.warning(
            "Лайтсерверы недоступны (%s) — пачка из %d переводов уходит в HTTP-канал (toncenter)",
            exc,
            len(transfers),
        )
        return await _tp._send_ton_transfers_http(transfers)
