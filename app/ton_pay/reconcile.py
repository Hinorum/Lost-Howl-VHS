"""Анти-дубль: сверка memo с историей исходящих казначея.

Один модуль — одна причина: «перевод потерялся» почти всегда значит, что memo
уже ушло в цепочку, а статус сохранить не успели (краш/таймаут сразу после
вещания). Отсюда карта memo → хеш транзакции, множество недавних исходящих
(маркеры) и честная энтропия мастерчейна для ничьей дня.

Всё, что ходит в сеть, берётся через app.ton_pay, и соседние функции этого
модуля вызываются тоже через него: тесты патчат ton_pay.get_http_client и
ton_pay.fetch_broadcast_tx_map, а прямые вызовы модульных имён эти патчи
обходили бы — тест молча ушёл бы в живую сеть вместо фейкового клиента.

Пустой результат при сбое сети означает «не знаю», а не «перевода нет»:
сверщик в этом случае не решает ничего (риск задвоить дороже задержки).
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.ton_codec import api_headers, extract_comment

from . import state as _state

logger = logging.getLogger(__name__)


def _out_comments(item: dict) -> list[str]:
    """Комментарии исходящих сообщений одной транзакции.

    Единая расшифровка для TonAPI v2 и Toncenter v3 (app.ton_codec):
    decoded_body по op-имени → decoded_comment → base64 text →
    message_content.decoded (comment/text_comment) → короткий raw_message.
    """
    comments: list[str] = []
    for msg in item.get("out_msgs") or []:
        if not isinstance(msg, dict):
            continue
        comment = extract_comment(msg)
        if comment:
            comments.append(comment)
    return comments


# Совместимость имён: «формат-специфичные» экстракторы были двумя копиями
# одного декодера — тесты ходят по прежним именам (test_payout_dedupe).
_out_comments_tonapi = _out_comments
_out_comments_toncenter = _out_comments


async def _tx_map_via_tonapi(targets: set[str] | None = None) -> dict[str, str]:
    """memo исходящих казначея → реальный хеш (TonAPI v2), страницами вглубь.

    Одна страница (128 tx) — слишком мелкое окно: в длинной очереди слово
    «потерялся» выносится на пустом месте (memo уже отправленного легко лежит
    глубже 128 свежих транзакций), и сверка возвращает в очередь уже ушедший
    перевод. Ходим страницами (before_lt) вниз по времени, пока не накроем
    payout_reconcile_history_seconds или не упрёмся в пустую/повторную страницу.

    targets — кому это нужно: жадный полный скан (12 страниц) заменяется
    проходом до момента, когда ВСЕ цели найдены. Отсутствующая цель при этом
    вынуждает дойти до конца окна — отрицательный ответ остаётся честным.
    """
    import app.ton_pay as _tp

    if not settings.active_treasury_address:
        return {}
    url = (
        f"{settings.active_ton_api_base}/v2/blockchain/accounts/"
        f"{settings.active_treasury_address}/transactions"
    )
    headers = api_headers(settings.ton_api_key)
    tx_map: dict[str, str] = {}
    cutoff = time.time() - settings.payout_reconcile_history_seconds
    max_pages = max(1, settings.payout_reconcile_max_pages)
    before_lt: str | None = None
    first_hash: str | None = None
    client = _tp.get_http_client()
    for _ in range(max_pages):
        response = await client.get(
            url,
            params={
                "limit": _state._RECONCILE_PAGE_LIMIT,
                "sort_order": "desc",
                **({"before_lt": before_lt} if before_lt else {}),
            },
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "transactions" not in data:
            # HTTP 200 без поля transactions — НЕ «пустая история», а аномалия
            # провайдера (дроссель, ошибка-обёртка в 200): пустой {}-ответ
            # заставил бы сверщика поверить, что memo в цепочке нет, и повтор
            # уехал бы на уже ушедший платёж. Падаем — фолбэк на Toncenter,
            # и при его молчании _RECONCILE_HISTORY_OK=False замораживает повторы.
            raise RuntimeError(
                f"TonAPI истории казначея: ответ без поля transactions: {str(data)[:120]}"
            )
        items = data.get("transactions") or []
        if not items:
            break
        page_first_hash = str(items[0].get("hash") or "")
        if page_first_hash and page_first_hash == first_hash:
            break  # пагинация не сдвинулась (провайдер не взял before_lt) — хватит
        first_hash = page_first_hash
        for item in items:
            tx_hash = str(item.get("hash") or "")
            if not tx_hash:
                continue
            # Каждая исходящая транзакция казначея имеет hash; комментарий
            # берём из её out_msgs. Если в одной транзакции несколько
            # переводов с разными memo — все попадают в карту.
            for comment in _out_comments_tonapi(item):
                tx_map[comment] = tx_hash
        if targets and targets <= set(tx_map):
            break  # цели найдены — дальше вглубь незачем (экономия запросов)
        oldest_utime = items[-1].get("utime")
        if oldest_utime is not None and float(oldest_utime) < cutoff:
            break  # окно истории покрыто
        if len(items) < _state._RECONCILE_PAGE_LIMIT:
            break  # неполная страница = хвост истории, следующая запрос пуста
        before_lt = str(items[-1].get("lt") or "")
        if not before_lt:
            break  # lt нет — следующий шаг невозможен, пагинация провалится
    return tx_map


async def _tx_map_via_toncenter(targets: set[str] | None = None) -> dict[str, str]:
    """memo исходящих казначея → реальный хеш (Toncenter v3), страницами вглубь.

    То же глубокое окно, что в _tx_map_via_tonapi, но через параметр offset
    резервного провайдера: анти-дубль не должен слепнуть там, где TonAPI молчит.
    targets — см. _tx_map_via_tonapi: досрочный стоп после нахождения всех целей.
    """
    import app.ton_pay as _tp

    if not settings.active_treasury_address:
        return {}
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    tx_map: dict[str, str] = {}
    cutoff = time.time() - settings.payout_reconcile_history_seconds
    max_pages = max(1, settings.payout_reconcile_max_pages)
    offset = 0
    first_hash: str | None = None
    client = _tp.get_http_client()
    for _ in range(max_pages):
        params = {
            "account": settings.active_treasury_address,
            "limit": _state._RECONCILE_PAGE_LIMIT,
            "sort": "desc",
            "offset": offset,
        }
        response = await _tp.http_get_with_retry(client, url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "transactions" not in data:
            # Тот же сторож, что в _tx_map_via_tonapi: 200 без transactions —
            # «не знаем», история не считается здоровой. Иначе пустой {}-ответ
            # резервного провайдера снял бы стопор повтора на основном.
            raise RuntimeError(
                f"Toncenter истории казначея: ответ без поля transactions: {str(data)[:120]}"
            )
        items = data.get("transactions") or []
        if not items:
            break
        page_first_hash = str(items[0].get("hash") or "")
        if page_first_hash and page_first_hash == first_hash:
            break
        first_hash = page_first_hash
        for item in items:
            tx_hash = str(item.get("hash") or "")
            if not tx_hash:
                continue
            for comment in _out_comments_toncenter(item):
                tx_map[comment] = tx_hash
        if targets and targets <= set(tx_map):
            break  # цели найдены — дальше вглубь незачем (экономия запросов)
        oldest_utime = items[-1].get("utime")
        if oldest_utime is not None and float(oldest_utime) < cutoff:
            break
        if len(items) < _state._RECONCILE_PAGE_LIMIT:
            break  # неполная страница = хвост истории, дальше пусто
        offset += _state._RECONCILE_PAGE_LIMIT - _state._RECONCILE_PAGE_OVERLAP
    return tx_map


async def fetch_broadcast_tx_map(targets: set[str] | None = None) -> dict[str, str]:
    """memo последних исходящих казначея → реальный хеш транзакции.

    TonAPI → фолбэк Toncenter. Пустой результат при сбое сети значит
    «не знаем»: сверщик (confirm_broadcast_payouts) в этом случае НИЧЕГО
    не решает — ни подтверждает, ни возвращает в очередь (риск задвоить).

    targets (необязательно) — подмножество memo, ради которого ходим:
    скан останавливается, как только все цели найдены. Кому-то ещё нужен
    полный скан окна — зовут без targets и получают прежнее поведение.
    """
    import app.ton_pay as _tp

    for fetch in (_tp._tx_map_via_tonapi, _tp._tx_map_via_toncenter):
        try:
            result = await fetch(targets)
            _state._RECONCILE_HISTORY_OK = True
            return result
        except Exception as exc:
            logger.warning("Карта исходящих казначея (%s) недоступна: %s", fetch.__name__, exc)
    _state._RECONCILE_HISTORY_OK = False
    return {}


async def fetch_masterchain_entropy() -> str | None:
    """«seqno:root_hash» последнего мастерхчейн-блока TON — честная энтропия ничьей.

    Блок уже лежит в цепочке в момент жеребьёвки: его нельзя подменить или
    подогнать задним числом, а каждый игрок может проверить seqno в эксплорере
    и пересчитать исход. TonAPI → фолбэк Toncenter. При выключенном TON или
    сбое обоих узлов возвращает None — день откатится на легаси-жребий (seed
    без энтропии), чтобы ничья никогда не «зависала» на сетевой ошибке.
    """
    import app.ton_pay as _tp

    if not settings.ton_enabled:
        return None
    candidates = (
        (
            f"{settings.active_ton_api_base}/v2/blockchain/masterchain-head",
            {"X-API-Key": settings.ton_api_key} if settings.ton_api_key else {},
            lambda data: data,
        ),
        (
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/masterchainInfo",
            {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {},
            lambda data: data.get("last") or data,
        ),
    )
    for url, headers, pick in candidates:
        try:
            client = _tp.get_http_client()
            response = await _tp.http_get_with_retry(
                client, url, headers=headers, max_retries=0, timeout=8.0
            )
            response.raise_for_status()
            block = pick(response.json())
            seqno = block.get("seqno")
            root_hash = block.get("root_hash")
            if seqno is not None and root_hash:
                return f"{seqno}:{root_hash}"
        except Exception as exc:
            logger.warning("Энтропия мастерчейна (%s) недоступна: %s", url, exc)
    return None


async def fetch_broadcast_markers() -> set[str]:
    """Memo недавних исходящих переводов казначея как set.

    Сверка перед ПОВТОРНОЙ отправкой: перевод мог уйти в цепочку в прошлый
    раз, но статус «sent» сохранить не успели (краш/таймаут сразу после
    вещания). Повтор такой выплаты — реальные чужие деньги дважды. Пустой
    результат при сбое сети значит «не знаю»: ведём себя как раньше и
    пытаемся отправить — узкое окно риска лучше постоянной блокировки очереди.
    """
    import app.ton_pay as _tp

    return set(await _tp.fetch_broadcast_tx_map())


async def fetch_masterchain_head_seqno() -> int | None:
    """Текущий seqno мастерчейна TON: head блокчейна прямо сейчас.

    Используется для подсчёта подтверждений уже ушедших транзакций: confirm =
    head - tx_seqno. При сбое обоих провайдеров возвращает None — это сигнал
    «не знаю», и confirm_broadcast_payouts в этом случае не ставит
    confirmed=True (риск пропустить нефинализированный блок дороже задержки).
    """
    import app.ton_pay as _tp

    if not settings.ton_enabled:
        return None
    candidates = (
        (
            f"{settings.active_ton_api_base.rstrip('/')}/v2/blockchain/masterchain-head",
            api_headers(settings.ton_api_key),
            lambda data: data,
        ),
        (
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/masterchainInfo",
            api_headers(settings.toncenter_api_key),
            lambda data: data.get("last") or data,
        ),
    )
    for url, headers, pick in candidates:
        try:
            client = _tp.get_http_client()
            response = await _tp.http_get_with_retry(
                client, url, headers=headers, max_retries=0, timeout=8.0
            )
            response.raise_for_status()
            block = pick(response.json())
            seqno = block.get("seqno")
            if seqno is not None:
                return int(seqno)
        except Exception as exc:
            logger.warning("Masterchain head (%s) недоступен: %s", url, exc)
    return None


async def fetch_tx_mc_seqno(tx_hash: str) -> int | None:
    """masterchain seqno блока, в который попала транзакция tx_hash.

    TonAPI: GET /v2/blockchain/transactions/{hash} → блок-предок в mainchain,
    поле `mc_block_seqno`. Toncenter v3: GET /api/v3/transactions/{hash} →
    поле `mc_block_seqno` (та же схема). При сбое обоих — None. Не путать
    с `block_seqno`: логически транзакция живёт в shard, а финальность
    считается по мастерчейну, поэтому только mc_block_seqno подходит для
    оценки reorg-риска.
    """
    import app.ton_pay as _tp

    candidates = (
        (
            f"{settings.active_ton_api_base.rstrip('/')}/v2/blockchain/transactions/{tx_hash}",
            api_headers(settings.ton_api_key),
            lambda data: data.get("mc_block_seqno"),
        ),
        (
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions/{tx_hash}",
            api_headers(settings.toncenter_api_key),
            lambda data: data.get("mc_block_seqno"),
        ),
    )
    for url, headers, pick in candidates:
        try:
            client = _tp.get_http_client()
            response = await _tp.http_get_with_retry(
                client, url, headers=headers, max_retries=0, timeout=8.0
            )
            response.raise_for_status()
            seqno = pick(response.json())
            if seqno is not None:
                return int(seqno)
        except Exception as exc:
            logger.warning("mc_block_seqno (%s) недоступен: %s", url, exc)
    return None


async def fetch_tx_confirmations(tx_hash: str) -> int | None:
    """Число подтверждений мастерчейна для транзакции (head_seqno - mc_block_seqno).

    Используется confirm_broadcast_payouts для решения «ставить confirmed=True
    или ждать» при payout_confirm_blocks > 0. При сбое любого из запросов —
    None: вызывающий код трактует это как «не знаю» и оставляет строку
    confirmed=False до следующего цикла (а не ставит confirmed=True наугад).
    Возврат 0 транзакции в head блоке (теоретически) — корректный ноль, не None.
    """
    head_seqno = await fetch_masterchain_head_seqno()
    tx_seqno = await fetch_tx_mc_seqno(tx_hash)
    if head_seqno is None or tx_seqno is None:
        return None
    if tx_seqno > head_seqno:
        # Невозможно на mainnet: tx в «будущем» относительно head. Не падаем,
        # но и подтверждение не считаем — вызывающий код оставит строку
        # confirmed=False и попробует на следующем цикле.
        logger.warning(
            "fetch_tx_confirmations: tx_seqno=%d > head_seqno=%d для %s",
            tx_seqno, head_seqno, tx_hash[:16],
        )
        return None
    return head_seqno - tx_seqno
