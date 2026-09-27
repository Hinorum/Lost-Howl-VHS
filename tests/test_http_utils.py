"""HTTP-утилиты: ретраи, backoff, Retry-After, общий пул соединений.

Ретраи — единственная защита watcher'а, сверки и HTTP-канала отправки от
транзиентных сбоев индексаторов. Их семантика (когда повтор, когда отдать
ответ хранителю, сколько ждать) держится на трёх рубежах: 429 с Retry-After,
5xx и транспортные исключения. Здесь всё это зафиксировано явно: правило
«не долбить отказ» проверяется на задержках, а не на глаз.

Живой сети нет: клиент — скрипт ответов, sleep перехвачен (проверяем сами
паузы вместо реального ожидания).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app import http_utils


class _Resp:
    """Ответ как у httpx: статус, заголовки, тело."""

    def __init__(self, status_code: int = 200, *, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        return {"ok": True}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Клиент-скрипт: по очереди отдаёт ответы или бросает исключения."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    def _next(self, verb: str, url: str, kwargs: dict):
        self.calls.append({"verb": verb, "url": url, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url: str, **kwargs):
        return self._next("get", url, kwargs)

    async def post(self, url: str, **kwargs):
        return self._next("post", url, kwargs)


@pytest.fixture()
def sleeps(monkeypatch) -> list[float]:
    """Перехватывает asyncio.sleep: накопленные паузы вместо ожидания."""
    captured: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        captured.append(seconds)

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    return captured


# ---------- Успешный путь: повторов быть не должно ----------


async def test_get_returns_first_success_without_retry(sleeps) -> None:
    client = _Client([_Resp(200)])
    response = await http_utils.http_get_with_retry(client, "http://x/api")
    assert response.status_code == 200
    assert len(client.calls) == 1, "2xx не ретраится — провайдер ответил"
    assert sleeps == []


async def test_get_forwards_params_headers_and_omits_default_timeout(sleeps) -> None:
    client = _Client([_Resp(200)])
    await http_utils.http_get_with_retry(
        client, "http://x/api", params={"limit": 100}, headers={"X-API-Key": "k"}
    )
    call = client.calls[0]
    assert call["params"] == {"limit": 100}
    assert call["headers"] == {"X-API-Key": "k"}
    assert "timeout" not in call, "таймаут клиента не переопределяем молча"


async def test_get_uses_per_request_timeout_when_given(sleeps) -> None:
    client = _Client([_Resp(200)])
    await http_utils.http_get_with_retry(client, "http://x/api", timeout=8.0)
    assert client.calls[0]["timeout"] == 8.0


# ---------- 5xx: экспоненциальный backoff ----------


async def test_get_retries_5xx_then_succeeds(sleeps) -> None:
    client = _Client([_Resp(503), _Resp(502), _Resp(200)])
    response = await http_utils.http_get_with_retry(
        client, "http://x/api", max_retries=2, retry_delay=1.0, backoff_factor=2.0
    )
    assert response.status_code == 200
    assert len(client.calls) == 3
    assert sleeps == [1.0, 2.0], "пауза растёт: retry_delay × backoff^попытка"


async def test_get_hands_over_last_5xx_when_retries_exhausted(sleeps) -> None:
    """Исчерпали попытки — отдаём ответ провайдера как есть.

    Ошибку 5xx должен увидеть вызывающий (он решает: фолбэк на второй
    провайдер или «не знаю»), а не превращать её в исключение: оба
    индексатора молчат — это штатная картина free tier.
    """
    client = _Client([_Resp(500), _Resp(500)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert response.status_code == 500
    assert len(client.calls) == 2
    assert sleeps == [1.0]


async def test_retry_delay_is_capped_by_max_delay(sleeps) -> None:
    client = _Client([_Resp(500), _Resp(200)])
    await http_utils.http_get_with_retry(
        client, "http://x/api", max_retries=1, retry_delay=100.0, max_delay=30.0
    )
    assert sleeps == [30.0], "потолок max_delay держит минутный цикл живым"


# ---------- 429: уважаем Retry-After, иначе ловим бан ключа ----------


async def test_429_retries_after_seconds_from_header(sleeps) -> None:
    client = _Client([_Resp(429, headers={"Retry-After": "2"}), _Resp(200)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert response.status_code == 200
    assert sleeps == [2.0], "провайдер сказал «ждать 2с» — ждём 2с, а не свой backoff"


async def test_429_retries_after_http_date(sleeps) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    client = _Client([_Resp(429, headers={"Retry-After": when}), _Resp(200)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert response.status_code == 200
    assert len(sleeps) == 1
    assert 25.0 <= sleeps[0] <= 30.0, f"HTTP-дата разобрана как ожидание: {sleeps[0]}"


async def test_429_with_past_date_retries_immediately(sleeps) -> None:
    when = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)
    client = _Client([_Resp(429, headers={"Retry-After": when}), _Resp(200)])
    await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert sleeps == [0.0], "дата в прошлом = повтор немедленно, а не долгий сон"


async def test_429_with_garbage_header_falls_back_to_backoff(sleeps) -> None:
    client = _Client([_Resp(429, headers={"Retry-After": "завтра"}), _Resp(200)])
    await http_utils.http_get_with_retry(
        client, "http://x/api", max_retries=1, retry_delay=1.0
    )
    assert sleeps == [1.0], "мусор в заголовке → обычный backoff, не исключение"


async def test_429_retry_after_is_capped_by_max_delay(sleeps) -> None:
    """Провайдер просит час — минутный цикл так не живёт."""
    client = _Client([_Resp(429, headers={"Retry-After": "3600"}), _Resp(200)])
    await http_utils.http_get_with_retry(
        client, "http://x/api", max_retries=1, max_delay=30.0
    )
    assert sleeps == [30.0]


async def test_429_on_last_attempt_is_returned_not_retried(sleeps) -> None:
    client = _Client([_Resp(429, headers={"Retry-After": "5"}), _Resp(429)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert response.status_code == 429
    assert len(client.calls) == 2
    assert sleeps == [5.0], "после последней попытки повторов нет"


# ---------- Транспортные сбои ----------


async def test_transport_error_is_retried_then_raised(sleeps) -> None:
    client = _Client([httpx.ConnectError("boom"), httpx.ConnectError("boom again")])
    with pytest.raises(httpx.ConnectError):
        await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert len(client.calls) == 2
    assert sleeps == [1.0]


async def test_transport_error_then_success_recovers(sleeps) -> None:
    client = _Client([httpx.ReadTimeout("slow"), _Resp(200)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=1)
    assert response.status_code == 200


async def test_http_error_status_is_not_retried(sleeps) -> None:
    """404 — ответ провайдера, а не сбой: повтор только тратит квоту."""
    client = _Client([_Resp(404)])
    response = await http_utils.http_get_with_retry(client, "http://x/api", max_retries=3)
    assert response.status_code == 404
    assert len(client.calls) == 1
    assert sleeps == []


# ---------- POST: те же правила (канал отправки) ----------


async def test_post_retries_5xx_then_succeeds(sleeps) -> None:
    client = _Client([_Resp(500), _Resp(200)])
    response = await http_utils.http_post_with_retry(
        client, "http://x/sendBoc", json={"method": "sendBoc"}, max_retries=1
    )
    assert response.status_code == 200
    assert client.calls[0]["verb"] == "post"
    assert client.calls[0]["json"] == {"method": "sendBoc"}
    assert sleeps == [1.0]


async def test_post_respects_429_retry_after(sleeps) -> None:
    client = _Client([_Resp(429, headers={"Retry-After": "3"}), _Resp(200)])
    response = await http_utils.http_post_with_retry(client, "http://x/sendBoc", max_retries=1)
    assert response.status_code == 200
    assert sleeps == [3.0]


async def test_post_hands_over_last_429_and_5xx(sleeps) -> None:
    client = _Client([_Resp(429), _Resp(503)])
    response = await http_utils.http_post_with_retry(client, "http://x/sendBoc", max_retries=1)
    assert response.status_code == 503
    assert len(client.calls) == 2


async def test_post_transport_error_is_retried_then_raised(sleeps) -> None:
    client = _Client([httpx.ReadError("half-boc"), httpx.ReadError("half-boc")])
    with pytest.raises(httpx.ReadError):
        await http_utils.http_post_with_retry(client, "http://x/sendBoc", max_retries=1)
    assert sleeps == [1.0]


async def test_post_forwards_per_request_timeout(sleeps) -> None:
    client = _Client([_Resp(200)])
    await http_utils.http_post_with_retry(client, "http://x/sendBoc", timeout=5.0)
    assert client.calls[0]["timeout"] == 5.0


# ---------- Разбор Retry-After ----------


def test_retry_after_delay_parsing() -> None:
    assert http_utils._retry_after_delay(_Resp(200)) is None
    assert http_utils._retry_after_delay(_Resp(429, headers={"Retry-After": ""})) is None
    assert http_utils._retry_after_delay(_Resp(429, headers={"Retry-After": " 7 "})) == 7.0
    assert http_utils._retry_after_delay(_Resp(429, headers={"Retry-After": "завтра"})) is None
    past = _Resp(429, headers={"Retry-After": format_datetime(
        datetime.now(UTC) - timedelta(hours=1), usegmt=True)})
    assert http_utils._retry_after_delay(past) == 0.0
    naive = _Resp(429, headers={"Retry-After": format_datetime(
        datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=5))})
    assert 0.0 <= (http_utils._retry_after_delay(naive) or 0.0) <= 6.0, "наивная дата = UTC"


# ---------- Общий клиент ----------


async def test_shared_client_is_reused_and_closed(monkeypatch) -> None:
    # Глобальный клиент — общий на процесс, а pytest-asyncio даёт каждому тесту
    # свой event loop: клиент, поднятый в чужом цикле, закрывать нельзя
    # («Event loop is closed»). Поэтому тест начинает с чистого состояния и
    # monkeypatch возвращает как было — чужой клиент не трогаем.
    monkeypatch.setattr(http_utils, "_HTTP_CLIENT", None)
    first = http_utils.get_http_client()
    second = http_utils.get_http_client()
    assert first is second, "пул соединений один на процесс"
    assert first.is_closed is False
    await http_utils.close_http_client()
    assert first.is_closed is True
    assert http_utils._HTTP_CLIENT is None, "после закрытия клиент забыт"
    third = http_utils.get_http_client()
    assert third is not first, "после закрытия поднимается новый клиент"
    await http_utils.close_http_client()


async def test_close_http_client_is_noop_without_client(monkeypatch) -> None:
    """Гасить нечего — не падаем: выключение процесса не должно шуметь ошибкой."""
    monkeypatch.setattr(http_utils, "_HTTP_CLIENT", None)
    await http_utils.close_http_client()
    assert http_utils._HTTP_CLIENT is None
