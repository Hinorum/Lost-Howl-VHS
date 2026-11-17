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
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.ton_codec import extract_comment, tonapi_headers

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


def _scan_page_map(items: list) -> dict[str, str]:
    """Карта memo → хеш по одной странице истории исходящих."""
    page: dict[str, str] = {}
    for item in items:
        tx_hash = str(item.get("hash") or "")
        if not tx_hash:
            continue
        # Каждая исходящая транзакция казначея имеет hash; комментарий
        # берём из её out_msgs. Если в одной транзакции несколько
        # переводов с разными memo — все попадают в карту.
        for comment in _out_comments(item):
            page[comment] = tx_hash
    return page


def _scan_window_closed(
    items: list,
    *,
    page_limit: int,
    cutoff: float,
    targets: set[str] | None,
    found: set[str],
) -> bool:
    """Окно истории покрыто целиком по ФАКТУ прохода по странице."""
    if targets and targets <= found:
        return True  # все цели найдены — вопросов нет
    oldest_utime = items[-1].get("utime")
    if oldest_utime is not None and float(oldest_utime) < cutoff:
        return True  # окно истории покрыто целиком
    return len(items) < page_limit  # неполная страница = хвост истории


async def _scan_broadcast_history(
    targets: set[str] | None,
    *,
    max_pages: int,
    page_limit: int,
    cutoff: float,
    fetch_page: Callable[[str], Awaitable[Any]],
    next_token: Callable[[list, str], str | None],
    error_context: str,
) -> dict[str, str]:
    """Глубокий скан истории исходящих переводов казначея через страницы.

    Одна страница (128 tx) — слишком мелкое окно: в длинной очереди слово
    «потерялся» выносится на пустом месте (memo уже отправленного легко лежит
    глубже 128 свежих транзакций), и сверка возвращает в очередь уже ушедший
    перевод. Ходим страницами вниз по времени, пока не накроем
    payout_reconcile_history_seconds или не упрёмся в пустую/повторную страницу.

    fetch_page(token) — получить следующую страницу по курсору (before_lt у
    TonAPI / offset у Toncenter); next_token(items, token) — токен следующей
    страницы, None — следующий шаг невозможен. targets — кому нужно: жадный
    полный скан (12 страниц) заменяется проходом до момента, когда ВСЕ цели
    найдены; отсутствующая цель вынуждает дойти до конца окна — отрицательный
    ответ остаётся честным.

    Полнота окна считается по ФАКТУ прохода, а не по «провайдер ответил»: скан
    может упереться в max_pages, не дойдя до границы окна, и тогда «memo не
    найдено» — это «не знаю», а не «перевода нет». Стартовое False пессимистично:
    аварийный выход (raise, истощение страниц) оставляет отрицательные ответы
    недоказанными (см. app.ton_pay.state).
    """
    tx_map: dict[str, str] = {}
    _state._RECONCILE_WINDOW_COMPLETE = False
    first_hash: str | None = None
    before_lt = ""
    for _ in range(max_pages):
        response = await fetch_page(before_lt)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "transactions" not in data:
            # HTTP 200 без поля transactions — НЕ «пустая история», а аномалия
            # провайдера (дроссель, ошибка-обёртка в 200): пустой {}-ответ
            # заставил бы сверщика поверить, что memo в цепочке нет, и повтор
            # уехал бы на уже ушедший платёж. Падаем — фолбэк на Toncenter,
            # и при его молчании _RECONCILE_HISTORY_OK=False замораживает повторы.
            raise RuntimeError(
                f"{error_context}: ответ без поля transactions: {str(data)[:120]}"
            )
        items = data.get("transactions") or []
        if not items:
            _state._RECONCILE_WINDOW_COMPLETE = True  # пустая страница — хвост истории
            break
        page_first_hash = str(items[0].get("hash") or "")
        if page_first_hash and page_first_hash == first_hash:
            break  # пагинация не сдвинулась (провайдер не взял курсор) — хватит
        first_hash = page_first_hash
        tx_map.update(_scan_page_map(items))
        if _scan_window_closed(
            items,
            page_limit=page_limit,
            cutoff=cutoff,
            targets=targets,
            found=set(tx_map),
        ):
            _state._RECONCILE_WINDOW_COMPLETE = True
            break
        before_lt = next_token(items, before_lt) or ""
        if not before_lt:
            break  # следующий шаг невозможен, пагинация провалится
    return tx_map


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
        # Казначей не задан — истории нет вовсе, искать нечего.
        _state._RECONCILE_WINDOW_COMPLETE = True
        return {}
    url = (
        f"{settings.active_ton_api_base}/v2/blockchain/accounts/"
        f"{settings.active_treasury_address}/transactions"
    )
    # TonAPI ждёт Authorization: Bearer; X-API-Key он не считает вовсе и тихо
    # переводит запрос в анонимный режим, где /v2/blockchain режет страницу до
    # 100 записей при запрошенных 256. Тогда цикл ниже видит короткую страницу и
    # выходит после первой, а окно сверки схлопывается с 3072 переводов до 100:
    # выплата, чей memo вытеснили из окна, объявляется неушедшей и уходит
    # повторно — уже с новым seqno. Ключ обязан доезжать.
    headers = tonapi_headers(settings.ton_api_key)

    page_limit, max_pages = _state.RECONCILE_SCAN_OVERRIDE.get() or (
        _state._RECONCILE_PAGE_LIMIT,
        max(1, settings.payout_reconcile_max_pages),
    )
    max_pages = max(1, max_pages)
    page_limit = max(1, page_limit)

    async def fetch_page(before_lt: str) -> Any:
        params: dict[str, Any] = {"limit": page_limit, "sort_order": "desc"}
        if before_lt:
            params["before_lt"] = before_lt
        response = await _tp.get_http_client().get(url, params=params, headers=headers)
        return response

    def next_token(items: list, token: str) -> str | None:
        # Пагинация глубокая: следующий курсор — lt последней записи страницы.
        # None — следующий шаг невозможен (lt нет): хватим.
        return str(items[-1].get("lt") or "") or None

    return await _scan_broadcast_history(
        targets,
        fetch_page=fetch_page,
        max_pages=max_pages,
        page_limit=page_limit,
        cutoff=time.time() - settings.payout_reconcile_history_seconds,
        next_token=next_token,
        error_context="TonAPI истории казначея",
    )


