"""Проверка собственных логов Render на секреты.

Зачем: пункт чек-листа mainnet «мнемоника mainnet-кошелька НЕ появляется
в логах» требовал ручного `grep -ri mnemonic logs/` по странице логов
Render. Бот умеет ходить в свой же хостинг по API (RENDER_API_KEY) и
проверять это сам — регулярно и без участия человека.

Что проверяем: в тексте логов не должно встречаться НИ ОДНОГО секрета
из окружения — мнемоника казначея (главной и тестнет), токен бота, токен
/health, webhook-секрет, реферальный секрет, ключи API и пароль бэкапа.
Краска секретов в логах (app/log_scrub.py) — основная защита; эта проверка —
второй рубеж, который замечает, если краску отключили или она не справилась.

Чего проверка НЕ делает:
- не печатает и не хранит сами секреты: в вердикте только СЧЁТ строк по
  каждой категории и номер строки-примера (без содержимого);
- не пишет ничего в лог, кроме счётчиков: проверяемый секрет не должен
  попасть в проверяющие логи.

Ключ RENDER_API_KEY — учётные данные всего аккаунта Render, поэтому бот
пользуется им ТОЛЬКО на чтение (GET) и никогда не пишет им в логи.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.core.registry import RENDER_LOGS_CHECK_KEY
from app.http_utils import get_http_client, http_get_with_retry

logger = logging.getLogger(__name__)

_RENDER_API_BASE = "https://api.render.com/v1"

# Секреты окружения: имя категории → (где лежит, имя поля).
# Часть секретов живёт в Settings, часть — просто переменные окружения Render
# (LLM/картинки/бэкап): бот получает их в процессе целиком, и утечка в лог
# equally возможна. Секрет Render API — тоже: им бот ходит в свой хостинг.
_SECRET_SETTINGS = (
    ("treasury_mnemonic", "treasury_mnemonic"),
    ("treasury_testnet_mnemonic", "treasury_testnet_mnemonic"),
    ("bot_token", "bot_token"),
    ("health_token", "health_token"),
    ("webhook_secret", "webhook_secret"),
    ("referral_secret", "referral_secret"),
    ("ton_api_key", "ton_api_key"),
    ("toncenter_api_key", "toncenter_api_key"),
)
_SECRET_ENV = (
    ("render_api_key", "RENDER_API_KEY"),
    ("llm_api_key", "LLM_API_KEY"),
    ("gemini_api_key", "GEMINI_API_KEY"),
    ("imgbb_api_key", "IMGBB_API_KEY"),
    ("pollinations_token", "POLLINATIONS_TOKEN"),
    ("backup_passphrase", "BACKUP_PASSPHRASE"),
)


def _secrets() -> list[tuple[str, str]]:
    """Секреты окружения, которые ищем в логах (непустые, длиной от 8)."""
    out: list[tuple[str, str]] = []
    for label, field in _SECRET_SETTINGS:
        value = str(getattr(settings, field, "") or "").strip()
        if len(value) >= 8:
            out.append((label, value))
    for label, env_name in _SECRET_ENV:
        value = str(os.environ.get(env_name, "") or "").strip()
        if len(value) >= 8:
            out.append((label, value))
    return out


def _mnemonic_fragments(mnemonic: str, words: int = 3) -> list[str]:
    """Кусочки мнемоники: три подряд идущих слова.

    Точное совпадение всей фразы почти невозможно (лог обычно печатает часть
    строки), а вот три слова подряд — уже надёжный признак утечки.
    """
    parts = [word for word in mnemonic.replace("\n", " ").split(" ") if word]
    if len(parts) < words:
        return []
    return [" ".join(parts[index : index + words]) for index in range(len(parts) - words + 1)]


def scan_messages(messages: list[str]) -> dict[str, Any]:
    """Ищет секреты в текстах логов. Возвращает только СЧЁТЧИКИ.

    Секреты в вердикт не попадают: в него идёт имя категории, число строк и
    номер первой найденной строки (без содержимого) — иначе проверка сама
    стала бы утечкой.
    """
    hits: dict[str, int] = {}
    samples: dict[str, int] = {}
    haystacks = [message.lower() for message in messages]
    for label, secret in _secrets():
        needle = secret.lower()
        count = 0
        first = 0
        for index, text in enumerate(haystacks, 1):
            if needle in text:
                count += 1
                if not first:
                    first = index
        if count:
            hits[label] = count
            samples[label] = first
    # Мнемоника: кроме всей фразы — три слова подряд (частая утечка).
    for label, field in (("treasury_mnemonic", "treasury_mnemonic"), ("treasury_testnet_mnemonic", "treasury_testnet_mnemonic")):
        secret = str(getattr(settings, field, "") or "")
        fragments = _mnemonic_fragments(secret)
        if not fragments:
            continue
        count = 0
        first = 0
        for index, text in enumerate(haystacks, 1):
            if any(fragment.lower() in text for fragment in fragments):
                count += 1
                if not first:
                    first = index
        if count:
            # Считаем отдельно: полная фраза входит в подсчёт фрагментов.
            hits[label] = hits.get(label, 0) + count
            samples.setdefault(label, first)
    return {"hits": hits, "samples": samples, "scanned": len(messages)}


def _entry_time(raw: Any) -> datetime | None:
    """Время записи лога (RFC3339) — для постраничного обхода."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET к Render API. Только чтение — ключ никогда не уходит на запись.

    Тело ответа при ошибке попадает в сообщение: API объясняет, что именно
    ему не понравилось, — а без этого вердикт проверки был бы «400» без
    причины.
    """
    key = (settings.render_api_key or "").strip()
    if not key:
        raise RuntimeError("RENDER_API_KEY не задан")
    client = get_http_client()
    response = await http_get_with_retry(
        client,
        f"{_RENDER_API_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=30.0,
        max_retries=1,
    )
    if response.status_code >= 400:
        body = (response.text or "").strip().replace("\n", " ")[:240]
        raise RuntimeError(f"HTTP {response.status_code} от Render API: {body}")
    return response.json()


def _log_query_shapes(since: datetime) -> list[dict[str, Any]]:
    """Формы запроса логов — от строгой к простой.

    Имена параметров взяты из сгенерированного клиента Render (CLI
    render-oss/cli, репозиторий public-api-schema): `resource` (список
    resource id), `startTime`/`endTime` (date-time, RFC3339), `direction`
    (backward/forward), `limit`. Формат не документирован на сайте Render, но
    зашит в клиент — и бот узнаёт форму на живой сети: перебирает варианты и
    запоминает сработавший. Порядок отражает поведение CLI: стартовый запрос
    отдаёт не больше 100 строк, пагинация — до 1000.
    """
    stamp = since.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return [
        {"startTime": stamp, "limit": 100, "direction": "backward"},
        {"limit": 100},
        {"startTime": stamp, "limit": 1000, "direction": "backward"},
        {"limit": 1000},
    ]


async def resolve_service() -> tuple[str, str]:
    """Определяет resource id и owner id своего сервиса Render.

    Явный RENDER_SERVICE_ID выигрывает. Иначе — список сервисов: берём
    единственный web-сервис (у бота он один), при нескольких — по имени
    RENDER_SERVICE_NAME. Если определить нельзя — честная ошибка, а не
    догадка: молча проверили бы чужой сервис.

    ownerId нужен самому API: без него запрос логов отклоняется
    («ownerId is required»).
    """
    explicit = (settings.render_service_id or "").strip()
    services = await _api_get("/services")
    web: list[dict[str, Any]] = []
    for item in services or []:
        service = item.get("service") or item
        if service.get("type") == "web":
            web.append(service)
    wanted = (settings.render_service_name or "").strip()
    chosen: dict[str, Any] | None = None
    if explicit:
        for service in services or []:
            candidate = service.get("service") or service
            if str(candidate.get("id")) == explicit:
                chosen = candidate
                break
        if chosen is None:
            raise RuntimeError(f"RENDER_SERVICE_ID={explicit} не найден среди сервисов")
    elif wanted:
        for service in web:
            if service.get("name") == wanted:
                chosen = service
                break
        if chosen is None:
            raise RuntimeError(f"сервис с именем {wanted!r} не найден")
    elif len(web) == 1:
        chosen = web[0]
    elif not web:
        raise RuntimeError("у Render нет web-сервисов")
    else:
        raise RuntimeError(
            "у Render несколько web-сервисов — задай RENDER_SERVICE_ID (или RENDER_SERVICE_NAME)"
        )
    owner_id = str(chosen.get("ownerId") or "").strip()
    if not owner_id:
        raise RuntimeError("у сервиса нет ownerId — запрос логов без него не пройдёт")
    return str(chosen.get("id")), owner_id


async def fetch_logs(
    service_id: str, owner_id: str, since: datetime, limit: int
) -> tuple[list[str], dict[str, Any]]:
    """Забирает тексты логов сервиса за окно назад — постранично.

    Логи Render листаются временем (курсора у списка нет), поэтому страницы
    идут ОТ САМОГО СТАРОГО к новому: утечка при старте процесса (главный
    кандидат — мнемоника в первых же строках) живёт в начале окна, а в хвосте
    лежит шум тиков. Направление — forward.

    Возвращает (тексты, сводка): сколько строк, какое окно реально покрыто
    и доводилось ли окно до конца. Если страницы кончились, а окно — нет,
    вердикт говорит об этом прямо: молчаливая частичная проверка хуже
    явной.
    """
    last_error: Exception | None = None
    for candidate in _log_query_shapes(since):
        params: dict[str, Any] = {
            key: value for key, value in candidate.items() if value is not None
        }
        params["resource"] = service_id
        params["ownerId"] = owner_id
        params["direction"] = "forward"
        page_limit = int(params.get("limit") or 100)
        max_pages = max(1, int(settings.render_logs_max_pages))
        total_limit = max(page_limit, int(settings.render_logs_limit))
        messages: list[str] = []
        stamps: list[datetime] = []
        seen_ids: set[str] = set()
        cursor = since
        pages = 0
        try:
            while pages < max_pages and len(messages) < total_limit:
                params["startTime"] = (
                    cursor.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                )
                payload = await _api_get("/logs", params)
                entries = (
                    payload if isinstance(payload, list) else (payload or {}).get("logs") or []
                )
                pages += 1
                if not entries:
                    break
                page_stamps: list[datetime] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    message = entry.get("message")
                    if isinstance(message, str):
                        messages.append(message)
                    stamp = _entry_time(entry.get("timestamp"))
                    if stamp is not None:
                        page_stamps.append(stamp)
                if not page_stamps:
                    break
                fresh = [
                    entry
                    for entry in entries
                    if isinstance(entry, dict)
                    and str(entry.get("id") or "") not in seen_ids
                ]
                if not fresh:
                    # Страница повторилась — дальше идти бессмысленно.
                    break
                for entry in fresh:
                    seen_ids.add(str(entry.get("id") or ""))
                    stamp = _entry_time(entry.get("timestamp"))
                    if stamp is not None:
                        stamps.append(stamp)
                # Дальше идём ЗА самой свежей записью страницы: фильтр startTime
                # отсекает всё, что раньше, поэтому следующая страница — это
                # продолжение окна, а не повтор той же порции.
                cursor = max(page_stamps) + timedelta(seconds=1)
                if len(entries) < page_limit:
                    break
        except Exception as exc:
            # Форма не подошла — пробуем следующую.
            last_error = exc
            continue
        now = datetime.now(UTC)
        if stamps:
            oldest = min(stamps)
            newest = max(stamps)
            span = (now - since).total_seconds() or 1
            share = min(1.0, max(0.0, (newest - oldest).total_seconds() / span))
            summary = {
                "pages": pages,
                "oldest": oldest.isoformat(),
                "newest": newest.isoformat(),
                "covered_share": round(share, 3),
                "window_covered": share >= 0.99,
            }
        else:
            summary = {
                "pages": pages,
                "oldest": None,
                "newest": None,
                "covered_share": 0.0,
                "window_covered": True,
            }
        summary["query"] = ",".join(
            f"{key}={value}"
            for key, value in params.items()
            if key not in ("resource", "ownerId", "startTime")
        )
        return messages, summary
    raise RuntimeError(f"ни одна форма запроса логов не подошла: {last_error}")


async def check_render_logs() -> dict[str, Any]:
    """Проверяет логи Render на секреты и пишет вердикт в watcher_state.

    Вердикт: {checked_at, scanned, hits, samples, service, query, window_hours,
    error}. Значений секретов в нём нет — только счётчики.
    """
    verdict: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scanned": 0,
        "hits": {},
        "samples": {},
        "service": None,
        "query": None,
        "pages": 0,
        "oldest": None,
        "newest": None,
        "covered_share": 0.0,
        "window_covered": False,
        "window_hours": int(settings.render_logs_lookback_hours),
        "error": None,
    }
    try:
        service_id, owner_id = await resolve_service()
        verdict["service"] = service_id
        since = datetime.now(UTC) - timedelta(hours=verdict["window_hours"])
        messages, summary = await fetch_logs(
            service_id, owner_id, since, int(settings.render_logs_limit)
        )
        verdict.update(summary)
        verdict.update(scan_messages(messages))
    except Exception as exc:
        verdict["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Проверка логов Render не удалась: %s", verdict["error"])
    await _save_verdict(verdict)
    if not verdict["error"]:
        logger.info(
            "Проверка логов Render: строк %d, утечек %d",
            verdict["scanned"],
            len(verdict["hits"]),
        )
    return verdict


async def _save_verdict(verdict: dict[str, Any]) -> None:
    from app.db import SessionLocal
    from app.models import WatcherState

    try:
        async with SessionLocal() as session:
            row = await session.get(WatcherState, RENDER_LOGS_CHECK_KEY)
            payload = json.dumps(verdict, ensure_ascii=False)
            if row is None:
                session.add(WatcherState(key=RENDER_LOGS_CHECK_KEY, value=payload))
            else:
                row.value = payload
            await session.commit()
    except Exception:
        logger.warning("Вердикт проверки логов не сохранился", exc_info=True)


async def last_verdict() -> dict[str, Any] | None:
    """Последний вердикт проверки логов (для /ops)."""
    from app.db import SessionLocal
    from app.models import WatcherState

    try:
        async with SessionLocal() as session:
            row = await session.get(WatcherState, RENDER_LOGS_CHECK_KEY)
            if row is None:
                return None
            data = json.loads(row.value)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def verdict_line(verdict: dict[str, Any] | None) -> str:
    """Строка о проверке логов для /ops — без секретов, только счётчики."""
    if not verdict:
        return "Логи Render: проверка ещё не выполнялась"
    if verdict.get("error"):
        return f"⚠️ Логи Render: проверка не удалась — {verdict['error']}"
    hits = verdict.get("hits") or {}
    scanned = int(verdict.get("scanned") or 0)
    checked = str(verdict.get("checked_at") or "")[:16].replace("T", " ")
    window = verdict.get("window_hours")
    share = verdict.get("covered_share")
    coverage = ""
    if share is not None and window:
        if verdict.get("window_covered"):
            coverage = f", окно {window} ч пройдено целиком"
        else:
            coverage = f", окно {window} ч пройдено на {int(float(share) * 100)}%"
    if hits:
        names = ", ".join(f"{name} ×{count}" for name, count in sorted(hits.items()))
        return (
            f"⚠️ Логи Render: утечки секретов! {names} "
            f"(строк {scanned}{coverage}, проверено {checked})"
        )
    return (
        f"Логи Render: секретов в логах нет (строк {scanned}{coverage}, проверено {checked})"
    )
