"""Личные сообщения игроку по судьбе его перевода. Доставка не обязательна для
успеха обработки — перевод уже учтён."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode

from app.models import Player
from app.ton_utils import from_nano
from app.ton_watch.sources import Transfer

logger = logging.getLogger(__name__)

async def _dm_stake(bot: Bot | None, player_id: int, text: str) -> None:
    """Личное сообщение о судьбе ставки; доставка не обязательна для учёта.

    Тело несёт <code>bv:…</code> и прочие HTML-теги — шлём с разметкой.
    """
    if bot is None or player_id <= 0:
        return
    try:
        await bot.send_message(player_id, text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.info("Сообщение игроку %s не доставлено: %s", player_id, exc)

async def _dm_verify_mismatch(bot: Bot | None, player: Player | None, transfer: Transfer) -> None:
    """Личное объяснение, почему микро-перевод с verify-мемо не привязал кошелёк.

    Перевод уже возвращается отправителю штатным авто-возвратом, но без
    сообщения игрок, обрезавший/перепутавший код, не понимает, что случилось —
    и награда за его верные дни продолжает ждать подтверждения кошелька.
    """
    if bot is None or player is None:
        return
    code = player.wallet_verify_code
    code_hint = f" с кодом <code>bv:{code}</code>" if code else ""
    await _dm_stake(
        bot,
        player.id,
        f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
        "код подтверждения кошелька не сошёлся (код другой или переведено "
        f"не с привязанного адреса). Повтори микро-перевод строго с привязанного "
        f"адреса{code_hint} — префикс bv: писать не обязательно.",
    )

async def _kick_dispatch_after_verify(bot: Bot | None) -> None:
    """После верификации кошелька — кик очереди выплат (разумеется, под замком).

    Удержанные на неподтверждённом кошельке призы разблокированы: без кика
    диспетчер сработает только на закрытии дня, а игрок ждал бы награды до 11:00.
    """
    try:
        from app.ton_pay import dispatch_pending_payouts

        await dispatch_pending_payouts(bot=bot)
    except Exception as exc:
        logger.info("Кик очереди выплат после верификации не удался: %s", exc)
