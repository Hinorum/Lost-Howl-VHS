"""Метрики /metrics: счётчики фоновых задач в формате Prometheus.

Главная проверка — не формат, а честность: ошибка, проглоченная самим тиком,
обязана попасть в метрики как ошибка (а не как успешный запуск), а
недоступный снимок БД — как `way_snapshot_up 0`, а не как 500 и не как ноль.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main as main_module
from app import metrics as metrics_mod
from app.handlers import bootstrap as bootstrap_mod
from app.handlers import handle_update_error
from app.scheduler import _alert_guarded


@pytest.fixture(autouse=True)
def _clean_counters():
    metrics_mod.reset()
    yield
    metrics_mod.reset()


def _request(headers: dict | None = None, query: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        headers={} if headers is None else headers,
        query={} if query is None else query,
    )


# ---------- формат ----------


def test_counter_renders_prometheus_family_with_labels() -> None:
    metrics_mod.inc("job_runs_total", {"job": "ops-sweep", "result": "ok"})
    metrics_mod.inc("job_runs_total", {"job": "ops-sweep", "result": "ok"})
    text = metrics_mod.render()
    assert "# TYPE way_job_runs_total counter" in text
    assert 'way_job_runs_total{job="ops-sweep",result="ok"} 2' in text


def test_counter_without_labels_renders_bare_name() -> None:
    metrics_mod.inc("update_errors_total")
    assert "way_update_errors_total 1" in metrics_mod.render()


def test_label_values_are_escaped() -> None:
    """Строки извне (тип апдейта, текст ошибки) не должны ломать разбор."""
    metrics_mod.inc("update_errors_total", {"kind": 'mess"age\\'})

    line = next(
        row for row in metrics_mod.render().splitlines() if "update_errors_total{" in row
    )
    assert line == 'way_update_errors_total{kind="mess\\"age\\\\"} 1'


def test_fractions_are_kept_in_duration_gauge() -> None:
    metrics_mod.set_gauge("job_duration_seconds", {"job": "way-tick"}, 1.23456)
    assert 'way_job_duration_seconds{job="way-tick"} 1.2346' in metrics_mod.render()


# ---------- учёт джоб ----------


async def test_timed_counts_ok_and_keeps_duration() -> None:
    async with metrics_mod.timed("ton-settle"):
        pass
    text = metrics_mod.render()
    assert 'way_job_runs_total{job="ton-settle",result="ok"} 1' in text
    assert 'way_job_duration_seconds{job="ton-settle"}' in text


async def test_timed_counts_error_and_reraises() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with metrics_mod.timed("treasury-mirror"):
            raise RuntimeError("boom")
    assert (
        'way_job_runs_total{job="treasury-mirror",result="error"} 1' in metrics_mod.render()
    )


async def test_timed_marks_swallowed_failure_via_span() -> None:
    """Джоба ловит сбой сама (tick) и не бросает наружу — без span.fail() она
    была бы засчитана как успешный запуск, то есть ровно то враньё, которое
    чинили в срезе 1."""

    async def job() -> None:
        async with metrics_mod.timed("way-tick") as span:
            span.fail()

    await job()
    assert 'way_job_runs_total{job="way-tick",result="error"} 1' in metrics_mod.render()


async def test_alert_guarded_measures_every_background_job() -> None:
    """Точка учёта одна для всех джоб планировщика: забыть отдельную обёртку
    невозможно, а «тонкий» расписание с max_instances=1 видно в метриках."""

    async def ok() -> None:
        pass

    async def boom() -> None:
        raise ValueError("db gone")

    await _alert_guarded("db-backup", ok)
    await _alert_guarded("db-backup", boom)
    text = metrics_mod.render()
    assert 'way_job_runs_total{job="db-backup",result="ok"} 1' in text
    assert 'way_job_runs_total{job="db-backup",result="error"} 1' in text


async def test_tick_body_runs_inside_timer(monkeypatch) -> None:
    from app import scheduler as sched

    monkeypatch.setattr(sched, "_tick_body", AsyncMock())
    await sched.tick()
    assert 'way_job_runs_total{job="way-tick",result="ok"} 1' in metrics_mod.render()


async def test_update_error_kind_is_counted(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_mod, "_LAST_UPDATE_ERROR_ALERT", {"ts": 0.0})
    update = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=1, type="private")),
        callback_query=None,
        from_user=SimpleNamespace(id=2),
        update_id=3,
    )
    await handle_update_error(
        AsyncMock(), SimpleNamespace(update=update, exception=ValueError("boom"))
    )
    assert 'way_update_errors_total{kind="message"} 1' in metrics_mod.render()


# ---------- gauge из снимка ----------


def test_operational_gauges_come_from_snapshot() -> None:
    text = metrics_mod.render(
        {
            "uptime_seconds": 12,
            "last_tick_age": 1.5,
            "tick_failures": 2,
            "payout_queue": 4,
            "dead_letter_payouts": 1,
            "pending_stakes": 0,
            "problems": ["a", "b"],
            "problems_age": 3.0,
        }
    )
    assert "way_snapshot_up 1" in text
    assert "way_payout_queue 4" in text
    assert "way_payout_dead 1" in text
    assert "way_problems 2" in text
    assert "way_problems_age_seconds 3" in text
    assert "way_last_tick_age_seconds 1.5" in text


def test_absent_values_are_skipped_not_zeroed() -> None:
    """watcher_beat_age=None (бития ещё не было) — метрики нет вовсе, а не
    ноль: ноль означал бы «битие только что», которого не было."""
    text = metrics_mod.render({"problems": None, "watcher_beat_age": None})
    assert "way_watcher_beat_age_seconds" not in text
    assert "way_problems 0" in text


def test_render_without_snapshot_marks_snapshot_down() -> None:
    text = metrics_mod.render()
    assert "way_snapshot_up 0" in text


# ---------- эндпоинт ----------


async def test_metrics_endpoint_serves_text(monkeypatch) -> None:
    async def good_snapshot():
        return {"status": "ok", "payout_queue": 1}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    metrics_mod.inc("job_runs_total", {"job": "ops-sweep", "result": "ok"})
    response = await main_module.metrics(_request())
    assert response.status == 200
    body = response.text
    assert 'way_job_runs_total{job="ops-sweep",result="ok"} 1' in body
    assert "way_payout_queue 1" in body
    assert "version=0.0.4" in response.headers["Content-Type"]


async def test_metrics_endpoint_survives_snapshot_failure(monkeypatch) -> None:
    """Сборщик предпочитает частичные данные 500-ке: процесс жив, снимок
    недоступен — это way_snapshot_up 0, а счётчики памяти всё равно полезны."""

    async def broken_snapshot():
        raise RuntimeError("cached statement plan is invalid")

    monkeypatch.setattr("app.ops.snapshot", broken_snapshot)
    metrics_mod.inc("update_errors_total")
    response = await main_module.metrics(_request())
    assert response.status == 200
    assert "way_snapshot_up 0" in response.text
    assert "way_update_errors_total 1" in response.text


async def test_metrics_endpoint_requires_token(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")

    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    assert (await main_module.metrics(_request())).status == 401
    assert (
        await main_module.metrics(_request(headers={"Authorization": "Bearer s3cret"}))
    ).status == 200
    assert (await main_module.metrics(_request(query={"token": "s3cret"}))).status == 200


async def test_metrics_endpoint_locked_when_require_token_without_secret(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_require_token", True)
    monkeypatch.setattr("app.config.settings.health_token", "")
    assert (await main_module.metrics(_request())).status == 401


async def test_health_still_serves_after_auth_extraction() -> None:
    """Проверка доступа вынесена в общий _authorized — /health не должен был
    потерять ни одну из прежних мер."""
    response = await main_module.health(_request())
    assert response.status == 200
