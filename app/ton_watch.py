"""Наблюдатель входящих переводов казначейского кошелька (заготовка).

Раз в минуту забирает свежие транзакции казначея через TonAPI v2, сопоставляет
отправителя с привязанным кошельком игрока и регистрирует ставку на открытый
день. Переводы, которые не могут стать ставкой (неопознанный отправитель,
повтор за уже поставившего, закрытый день, слишком мелкая оплата смены пути),
автоматически попадают в очередь возвратов — деньги не оседают в казнее молча.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy import select, update

from app.config import settings
from app.core.registry import BEAT_KEY, CURSOR_KEY, SOURCE_KEY, STUCK_TX_KEY, WALLET_NORM_KEY
from app.db import SessionLocal
from app.http_utils import get_http_client, http_get_with_retry
from app.models import Income, Payout, Player, RevoteGrant, Round, RoundStatus, Stake, WatcherState
from app.ops import claim_once, is_game_paused
from app.payments import parse_bank_memo, parse_revote_memo, parse_verify_memo
from app.stakes import confirm_stake, current_network, register_stake
from app.ton_codec import api_headers, clean_comment, extract_comment, norm_tx_hash
from app.ton_utils import from_nano, normalize_address, to_nano

logger = logging.getLogger(__name__)

# Стартовый откат для первого запуска: не глубже полусуток.
_CURSOR_FALLBACK_HOURS = 12
# Перекрытие при чтении курсора: переводы, пришедшие в ту же секунду, что и
# последний обработанный, у индексатора могут появиться с задержкой. Берём
# курсор на N секунд раньше и пересматриваем окно заново каждым циклом —
# идемпотентность держится на tx_hash (duplicate_tx / refund_duplicated).
# Настраивается через watch_cursor_overlap_seconds: сеть с частыми reorg
# требует глубже перечитывать историю.
_CURSOR_OVERLAP_SECONDS = max(0, settings.watch_cursor_overlap_seconds)
# TonAPI v2 отдаёт страницы транзакций; идём вглубь, пока не накроем курсор
# или не упрёмся в пустое место (история кончилась / подряд пустые страницы).
_PAGE_LIMIT = max(1, settings.watch_page_limit)
_MAX_PAGES = max(1, settings.watch_max_pages)


_EMPTY_STOP = 2


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    source: str
    value_nanotons: int
    comment: str
    utime: int
    # Пагинационный ключ провайдера (Toncenter v3 требует lt, TonAPI — хеш);
    # для TonAPI-переводов остаётся пустым.
    provider_ref: str = ""


# Кодеки (заголовки, комментарии, хеши) переехали в app.ton_codec — единая
# реализация для watcher'а и диспетчера выплат. Префиксные имена оставлены
# псевдонимами: по ним ходят тесты (test_ops, test_watch_sources).
_api_headers = api_headers
_norm_tx_hash = norm_tx_hash
_clean_comment = clean_comment
_decode_comment = extract_comment


async def fetch_recent_transfers(since_utime: int, before_lt: str | None = None) -> tuple[list[Transfer], bool]:
    """Страница входящих переводов казначея активной сети (новые сверху).

    Ошибки сети не поднимают исключение: возвращается (пусто, False), чтобы
    цикл знал, что проверка не состоялась, и не ставил сердцебиение.
    before_lt — пагинация вглубь по логическому времени (lt): каждая следующая
    страница строго старше последнего lt предыдущей.

    Честная работа с 404: раньше «нет истории» считалось здоровьем, и падение
    индексатора TonAPI маскировалось под тихую цепочку (реальный инцидент:
    ставки не находятся, а /health зелёный). Теперь 404 перепроверяется по
    /v2/accounts/{адрес}: если аккаунт активен и у него есть активность после
    курсора — история TonAPI врёт, цикл считается несостоявшимся (False),
    и _collect_transfers переключается на фолбэк Toncenter v3.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], True
    url = (
        f"{settings.active_ton_api_base}/v2/blockchain/accounts/"
        f"{settings.active_treasury_address}/transactions"
    )
    headers = _api_headers(settings.ton_api_key)
    try:
        client = get_http_client()
        response = await http_get_with_retry(
            client, url,
            params={"limit": _PAGE_LIMIT, "sort_order": "desc", **({"before_lt": before_lt} if before_lt else {})},
            headers=headers,
        )
        if response.status_code == 404:
            # Пустая история бывает у двух причин: кошелёк правда молчал
            # или индексатор потерял историю. Различаем честно.
            return await _resolve_tonapi_empty_history(since_utime)
        response.raise_for_status()
        items = response.json().get("transactions", [])
    except Exception as exc:
        logger.warning("TonAPI недоступен: %s", exc)
        return [], False
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_tx_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, True


async def _resolve_tonapi_empty_history(since_utime: int) -> tuple[list[Transfer], bool]:
    """404 истории транзакций: «правда пусто» или «индекс сломан»?

    Сверяемся с карточкой аккаунта: активный кошелёк с активностью после
    курсора при пустой истории — деградация индексатора. Не сумели проверить
    (сеть/не-200) — тоже считаем цикл несостоявшимся: лучше лишний проход
    через фолбэк, чем пропущенная ставка.
    """
    info = await _tonapi_account_info()
    if not isinstance(info, dict):
        logger.warning(
            "TonAPI отдал 404 истории транзакций, но карточка аккаунта недоступна — "
            "цикл не признаётся успешным, переводы пойдут через фолбэк"
        )
        return [], False
    status = str(info.get("status") or "").strip().lower()
    try:
        last_activity = int(info.get("last_activity") or 0)
    except (TypeError, ValueError):
        last_activity = 0
    if status == "active" and last_activity > since_utime:
        logger.warning(
            "TonAPI отдал 404 истории транзакций при активном казначее с активностью %s "
            "(курсор %s) — индекс истории деградировал",
            last_activity,
            since_utime,
        )
        return [], False
    return [], True


