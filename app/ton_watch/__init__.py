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

from app.config import settings
from app.core.registry import BEAT_KEY as BEAT_KEY  # noqa: F401  (ре-экспорт ключей watcher_state)
from app.core.registry import CURSOR_KEY as CURSOR_KEY  # noqa: F401
from app.core.registry import SCAN_BOOST_KEY as SCAN_BOOST_KEY  # noqa: F401
from app.core.registry import SCAN_GAP_KEY as SCAN_GAP_KEY  # noqa: F401
from app.core.registry import SOURCE_KEY as SOURCE_KEY  # noqa: F401
from app.core.registry import STUCK_TX_KEY, WALLET_NORM_KEY
from app.db import SessionLocal
from app.http_utils import (
    get_http_client as get_http_client,  # noqa: F401  (подменяется в тестах скриптованным клиентом)
)
from app.models import Player, Round, RoundStatus, WatcherState
from app.ops import claim_once as claim_once  # noqa: F401  (тесты чистят маркеры через ton_watch.claim_once)
from app.ops import is_game_paused
from app.payments import parse_bank_memo, parse_revote_memo, parse_verify_memo
from app.stakes import confirm_stake, register_stake
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
from app.ton_watch import ledger as ledger  # noqa: F401
from app.ton_watch import notify as notify  # noqa: F401
from app.ton_watch import refunds as refunds  # noqa: F401
from app.ton_watch import revote as revote  # noqa: F401
from app.ton_watch import sources as sources  # noqa: F401
from app.ton_watch import state as state  # noqa: F401
from app.ton_watch.ledger import (  # noqa: F401
    PAUSE_REFUND_COMMENT,
    _ledger_incoming,
    _ledger_stuck_incoming,
    _record_bank_credit,
)
from app.ton_watch.notify import (  # noqa: F401
    _dm_stake,
    _dm_verify_mismatch,
    _kick_dispatch_after_verify,
)
from app.ton_watch.refunds import (  # noqa: F401
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
    _bump_scan_boost,
    _load_stuck,
    _read_cursor,
    _read_cursor_raw,
    _read_scan_boost,
    _read_scan_gap,
    _read_stuck,
    _reset_scan_state,
    _write_beat,
    _write_cursor,
    _write_scan_gap,
    _write_source,
    _write_stuck,
)


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
                        "сегодня версия без ставок: смена выбора закрыта — платить за неё нечем.",
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
                    "без мемо — зачтено по сумме). Нажми другой вариант — кадр перемотается.",
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
                    "сегодня версия без ставок: смена выбора закрыта — платить за неё нечем.",
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
                    f"↩️ Перевод {amount} Gram возвращается: сегодня версия без ставок — "
                    "ставки не принимаются. Поставить можно в день со ставками.",
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

async def watch_once(bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        since = await _read_cursor(session)
        raw_cursor = await _read_cursor_raw(session)
        stuck = await _read_stuck(session)
        scan_boost = await _read_scan_boost(session)
    transfers, api_ok, source, gap_at = await _collect_transfers(
        since, _MAX_PAGES * scan_boost
    )
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
# Курсор — нижняя граница окна, прочитанного ЦЕЛИКОМ. Полный проход даёт
    # processed_through (самая свежая обработанная транзакция). Усечённый по
    # бюджету страниц проход даёт gap_at — дно прочитанного: всё выше него
    # перечислено, а непрочитанное ниже догоняется следующими циклами, потому
    # что курсор стоит на границе покрытия, а не на «непрочитанной» позиции.
    # Раньше усечённый проход считался полным (complete=True), и курсор уезжал
    # по processed_through — окно между gap_at и processed_through выпадало из
    # чтения навсегда: тихо, без записи и без тревоги.
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
            await _write_cursor(session, cursor_candidate)
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
    # Дыра окна (бюджет страниц исчерпан раньше курсора): запомнить границы,
    # удвоить бюджет страниц на следующий цикл и НИКОГДА не терять её молча —
    # тревогу поднимет ops.py по watcher_state[SCAN_GAP_KEY].
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
            "полный" if gap_at is None else "ЧАСТИЧНЫЙ (курсор встанет на дно прочитанного)",
            source,
            len(stuck),
        )
