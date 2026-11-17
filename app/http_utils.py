"""Общие HTTP-утилиты: GET с ретраями на транзиентные сбои.

Вынесено из ton_watch.py и ton_pay.py (идентичный дубль) — одна точка
реализации backoff'а для внешних REST-узлов (TonAPI, лайтсерверы).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from app.metrics import inc

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


async def _send_attempt(
    send: Callable[..., Awaitable[httpx.Response]],
    timeout: float | None,
    host: str,
) -> httpx.Response:
    """Одна попытка запроса с метрикой; timeout — только если задан явно.

    Считаем и байты ответа: на free-плане Render каждый скачанный байт —
    это исходящий трафик, и без счётчика не видно, какой внешний API съедает
    лимит. Тело ответа — то, что реально ушло по сети (заголовки не считаем:
    они ничтожны по сравнению с JSON-страницами истории транзакций).
    """
    request_kwargs: dict = {}
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    response = await send(**request_kwargs)
    inc("http_requests_total", {"host": host, "status": str(response.status_code)})
    inc("http_response_bytes_total", {"host": host}, _response_bytes(response))
    return response


def _response_bytes(response: httpx.Response) -> int:
    """Размер тела ответа в байтах (0, если тело не прочитано)."""
    try:
        return len(response.content)
    except Exception:
        # Потоковый ответ без прочитанного тела: считать нечего.
        return 0


def _backoff_delay(
    attempt: int, retry_delay: float, backoff_factor: float, max_delay: float
) -> float:
    """Экспоненциальная пауза перед повтором: не выше max_delay."""
    return min(max_delay, retry_delay * (backoff_factor**attempt))


def _throttle_delay(
    response: httpx.Response,
    attempt: int,
    *,
    retry_delay: float,
    backoff_factor: float,
    max_delay: float,
) -> float:
    """Пауза после 429: Retry-After с потолком, иначе обычный backoff."""
    delay = _retry_after_delay(response)
    if delay is None:
        delay = _backoff_delay(attempt, retry_delay, backoff_factor, max_delay)
    # Потолок удерживаем и для Retry-After: провайдер может
    # попросить подождать час — минуточный цикл так не живёт.
    return min(delay, max_delay)


def _log_retry(label: str, url: str, attempt: int, total: int, delay: float) -> None:
    """Единый лог повтора: что ответил провайдер и сколько ждём."""
    logger.warning(
        "HTTP %s от %s (попытка %d/%d), повтор через %.1fs",
        label,
        url,
        attempt + 1,
        total,
        delay,
    )


async def _request_with_retry(
    send: Callable[..., Awaitable[httpx.Response]],
    url: str,
    *,
    timeout: float | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """Запрос с ретраями на 429/5xx и транзиентные таймауты.

    Общая реализация для GET и POST: правила ретрая, backoff и уважение
    Retry-After должны быть одинаковыми, а раньше это были две копии, и они
    разъехались — в POST потолка на паузу из Retry-After не было вовсе.

    send — уже связанный с адресом и телом вызов клиента (get или post):
    так правила ретрая живут в одном месте, а способ отправки — в своём.

    Пауза между попытками растёт экспоненциально: retry_delay,
    retry_delay × backoff_factor, ×backoff_factor²… — не выше max_delay.
    Прежняя постоянная задержка долбила молчащий индексатор с одинаковой
    частотой и не давала ему оправиться. timeout — пер-запросный таймаут
    (None = по умолчанию клиента): для «не смеющего тормозить тик» вызовов
    (энтропия мастерчейна).

    429 (квота провайдера — реальность free tier TonAPI/Toncenter): респект
    заголовка Retry-After (сек до абсолютной даты), иначе обычный backoff —
    долбить в отказ после ответа «подожди» значит ловить бан ключа.
    """
    last_exc: Exception | None = None
    host = _host_of(url)
    total_attempts = 1 + max_retries
    for attempt in range(total_attempts):
        try:
            response = await _send_attempt(send, timeout, host)
            if response.status_code == 429 and attempt < max_retries:
                inc("http_retries_total", {"host": host, "reason": "429"})
                delay = _throttle_delay(
                    response,
                    attempt,
                    retry_delay=retry_delay,
                    backoff_factor=backoff_factor,
                    max_delay=max_delay,
                )
                _log_retry("429", url, attempt, total_attempts, delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code < 500 or attempt == max_retries:
                return response
            inc("http_retries_total", {"host": host, "reason": str(response.status_code)})
            delay = _backoff_delay(attempt, retry_delay, backoff_factor, max_delay)
            _log_retry(str(response.status_code), url, attempt, total_attempts, delay)
            await asyncio.sleep(delay)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            inc("http_requests_total", {"host": host, "status": "error"})
            inc("http_retries_total", {"host": host, "reason": "transport"})
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = _backoff_delay(attempt, retry_delay, backoff_factor, max_delay)
            _log_retry(f"ошибка {exc}", url, attempt, total_attempts, delay)
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


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

    Общие правила ретрая — в _request_with_retry.
    """

    async def send(**kwargs) -> httpx.Response:
        return await client.get(url, params=params, headers=headers, **kwargs)

    return await _request_with_retry(
        send,
        url,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )


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
    """HTTP POST с теми же правилами ретрая, что GET (см. _request_with_retry).

    Нужен HTTP-каналу отправки (ton_pay): подписанное внешнее сообщение
    казначея вещается через Toncenter v2 jsonRPC sendBoc, когда ADNL/TCP до
    лайтсерверов закрыт окружением. Тот же backoff на 429/5xx и транзиентные
    сбои — «отправить и забыть» тут недопустимо: потерянный ответ равноценен
    двойной отправке, если сообщение уже ушло штатно.
    """

    async def send(**kwargs) -> httpx.Response:
        return await client.post(url, json=json, headers=headers, **kwargs)

    return await _request_with_retry(
        send,
        url,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )


def _host_of(url: str) -> str:
    """Хост URL для лейбла метрики (без пути и query: там может быть ключ)."""
    if "//" in url:
        return url.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0][:64]
    return url[:64]


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