async def _tonapi_account_info() -> dict | None:
    """Карточка казначея в TonAPI (/v2/accounts/{адрес}) или None при сбое."""
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}"
    try:
        client = get_http_client()
        response = await http_get_with_retry(client, url, headers=_api_headers(settings.ton_api_key))
    except Exception as exc:
        logger.warning("TonAPI не ответил на запрос карточки аккаунта: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


# Jetton::transfer_notification — входящий токен (USDt, NOT, …), а не TON.
_JETTON_OPCODES = {"0x7362d09c"}
# Не спамим в лог одним и тем же токеном каждый минутный цикл.
_warned_jettons: set[str] = set()


def _is_jetton_notification(in_msg: dict) -> bool:
    opcode = str(in_msg.get("opcode") or "").strip().lower()
    if opcode in _JETTON_OPCODES:
        return True
    msg_data = in_msg.get("msg_data")
    if isinstance(msg_data, dict):
        decoded_op = str(msg_data.get("decoded_op") or "").strip().lower()
        if decoded_op == "transfer_notification":
            return True
    return False


def _parse_tx_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция страницы -> Transfer либо None (старая/джеттон/мусор).

    Джеттон-уведомление — это НЕ ставка: value внутри обёртки — копейки
    газа, источник — jetton-кошелёк игрока. Такой перевод нельзя ни
    зачесть, ни автоматически вернуть, поэтому он пропускается целиком,
    без пыльного refund-payout: токены ждут ручного возврата с казначея.
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("utime", 0))
        if utime <= since_utime:
            return None
        if _is_jetton_notification(in_msg):
            tx_hash = str(item.get("hash") or "")
            if tx_hash and tx_hash not in _warned_jettons:
                if len(_warned_jettons) > 256:
                    _warned_jettons.clear()
                _warned_jettons.add(tx_hash)
                logger.warning(
                    "Входящий перевод %s… — токен (jetton), а не нативный Gram/TON. Ставкой не становится "
                    "и автоматически не возвращается: верни вручную с казначея.",
                    tx_hash[:16],
                )
            return None
        source = ((in_msg.get("source") or {}).get("address")) or ""
        value = int(in_msg.get("value") or 0)
        if value <= 0 or not source:
            return None
        return Transfer(
            tx_hash=_norm_tx_hash(str(item.get("hash") or "")),
            source=source,
            value_nanotons=value,
            comment=_decode_comment(in_msg),
            utime=utime,
            provider_ref=str(item.get("lt") or ""),
        )
    except Exception as exc:
        logger.warning("Странная транзакция пропущена: %s", exc)
        return None


def _parse_toncenter_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция Toncenter v3 -> Transfer либо None (старая/пустая).

    Джеттон-уведомления в выборку по аккаунту казначея не попадают вовсе
    (они садятся на jetton-кошелёк отправителя), поэтому отдельного фильтра,
    как у TonAPI, здесь не нужно. Комментарий приходит декодированным в
    message_content.decoded с типом «comment».
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("now") or 0)
        if utime <= since_utime:
            return None
        source = in_msg.get("source") or ""
        if isinstance(source, dict):
            source = source.get("address") or ""
        value = int(str(in_msg.get("value") or 0))
        if value <= 0 or not source:
            return None
        decoded = (in_msg.get("message_content") or {}).get("decoded") or {}
        comment = ""
        if isinstance(decoded, dict) and decoded.get("@type") in ("comment", "text_comment"):
            comment = _clean_comment(str(decoded.get("comment") or ""))
        return Transfer(
            tx_hash=_norm_tx_hash(str(item.get("hash") or "")),
            source=str(source),
            value_nanotons=value,
            comment=comment,
            utime=utime,
            provider_ref=str(item.get("lt") or ""),
        )
    except Exception as exc:
        logger.warning("Странная транзакция Toncenter пропущена: %s", exc)
        return None


# Toncenter v3 не отдаёт страницы больше этого размера.
_TONCENTER_MAX_LIMIT = 256


async def _toncenter_page(since_utime: int, before_lt: str | None = None) -> tuple[list[Transfer], str]:
    """Страница переводов казначея через Toncenter API v3 (фолбэк TonAPI).

    Контракт как у fetch_recent_transfers, но вместо булева — состояние
    страницы (_PAGE_OK/_PAGE_DEGRADED): фолбэк вызывается, только когда
    основной источник деградировал. Пагинация вглубь по before_lt.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], _PAGE_OK
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    params: dict = {
        "account": settings.active_treasury_address,
        "limit": min(_PAGE_LIMIT, _TONCENTER_MAX_LIMIT),
        "sort": "desc",
    }
    if before_lt:
        params["before_lt"] = before_lt
    try:
        client = get_http_client()
        response = await http_get_with_retry(client, url, params=params, headers=_api_headers(settings.toncenter_api_key))
        response.raise_for_status()
        items = response.json().get("transactions") or []
    except Exception as exc:
        logger.warning("Toncenter v3 недоступен: %s", exc)
        return [], _PAGE_DEGRADED
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_toncenter_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, _PAGE_OK


async def _ledger_stuck_incoming(
    session, transfer: Transfer, player_id: int | None, result: str
) -> None:
    """Учёт входящего перевода, который НЕ возвращается (пыль/древний).

    Деньги остаются в казне навсегда — без строки Income сверка с балансом
    цепочки работала бы на «проценты пропажи» для каждой такой суммы. Пыль
    и старый хлам тоже становятся строчкой дохода: «in:refund:dust» /
    «in:refund:expired», и ожидания БД сходятся с реальностью.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    # Cross-process в той же транзакции, что и сама запись: два watcher-инстанса
    # на один перевод не гоняются по check-then-insert (Income.unit_ref
    # уникален, но проигравший поймал бы IntegrityError и ушёл в stuck-список
    # ложным «не обработано»). Проигравший против метки выходит без изменений;
    # откат транзакции снимает метку вместе с записью.
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=None,
            player_id=player_id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=f"in:{result};src:…{transfer.source[-10:]}"[:200],
        )
    )
    await session.commit()


