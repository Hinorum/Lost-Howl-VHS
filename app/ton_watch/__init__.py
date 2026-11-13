"""Наблюдатель входящих переводов казначейского кошелька.

Раз в минуту забирает свежие транзакции казначея через TonAPI v2, сопоставляет
отправителя с привязанным кошельком игрока и регистрирует ставку на открытый
день. Переводы, которые не могут стать ставкой (неопознанный отправитель,
повтор за уже поставившего, закрытый день, слишком мелкая оплата смены пути),
автоматически попадают в очередь возвратов — деньги не оседают в казнее молча.

Пакет: источники и разбор в sources.py, журнал в ledger.py, курсор и
stuck-список в state.py, возвраты в refunds.py, уведомления в notify.py,
revote в revote.py. Оркестрация (process_transfer, watch_once) остаётся здесь,
потому что тесты подменяют её внутренние вызовы через app.ton_watch."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ton_head as _head
from app.config import settings
from app.core.processed import KIND_REFUND, KIND_STAKE, transfer_claim_kind
from app.core.registry import BEAT_KEY as BEAT_KEY
from app.core.registry import CURSOR_KEY as CURSOR_KEY
from app.core.registry import SCAN_BOOST_KEY as SCAN_BOOST_KEY
from app.core.registry import SCAN_GAP_KEY as SCAN_GAP_KEY
from app.core.registry import SOURCE_KEY as SOURCE_KEY
from app.core.registry import STUCK_TX_KEY, WALLET_NORM_KEY
from app.db import SessionLocal
from app.http_utils import (
    get_http_client as get_http_client,
)
from app.models import Player, Round, RoundStatus, WatcherState
from app.ops import claim_once as claim_once
from app.ops import is_game_paused
from app.payments import parse_bank_memo, parse_revote_memo, parse_verify_memo
from app.stakes import confirm_stake, current_network, register_stake
from app.ton_codec import (
    api_headers,
    clean_comment,
    extract_comment,
    norm_tx_hash,
    tonapi_headers,
)
from app.ton_utils import from_nano, normalize_address, to_nano

# Алиасы ton_codec живут в корне пакета: ими пользуются sources.py, а тесты
# подменяют их через app.ton_watch. Приватное имя с подчёркиванием — историческое.
_api_headers = api_headers  # Toncenter
_tonapi_headers = tonapi_headers  # TonAPI (Bearer)
_api_headers_tonapi = tonapi_headers  # устаревший алиас
_norm_tx_hash = norm_tx_hash
_clean_comment = clean_comment
_decode_comment = extract_comment

logger = logging.getLogger(__name__)

# Подмодули пакета доступны как app.ton_watch.<имя> -- тем же способом, чем
# раньше было app.ton_pay.
from app.ton_watch import ledger as ledger
from app.ton_watch import notify as notify
from app.ton_watch import refunds as refunds
from app.ton_watch import revote as revote
from app.ton_watch import sources as sources
from app.ton_watch import state as state
from app.ton_watch.ledger import (  # noqa: F401
    PAUSE_REFUND_COMMENT,
    _ledger_incoming,
    _ledger_stuck_incoming,
    _record_bank_credit,
)
from app.ton_watch.notify import (
    _dm_refund_queued,
    _dm_stake,
    _dm_verify_mismatch,
    _kick_dispatch_after_verify,
)
from app.ton_watch.refunds import (
    _stash_refund,
)
from app.ton_watch.revote import (  # noqa: F401
    _grant_revote,
    _maybe_auto_grant,
    _process_revote,
    confirm_aged_pending,
)

# Ре-экспорт содержимого подмодулей. Переезд из плоского ton_watch.py в пакет
# не должен ломать ни код, ни тесты: имена, которые раньше были просто
# именами модуля, остаются именами app.ton_watch. Оркестрация (process_
# transfer, watch_once) живёт в этом файле, поэтому её глобалы -- это
# namespace пакета, и monkeypatch.setattr(ton_watch, ...) доходит до неё.
from app.ton_watch.sources import (  # noqa: F401
    _EMPTY_STOP,
    _FALLBACK_WARN_EVERY_SECONDS,
    _JETTON_OPCODES,
    _MAX_PAGES,
    _PAGE_DEGRADED,
    _PAGE_LIMIT,
    _PAGE_OK,
    _TONCENTER_MAX_LIMIT,
    Page,
    PassResult,
    Transfer,
    _collect_transfers,
    _deep_collect,
    _is_jetton_notification,
    _merge_unique,
    _parse_toncenter_item,
    _parse_tx_item,
    _ParseFailure,
    _resolve_tonapi_empty_history,
    _tonapi_account_info,
    _tonapi_page,
    _toncenter_page,
    _warn_degraded_primary,
    fetch_recent_transfers_page,
)
from app.ton_watch.state import (  # noqa: F401
    _CURSOR_FALLBACK_HOURS,
    _CURSOR_OVERLAP_SECONDS,
    _MAX_SCAN_BOOST,
    _STUCK_MAX_FAILS,
    _bump_parse_fail,
    _bump_scan_boost,
    _load_stuck,
    _read_cursor,
    _read_cursor_raw,
    _read_head,
    _read_head_check,
    _read_scan_boost,
    _read_scan_gap,
    _read_stuck,
    _reset_parse_fail,
    _reset_scan_state,
    _write_beat,
    _write_cursor,
    _write_head,
    _write_scan_gap,
    _write_source,
    _write_stuck,
)


def _is_treasury_self_transfer(transfer: Transfer) -> bool:
    """Самоперевод казначея: если OWNER_WALLET_ADDRESS совпадает с адресом казны,
    рейк и доли копилки уходят «казначею самому себе». Для watcher'а это
    «входящий от неизвестного» — без этого фильтра каждый такой перевод
    порождал бы бесконечный refund-цикл на себя же (сеть берёт газ за каждое
    кольцо). Деньги при этом никуда не уходят — возвращать нечего.
    """
    return bool(
        settings.active_treasury_address
        and normalize_address(transfer.source) == normalize_address(settings.active_treasury_address)
    )


async def _is_duplicate_refund(session, transfer: Transfer) -> bool:
    """Возврат по уже обработанному переводу: повторный проход watcher'а не должен
    плодить второй возврат (ledger уже занял unit_ref)."""
    if await session.get(WatcherState, f"refund:{transfer.tx_hash}") is not None:
        return True
    return (
        await transfer_claim_kind(session, transfer.tx_hash, current_network())
        == KIND_REFUND
    )


async def _maybe_record_bank_credit(session, transfer: Transfer) -> str | None:
    """Попадание казны на баланс владельца по мемо bank: — строка дохода.

    Возвращает результат _record_bank_credit или None, если перевод не
    выглядел пополнением казны.
    """
    if (
        parse_bank_memo(transfer.comment)
        and settings.owner_wallet_address
        and normalize_address(transfer.source) == normalize_address(settings.owner_wallet_address)
    ):
        return await _record_bank_credit(session, transfer)
    return None


async def _stash_paused_refund(
    session, transfer: Transfer, bot: Bot | None, player: Player | None
) -> str:
    """Возврат при паузе игры: деньги игрока возвращаем с объяснением, что
    идут техработы (comment=PAUSE_REFUND_COMMENT)."""
    result = await _stash_refund(
        session,
        transfer,
        None,
        comment=PAUSE_REFUND_COMMENT,
        ledger_result="paused",
        ledger_player_id=player.id if player is not None else None,
    )
    if player is not None and result == "refund_queued":
        await _dm_stake(
            bot,
            player.id,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            f"{PAUSE_REFUND_COMMENT.lower()}.",
        )
    return f"paused_{result}"


async def _maybe_verify_wallet(
    session, transfer: Transfer, bot: Bot | None, player: Player
) -> str | None:
    """Проверка владения кошельком через мемо bv:<код>.

    Возвращает статус обработки (в т.ч. результат возврата, если код не
    прошёл), либо None, если верификация не требуется — переход к ставке.
    """
    verify_code = parse_verify_memo(transfer.comment)
    if verify_code is None and player.wallet_verify_code:
        bare = re.sub(r"[\s\u200b\u200c\u200d]+", "", (transfer.comment or "")).upper()
        if bare == player.wallet_verify_code.upper():
            verify_code = player.wallet_verify_code
    if player.wallet_verify_code is None:
        # Кошелёк уже подтверждён (код очищен) — проверять нечего.
        return None
    if (
        verify_code is not None
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
    # уже едут обратно, а не гадаеся в тишине (источник этого кейса —
    # обрезанный игроком код после двоеточия). force=True — возврат даже
    # проверочной «пыли» < refund_min_gram: это конкретный привязанный
    # человек, а не анонимный спам-бот.
    result = await _stash_refund(
        session, transfer, None, ledger_result="unknown", force=True
    )
    if not (transfer.comment or "").strip():
        # Игрок просто забыл комментарий — частый кейс новичка. Текст
        # прямо называет причину, а не ограничивается «не подтверждён».
        await _dm_refund_queued(
            bot,
            player.id,
            result,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "в переводе не было комментария.\n\n"
            "Кошелёк привязан, но ещё не подтверждён. Отправь с него казначеем "
            "микро-перевод и <b>обязательно укажи комментарий</b> "
            f"<b><code>bv:{player.wallet_verify_code}</code></b> — "
            "сумма вернётся целиком.",
        )
    elif result == "refund_queued":
        await _dm_verify_mismatch(bot, player, transfer)
    return result


_REVOTE_REFUND_STATUSES = frozenset(
    {
        "revote_closed",
        "revote_too_small",
        "revote_too_large",
        "revote_no_vote",
        "revote_money_off",
        "revote_disabled",
    }
)


async def _handle_revote(
    session, transfer: Transfer, bot: Bot | None, player: Player, revote_round_id: int
) -> str:
    status = await _process_revote(session, transfer, player, revote_round_id)
    if status == "ok":
        # Оплата принята — подтверждаем в личку. Инструкция обещает
        # «грант придёт в течение минуты», и без этого сообщения игрок
        # ждёт уведомления, которого не будет, и платит повторно:
        # второй перевод с другим хешем дедупликацию проходит и
        # приносит ещё один грант — деньги списываются дважды.
        await _dm_stake(
            bot,
            player.id,
            f"💎 Перемотка кадра оплачена ({from_nano(transfer.value_nanotons):g} Gram). "
            "Нажми другой вариант — кадр перемотается.",
        )
        return status
    if status not in _REVOTE_REFUND_STATUSES:
        return status
    refund_round_id = (
        revote_round_id if status not in ("revote_no_vote", "revote_disabled") else None
    )
    refund_status = await _stash_refund(
        session,
        transfer,
        refund_round_id,
        ledger_result=f"revote:{status}",
        ledger_player_id=player.id,
    )
    if status == "revote_no_vote":
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "за перемотку кадра платить нечего — ты ещё не сделал выбор дня. "
            "Первая запись бесплатная: жми свой вариант, без оплаты.",
        )
    elif status == "revote_too_large":
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "сумма с rv:-мемо превышает минимум ставки — это ставка, а не перемотка кадра. "
            "Отправь без rv:-мемо, чтобы поставить.",
        )
    elif status == "revote_money_off":
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "сегодня версия без ставок: смена выбора закрыта — платить за неё нечем.",
        )
    elif status == "revote_disabled":
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "смена выбора сейчас отключена — плата за неё не взимается.",
        )
    else:
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Оплата {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            + ("день уже закрыт." if status == "revote_closed" else "сумма меньше нужной."),
        )
    return status


async def _handle_auto_grant(
    session, transfer: Transfer, bot: Bot | None, player: Player
) -> str | None:
    """Авто-грант по сумме: кошелёк не всегда доносит rv:-мемо. Сумма из
    вилки [revote_ton, stake_min_ton) ставкой быть не может (минимальная
    ставка выше), зато это ровная зона платы за смену пути. Если игрок
    уже выбрал путь на открытном дне — выдаём грант автоматически.

    Возвращает статус обработки для возвратов/DM или None, если игрок
    не выбирал путь (auto_status == "revote_closed") — обработка продолжается
    штатным путём ставки.
    """
    auto_status = await _maybe_auto_grant(session, transfer, player)
    if auto_status == "revote_ok":
        await _dm_stake(
            bot,
            player.id,
            f"💎 Перемотка кадра оплачена ({from_nano(transfer.value_nanotons):g} Gram, "
            "без мемо — зачтено по сумме). Нажми другой вариант — кадр перемотается.",
        )
        return "revote_ok"
    if auto_status == "no_vote":
        # День открыт, но пути ещё нет — менять нечего, а суммой это
        # и не ставка: возвращаем сразу с объяснением. Уведомление —
        # только если возврат действительно встал в очередь: повторный
        # проход watcher'а по тому же переводу не должен повторять DM.
        refund_status = await _stash_refund(
            session,
            transfer,
            None,
            ledger_result="revote_auto:no_vote",
            ledger_player_id=player.id,
        )
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "он меньше минимума ставки, а за перемотку кадра платить нечего — "
            "ты ещё не сделал выбор дня. Первая запись бесплатная: жми свой "
            "вариант, без оплаты.",
        )
        return "revote_auto_no_vote"
    if auto_status == "duplicate_tx":
        # Грант уже выдан ранее, молча выходим.
        return "revote_dup"
    if auto_status == "revote_money_off":
        # Бесплатный день: серверный гейт сработал, когда UI уже принял
        # сумму за смену пути — возвращаем с объяснением.
        refund_status = await _stash_refund(
            session,
            transfer,
            None,
            ledger_result="revote_auto:money_off",
            ledger_player_id=player.id,
        )
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "сегодня версия без ставок: смена выбора закрыта — платить за неё нечем.",
        )
        return "revote_auto_money_off"
    if auto_status == "revote_disabled":
        # Смена выбора выключена флагом: сумма из вилки не ставка,
        # поэтому сразу возвращаем её, а не оставляем в казне.
        refund_status = await _stash_refund(
            session,
            transfer,
            None,
            ledger_result="revote_auto:disabled",
            ledger_player_id=player.id,
        )
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {from_nano(transfer.value_nanotons):g} Gram возвращается: "
            "смена выбора сейчас отключена — плата за неё не взимается.",
        )
        return "revote_auto_disabled"
    # auto_status == "revote_closed" — открытого дня нет: обработка
    # продолжается штатным путём (закрытый день вернёт перевод штатно).
    return None


async def _finalize_stake_refund(
    session, transfer, round_row, player, bot, result: str, amount: str
) -> None:
    """Возврат ставки: payout-строка в очередь + персональное уведомление.

    already_staked / closed / money_off: ставка не была принята (день уже
    закрылся, ставка уже стоит или день живёт без ставок) — перевод возвращаем.
    """
    refund_status = await _stash_refund(
        session,
        transfer,
        round_row.id,
        ledger_result=f"stake:{result}",
        ledger_player_id=player.id,
    )
    suspected_revote = (
        result == "already_staked"
        and transfer.value_nanotons < to_nano(settings.stake_min_ton)
    )
    if suspected_revote:
        # Ставка — от stake_min_ton, а плата за смену пути (revote_ton) —
        # ниже минимума. Значит повторный «ставкоподобный» перевод за того,
        # кто уже поставил и чья сумма не дотягивает до минимума, — это
        # почти наверняка «недоехавший» revote: кошелёк не приложил rv:-мемо
        # или исказил его. Объясняем внятно, а не путанным «ставка уже есть».
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {amount} Gram возвращается: эта сумма ниже минимума "
            f"ставки ({settings.stake_min_ton:g} Gram). Если ты менял путь — "
            "бот не распознал комментарий rv:… за переводом. Переведи снова "
            "без мемо (сумма из вилки зачтётся автоматически) либо с "
            "<code>rv:день</code> в комментарии.",
        )
    elif result == "money_off":
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {amount} Gram возвращается: сегодня версия без ставок — "
            "ставки не принимаются. Поставить можно в день со ставками.",
        )
    else:
        reason = "ставка на этот день уже есть" if result == "already_staked" else "день уже закрылся"
        await _dm_refund_queued(
            bot,
            player.id,
            refund_status,
            f"↩️ Перевод {amount} Gram возвращается: {reason}.",
        )


async def _finalize_stake_rejected(bot, player, result: str, amount: str) -> None:
    """Ставка отклонена (too_small / too_big): персональное сообщение + возврат.

    too_small — ниже stake_min_ton; too_big — выше stake_max_ton (потолок).
    """
    if result == "too_small":
        await _dm_stake(
            bot,
            player.id,
            f"↩️ Ставка {amount} Gram не принята (меньше минимума) — вернём после закрытия дня.",
        )
    else:
        await _dm_stake(
            bot,
            player.id,
            f"↩️ Ставка {amount} Gram не принята (выше потолка "
            f"{settings.stake_max_ton:g} Gram) — вернём после закрытия дня. "
            "Поставить можно сумму в пределах потолка.",
        )


async def _finalize_stake_unverified(
    session, transfer, round_row, player, bot, result: str, amount: str
) -> None:
    """Кошелёк игрока не подтверждён: возврат + инструкция по доказыванию."""
    refund_status = await _stash_refund(
        session,
        transfer,
        round_row.id,
        ledger_result=f"stake:{result}",
        ledger_player_id=player.id,
    )
    await _dm_refund_queued(
        bot,
        player.id,
        refund_status,
        f"↩️ Перевод {amount} Gram возвращается: кошелёк ещё не подтверждён — "
        "ставки с него не принимаются.\n\n"
        "Отправь с него микро-перевод казначею и <b>обязательно укажи "
        f"комментарий</b> <b><code>bv:{player.wallet_verify_code}</code></b> "
        "(код виден в /wallet), затем повтори ставку.",
    )


async def _finalize_stake_ok(
    session, transfer, bot, player, round_row, result: str, amount: str
) -> None:
    """Принята ставка: ожидание подтверждения перевода, уведомление, ledger."""
    age = datetime.now(UTC).timestamp() - transfer.utime
    if age >= settings.stake_confirm_seconds:
        if await confirm_stake(session, transfer.tx_hash):
            await _dm_stake(
                bot,
                player.id,
                f"✅ Ставка {amount} Gram на день {round_row.day_index} принята.",
            )
    await _ledger_incoming(
        session, transfer, player.id, round_row.id, f"stake:{result}"
    )


async def _finalize_stake(session, transfer: Transfer, bot: Bot | None, player: Player, round_row: Round, result: str) -> str:
    """Доводит ставку до уведомления/возврата и запись в ledger.

    result — статус register_stake: ok / already_staked / closed / money_off /
    too_small / too_big / wallet_unverified / duplicate_tx.
    """
    amount = f"{from_nano(transfer.value_nanotons):g}"
    if result in ("already_staked", "closed", "money_off"):
        await _finalize_stake_refund(session, transfer, round_row, player, bot, result, amount)
    elif result == "too_small":
        await _finalize_stake_rejected(bot, player, result, amount)
        await _ledger_incoming(session, transfer, player.id, round_row.id, f"stake:{result}")
    elif result == "too_big":
        await _finalize_stake_rejected(bot, player, result, amount)
        await _ledger_incoming(session, transfer, player.id, round_row.id, f"stake:{result}")
    elif result == "wallet_unverified":
        await _finalize_stake_unverified(session, transfer, round_row, player, bot, result, amount)
    elif result == "ok":
        await _finalize_stake_ok(session, transfer, bot, player, round_row, result, amount)
    elif result == "duplicate_tx":
        # Перевод уже разобран. Если это была ставка, строка дохода
        # обязана быть в журнале: без неё перевод невидим для сверки
        # казны — расхождение висит молча (тревога срабатывает только на
        # нехватку, а здесь излишек). Повторный проход чинит журнал
        # (идемпотентно по unit_ref=tx_hash), а не плодит деньги.
        if (
            await transfer_claim_kind(session, transfer.tx_hash, current_network())
            == KIND_STAKE
        ):
            await _ledger_incoming(
                session, transfer, player.id, round_row.id, "stake:repair"
            )
    else:
        await _ledger_incoming(
            session, transfer, player.id, round_row.id, f"stake:{result}"
        )
    return result


async def _process_player_transfer(
    session, transfer: Transfer, bot: Bot | None, player: Player
) -> str:
    """Вся обработка игрока: верификация кошелька, рейт или авто-грант, ставка."""
    verified = await _maybe_verify_wallet(session, transfer, bot, player)
    if verified is not None:
        return verified
    revote_round_id = parse_revote_memo(transfer.comment)
    if revote_round_id is not None:
        return await _handle_revote(session, transfer, bot, player, revote_round_id)
    if to_nano(settings.revote_ton) <= transfer.value_nanotons < to_nano(settings.stake_min_ton):
        auto_status = await _handle_auto_grant(session, transfer, bot, player)
        if auto_status is not None:
            return auto_status
    round_result = await session.execute(
        select(Round)
        .where(Round.status == RoundStatus.OPEN)
        .order_by(Round.day_index.desc())
        .limit(1)
    )
    round_row = round_result.scalar_one_or_none()
    if round_row is None:
        return await _stash_refund(session, transfer, None, ledger_result="no_round")
    result = await register_stake(
        session,
        round_row,
        player,
        transfer.value_nanotons,
        transfer.tx_hash,
        memo=transfer.comment,
    )
    return await _finalize_stake(session, transfer, bot, player, round_row, result)


async def process_transfer(transfer: Transfer, bot: Bot | None = None) -> str:
    """Сопоставляет перевод с игроком и открытым днём: ставка или оплата смены пути."""
    if _is_treasury_self_transfer(transfer):
        return "self_transfer"
    async with SessionLocal() as session:
        if await _is_duplicate_refund(session, transfer):
            return "refund_duplicated"
        bank_result = await _maybe_record_bank_credit(session, transfer)
        if bank_result is not None:
            return bank_result
        player_result = await session.execute(
            select(Player).where(Player.wallet_address == normalize_address(transfer.source))
        )
        player = player_result.scalar_one_or_none()
        if await is_game_paused(session):
            return await _stash_paused_refund(session, transfer, bot, player)
        if player is None:
            return await _stash_refund(session, transfer, None, ledger_result="unknown")
        return await _process_player_transfer(session, transfer, bot, player)


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

# Сколько раз подряд лечение застрявшего перевода имеет право не помочь, прежде
# чем это перестанет быть «зависшим» и станет поводом разбираться вручную.
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

async def _watch_state_read(
    session: AsyncSession,
) -> tuple[int, int, dict, int, dict | None, str | None, dict | None]:
    """Состояние цикла watcher'а: курсор, stuck, буст, голова и дыра.

    Голова и её метка времени нужны дешёвому зонду: по ним решается, можно ли
    пропустить полный проход истории. Дыра прошлого цикла (SCAN_GAP) — тоже
    стоп-сигнал: пока окно не вычитано, пропускать нельзя.
    """
    await _migrate_wallet_formats(session)
    since = await _read_cursor(session)
    raw_cursor = await _read_cursor_raw(session)
    stuck = await _read_stuck(session)
    scan_boost = await _read_scan_boost(session)
    head = await _read_head(session)
    head_check = await _read_head_check(session)
    scan_gap = await _read_scan_gap(session)
    return since, raw_cursor, stuck, scan_boost, head, head_check, scan_gap


async def _watch_process_batch(
    transfers: list[Transfer], bot: Bot | None, stuck: dict, since: int
) -> int:
    """Обрабатывает пачку переводов watcher'а; возвращает processed_through.

    Сбойная транзакция НЕ двигает курсор за себя: обрабатываем остаток
    пачки (skip без потери), но курсор останавливается перед ней, и в
    следующем цикле окно перечитает её заново. Так временный сбой
    (сеть, провайдер, баг версии) не стирает деньги молча.
    """
    processed_through = since
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
    return processed_through


async def _watch_persist_cursor(
    session: AsyncSession,
    *,
    api_ok: bool,
    since: int,
    gap_at: int | None,
    raw_cursor: int,
    stuck: dict,
    processed_through: int,
) -> int:
    """Запись курсора цикла watcher'а с защитой от потери/залипания окна.

    Возвращает эффективный курсор после цикла: его же запоминает отпечаток
    головы. Без возврата отпечаток хранил бы предпроходное значение и каждый
    следующий цикл видел бы «курсор изменился» — то есть пропуск не работал
    бы никогда после первого же нового перевода.

    Курсор — нижняя граница окна, прочитанного ЦЕЛИКОМ. Полный проход даёт
    processed_through (самая свежая обработанная транзакция). Усечённый по
    бюджету страниц проход даёт gap_at — дно прочитанного: всё выше него
    перечислено, а непрочитанное ниже догоняется следующими циклами, потому
    что курсор стоит на границе покрытия, а не на «непрочитанной» позиции.
    Раньше усечённый проход считался полным (complete=True), и курсор уезжал
    по processed_through — окно между gap_at и processed_through выпадало из
    чтения навсегда: тихо, без записи и без тревоги.
    """
    cursor_candidate = gap_at if gap_at is not None else processed_through
    if api_ok and cursor_candidate > raw_cursor:
        # Stuck-защита: курсор не уходит дальше самой свежей ТАК И НЕ обработанной
        # транзакции — иначе упавшая навсегда теряется за окном перекрытия.
        # max(raw_cursor, floor) хранит монотонность: сбойная в окне перекрытия
        # (ниже raw_cursor) не откатывает курсор, а просто не двигает его, и окно
        # следующего цикла перечитает её заново (skip без потери).
        fresh_floor = min(
            (r["utime"] for r in stuck.values() if r.get("fails", 0) <= _STUCK_MAX_FAILS),
            default=None,
        )
        if fresh_floor is not None:
            cursor_candidate = min(cursor_candidate, max(raw_cursor, fresh_floor))
        expired = [
            r
            for r in stuck.values()
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
        await _write_cursor(session, cursor_candidate)
        return cursor_candidate
    return raw_cursor


async def _watch_persist_stuck(session: AsyncSession, *, api_ok: bool, stuck: dict) -> None:
    """Фиксирует stuck-список каждым успешным циклом (пустой dict — пустой список)."""
    if api_ok:
        # stuck-список фиксируем каждым полным проходом, когда в нём что-то
        # есть ИЛИ когда его нужно очистить после успешных повторов: прежний
        # сбой уже записан в БД, молчание сейчас оставило бы устаревшую
        # запись висеть в ручном разборе. Пустой dict=пустой список.
        if stuck or (await session.get(WatcherState, STUCK_TX_KEY)) is not None:
            await _write_stuck(session, stuck)


async def _watch_heartbeat(session: AsyncSession, source: str, *, api_ok: bool) -> None:
    """Сердцебиение ставится каждым успешным циклом — даже без переводов:
    тишина в цепочке это здоровье, а не простой."""
    if api_ok:
        await _write_beat(session)
        await _write_source(session, source)


async def _watch_scan_gap(
    *, api_ok: bool, since: int, gap_at: int | None, scan_boost: int
) -> None:
    """Дыра окна (бюджет страниц исчерпан раньше курсора): запомнить границы,
    удвоить бюджет страниц на следующий цикл и НИКОГДА не терять её молча —
    тревогу поднимет ops.py по watcher_state[SCAN_GAP_KEY].
    """
    async with SessionLocal() as session:
        if gap_at is not None and api_ok:
            await _write_scan_gap(session, since, gap_at, _MAX_PAGES * scan_boost)
            boosted = await _bump_scan_boost(session)
            logger.error(
                "Окно входящих прочитано не полностью: курсор с %s → %s (дно прочитанного), "
                "ниже осталось непрочитанным. Бюджет страниц поднят x%s (база %s): следующий "
                "цикл продолжит читать глубже. Если дыра не закрылась за несколько циклов — "
                "подними WATCH_MAX_PAGES или разбери окно вручную через /incoming и /adjust.",
                since,
                gap_at,
                boosted,
                _MAX_PAGES,
            )
        elif api_ok:
            await _reset_scan_state(session)


async def _watch_heal_stuck(bot: Bot | None) -> int:
    """Авто-лечение брошенных сбойных переводов: не даём деньгам зависать
    в казне до ручного разбора (инцидент Kote). Идемпотентно и не мешает
    циклу, если лечение временно падает."""
    return await _heal_stuck_transfers(bot)


def _watch_probe_allowed(
    *,
    stuck: dict,
    scan_boost: int,
    scan_gap: dict | None,
) -> bool:
    """Стоит ли вообще тратить запрос на зонд головы в этом цикле.

    Зонд нужен и для пропуска, и для записи отпечатка после полного прохода
    (в том числе первого и страховочного). Поэтому он выполняется всегда,
    когда контур здоров; при stuck/дыре/бусте полный проход и так неизбежен,
    и лишний запрос не нужен.
    """
    if not settings.ton_head_probe_enabled:
        return False
    if stuck or scan_gap is not None or scan_boost != 1:
        return False
    return True


def _watch_skip_by_head(
    *,
    head: dict | None,
    head_check: str | None,
    raw_cursor: int,
    probe: _head.HeadProbe | None,
) -> bool:
    """Можно ли пропустить полный проход по неизменной голове.

    Пропуск разрешён только в полностью здоровом простое: голова записана
    после полного прохода того же курсора, ещё не устарела и совпала с
    зондом. Любое отклонение — полный проход: экономия трафика не стоит
    пропущенного перевода.
    """
    if probe is None or not probe.ok or head is None:
        return False
    if int(head.get("cursor") or 0) != raw_cursor:
        # Ручной сброс/сдвиг курсора инвалидирует отпечаток: окно между
        # старой головой и новым курсором ещё не прочитано.
        return False
    if not _head.head_is_fresh(head_check, settings.watch_full_rescan_seconds):
        return False
    return _head.same_head(head, probe)


async def _watch_confirm_side_effects(bot: Bot | None) -> None:
    """Слои, не зависящие от новых переводов: подтверждение отложенных ставок
    и актуализация банка дня. Выполняются и в цикле-пропуске, чтобы дешёвый
    простой не замедлял подтверждение ставок."""
    try:
        await confirm_aged_pending(bot)
    except Exception:
        logger.exception("Подтверждение отложенных ставок упало (не мешает циклу)")
    # Свежие подтверждения выросли банк дня, а в постах-статусах он застыл:
    # правим их актуальным банком, чтобы игроки не гоняли /today за цифрой.
    # Необязательный слой: сбой правки не должен рвать цикл watcher'а.
    try:
        from app.broadcast import refresh_day_bank

        await refresh_day_bank(bot)
    except Exception:
        logger.exception("Актуализация банка дня в постах упала (не мешает циклу)")


async def watch_once(bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        (
            since,
            raw_cursor,
            stuck,
            scan_boost,
            head,
            head_check,
            scan_gap,
        ) = await _watch_state_read(session)
    probe: _head.HeadProbe | None = None
    if _watch_probe_allowed(
        stuck=stuck,
        scan_boost=scan_boost,
        scan_gap=scan_gap,
    ):
        probe = await _head.fetch_head_probe()
        if _watch_skip_by_head(
            head=head,
            head_check=head_check,
            raw_cursor=raw_cursor,
            probe=probe,
        ):
            # Голова та же, окно курсора не двигалось: сверху новых переводов
            # нет, полная страница истории (≈70 КБ) не нужна. Сердцебиение и
            # подтверждение ставок всё равно обновляем.
            async with SessionLocal() as session:
                await _watch_heartbeat(session, probe.source, api_ok=True)
            await _watch_confirm_side_effects(bot)
            logger.debug(
                "Цикл watcher: голова не изменилась (lt %d, источник %s) — проход пропущен",
                probe.lt,
                probe.source,
            )
            return
    transfers, api_ok, source, gap_at, parse_fail_count = await _collect_transfers(
        since, _MAX_PAGES * scan_boost
    )
    # Item'ы, отброшенные парсером с исключением (см. P0 #4 в README лога):
    # раньше parser глушил ошибку как «нормальный None» и считать их было
    # негде. Теперь счётчик за цикл уезжает в watcher_state как
    # parse_fail_count и поднимает alarm до чистого цикла.
    if parse_fail_count > 0:
        async with SessionLocal() as session:
            await _bump_parse_fail(session, parse_fail_count)
        logger.warning(
            "За цикл отброшено %d item'ов парсером TonAPI/Toncenter — "
            "счётчик записан в watcher_state[parse_fail_count]",
            parse_fail_count,
        )
    processed_through = await _watch_process_batch(transfers, bot, stuck, since)
    await _watch_confirm_side_effects(bot)
    async with SessionLocal() as session:
        effective_cursor = await _watch_persist_cursor(
            session,
            api_ok=api_ok,
            since=since,
            gap_at=gap_at,
            raw_cursor=raw_cursor,
            stuck=stuck,
            processed_through=processed_through,
        )
        await _watch_persist_stuck(session, api_ok=api_ok, stuck=stuck)
        await _watch_heartbeat(session, source, api_ok=api_ok)
        # Отпечаток головы — только после полного здорового прохода, который
        # читал тот же источник, что и зонд. Иначе следующий цикл мог бы
        # пропустить окно, которое этот проход не видел (например, зонд по
        # TonAPI, а страницы — по отстающему Toncenter).
        if (
            probe is not None
            and probe.ok
            and probe.source == source
            and api_ok
            and gap_at is None
            and parse_fail_count == 0
            and not stuck
        ):
            await _write_head(
                session,
                lt=probe.lt,
                head_hash=probe.hash,
                source=probe.source,
                cursor=effective_cursor,
            )
    await _watch_scan_gap(api_ok=api_ok, since=since, gap_at=gap_at, scan_boost=scan_boost)
    try:
        # Авто-лечение брошенных сбойных переводов: не даём деньгам зависать
        # в казне до ручного разбора (инцидент Kote). Идемпотентно и не мешает
        # циклу, если лечение временно падает.
        healed = await _watch_heal_stuck(bot)
        if healed:
            logger.info("Авто-лечение stuck-трансакций: исцелено %d", healed)
    except Exception:
        logger.exception("Авто-лечение stuck-трансакций упало (не мешает циклу)")
    if transfers:
        logger.info(
            "Цикл watcher: найдено %d переводов, курсор %d → %d, проход %s (источник %s), stuck %d",
            len(transfers), since, processed_through,
            "полный" if gap_at is None else "ЧАСТИЧНЫЙ (курсор встанет на дно прочитанного)",
            source,
            len(stuck),
        )
