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

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, or_, select, update

from app.config import settings
from app.db import SessionLocal

# HTTP-хелперы остаются ре-экспортом пакета: reconcile.py и http_channel.py
# берут их через app.ton_pay, чтобы monkeypatch.setattr(ton_pay, ...) доходил.
from app.http_utils import (
    get_http_client as get_http_client,
)
from app.http_utils import (
    http_get_with_retry as http_get_with_retry,
)
from app.http_utils import (
    http_post_with_retry as http_post_with_retry,
)
from app.models import Payout, Player, Round, RoundStatus
from app.stakes import finalize_day_payouts
from app.ton_utils import normalize_address, to_nano

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
_http_channel_engaged_at = _state._http_channel_engaged_at
_last_http_channel_alert_at = _state._last_http_channel_alert_at
_HTTP_CHANNEL_ALERT_COOLDOWN = _state._HTTP_CHANNEL_ALERT_COOLDOWN
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
        payout.status = "dismissed"
    elif action == "retry":
        payout.status = "pending"
        payout.alerted = False
    else:
        return None
    await session.commit()
    logger.info("Выплата %d разобрана вручную: %s", payout_id, payout.status)
    return payout.status


async def _fetch_remote_json(url: str) -> dict:
    """DEPRECATED: тонкая обёртка над wallet.fetch_remote_json для обратной
    совместимости тестов; новый код импортирует из wallet напрямую."""
    from .wallet import fetch_remote_json

    return await fetch_remote_json(url)


async def _get_wallet():
    """DEPRECATED: тонкая обёртка над wallet.get_wallet для обратной совместимости
    тестов и внутренних вызовов __init__. Новый код импортирует из wallet."""
    from .wallet import get_wallet

    return await get_wallet()


def _comment_cell(text: str):
    """Memo-ячейка для исходящего сообщения: 32-битный нулевой op + utf8 текст."""
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


# ---------- Анти-дубль: сверка memo с историей казначея (reconcile.py) ----------
#
# Реализация вынесена в reconcile.py. Здесь — прямые re-export'ы тех же объектов
# (не обёртки): app/rounds/lifecycle.py делает `from app.ton_pay import
# fetch_masterchain_entropy`, а тесты патчат ton_pay.fetch_broadcast_tx_map /
# ton_pay.fetch_broadcast_markers / ton_pay._tx_map_via_tonapi. Поэтому вызовы
# отсюда и внутри reconcile.py идут через app.ton_pay — иначе патч мимо.

# ---------- HTTP-канал отправки (fallback при мёртвых лайтсерверах) ----------
#
# ADNL/TCP до лайтсерверов может быть закрыт окружением (типовой тестнет под
# файрволом: чтение через REST-индексаторы работает, а вещание — нет, payout
# висит в очереди навсегда). Чтобы казна не «зависала», исходящие собираются
# ОФФЛАЙН (чистая локальная математика + seqno/публичный ключ через Toncenter
# v3 runGetMethod) и вещаются через HTTPS (Toncenter v2 jsonRPC sendBoc).
# Жизненный цикл строки и анти-дубль по memo не меняются: диспетчер перед
# повтором по-прежнему сверяется с историей исходящих.

# Реализация вынесена в http_channel.py. Здесь — прямые re-export'ы тех же
# объектов (не обёртки), чтобы monkeypatch.setattr(ton_pay, "_http_broadcast_external", ...)
# из тестов доходил до реальной функции в http_channel.py.
from . import reconcile as _reconcile_pkg  # noqa: E402,F401
from . import treasury as _treasury_pkg  # noqa: E402,F401
from .http_channel import (  # noqa: E402,F401
    _http_broadcast_external,
    _send_ton_transfer_http,
    http_get_wallet_seqno,
    is_liteserver_down,
    parse_run_method_seqno,
    send_wallet_transfer_http,
)
from .http_channel import (  # noqa: E402,F401
    http_get_wallet_seqno as _http_get_wallet_seqno,
)
from .http_channel import (  # noqa: E402,F401
    is_liteserver_down as _is_liteserver_down,
)
from .http_channel import (  # noqa: E402,F401
    parse_run_method_seqno as _parse_run_method_seqno,
)
from .reconcile import (  # noqa: E402,F401
    _out_comments,
    _out_comments_tonapi,
    _out_comments_toncenter,
    _tx_map_via_tonapi,
    _tx_map_via_toncenter,
    fetch_broadcast_markers,
    fetch_broadcast_tx_map,
    fetch_masterchain_entropy,
)