async def _stash_refund(
    session,
    transfer: Transfer,
    round_id: int | None,
    comment: str | None = None,
    *,
    ledger_result: str | None = None,
    ledger_player_id: int | None = None,
    force: bool = False,
) -> str:
    """Авто-возврат перевода + запись в ledger доходов за один коммит.

    Идемпотентно по tx_hash: повторная обработка той же транзакции не плодит
    вторую выплату. Отправка — обычным порядком через dispatch_pending_payouts.

    Древние переводы (старше WATCH_REFUND_MAX_AGE_DAYS) не возвращаются: после
    сброса базы курсор обнуляется и история казны перечитывается целиком —
    без лимита старый спам вечно рождал бы новые dead-letter возвраты.
    comment — свободный текст перевода вместо служебного memo «way:…»
    (возвраты при паузе игры объясняют игроку, что идут техработы).
    ledger_result — если передан, создаётся запись Income в том же коммите.
    force — вернуть даже сумму меньше refund_min_gram («пыль»). По умолчанию
    пыль не возвращается (газ дороже), НО это верно для анонимного спама;
    известный отправитель (привязанный игрок, неудачная верификация кошелька)
    должен получить свои копейки назад — иначе деньги пропадают молча.
    """
    age_days = (datetime.now(UTC).timestamp() - transfer.utime) / 86_400
    if age_days > max(0, settings.watch_refund_max_age_days):
        logger.warning(
            "Перевод %s старше %d дн. — авто-возврат не создаётся (спам/хлам остаётся в казне)",
            transfer.tx_hash[:16],
            int(age_days),
        )
        await _ledger_stuck_incoming(session, transfer, ledger_player_id, "refund:expired")
        return "refund_expired"
    if not force and transfer.value_nanotons < to_nano(settings.refund_min_gram):
        # Газ возврата дороже самой пыли: микро-перевод остаётся в казне, а не
        # превращается в убыточный dead-letter. Игроку не пишем — это спам-боты.
        logger.info(
            "Перевод %s на %s Gram дешевле порога %s Gram — авто-возврат не создаётся",
            transfer.tx_hash[:16],
            f"{from_nano(transfer.value_nanotons):g}",
            settings.refund_min_gram,
        )
        await _ledger_stuck_incoming(session, transfer, ledger_player_id, "refund:dust")
        return "refund_dust"
    # Защита от двойной выплаты: транзакция уже учтена как ставка (Stake) или
    # как входящий доход казны (Income — ставка, revote-оплата, микро-верификация).
    # Повторный проход (сброс курсора, overlap-окно, пауза, ре-скан) НЕ должен
    # создавать второй авто-возврат: монета уже легла в банк дня (приз/Фонд)
    # или в выручку, и повторный refund — это двойной расход казны.
    booked = await session.execute(
        select(Stake.id).where(
            Stake.tx_hash == transfer.tx_hash, Stake.network == current_network()
        )
    )
    if booked.scalar_one_or_none() is not None:
        logger.warning(
            "Перевод %s уже учтён ставкой (tx в Stake) — авто-возврат отменён "
            "(защита от двойной выплаты)",
            transfer.tx_hash[:16],
        )
        return "already_booked"
    booked = await session.execute(
        select(Income.id).where(
            Income.unit_ref == transfer.tx_hash, Income.network == current_network()
        )
    )
    if booked.scalar_one_or_none() is not None:
        logger.warning(
            "Перевод %s уже учтён входящим доходом казны (Income) — авто-возврат "
            "отменён (защита от двойной выплаты)",
            transfer.tx_hash[:16],
        )
        return "already_booked"
    duplicate = await session.execute(
        select(Payout.id).where(Payout.kind == "refund", Payout.tx_hash == transfer.tx_hash).limit(1)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "refund_duplicated"
    # Cross-process маркер в той же транзакции, что и создание выплаты:
    # два watcher-процесса не создадут по своему возврату на один перевод
    # (payouts.tx_hash не уникален), проигравший уйдёт без изменения данных.
    if not await claim_once(session, f"refund:{transfer.tx_hash}"):
        return "refund_duplicated"
    session.add(
        Payout(
            round_id=round_id,
            player_id=None,
            kind="refund",
            amount_nanotons=transfer.value_nanotons,
            dest_address=transfer.source or "",
            tx_hash=transfer.tx_hash[:80],
            network=current_network(),
            comment_override=comment[:120] if comment else None,
        )
    )
    if ledger_result is not None:
        existing_income = await session.execute(
            select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
        )
        if existing_income.scalar_one_or_none() is None:
            session.add(
                Income(
                    kind="ton",
                    amount_nanotons=transfer.value_nanotons,
                    round_id=round_id,
                    player_id=ledger_player_id,
                    network=current_network(),
                    unit_ref=transfer.tx_hash,
                    note=f"in:{ledger_result};src:…{transfer.source[-10:]}"[:200],
                )
            )
    await session.commit()
    logger.info("Перевод %s возвращён отправителю", transfer.tx_hash[:16])
    return "refund_queued"


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


async def _ledger_incoming(
    session, transfer: Transfer, player_id: int | None, round_id: int | None, result: str
) -> None:
    """Каждый входящий перевод казначея — в журнал доходов (/incoming).

    Аудит «откуда деньги»: сумма, момент, хеш, хвост адреса отправителя и
    чем перевод стал (ставка / возврат / оплата смены). Идемпотентно по
    unit_ref (=tx_hash): повторный проход watcher'а не плодит строк.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    # Cross-process в той же транзакции, что и строка дохода: два инстанса на
    # один перевод не дерутся по check-then-insert (см. _ledger_stuck_incoming).
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=round_id,
            player_id=player_id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=f"in:{result};src:…{transfer.source[-10:]}"[:200],
        )
    )
    await session.commit()


# Комментарий возвратов, пока игра на паузе: игрок видит причину прямо
# в проводнике блокчейна и в кошельке, без обращения к хранителю.
PAUSE_REFUND_COMMENT = "Игра приостановлена: идут технические работы"


async def _record_bank_credit(session, transfer: Transfer) -> str:
    """Пополнение казны владельцем (мемо bank:): доход без «банка дня».

    Не ставка (пот дня не растёт) и не возврат (деньги остаются в казне),
    пишется строкой входящего дохода — зеркало учитывает его в тождестве
    «в ноль». Идемпотентно по tx_hash: повторный проход (overlap-окно, сброс
    курсора) не плодит вторую строку.
    """
    existing = await session.execute(
        select(Income.id).where(Income.unit_ref == transfer.tx_hash).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return "bank_credit"
    # Cross-process в той же транзакции, что и строка дохода (см. _ledger_incoming).
    if not await claim_once(session, f"ledger:{transfer.tx_hash}"):
        return "bank_credit"
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=None,
            player_id=None,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note="in:bank;src:…" + transfer.source[-10:][:200],
        )
    )
    await session.commit()
    return "bank_credit"


async def process_transfer(transfer: Transfer, bot: Bot | None = None) -> str:
    """Сопоставляет перевод с игроком и открытым днём: ставка или оплата смены пути."""
    # Самоперевод казначея: если OWNER_WALLET_ADDRESS совпадает с адресом казны,
    # рейк и доли копилки уходят «казначею самому себе». Для watcher'а это
    # «входящий от неизвестного» — без этого фильтра каждый такой перевод
    # порождал бы бесконечный refund-цикл на себя же (сеть берёт газ за каждое
    # кольцо). Деньги при этом никуда не уходят — возвращать нечего.
    if settings.active_treasury_address and normalize_address(transfer.source) == normalize_address(
        settings.active_treasury_address
    ):
        return "self_transfer"
    async with SessionLocal() as session:
        if await session.get(WatcherState, f"refund:{transfer.tx_hash}") is not None:
            return "refund_duplicated"
        # Капитал казны: перевод владельца игры с мемо bank: — внешнее пополнение
        # казны. Не ставка (не создаёт «банка дня») и не возврат, идёт строкой
        # входящего дохода. Принимаем ТОЛЬКО с кошелька OWNER_WALLET_ADDRESS;
        # чужой отправитель с этим мемо обрабатывается штатно (возврат/ставка).
        if parse_bank_memo(transfer.comment) and settings.owner_wallet_address and normalize_address(
            transfer.source
        ) == normalize_address(settings.owner_wallet_address):
            return await _record_bank_credit(session, transfer)
        player_result = await session.execute(
            select(Player).where(Player.wallet_address == normalize_address(transfer.source))
        )
        player = player_result.scalar_one_or_none()
        player_id = player.id if player is not None else None
        if await is_game_paused(session):
            result = await _stash_refund(
                session,
                transfer,
                None,
                comment=PAUSE_REFUND_COMMENT,
                ledger_result="paused",
                ledger_player_id=player_id,
            )
            if player is not None and result == "refund_queued":
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                    f"{PAUSE_REFUND_COMMENT.lower()}.",
                )
            return f"paused_{result}"
        if player is None:
            return await _stash_refund(
                session, transfer, None, ledger_result="unknown"
            )
        # Подтверждение владения кошельком (защита от сквата чужих публичных
        # адресов): перевод с мемо bv:<код>. Код при привязке получил
        # только владелец телеграм-аккаунта, а перевести с адреса может только
        # владелец кошелька — совпадение «отправитель + код» доказывает контроль.
        verify_code = parse_verify_memo(transfer.comment)
        if verify_code is None and player.wallet_verify_code:
            # Частая ошибка игрока: копирует только код без префикса bv:.
            # Голый код — тот же секрет владельца, принимаем точное совпадение
            # (регистр, обычные пробелы и «невидимые» нулевые символы кошелька
            # значения не имеют). Неусечённое совпадение — не перебор по маске.
            bare = re.sub(r"[\s\u200b\u200c\u200d]+", "", (transfer.comment or "")).upper()
            if bare == player.wallet_verify_code.upper():
                verify_code = player.wallet_verify_code
        if verify_code:
            if (
                player.wallet_verify_code
                and verify_code == player.wallet_verify_code
                and player.wallet_address == normalize_address(transfer.source or "")
            ):
                player.wallet_verified = True
                player.wallet_verify_code = None
                player.wallet_verify_created = None
                await session.commit()
                result = await _stash_refund(
                    session,
                    transfer,
                    None,
                    ledger_result="walletverify:ok",
                    ledger_player_id=player.id,
                    force=True,
                )
                if result == "refund_queued":
                    await _dm_stake(
                        bot,
                        player.id,
                        f"✅ Кошелёк подтверждён. {from_nano(transfer.value_nanotons):g} Gram "
                        "проверочного перевода возвращаются на него целиком.",
                    )
                else:
                    await _dm_stake(
                        bot,
                        player.id,
                        "✅ Кошелёк подтверждён — теперь переводы с него засчитываются ставками.",
                    )
                # Приз/доли, удержанные на неподтверждённом кошельке (last_error
                # «кошелёк привязан, но не подтверждён»), разблокированы: кикаем
                # очередь выплат, чтобы игрок получил награду сразу, а не ждал
                # следующего закрытия дня.
                await _kick_dispatch_after_verify(bot)
                return f"walletverify_{result}"
            # bv: с неверным/чужим кодом или не с привязанного адреса — возвращаем
            # штатно, но объясняем игроку, почему кошелёк НЕ привязался: деньги
            # уже едут обратно, а не гадаеется в тишине (источник этого кейса —
            # обрезанный игроком код после двоеточия). force=True — возврат даже
            # проверочной «пыли» < refund_min_gram: это конкретный привязанный
            # человек, а не анонимный спам-бот.
            result = await _stash_refund(
                session, transfer, None, ledger_result="unknown", force=True
            )
            await _dm_verify_mismatch(bot, player, transfer)
            return result
        # Кошелёк привязан, но владение ещё не доказано (bv:<код> ждёт встречного
        # микро-перевода). Любой ДРУГОЙ перевод с адреса уже не может быть ни
        # ставкой, ни платой за смену пути: ветки ниже (rv:-мемо и авто-грант по
        # сумме из вилки [revote_ton, stake_min_ton)) молча съедали проверочный
        # микро-перевод с искажённым/обрезанным комментарием — деньги уходили
        # как грант смены пути, кошелёк не привязывался, а игрок не получал ни
        # возврата, ни удержанного приза. Возвращаем всё до доказательства
        # владения, со внятным объяснением и образцом верного memo.
        if player.wallet_verify_code and not player.wallet_verified:
            result = await _stash_refund(
                session,
                transfer,
                None,
                ledger_result="verify:pending",
                ledger_player_id=player.id,
                force=True,
            )
            await _dm_stake(
                bot,
                player.id,
                f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                "кошелёк привязан, но ещё не подтверждён. Докажи владение — отправь "
                "с него микро-перевод казначею с комментарием "
                f"<code>bv:{player.wallet_verify_code}</code> (код виден в /wallet); "
                "сумма вернётся целиком. Пока кошелёк не подтверждён, ставки и плата "
                "за смену пути с него не принимаются.",
            )
            return result
        revote_round_id = parse_revote_memo(transfer.comment)
        if revote_round_id is not None:
            status = await _process_revote(session, transfer, player, revote_round_id)
            if status in (
                "revote_closed",
                "revote_too_small",
                "revote_too_large",
                "revote_no_vote",
                "revote_money_off",
            ):
                await _stash_refund(
                    session,
                    transfer,
                    revote_round_id if status not in ("revote_no_vote",) else None,
                    ledger_result=f"revote:{status}",
                    ledger_player_id=player.id,
                )
                if status == "revote_no_vote":
                    await _dm_stake(
                        bot,
                        player.id,
                        f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                        "за перемотку кадра платить нечего — ты ещё не сделал выбор дня. "
                        "Первая запись бесплатная: жми свой вариант, без оплаты.",
                    )
                elif status == "revote_too_large":
                    await _dm_stake(
                        bot,
                        player.id,
                        f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                        "сумма с rv:-мемо превышает минимум ставки — это ставка, а не перемотка кадра. "
                        "Отправь без rv:-мемо, чтобы поставить.",
                    )
                elif status == "revote_money_off":
                    await _dm_stake(
                        bot,
                        player.id,
                        f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                        "сегодня бесплатный день — перемотка кадра ничего не стоит, деньги не сгорят.",
                    )
                else:
                    await _dm_stake(
                        bot,
                        player.id,
                        f"↩️ Оплата {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                        + ("день уже закрыт." if status == "revote_closed" else "сумма меньше нужной."),
                    )
            return status
        # Авто-грант по сумме: кошелёк не всегда доносит rv:-мемо. Сумма из
        # вилки [revote_ton, stake_min_ton) ставкой быть не может (минимальная
        # ставка выше), зато это ровная зона платы за смену пути. Если игрок
        # уже выбрал путь на открытом дне — выдаём грант автоматически.
        if to_nano(settings.revote_ton) <= transfer.value_nanotons < to_nano(settings.stake_min_ton):
            auto_status = await _maybe_auto_grant(session, transfer, player)
            if auto_status == "revote_ok":
                await _dm_stake(
                    bot,
                    player.id,
                    f"💎 Перемотка кадра оплачена ({from_nano(transfer.value_nanotons):g} Gram, "
                    "без мемо — зачтено по сумме). Нажми другой вариант — кадр перемотан.",
                )
                return "revote_ok"
            if auto_status == "no_vote":
                # День открыт, но пути ещё нет — менять нечего, а суммой это
                # и не ставка: возвращаем сразу с объяснением.
                await _stash_refund(
                    session,
                    transfer,
                    None,
                    ledger_result="revote_auto:no_vote",
                    ledger_player_id=player.id,
                )
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                    "он меньше минимума ставки, а за перемотку кадра платить нечего — "
                    "ты ещё не сделал выбор дня. Первая запись бесплатная: жми свой "
                    "вариант, без оплаты.",
                )
                return "revote_auto_no_vote"
            # revote_closed — открытого дня нет: поведение обращения как обычно
            # (закрытый день вернёт перевод штатно). duplicate_tx — грант уже
            # был выдан ранее, молча выходим.
            if auto_status == "duplicate_tx":
                return "revote_dup"
            if auto_status == "revote_money_off":
                # Бесплатный день: серверный гейт сработал, когда UI уже принял
                # сумму за смену пути — возвращаем с объяснением.
                await _stash_refund(
                    session,
                    transfer,
                    None,
                    ledger_result="revote_auto:money_off",
                    ledger_player_id=player.id,
                )
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                    "сегодня бесплатный день — перемотка кадра бесплатна, платить не нужно.",
                )
                return "revote_auto_money_off"
        round_result = await session.execute(
            select(Round)
            .where(Round.status == RoundStatus.OPEN)
            .order_by(Round.day_index.desc())
            .limit(1)
        )
        round_row = round_result.scalar_one_or_none()
        if round_row is None:
            return await _stash_refund(
                session, transfer, None, ledger_result="no_round"
            )
        result = await register_stake(
            session,
            round_row,
            player,
            transfer.value_nanotons,
            transfer.tx_hash,
            memo=transfer.comment,
        )
        amount = f"{from_nano(transfer.value_nanotons):g}"
        if result in ("already_staked", "closed", "money_off"):
            await _stash_refund(
                session,
                transfer,
                round_row.id,
                ledger_result=f"stake:{result}",
                ledger_player_id=player.id,
            )
            # Ставка — от stake_min_ton, а плата за смену пути (revote_ton) —
            # ниже минимума. Значит повторный «ставкоподобный» перевод за того,
            # кто уже поставил и чья сумма не дотягивает до минимума, — это
            # почти наверняка «недоехавший» revote: кошелёк не приложил rv:-мемо
            # или исказил его. Объясняем внятно, а не путанным «ставка уже есть».
            suspected_revote = (
                result == "already_staked"
                and transfer.value_nanotons < to_nano(settings.stake_min_ton)
            )
            if suspected_revote:
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Перевод {amount} Gram возвращается: эта сумма ниже минимума "
                    f"ставки ({settings.stake_min_ton:g} Gram). Если ты менял путь — "
                    "бот не распознал комментарий rv:… за переводом. Переведи снова "
                    "без мемо (сумма из вилки зачтётся автоматически) либо с `rv:день` "
                    "в комментарии. Или выбери Stars в /change — надёжнее.",
                )
            elif result == "money_off":
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Перевод {amount} Gram возвращается: сегодня бесплатный день — "
                    "ставки не принимаются, деньги не сгорят. Завтра день снова со ставками.",
                )
            else:
                reason = "ставка на этот день уже есть" if result == "already_staked" else "день уже закрылся"
                await _dm_stake(bot, player.id, f"↩️ Перевод {amount} Gram возвращается: {reason}.")
        elif result == "too_small":
            await _dm_stake(
                bot,
                player.id,
                f"↩️ Ставка {amount} Gram не принята (меньше минимума) — вернём после закрытия дня.",
            )
            await _ledger_incoming(
                session, transfer, player.id, round_row.id, f"stake:{result}"
            )
        elif result == "wallet_unverified":
            await _stash_refund(
                session,
                transfer,
                round_row.id,
                ledger_result=f"stake:{result}",
                ledger_player_id=player.id,
            )
            await _dm_stake(
                bot,
                player.id,
                f"↩️ Перевод {amount} Gram возвращается: этот кошелёк ещё не подтверждён. "
                "Сначала докажи владение — отправь с него микро-перевод казначею с мемо "
                "bv:… (код из ответа при привязке, дублируется в /wallet).",
            )
        elif result == "ok":
            age = datetime.now(UTC).timestamp() - transfer.utime
            if age >= settings.stake_confirm_seconds:
                if await confirm_stake(session, transfer.tx_hash):
                    await _dm_stake(
                        bot, player.id, f"✅ Ставка {amount} Gram на день {round_row.day_index} принята."
                    )
            await _ledger_incoming(
                session, transfer, player.id, round_row.id, f"stake:{result}"
            )
        elif result != "duplicate_tx":
            await _ledger_incoming(
                session, transfer, player.id, round_row.id, f"stake:{result}"
            )
    return result


async def confirm_aged_pending(bot: Bot | None = None) -> int:
    """Свежие переводы на момент обработки младше порога и остаются pending.

    Этот проход подтверждает их, когда возраст уже точно больше
    stake_confirm_seconds, и сообщает игроку. Закрытые дни не трогаем:
    их pending-ставки финализация вернёт как «залипшие».
    """
    confirmed = 0
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.stake_confirm_seconds)
    async with SessionLocal() as session:
        rows = (
            (await session.execute(
                select(Stake).where(
                    Stake.status == "pending",
                    Stake.network == current_network(),
                    Stake.created_at <= cutoff,
                )
            ))
            .scalars()
            .all()
        )
        for stake in rows:
            round_row = await session.get(Round, stake.round_id)
            if round_row is None or round_row.status != RoundStatus.OPEN:
                continue
            # Условный UPDATE-claim: ручной возврат ставит refunded в ЯВНОЙ
            # гонке со свипом; без WHERE pending свип перезаписал бы refunded
            # на confirmed — ставка засчитана И игрок ещё получает возврат.
            claimed = await session.execute(
                update(Stake)
                .where(Stake.id == stake.id, Stake.status == "pending")
                .values(status="confirmed", confirmed_at=now)
            )
            if claimed.rowcount != 1:
                continue  # уже не pending (возврат/дубль) — не трогаем
            confirmed += 1
            await _dm_stake(
                bot,
                stake.player_id,
                f"✅ Ставка {from_nano(stake.amount_nanotons):g} Gram на день {round_row.day_index} принята.",
            )
        if confirmed:
            await session.commit()
    return confirmed


async def _grant_revote(session, transfer: Transfer, player: Player, round_row: Round, note: str) -> str:
    """Общий путь выдачи гранта: идемпотентность + грант + учёт дохода.

    Возвращает ok / duplicate_tx. Проверки раунда, суммы и голоса — на совести
    вызывающего, грант создаётся здесь один раз.
    """
    duplicate = await session.execute(
        select(RevoteGrant.id).where(RevoteGrant.unit_ref == transfer.tx_hash)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "duplicate_tx"
    session.add(
        RevoteGrant(
            round_id=round_row.id,
            player_id=player.id,
            source="ton",
            unit_ref=transfer.tx_hash,
        )
    )
    # Ledger доходов: revote-перевод — выручка казны, её надо сверять.
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=round_row.id,
            player_id=player.id,
            network=current_network(),
            unit_ref=transfer.tx_hash,
            note=note,
        )
    )
    await session.commit()
    return "ok"


async def _process_revote(session, transfer: Transfer, player: Player, round_id: int) -> str:
    round_row = await session.get(Round, round_id)
    if round_row is None or round_row.status != RoundStatus.OPEN:
        return "revote_closed"
    if not round_row.money_mode:
        # Бесплатный день: смена пути бесплатна, платить за неё нельзя.
        return "revote_money_off"
    if transfer.value_nanotons < to_nano(settings.revote_ton):
        return "revote_too_small"
    # Симметрично автогранту по сумме ([revote_ton, stake_min_ton)): даже с
    # rv:-мемо «ставкоподобный» перевод (>= минимума ставки) не должен тихо
    # списываться как дешёвая смена пути, а фиксироваться как полноценная
    # ставка всего баланса. Иначе большой перевод с rv:-мемо превращался бы
    # в грант без соответствующей записи ставки.
    if transfer.value_nanotons >= to_nano(settings.stake_min_ton):
        return "revote_too_large"
    # Как и в автогранте без мемо: если пути ещё нет, менять нечего — грант
    # не выдаём, иначе игрок платил бы за бесполезный жетон.
    from app.voting import get_vote

    vote = await get_vote(session, round_row.id, player.id)
    if vote is None:
        return "revote_no_vote"
    return await _grant_revote(session, transfer, player, round_row, f"rv:{round_id}")


async def _maybe_auto_grant(session, transfer: Transfer, player: Player) -> str:
    """Фолбэк «недоехавшего» revote по сумме (когда кошелёк не приложил мемо).

    Плата за смену пути (revote_ton) ниже минимума ставки (stake_min_ton), а
    сам перевод в вилке [revote_ton, stake_min_ton) ставкой быть не может
    (мал). Если игрок уже выбрал путь на открытом дне — выдаём грант по сумме.
    Абсолютную равнозначность мемо не требуется: автогрант выдаётся один раз
    за перевод (unit_ref=tx_hash).

    Возвращает revote_ok / no_vote / revote_closed / duplicate_tx.
    """
    round_result = await session.execute(
        select(Round)
        .where(Round.status == RoundStatus.OPEN)
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    round_row = round_result.scalar_one_or_none()
    if round_row is None:
        return "revote_closed"
    if not round_row.money_mode:
        # Бесплатный день: грант за смену пути не выдаётся, перевод вернём.
        return "revote_money_off"
    # Грант нужен тем, кто уже выбрал путь (иначе смена выбора бесплатна —
    # платить за неё бессмысленно).
    from app.voting import get_vote

    vote = await get_vote(session, round_row.id, player.id)
    if vote is None:
        return "no_vote"
    status = await _grant_revote(session, transfer, player, round_row, "rv:auto")
    if status == "duplicate_tx":
        return "duplicate_tx"
    return "revote_ok"


async def _read_cursor(session) -> int:
    """Курсор CI/времени с окном перекрытия.

    Чистовой курсор хранит последний обработанный utime, но читается он на
    _CURSOR_OVERLAP_SECONDS раньше: у провайдеров входящий перевод публикуется
    не мгновенно, и транзакция ТОЙ ЖЕ секунды, что курсор, не должна остаться
    за бортом навсегда. Окно пересматривается каждый цикл — лишние повторы
    гасит идемпотентность по tx_hash.
    """
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is not None and row.value.isdigit():
        return max(0, int(row.value) - _CURSOR_OVERLAP_SECONDS)
    return int((datetime.now(UTC) - timedelta(hours=_CURSOR_FALLBACK_HOURS)).timestamp())


async def _read_cursor_raw(session) -> int:
    """Чистовое значение курсора (без окна перекрытия) или фолбэк.

    Нужно watch_once, чтобы курсор никогда не откатывался назад: окно
    перекрытия читается раньше, но записывать можно только значение не
    младше уже записанного.
    """
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return int((datetime.now(UTC) - timedelta(hours=_CURSOR_FALLBACK_HOURS)).timestamp())


async def _write_cursor(session, utime: int) -> None:
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is None:
        session.add(WatcherState(key=CURSOR_KEY, value=str(utime)))
    else:
        row.value = str(utime)
    await session.commit()


async def _write_beat(session) -> None:
    """Сердцебиение успешного цикла — для алертов и /health."""
    row = await session.get(WatcherState, BEAT_KEY)
    stamp = datetime.now(UTC).isoformat()
    if row is None:
        session.add(WatcherState(key=BEAT_KEY, value=stamp))
    else:
        row.value = stamp
    await session.commit()


async def _write_source(session, source: str) -> None:
    """Источник данных последнего успешного цикла (для /health)."""
    row = await session.get(WatcherState, SOURCE_KEY)
    if row is None:
        session.add(WatcherState(key=SOURCE_KEY, value=source))
    else:
        row.value = source
    await session.commit()


# Сколько циклов подряд транзакция может падать, пока курсор держится за неё
# (не уходит вперёд — иначе упавшая навсегда теряется за окном перекрытия).
# После исчерпания лимита курсор проходит мимо, но транзакция остаётся в
# stuck-списке (watcher_state, ключ STUCK_TX_KEY) для ручного разбора админом —
# не теряется молча, но и не тормозит весь входящий поток вечно.
_STUCK_MAX_FAILS = 5


def _load_stuck(raw: str | None) -> dict:
    """Разбор JSON stuck-списка из watcher_state (битый/пустой — пустой словарь)."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        logger.warning("Стuck-список ton_watch повреждён (%r) — начинаю заново", raw[:128])
    return {}


async def _read_stuck(session) -> dict:
    row = await session.get(WatcherState, STUCK_TX_KEY)
    return _load_stuck(row.value if row is not None else None)


async def _write_stuck(session, stuck: dict) -> None:
    # Ротация: запись сбойной транзакции живёт ограниченно (stuck_retention_days).
    # Без прунинга врачующиеся (reported) входы висели бы в watcher_state вечно,
    # отравляя /blockchain и ручной разбор. Свежие незарепортированные НЕ трогаем.
    cutoff = time.time() - settings.stuck_retention_days * 86400
    stuck = {
        hash_: rec
        for hash_, rec in stuck.items()
        if isinstance(rec, dict) and float(rec.get("utime") or 0) >= cutoff
    }
    row = await session.get(WatcherState, STUCK_TX_KEY)
    if row is None:
        session.add(WatcherState(key=STUCK_TX_KEY, value=json.dumps(stuck)))
    else:
        row.value = json.dumps(stuck)
    await session.commit()


# Состояние страницы провайдера: доверенная или нет.
_PAGE_OK = "ok"
_PAGE_DEGRADED = "degraded"

# Предупреждение о деградации TonAPI — не чаще раза в 10 минут, чтобы
# минутный цикл наблюдателя не заваливал лог одним и тем же сообщением.
_FALLBACK_WARN_EVERY_SECONDS = 600.0
_last_fallback_warning_at = 0.0


def _warn_degraded_primary() -> None:
    global _last_fallback_warning_at
    now = time.monotonic()
    if now - _last_fallback_warning_at < _FALLBACK_WARN_EVERY_SECONDS:
        return
    _last_fallback_warning_at = now
    logger.warning(
        "TonAPI деградировал (ошибка сети или 404 истории при живом казначее) — "
        "переводы читаются через фолбэк Toncenter v3"
    )


async def _tonapi_page(since_utime: int, before_lt: str | None) -> tuple[list[Transfer], str]:
    """Адаптер основного источника под единый контракт (список, состояние)."""
    transfers, ok = await fetch_recent_transfers(since_utime, before_lt=before_lt)
    return transfers, (_PAGE_OK if ok else _PAGE_DEGRADED)


async def _deep_collect(fetch_page, cursor_of, since: int) -> tuple[list[Transfer], bool]:
    """Глубокий проход по страницам одного провайдера.

    Возвращает (переводы, прошёл_ли_проход_полностью). Ненадёжная страница
    обрывает проход: частичный результат сохраняется, но вызывающий обязан
    не считать такой цикл успешным. Уходим вглубь до _MAX_PAGES × _PAGE_LIMIT
    переводов; пустое место (история короче страницы или две страницы подряд
    без новых переводов) завершает проход досрочно. Курсор хранится в БД,
    поэтому покрытие кумулятивно: после простоя накопившийся хвост
    догоняется за несколько минут.
    """
    transfers: list[Transfer] = []
    seen: set[str] = set()
    before: str | None = None
    empty_pages = 0
    for _page in range(_MAX_PAGES):
        page, state = await fetch_page(since, before)
        if state != _PAGE_OK:
            return transfers, False
        fresh = [t for t in page if t.tx_hash and t.tx_hash not in seen]
        for item in fresh:
            seen.add(item.tx_hash)
        transfers.extend(fresh)
        if not page or len(page) < _PAGE_LIMIT:
            return transfers, True  # история кончилась — глубже пусто
        oldest = page[-1]
        if oldest.utime <= since:
            return transfers, True  # страница дотянулась до курсора
        if not fresh:
            empty_pages += 1
            if empty_pages >= _EMPTY_STOP:
                return transfers, True  # подряд страницы без новых переводов
        else:
            empty_pages = 0
        before = cursor_of(page)
        await asyncio.sleep(0.12)  # бережём лимиты API на глубоком проходе
    return transfers, True


def _merge_unique(batches: list[list[Transfer]]) -> list[Transfer]:
    """Слияние результатов источников без дублей, по возрастанию utime."""
    seen: set[str] = set()
    merged: list[Transfer] = []
    for batch in batches:
        for transfer in batch:
            if transfer.tx_hash and transfer.tx_hash not in seen:
                seen.add(transfer.tx_hash)
                merged.append(transfer)
    return sorted(merged, key=lambda item: item.utime)


async def _collect_transfers(since: int) -> tuple[list[Transfer], bool, str]:
    """Все переводы после курсора: основной источник + фолбэк Toncenter.

    Основной проход TonAPI'ем; если он не завершился полностью (сеть легла
    или индекс отдаёт 404 истории при живом кошельке) — тот же проход
    повторяется по Toncenter v3, результаты сливаются без дублей. Источник
    успешного прохода возвращается третьим значением для /health.
    """
    primary, primary_complete = await _deep_collect(
        _tonapi_page, lambda page: page[-1].provider_ref or page[-1].tx_hash, since
    )
    if primary_complete:
        return primary, True, "tonapi"
    _warn_degraded_primary()
    fallback, fallback_complete = await _deep_collect(
        _toncenter_page, lambda page: page[-1].provider_ref or page[-1].tx_hash, since
    )
    merged = _merge_unique([primary, fallback])
    if fallback_complete:
        return merged, True, "toncenter"
    logger.error(
        "ОБА индексатора (%s) недоступны: переводы не читаются! "
        "Проверь TonAPI/Toncenter или перезапусти сервис.",
        "testnet" if settings.is_testnet else "mainnet",
    )
    return merged, False, "none"


async def _migrate_wallet_formats(session) -> None:
    """Разовый перевод старых привязок UQ/EQ… в канонический raw-hex.

    До нормализации watcher не находил отправителя: TonAPI отдаёт raw, а в БД
    лежала дружественная строка. Флаг в WatcherState делает миграцию идемпотентной.
    """
    row = await session.get(WatcherState, WALLET_NORM_KEY)
    if row is not None:
        return
    players = (
        await session.execute(select(Player).where(Player.wallet_address.is_not(None)))
    ).scalars().all()
    # Легаси-дубли UQ/EQ… одного кошелька нормализуются в один raw и нарушили бы
    # unique=wallet_address. Разбираем детерминированно: already — кто уже держит
    # канонический raw (таким не изменяем), pending — кандидаты на нормализацию;
    # один raw достаётся одному (каноническому держателю, либо меньшему id).
    already: dict[str, int] = {}
    pending: dict[str, list] = {}
    for player in players:
        normalized = normalize_address(player.wallet_address)
        if normalized == player.wallet_address:
            already[normalized] = player.id
        else:
            pending.setdefault(normalized, []).append((player, normalized))
    changed = 0
    skipped = 0
    for raw, candidates in pending.items():
        if raw in already:
            # Канонический raw уже занят другим игроком — всех кандидатов пропускаем.
            for player, _norm in candidates:
                skipped += 1
                logger.warning(
                    "Кошелёк %s игрока %s дублирует raw игрока %s — не нормализую",
                    player.wallet_address, player.id, already[raw],
                )
            continue
        owner = min(candidates, key=lambda c: c[0].id)
        for player, normalized in candidates:
            if player is not owner[0]:
                skipped += 1
                logger.warning(
                    "Кошелёк %s игрока %s дублирует raw игрока %s — не нормализую",
                    player.wallet_address, player.id, owner[0].id,
                )
                continue
            player.wallet_address = normalized
            changed += 1
    session.add(WatcherState(key=WALLET_NORM_KEY, value="1"))
    await session.commit()
    if changed or skipped:
        logger.info("Нормализовано адресов кошельков: %d, пропущено дублей: %d", changed, skipped)


# Брошенные транзакции (курсор прошёл мимо после _STUCK_MAX_FAILS) лечатся не
# только вручную: снимок перевода в stuck-записи позволяет переобработать его
# заново — деплой мог починить баг версии, а сеть — ожить.
_STUCK_HEAL_MAX_REFUND_FAILS = 3


async def _heal_stuck_transfers(bot: Bot | None = None) -> int:
    """Авто-лечение брошенных сбойных переводов (reported, cursor за ними).

    Каждые stuck_heal_recheck_seconds по каждой записанной с ошибкой
    транзакции (снимок в stuck-записи) заново запускается process_transfer:
    обработалась правильно — уходит из списка, снова упала — остаётся для
    следующего цикла лечения. Классификация идемпотентна (claim-маркеры
    refund:/ledger:, unique tx участника), повтор не задваивает выплату.

    Если после _STUCK_HEAL_MAX_REFUND_FAILS циклов лечение не возобновляется,
    а перевод так и не разобран — деньги возвращаются отправителю авто-возвратом
    (stash_refund с принудительным возвратом даже «пыли»: брошенная сумма не
    должна зависать в казне до ручного разбора, как было в инциденте Kote).

    Возвращает число исцелённых записей (для лога). Молча пропускает старые
    записи без снимка (до миграции формата) — они остаются на ручной разбор.
    """
    healed = 0
    now = time.time()
    async with SessionLocal() as session:
        stuck = await _read_stuck(session)
        touched = False
        for tx_hash, record in list(stuck.items()):
            if not isinstance(record, dict) or not record.get("reported"):
                continue
            if not record.get("source"):
                continue  # старый формат без снимка — только ручной разбор
            if now - float(record.get("heal_at") or 0) < settings.stuck_heal_recheck_seconds:
                continue
            record["heal_at"] = now
            touched = True
            transfer = Transfer(
                tx_hash=tx_hash,
                source=str(record["source"]),
                value_nanotons=int(record["value_nanotons"]),
                comment=str(record.get("comment") or ""),
                utime=int(record["utime"]),
            )
            try:
                status = await process_transfer(transfer, bot=bot)
                logger.info(
                    "Stuck-транзакция %s исцелена повторной обработкой: %s",
                    tx_hash[:16], status,
                )
                del stuck[tx_hash]
                healed += 1
            except Exception as exc:
                record["heal_fails"] = int(record.get("heal_fails", 0)) + 1
                logger.warning(
                    "Stuck-транзакция %s всё ещё не обрабатывается (попытка %d): %s",
                    tx_hash[:16], record["heal_fails"], exc,
                )
                if record["heal_fails"] >= _STUCK_HEAL_MAX_REFUND_FAILS:
                    try:
                        refund = await _stash_refund(
                            session,
                            transfer,
                            None,
                            ledger_result="stuck:abandoned",
                            ledger_player_id=None,
                            force=True,
                        )
                        logger.info(
                            "Stuck-транзакция %s: авто-возврат отправителю (%s)",
                            tx_hash[:16], refund,
                        )
                        del stuck[tx_hash]
                        healed += 1
                    except Exception as exc2:
                        logger.error(
                            "Stuck-транзакция %s: авто-возврат не удался: %s",
                            tx_hash[:16], exc2, exc_info=True,
                        )
        if healed or touched:
            await _write_stuck(session, stuck)
    return healed


async def watch_once(bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        since = await _read_cursor(session)
        raw_cursor = await _read_cursor_raw(session)
        stuck = await _read_stuck(session)
    transfers, api_ok, source = await _collect_transfers(since)
    processed_through = since
    # Сбойные транзакции попадают в stuck-список (watcher_state): они не должны
    # остаться за окном перекрытия навсегда (см. генерацию курсора ниже).
    for i, transfer in enumerate(transfers):
        try:
            status = await process_transfer(transfer, bot=bot)
            logger.info(
                "Перевод %s от %s: %s (%.4f Gram, utime %d)",
                transfer.tx_hash[:16],
                transfer.source[-10:] if transfer.source else "???",
                status,
                transfer.value_nanotons / 1e9,
                transfer.utime,
            )
        except Exception as exc:
            # Сбойная транзакция НЕ двигает курсор за себя: обрабатываем остаток
            # пачки (skip без потери), но курсор останавливается перед ней, и в
            # следующем цикле окно перечитает её заново. Так временный сбой
            # (сеть, провайдер, баг версии) не стирает деньги молча.
            logger.warning("Перевод %s не обработан: %s (продолжаем остаток пачки)", transfer.tx_hash[:16], exc)
            entry = stuck.get(transfer.tx_hash)
            if entry is None:
                entry = {
                    "utime": transfer.utime,
                    "fails": 1,
                    # Снимок перевода: после исчерпания лимита курсор проходит
                    # мимо, и авто-лечение больше не может перечитать переводы
                    # из API (окно ушло вперёд). Храним достаточно данных,
                    # чтобы достроить Transfer и переобработать/вернуть деньги
                    # даже за прошедшим окном. Без снимка старый формат записи
                    # лечится только вручную.
                    "source": transfer.source,
                    "value_nanotons": transfer.value_nanotons,
                    "comment": transfer.comment,
                }
                stuck[transfer.tx_hash] = entry
            else:
                entry["fails"] += 1
            continue
        # Успешно обработанная транзакция выходит из stuck-списка: повторный
        # сбой той же пачки/цикла не должен вечно топить её в ручном разборе.
        if transfer.tx_hash in stuck:
            del stuck[transfer.tx_hash]
        processed_through = max(processed_through, transfer.utime)
        if i % 50 == 49:
            await asyncio.sleep(0.05)
    try:
        await confirm_aged_pending(bot)
    except Exception:
        logger.exception("Подтверждение отложенных ставок упало (не мешает циклу)")
    # Свежие подтверждения выростили банк дня, а в постах-статусах он застыл:
    # правим их актуальным банком, чтобы игроки не гоняли /today за цифрой.
    # Необязательный слой: сбой правки не должен рвать цикл watcher'а.
    try:
        from app.broadcast import refresh_day_bank

        await refresh_day_bank(bot)
    except Exception:
        logger.exception("Актуализация банка дня в постах упала (не мешает циклу)")
    # Курсор двигаем ТОЛЬКО по полному проходу (api_ok): частичная пачка при
    # деградации обоих провайдеров содержит дыры по utime, и быстрый перевод
    # курсора вперёд потерял бы те транзакции, до которых проход не дошёл.
    # Следующий цикл начнётся с той же позиции и догонит пропущенное.
    if api_ok and processed_through > raw_cursor:
        # Стuck-защита: курсор не уходит дальше самой свежей ТАК И НЕ обработанной
        # транзакции — иначе упавшая навсегда теряется за окном перекрытия.
        # max(raw_cursor, floor) хранит монотонность: сбойная в окне перекрытия
        # (ниже raw_cursor) не откатывает курсор, а просто не двигает его, и окно
        # следующего цикла перечитает её заново (skip без потери).
        fresh_floor = min(
            (r["utime"] for r in stuck.values() if r.get("fails", 0) <= _STUCK_MAX_FAILS),
            default=None,
        )
        if fresh_floor is not None:
            processed_through = min(processed_through, max(raw_cursor, fresh_floor))
        expired = [
            r for r in stuck.values()
            if r.get("fails", 0) > _STUCK_MAX_FAILS and not r.get("reported")
        ]
        if expired:
            logger.error(
                "%d транзакций не обработаны за %d циклов и курсор прошёл мимо: "
                "смотри watcher_state[%s] (нужно ручное вмешательство)",
                len(expired), _STUCK_MAX_FAILS, STUCK_TX_KEY,
            )
            for record in expired:
                record["reported"] = True
        async with SessionLocal() as session:
            await _write_cursor(session, processed_through)
    if api_ok:
        async with SessionLocal() as session:
            # stuck-список фиксируем каждым полным проходом, когда в нём что-то
            # есть ИЛИ когда его нужно очистить после успешных повторов: прежний
            # сбой уже записан в БД, молчание сейчас оставило бы устаревшую
            # запись висеть в ручном разборе. Пустой dict=пустой список.
            if stuck or (await session.get(WatcherState, STUCK_TX_KEY)) is not None:
                await _write_stuck(session, stuck)
    if api_ok:
        async with SessionLocal() as session:
            # Сердцебиение ставится каждым успешным циклом — даже без
            # переводов: тишина в цепочке это здоровье, а не простой.
            await _write_beat(session)
            await _write_source(session, source)
    try:
        # Авто-лечение брошенных сбойных переводов: не даём деньгам зависать
        # в казне до ручного разбора (инцидент Kote). Идемпотентно и не мешает
        # циклу, если лечение временно падает.
        healed = await _heal_stuck_transfers(bot)
        if healed:
            logger.info("Авто-лечение stuck-транзакций: исцелено %d", healed)
    except Exception:
        logger.exception("Авто-лечение stuck-транзакций упало (не мешает циклу)")
    if transfers:
        logger.info(
            "Цикл watcher: найдено %d переводов, курсор %d → %d, проход %s (источник %s), stuck %d",
            len(transfers), since, processed_through,
            "полный" if api_ok else "ЧАСТИЧНЫЙ (курсор не сдвинут)",
            source,
            len(stuck),
        )
