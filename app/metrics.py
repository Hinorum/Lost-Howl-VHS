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
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "way_"

# семейство -> ("counter"|"gauge", описание)
_HELP: dict[str, tuple[str, str]] = {
    # Память процесса
    "job_runs_total": ("counter", "Запуски фоновых задач: успешных и упавших"),
    "job_duration_seconds": (
        "gauge",
        "Длительность последнего запуска фоновой задачи, секунды",
    ),
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
    inc("job_runs_total", {"job": job, "result": result})
    set_gauge("job_duration_seconds", {"job": job}, time.monotonic() - started)


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
    for name, value in sorted(_db_gauges(snapshot).items()):
        if value is None:
            continue
        lines.extend(_family(name))
        lines.append(f"{_PREFIX}{name} {_number(value)}")
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
    }


def reset() -> None:
    """Обнулить счётчики: тесты и пересоздание модуля при горячей правке."""
    _COUNTERS.clear()
    _GAUGES.clear()
