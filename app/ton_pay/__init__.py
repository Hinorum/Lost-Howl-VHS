"""Исходящие выплаты победителям: расчёт в stakes.finalize_day_payouts,
отправка здесь — через pytoniq напрямую к лайтсерверам активной сети.
Казначейский кошелёк поддерживается в двух версиях контракта — v4r2 и
v5r1 (кошельки нового поколения): версия детектируется автоматически по
адресу казначея либо задаётся явно переменной TREASURY_WALLET_VERSION.

Жизненный цикл выплаты: pending → sending (зафиксировано ДО вещания, чтобы
падение сервиса не привело к двойной отправке) → sent / failed. Зависшие
sending и неуспешные failed с attempts < PAYOUT_MAX_ATTEMPTS оживают каждый
цикл автоматически; при исчерпании лимита админ получает алерт. Перед
ПОВТОРНОЙ отправкой (attempts > 1) очередь сверяется с memo недавних
исходящих казначея: если перевод уже ушёл в цепочку в прошлый раз, он
помечается sent без повтора — краш между вещанием и коммитом не задваивает
платёж. Ручной retry (/payout, resolve_dead_payout) счётчик попыток НЕ
сбрасывает: он сам мог повернуть в очередь уже ушедший перевод, и только
attempts >= 1 заставляет диспетчер сверяться с историей перед отправкой.

Призы и возвраты без получателя (игрок не привязал кошелёк к моменту
финализации) не тонут в failed: строки ждут в очереди, и когда игрок
привязывает адрес, диспетчер вставляет его в следующий же цикл и платёж
уходит сам. Доли казны без OWNER_WALLET_ADDRESS и переводы без игрока
честно падают в failed с причиной-действием.

tx_hash после отправки — метка вещания «bcast:<unix>»: лайтсервер не
возвращает хеш транзакции. Фактический перевод ищется в эксплорере по адресу
казначея и memo-комментарию вида way:<день>:<тип>#<id выплаты>.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import (  # noqa: F401  (re-export for tests via `ton_pay.{datetime,UTC,timedelta}`)
    UTC,
    datetime,
    timedelta,
)

from sqlalchemy import func, select

# HTTP-хелперы остаются ре-экспортом пакета: reconcile.py и http_channel.py
# берут их через app.ton_pay, чтобы monkeypatch.setattr(ton_pay, ...) доходил.
from app.db import SessionLocal  # noqa: F401
from app.http_utils import (  # noqa: F401
    get_http_client as get_http_client,
)
from app.http_utils import (
    http_get_with_retry as http_get_with_retry,
)
from app.http_utils import (
    http_post_with_retry as http_post_with_retry,
)
from app.models import Payout, Player, Round, RoundStatus  # noqa: F401
from app.stakes import finalize_day_payouts  # noqa: F401

logger = logging.getLogger(__name__)

# Глобальное состояние пакета: блокировки, кэши, флаги доступности истории.
# Все обращения через `state.<имя>` (см. ниже). Этот шаг — первый шаг
# рефакторинга ton_pay.py в пакет; wallet.py и http_channel.py используют
# то же состояние через короткие псевдонимы внизу файла.
from . import (  # noqa: E402,F401  (submodules exposed as `app.ton_pay.{state,wallet,...}`)
    state,
)
from . import state as _state  # noqa: E402
from . import wallet as _wallet_pkg  # noqa: E402,F401  (re-export as `app.ton_pay.wallet`)

# Удобные алиасы внутри модуля (для краткости ссылок в __init__).
_wallet_lock = _state._wallet_lock
_provider = _state._provider
_wallet = _state._wallet
_wallet_network = _state._wallet_network
_DISPATCH_LOCK = _state._DISPATCH_LOCK
_HTTP_CHANNEL_ALERT_COOLDOWN = _state._HTTP_CHANNEL_ALERT_COOLDOWN
# Флаги `_http_channel_engaged_at` / `_last_http_channel_alert_at` (тип
# datetime | None) сюда НЕ алиасируем: алиас значимого типа — это снапшот на
# момент импорта, а пишет их http_channel в state. Так и молчал алерт о
# переключении на HTTP-канал: писали в state, читали из нулевой копии здесь.
# Читать и писать — только через `state.<имя>`.
_warned_no_toncenter_key = _state._warned_no_toncenter_key
_RECONCILE_PAGE_LIMIT = _state._RECONCILE_PAGE_LIMIT
_RECONCILE_PAGE_OVERLAP = _state._RECONCILE_PAGE_OVERLAP


@asynccontextmanager
async def dispatch_lock():
    """Лок очереди выплат для ручных блокирующих операций (admin /refinalize):
    удаление/перемаркировка строк не должна попадать в цикл диспетчера."""
    async with _DISPATCH_LOCK:
        yield


# Совместимость с тестами и старым кодом: алиасы для приватных имён из старого
# ton_pay.py. Реальные реализации живут в wallet.py, здесь — тонкие обёртки.
def _wallet_address(version, public_key, network_global_id, wc=0):  # noqa: ANN001,ANN201
    from .wallet import wallet_address as _impl

    return _impl(version, public_key, network_global_id, wc)


def _detect_wallet_version(public_key, treasury_address, network_global_id):  # noqa: ANN001,ANN201
    from .wallet import detect_wallet_version as _impl

    return _impl(public_key, treasury_address, network_global_id)


# Алиас для константы из wallet.py (тесты используют ton_pay._V4R2_WALLET_ID).
from .wallet import _V4R2_WALLET_ID  # noqa: E402,F401


async def pending_payout_count(session) -> int:
    """Сколько переводов ещё не ушли (обе сети, включая dead-letter failed).

    «sent» — единственное конечное состояние успеха; «dismissed» — ручной
    вердикт хранителя (спам-перевод с рекламой и т.п.), он деньгам игрокам
    не равен и сбросу не мешает. Всё остальное значит, что деньги игроку
    ещё должны: сброс игры обязан ждать, пока долг закрыт.
    """
    result = await session.execute(
        select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
    )
    return int(result.scalar_one())


async def resolve_dead_payout(session, payout_id: int, action: str) -> str | None:
    """Ручной разбор проблемной выплаты хранителем.

    action="spam" — статус «dismissed»: пыльный спам-перевод с рекламой,
    возврат которого не нужен или невозможен. Выплата исчезает из очереди,
    алертов и перестаёт блокировать /resetgame. action="retry" — обратно в
    очередь (настоящий долг игроку). Счётчик попыток НЕ сбрасываем: попытка
    могла реально уйти в цепочку (краш между вещанием и коммитом «sent»), и
    повтор без сверки с memo казначея задвоил бы платёж. Значение attempts
    >= 1 гарантирует, что диспетчер прогонит анти-дубль по истории исходящих.
    Возвращает новый статус или None, если выплаты нет либо она уже отправлена.
    """
    payout = await session.get(Payout, payout_id)
    if payout is None or payout.status == "sent":
        return None
    if action == "spam":
        if payout.kind != "refund":
            raise ValueError(
                f"Пометить спамом можно только refund-выплату (входящий перевод-реклама), "
                f"а не {payout.kind}: игроку полагаются деньги. Такую строку разбирай "
                "направленно, а не гаси."
            )
        payout.status = "dismissed"
    elif action == "retry":
        payout.status = "pending"
        payout.alerted = False
    else:
        return None
    await session.commit()
    logger.info("Выплата %d разобрана вручную: %s", payout_id, payout.status)
    return payout.status


async def _get_wallet():
    """Точка подмены кошелька: dispatch вызывает через app.ton_pay, чтобы
    monkeypatch.setattr(ton_pay, "_get_wallet", ...) доходил до отправки."""

    return await get_wallet()



# Диспетчер выплат: реальная реализация в dispatch.py, здесь — re-export'ы тех
# же объектов (не обёртки), чтобы monkeypatch.setattr(ton_pay, "send_ton_transfer", ...)
# из тестов доходил до фактической функции.
from .dispatch import (  # noqa: F401  # noqa: E402,F401
    _BROADCAST_CONFIRM_POLL,
    _TREASURY_KINDS,
    _alert_admin,
    _alert_http_channel_switch,
    _comment_cell,
    _dispatch_pending_payouts_impl,
    _hydrate_player_dests,
    _payout_comment_candidates,
    _reset_retriable,
    _send_raw_with_seqno,
    _wait_for_broadcast_memo,
    confirm_broadcast_payouts,
    dispatch_pending_payouts,
    send_ton_transfer,
    settle_closed_rounds,
)
from .http_channel import (  # noqa: F401
    _http_broadcast_external,
    _send_ton_transfer_http,
    http_get_wallet_seqno,
    is_liteserver_down,
    parse_run_method_seqno,
    send_wallet_transfer_http,
)
from .http_channel import (  # noqa: F401
    http_get_wallet_seqno as _http_get_wallet_seqno,
)

# HTTP-канал отправки: реализация в http_channel.py. Re-export для
# monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", ...) в тестах.
from .http_channel import (  # noqa: F401  # noqa: E402,F401
    is_liteserver_down as _is_liteserver_down,
)
from .http_channel import (  # noqa: F401
    parse_run_method_seqno as _parse_run_method_seqno,
)

# Сверка истории казначея: реализация в reconcile.py. Re-export для
# monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", ...) в тестах.
from .reconcile import (  # noqa: F401  # noqa: E402,F401
    _out_comments,
    _out_comments_tonapi,
    _out_comments_toncenter,
    _tx_map_via_tonapi,
    _tx_map_via_toncenter,
    fetch_broadcast_markers,
    fetch_broadcast_tx_map,
    fetch_masterchain_entropy,
)

# Диагностика казначея (/treasury, /blockchain): реализация в treasury.py.
# Re-export для monkeypatch.setattr(ton_pay, "fetch_account_state", ...) в тестах.
from .treasury import (  # noqa: F401  # noqa: E402,F401
    _tonapi_account_raw,
    _toncenter_account,
    blockchain_diagnostics,
    fetch_account_state,
    treasury_diagnostics,
    treasury_pair_check_text,
)

# Кошельки: реализация в wallet.py. Re-export публичных имён для обратной
# совместимости тестов и кода, импортирующего from app import ton_pay.
from .wallet import (  # noqa: F401  # noqa: E402,F401
    NETWORK_GLOBAL_IDS,
    WALLET_VERSIONS,
    build_offline_treasury_wallet,
    build_offline_wallet,
    detect_wallet_version,
    get_wallet,
    wallet_address,
)
from .wallet import (  # noqa: F401
    build_offline_treasury_wallet as _build_offline_treasury_wallet,
)


