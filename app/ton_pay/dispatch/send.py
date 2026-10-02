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
