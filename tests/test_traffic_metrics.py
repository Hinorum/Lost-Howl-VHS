"""Учёт трафика: сколько байт уходит наружу и сколько запросов идёт в БД.

Инцидент 2026-11: бот съел 15 ГБ трафика Render за 12 суток при лимите
5 ГБ/мес, и по логам это было не видно — счётчиков исходящих байтов не было
вообще. Эти тесты фиксируют сам счёт: без него тот же класс инцидента
следующий раз обнаружится только по счёту Render.

Что считаем:
* http_response_bytes_total — тела ответов внешних API (TonAPI, Toncenter,
  API Render, LLM): это и есть исходящий трафик инстанса;
* db_queries_total — запросы к БД: база живёт за пределами Render, поэтому
  каждый ответ СУБД тоже уходит с инстанца и попадает в лимит.
"""

from __future__ import annotations

import httpx
import pytest

from app import metrics as metrics_mod
from app.http_utils import _host_of, _response_bytes, http_get_with_retry


@pytest.fixture(autouse=True)
def _clean_counters():
    metrics_mod.reset()
    yield
    metrics_mod.reset()


class _Transport(httpx.AsyncBaseTransport):
    """Отдаёт заранее заданный ответ и считает, сколько раз его отдали."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, content=self.body)


async def test_http_bytes_are_counted_per_host() -> None:
    """Тело ответа external API — исходящий трафик: он считается по хосту."""
    transport = _Transport(b"x" * 1234)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await http_get_with_retry(
            client, "https://tonapi.io/v2/accounts/abc", max_retries=0
        )
    assert response.status_code == 200
    text = metrics_mod.render()
    assert 'way_http_response_bytes_total{host="tonapi.io"} 1234' in text


async def test_http_bytes_count_each_attempt() -> None:
    """Ретраи — тоже трафик: счётчик не должен терять повторные попытки."""
    transport = _Transport(b"y" * 10)
    async with httpx.AsyncClient(transport=transport) as client:
        await http_get_with_retry(
            client, "https://toncenter.com/api/v3/transactions", max_retries=1
        )
    assert transport.calls == 1
    assert 'way_http_response_bytes_total{host="toncenter.com"} 10' in metrics_mod.render()


def test_response_bytes_of_unread_stream_is_zero() -> None:
    """Потоковый ответ без прочитанного тела: считать нечего — ноль, не ошибка."""
    response = httpx.Response(200, stream=httpx.ByteStream(b"z" * 5))
    assert _response_bytes(response) == 0


def test_host_label_strips_path_and_query() -> None:
    """Лейбл хоста без пути и query: в query может быть ключ провайдера."""
    assert _host_of("https://tonapi.io/v2/accounts?api_key=secret") == "tonapi.io"


async def test_db_queries_are_counted() -> None:
    """Каждый запрос к БД — это ответ, который уходит с инстанса наружу."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import WatcherState

    # Слушатель ставится на старте процесса (app/main.py) — в тесте ставим сами.
    metrics_mod.install_db_query_counter()
    async with SessionLocal() as session:
        await session.execute(select(WatcherState).limit(1))
    text = metrics_mod.render()
    assert "way_db_queries_total" in text
    assert 'kind="SELECT"' in text
