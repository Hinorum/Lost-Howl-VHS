"""Общие HTTP-утилиты: GET с ретраями на транзиентные сбои.

Вынесено из ton_watch.py и ton_pay.py (идентичный дубль) — одна точка
реализации backoff'а для внешних REST-узлов (TonAPI, лайтсерверы).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Общий HTTP-клиент с переиспользуемым пулом соединений.

    Раньше каждый вызов watcher/сверки/диагностики поднимал собственный
    AsyncClient — DNS+TLS устанавливались заново на каждый запрос даже между
    соседними циклами. Один клиент на процесс даёт keep-alive между циклами
    и единую точку настройки таймаута/лимитов.
    """
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
        )
    return _HTTP_CLIENT


async def close_http_client() -> None:
    """Вежливо гасит общий клиент на выключении процесса (сброс пула)."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """HTTP GET с retry для 429/5xx ошибок и транзиентных таймаутов.

    Пауза между попытками растёт экспоненциально: retry_delay, retry_delay ×
    backoff_factor, ×backoff_factor²… — не выше max_delay. Прежняя постоянная
    задержка долбила молчащий индексатор с одинаковой частотой и не давала
    ему оправиться. timeout — пер-запросный таймаут (None = по умолчанию
    клиента): для «не смеющего тормозить тик» вызовов (энтропия мастерчейна).

    429 (квота провайдера — реальность free tier TonAPI/Toncenter): респект
    заголовка Retry-After (сек до абсолютной даты), иначе обычный backoff —
    долбить в отказ после ответа «подожди» значит ловить бан ключа.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            request_kwargs: dict = {}
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = await client.get(url, params=params, headers=headers, **request_kwargs)
            if response.status_code == 429 and attempt < max_retries:
                delay = _retry_after_delay(response)
                if delay is None:
                    delay = min(max_delay, retry_delay * (backoff_factor**attempt))
                # Потолок удерживаем и для Retry-After: провайдер может
                # попросить подождать час — минуточный цикл так не живёт.
                delay = min(delay, max_delay) if delay > 0 else max(0.0, delay)
                logger.warning(
                    "HTTP 429 от %s (попытка %d/%d), повтор через %.1fs",
                    url, attempt + 1, 1 + max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code < 500 or attempt == max_retries:
                return response
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP %d от %s (попытка %d/%d), повтор через %.1fs",
                response.status_code, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP ошибка %s от %s (попытка %d/%d), повтор через %.1fs",
                exc, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def http_post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict | list | None = None,
    headers: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """HTTP POST с теми же правилами ретрая, что GET (см. http_get_with_retry).

    Нужен HTTP-каналу отправки (ton_pay): подписанное внешнее сообщение
    казначея вещается через Toncenter v2 jsonRPC sendBoc, когда ADNL/TCP до
    лайтсерверов закрыт окружением. Тот же backoff на 429/5xx и транзиентные
    сбои — «отправить и забыть» тут недопустимо: потерянный ответ равноценен
    двойной отправке, if сообщение уже ушло штатно.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            request_kwargs: dict = {}
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = await client.post(url, json=json, headers=headers, **request_kwargs)
            if response.status_code == 429 and attempt < max_retries:
                delay = _retry_after_delay(response)
                if delay is None:
                    delay = min(max_delay, retry_delay * (backoff_factor**attempt))
                delay = min(delay, max_delay) if delay > 0 else max(0.0, delay)
                logger.warning(
                    "HTTP 429 от %s (попытка %d/%d), повтор через %.1fs",
                    url, attempt + 1, 1 + max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code < 500 or attempt == max_retries:
                return response
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP %d от %s (попытка %d/%d), повтор через %.1fs",
                response.status_code, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP ошибка %s от %s (попытка %d/%d), повтор через %.1fs",
                exc, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _retry_after_delay(response: httpx.Response) -> float | None:
    """Retry-After как секунды ожидания; неразбираемое значение — None.

    Форматы по RFC 7231: целое число секунд либо HTTP-дата. Дата в прошлом —
    0 (повтор немедленно); мусор — None (обычный экспоненциальный backoff).
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return max(0.0, float(raw))
    try:
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())