# Диагностика казначея живёт в treasury.py (её /treasury и /blockchain дёргают
# из admin-хендлеров). Здесь — прямые re-export'ы, не обёртки: http_channel и
# reconcile зовут fetch_account_state через app.ton_pay, чтобы
# monkeypatch.setattr(ton_pay, "fetch_account_state", ...) доходил до функции.
from .treasury import (  # noqa: E402,F401
    _tonapi_account_raw,
    _toncenter_account,
    blockchain_diagnostics,
    fetch_account_state,
    treasury_diagnostics,
    treasury_pair_check_text,
)

# Прямой алиас для send_ton_transfer (ниже по файлу).
_send_ton_transfer_http_direct = _send_ton_transfer_http


def build_offline_wallet(
    mnemonic: str,
    address: str,
    network_global_id: int,
    forced_version: str | None = None,
):
    """DEPRECATED: обёртка над wallet.build_offline_wallet для обратной совместимости."""
    from .wallet import build_offline_wallet as _build

    return _build(mnemonic, address, network_global_id, forced_version)


def _build_offline_treasury_wallet():
    """DEPRECATED: обёртка над wallet.build_offline_treasury_wallet."""
    from .wallet import build_offline_treasury_wallet as _build

    return _build()


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
        wallet = await _get_wallet()
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
        if not _is_liteserver_down(exc):
            raise
        # Лайтсерверы мертвы (ADNL/TCP режется окружением), а деньги слать
        # надо: оффлайн-подпись + HTTPS-вещание через Toncenter.
        logger.warning(
            "Лайтсерверы недоступны (%s) — переключаюсь на HTTP-канал (toncenter)",
            exc,
        )
        return await _send_ton_transfer_http(dest_address, amount_nanotons, comment)


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
    async with _DISPATCH_LOCK:
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
    global _last_http_channel_alert_at
    if bot is None or _http_channel_engaged_at is None:
        return
    now = datetime.now(UTC)
    if _last_http_channel_alert_at is not None:
        if now - _last_http_channel_alert_at < _HTTP_CHANNEL_ALERT_COOLDOWN:
            return
    _last_http_channel_alert_at = now
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


# Доли казны без игрока: адрес получателя — OWNER_WALLET_ADDRESS.
_TREASURY_KINDS = {"rake", "leaderboard"}


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


async def dispatch_pending_payouts(limit: int = 50, bot: Bot | None = None) -> int:
    """Разгребает очередь выплат. Весь цикл под _DISPATCH_LOCK: только один
    диспетчер в эвентлупе вещает, _reset_retriable не восстанавливает строки,
    которые другой цикл взял в работу (иначе двойная рассылка)."""
    async with _DISPATCH_LOCK:
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
            try:
                balance, _status, _source = await fetch_account_state()
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
                wallet_for_batch = await _get_wallet()
                _state._batch_seqno = await wallet_for_batch.get_seqno()
            except Exception:
                # Сбой не критичен: выродимся в старый путь, где send_ton_transfer
                # сам получает seqno (а её тред-безопасность отдельная история).
                _state._batch_seqno = None
                logger.warning("Не удалось получить seqno для батч-отправки — отправлю по одному", exc_info=True)
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
                try:
                    tx_hash = await asyncio.wait_for(
                        send_ton_transfer(
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
    """Финализирует фонды закрытых дней и разбирает очередь выплат."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Round.id).where(
                Round.status == RoundStatus.CLOSED,
                Round.payouts_finalized.is_(False),
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
