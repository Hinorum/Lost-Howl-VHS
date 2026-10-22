"""Метрики процесса: сборка, пул БД, внешние API, длительность джоб.

Чего не было и что нужно при разборе инцидента:
- какой коммит работает (и с какого момента) — иначе по метрикам не понять,
  что за деплой сейчас стоит;
- состояние пула соединений БД — «too many clients» без него неразбираем;
- обращения к внешним API и повторы по хостам — весь контур построен вокруг
  квот TonAPI/Toncenter, а счётчиков не было;
- сумма и счётчик длительностей джоб — по «последнему запуску» нельзя
  посчитать среднее и увидеть «джоба стала в десять раз медленнее».
"""

from __future__ import annotations

import pytest

from app import metrics
from app.http_utils import http_get_with_retry


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_build_info_is_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Метрика сборки: коммит и время старта."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234")

    text = metrics.render()

    assert "way_build_info" in text
    assert 'commit="abc1234"' in text
    assert "started_at=" in text


def test_pool_gauges_are_exported() -> None:
    """Состояние пула БД видно в метриках."""
    text = metrics.render()

    assert "way_db_pool_checkedout" in text
    assert "way_db_pool_size" in text


def test_job_duration_sum_and_count() -> None:
    """Сумма и счётчик длительностей — по ним считается среднее."""
    metrics.inc("job_duration_seconds_sum", {"job": "ops-sweep"}, 3.0)
    metrics.inc("job_duration_seconds_sum", {"job": "ops-sweep"}, 5.0)
    metrics.inc("job_duration_seconds_count", {"job": "ops-sweep"})
    metrics.inc("job_duration_seconds_count", {"job": "ops-sweep"})

    text = metrics.render()

    assert 'way_job_duration_seconds_sum{job="ops-sweep"} 8' in text
    assert 'way_job_duration_seconds_count{job="ops-sweep"} 2' in text


async def test_http_requests_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обращения к внешнему API считаются по хосту и коду ответа."""
    calls: list[str] = []

    class _Response:
        status_code = 200
        headers: dict[str, str] = {}

    class _Client:
        async def get(self, url, **kwargs):
            calls.append(url)
            return _Response()

    await http_get_with_retry(_Client(), "https://tonapi.io/v2/accounts?key=secret")

    text = metrics.render()
    assert 'way_http_requests_total{host="tonapi.io",status="200"} 1' in text


async def test_http_retries_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторы (429 и 5xx) считаются — квоты провайдеров видно по метрикам."""
    class _Response:
        status_code = 429
        headers = {"Retry-After": "0"}

    class _Client:
        async def get(self, url, **kwargs):
            return _Response()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.http_utils.asyncio.sleep", no_sleep)

    await http_get_with_retry(_Client(), "https://toncenter.com/api", max_retries=1)

    text = metrics.render()
    assert 'way_http_retries_total{host="toncenter.com",reason="429"} 1' in text
