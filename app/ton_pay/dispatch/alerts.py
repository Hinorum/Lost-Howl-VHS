"""Алерты казначея: почему выплаты не ушли (дедуп по колонке alerted)
и разовый сигнал хранителю, что исходящие пошли через HTTP-канал."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Payout

logger = logging.getLogger(__name__)

async def _alert_http_channel_switch(bot: Bot | None, network: str) -> None:
    """Разово (не чаще раза в кулдаун) сообщает хранителю: казначей пишет
    исходящие через HTTP-канал, лайтсерверы недоступны. Без bot — тихо."""
    # Флаги берём из app.ton_pay.state: их туда пишет http_channel при уходе
    # в HTTP-канал. Раньше тут стоял алиас `ton_pay._http_channel_engaged_at` —
    # это снапшот значения на момент импорта (None), который в проде никто не
    # обновляет: алерт молчал при живом переключении, а тесты проходили, потому
    # что писали в тот же алиас. Единственный источник правды — state.
    import app.ton_pay as _tp

    state = _tp.state
    if bot is None or state._http_channel_engaged_at is None:
        return
    now = datetime.now(UTC)
    if state._last_http_channel_alert_at is not None:
        if now - state._last_http_channel_alert_at < state._HTTP_CHANNEL_ALERT_COOLDOWN:
            return
    state._last_http_channel_alert_at = now
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