async def _tx_map_via_toncenter(targets: set[str] | None = None) -> dict[str, str]:
    """memo исходящих казначея → реальный хеш (Toncenter v3), страницами вглубь.

    То же глубокое окно, что в _tx_map_via_tonapi, но через параметр offset
    резервного провайдера: анти-дубль не должен слепнуть там, где TonAPI молчит.
    targets — см. _tx_map_via_tonapi: досрочный стоп после нахождения всех целей.
    """
    import app.ton_pay as _tp

    if not settings.active_treasury_address:
        # Казначей не задан — истории нет вовсе, искать нечего.
        _state._RECONCILE_WINDOW_COMPLETE = True
        return {}
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    page_limit, max_pages = _state.RECONCILE_SCAN_OVERRIDE.get() or (
        _state._RECONCILE_PAGE_LIMIT,
        max(1, settings.payout_reconcile_max_pages),
    )
    max_pages = max(1, max_pages)
    page_limit = max(1, page_limit)
    # Полнота окна — по факту прохода (см. _tx_map_via_tonapi): усечение по
    # max_pages оставляет отрицательные ответы недоказанными.
    _state._RECONCILE_WINDOW_COMPLETE = False
    client = _tp.get_http_client()

    async def fetch_page(token: str) -> Any:
        params = {
            "account": settings.active_treasury_address,
            "limit": page_limit,
            "sort": "desc",
            "offset": int(token or 0),
        }
        return await _tp.http_get_with_retry(client, url, params=params, headers=headers)

    def next_token(items: list, token: str) -> str | None:
        # Шаг — от ФАКТИЧЕСКИ пришедших записей, а не от запрошенного лимита.
        # Провайдер вправе отдать страницу короче запроса: при жёстком шаге по
        # константе следующий offset перепрыгнул бы через неперечитанный кусок,
        # и в карте анти-дубля появилась бы дыра. Реальный Toncenter лимит
        # отдаёт честно (проверено живыми запросами: limit=256 → 256 записей,
        # limit=1000 → 1000), но опираться на это в анти-дубле незачем: тот же
        # инвариант уже держит _advance_cursor в treasury_mirror.
        return str(int(token or 0) + max(1, len(items) - _state._RECONCILE_PAGE_OVERLAP))

    return await _scan_broadcast_history(
        targets,
        fetch_page=fetch_page,
        max_pages=max_pages,
        page_limit=page_limit,
        cutoff=time.time() - settings.payout_reconcile_history_seconds,
        next_token=next_token,
        error_context="Toncenter истории казначея",
    )


async def fetch_broadcast_tx_map(targets: set[str] | None = None) -> dict[str, str]:
    """memo последних исходящих казначея → реальный хеш транзакции.

    TonAPI → фолбэк Toncenter. Пустой результат при сбое сети значит
    «не знаем»: сверщик (confirm_broadcast_payouts) в этом случае НИЧЕГО
    не решает — ни подтверждает, ни возвращает в очередь (риск задвоить).

    targets (необязательно) — подмножество memo, ради которого ходим:
    скан останавливается, как только все цели найдены. Кому-то ещё нужен
    полный скан окна — зовут без targets и получают прежнее поведение.

    Полнота покрытия окна пишется в _state._RECONCILE_WINDOW_COMPLETE:
    усечённый скан (payout_reconcile_max_pages) — «не знаю» для отрицательных
    ответов, и потребители (confirm, диспетчер) обязаны это проверять.
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
    _state._RECONCILE_WINDOW_COMPLETE = False  # оба молчат — «не знаю» и о полноте
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
            tonapi_headers(settings.ton_api_key),
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
