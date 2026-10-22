"""Счётчики процесса в текстовом формате Prometheus — без новых зависимостей.

prometheus_client в requirements нет и не нужен ради шести чисел: формат
текстовый, сборщик (UptimeRobot, Grafana Cloud, любой Prometheus) забирает
его без агента. Модуль сознательно крошечный и без состояния вне памяти.

Что измеряем и почему именно это:

- фоновые джобы: сколько запусков, сколько провалов, сколько секунд занял
  последний запуск. До этого цифр не было ни одной, а при `max_instances=1`
  долгий ton-settle или treasury-mirror ТИХО съедал свои циклы — узнать об
  этом было нечем, кроме жалобы игрока «день не открылся»;
- сбои обработки апдейтов по типу (callback / message / update);
- операционные gauge из БД — те же числа, что в /health, но в формате, о
  котором можно построить график и алерт, а не читать глазами JSON.

Всё обнуляется на рестарте, как счётчики без persistence: нужны форма,
текущие значения и относительные изменения, а историю за месяц даёт БД через
`/ops` и `/treasury`. Один event loop, блокировок не требуется.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "way_"

# Время старта процесса — для метрики сборки.
PROCESS_STARTED_AT = datetime.now(UTC).isoformat(timespec="seconds")

# семейство -> ("counter"|"gauge", описание)
_HELP: dict[str, tuple[str, str]] = {
    # Память процесса
    "job_runs_total": ("counter", "Запуски фоновых задач: успешных и упавших"),
    "job_duration_seconds": (
        "gauge",
        "Длительность последнего запуска фоновой задачи, секунды",
    ),
    "job_duration_seconds_sum": (
        "counter",
        "Сумма длительностей запусков фоновых задач, секунды",
    ),
    "job_duration_seconds_count": (
        "counter",
        "Сколько запусков фоновых задач учтено в сумме длительностей",
    ),
    "http_requests_total": (
        "counter",
        "Обращения к внешним API по хосту и коду ответа",
    ),
    "http_retries_total": (
        "counter",
        "Повторы обращений к внешним API по хосту и причине",
    ),
    "db_pool_checkedout": ("gauge", "Соединений БД, взятых из пула"),
    "db_pool_size": ("gauge", "Размер пула соединений БД"),
    "db_pool_overflow": ("gauge", "Разрешено соединений БД сверх пула"),
    "build_info": ("gauge", "Сборка процесса: коммит и время старта"),
    "update_errors_total": ("counter", "Сбои обработки апдейтов по типу"),
    "announce_no_audience_total": (
        "counter",
        "Анонсы нового дня без единого получателя (нет чатов и нет подписчиков лички)",
    ),
    "announce_no_bot_total": (
        "counter",
        "Анонсы нового дня, пропущенные из-за неустановленного бота (set_bot не вызван)",
    ),
    # Операционные gauge из снимка БД
    "snapshot_up": ("gauge", "Снимок операционного состояния собран (1/0)"),
    "uptime_seconds": ("gauge", "Аптайм процесса, секунды"),
    "last_tick_age_seconds": ("gauge", "Возраст бития главного тика, секунды"),
    "tick_failures": ("gauge", "Падений главного тика подряд"),
    "watcher_beat_age_seconds": ("gauge", "Возраст сердцебиения TON-watcher'а, секунды"),
    "payout_queue": ("gauge", "Выплат в очереди (pending + sending)"),
    "payout_dead": ("gauge", "Безнадёжных выплат (failed после всех ретраев)"),
    "pending_stakes": ("gauge", "Переводов-ставок не обработано"),
    "oldest_payout_age_seconds": (
        "gauge",
        "Возраст самой старой выплаты в очереди, секунды",
    ),
    "problems": ("gauge", "Текущих тревог по снимку check_anomalies"),
    "problems_age_seconds": ("gauge", "Возраст снимка тревог, секунды"),
    "log_scrub_failures": (
        "gauge",
        "Сбоев краски секретов в логах (ненулевое значение — запись могла унести мнемонику)",
    ),
}

# Счётчики монотонные: семейство -> {лейблы: значение}
_COUNTERS: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
# Датчики с лейблами (длительности по джобам)
_GAUGES: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}


def inc(name: str, labels: dict[str, str] | None = None, amount: float = 1) -> None:
    """Счётчик: только растёт (запуски, сбои, отказы)."""
    if name not in _HELP:
        logger.warning("Метрика без описания: %s", name)
    samples = _COUNTERS.setdefault(name, {})
    key = tuple(sorted((labels or {}).items()))
    samples[key] = samples.get(key, 0.0) + amount


def set_gauge(name: str, labels: dict[str, str] | None, value: float) -> None:
    """Датчик: может расти и падать (длительность последнего запуска)."""
    if name not in _HELP:
        logger.warning("Метрика без описания: %s", name)
    samples = _GAUGES.setdefault(name, {})
    samples[tuple(sorted((labels or {}).items()))] = float(value)


class _Span:
    """Ручной режим для джобы, которая ловит сбой сама (tick): проглоченное
    исключение иначе будет учтено как успешный запуск."""

    def __init__(self) -> None:
        self.failed = False

    def fail(self) -> None:
        self.failed = True


@asynccontextmanager
async def timed(job: str):
    """Оборачивает фоновую джобу: результат и длительность последнего запуска.

    Исключение пробрасывается дальше (смысл обёртки — измерить, а не скрыть),
    результат при этом уже учтён.
    """
    span = _Span()
    started = time.monotonic()
    try:
        yield span
    except BaseException:
        _record_job(job, "error", started)
        raise
    else:
        _record_job(job, "error" if span.failed else "ok", started)


def _record_job(job: str, result: str, started: float) -> None:
    elapsed = time.monotonic() - started
    inc("job_runs_total", {"job": job, "result": result})
    set_gauge("job_duration_seconds", {"job": job}, elapsed)
    # Сумма и счётчик рядом с «длительностью последнего запуска»: по одному
    # последнему значению нельзя посчитать среднее и увидеть «джоба стала в
    # десять раз медленнее» — а это ровно тот сигнал, ради которого нужны
    # метрики.
    inc("job_duration_seconds_sum", {"job": job}, elapsed)
    inc("job_duration_seconds_count", {"job": job})


def _build_commit() -> str:
    """Коммит работающей сборки (Render отдаёт RENDER_GIT_COMMIT)."""
    import os

    return (os.environ.get("RENDER_GIT_COMMIT") or "unknown").strip() or "unknown"


def _db_pool_gauges() -> dict[str, float | None]:
    """Состояние пула соединений БД.

    Раньше его не было видно, а именно он объясняет «too many clients»:
    занятые слоты пула и лимит — первое, что нужно знать при разборе.
    """
    try:
        from app.db import engine

        pool = engine.pool
        checkedout = getattr(pool, "checkedout", None)
        size = getattr(pool, "size", None)
        overflow = getattr(pool, "overflow", None)
        overflow_value = overflow() if callable(overflow) else None
        return {
            "db_pool_checkedout": float(checkedout()) if callable(checkedout) else None,
            "db_pool_size": float(size()) if callable(size) else None,
            # Отрицательное значение — «лимита нет» (так отдаёт StaticPool).
            "db_pool_overflow": (
                float(overflow_value)
                if isinstance(overflow_value, (int, float)) and overflow_value >= 0
                else None
            ),
        }
    except Exception:
        return {"db_pool_checkedout": None, "db_pool_size": None, "db_pool_overflow": None}


def _escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{key}="{_escape(str(value))}"' for key, value in pairs) + "}"


def _number(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}"


def _family(name: str) -> list[str]:
    kind, doc = _HELP.get(name, ("untyped", ""))
    return [f"# HELP {_PREFIX}{name} {doc}".rstrip(), f"# TYPE {_PREFIX}{name} {kind}"]


def render(snapshot: dict[str, Any] | None = None) -> str:
    """Текст метрик для /metrics. snapshot=None — только память процесса.

    Значения None (например, возраст бития, которого ещё нет) пропускаются:
    отсутствие метрики честнее выдуманного нуля.
    """
    lines: list[str] = []
    for name, samples in sorted(_COUNTERS.items()):
        lines.extend(_family(name))
        for labels, value in sorted(samples.items()):
            lines.append(f"{_PREFIX}{name}{_labels(labels)} {_number(value)}")
    for name, samples in sorted(_GAUGES.items()):
        lines.extend(_family(name))
        for labels, value in sorted(samples.items()):
            lines.append(f"{_PREFIX}{name}{_labels(labels)} {_number(value)}")
    for name, gauge in sorted(_db_gauges(snapshot).items()):
        if gauge is None:
            continue
        lines.extend(_family(name))
        lines.append(f"{_PREFIX}{name} {_number(gauge)}")
    for name, gauge in sorted(_db_pool_gauges().items()):
        if gauge is None:
            continue
        lines.extend(_family(name))
        lines.append(f"{_PREFIX}{name} {_number(gauge)}")
    # Сборка: какой коммит работает и с какого момента. Без этого по метрикам
    # нельзя понять, что за деплой сейчас стоит.
    lines.extend(_family("build_info"))
    build_labels = (
        f'commit="{_escape(_build_commit())}",'
        f'started_at="{_escape(PROCESS_STARTED_AT)}"'
    )
    lines.append(f"{_PREFIX}build_info{{{build_labels}}} 1")
    return "\n".join(lines) + "\n"


def _db_gauges(snapshot: dict[str, Any] | None) -> dict[str, float | None]:
    if snapshot is None:
        # Снимок не собрался: счётчики процесса всё равно полезны сборщику.
        return {"snapshot_up": 0.0}
    return {
        "snapshot_up": 1.0,
        "uptime_seconds": snapshot.get("uptime_seconds"),
        "last_tick_age_seconds": snapshot.get("last_tick_age"),
        "tick_failures": snapshot.get("tick_failures"),
        "watcher_beat_age_seconds": snapshot.get("watcher_beat_age"),
        "payout_queue": snapshot.get("payout_queue"),
        "payout_dead": snapshot.get("dead_letter_payouts"),
        "pending_stakes": snapshot.get("pending_stakes"),
        "oldest_payout_age_seconds": snapshot.get("oldest_payout_age"),
        "problems": len(snapshot.get("problems") or []),
        "problems_age_seconds": snapshot.get("problems_age"),
        "log_scrub_failures": snapshot.get("log_scrub_failures"),
    }


def reset() -> None:
    """Обнулить счётчики: тесты и пересоздание модуля при горячей правке."""
    _COUNTERS.clear()
    _GAUGES.clear()
