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

Чтение инкрементальное: первый проход (и страховочный раз в
render_logs_full_rescan_hours) читает всё окно, остальные — только строки
новее сохранённой позиции с перекрытием. Позиция не перепрыгивает дыру:
она двигается, только когда проход покрыл отрезок после прошлой позиции.
Ошибка, таймаут или обрыв пагинации оставляют её на месте. Полнота всего
окна — отдельный вопрос: при потоке выше render_logs_limit полный проход
физически не закрывает все часы, и вердикт говорит об этом прямо.

Ключ RENDER_API_KEY — учётные данные всего аккаунта Render, поэтому бот
пользуется им ТОЛЬКО на чтение (GET) и никогда не пишет им в логи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.core.registry import RENDER_LOGS_CHECK_KEY, RENDER_LOGS_CURSOR_KEY
from app.http_utils import get_http_client, http_get_with_retry

logger = logging.getLogger(__name__)

_RENDER_API_BASE = "https://api.render.com/v1"

# Потолок одного прохода по логам. Проход — это до 100 страниц к API Render
# (render_logs_max_pages), и на медленном API он занимал десятки минут:
# проверка жила внутри ops-sweep, и всё это время единственный детектор
# аномалий молчал. Теперь у проверки своя джоба, но потолок по времени нужен
# всё равно: зависший HTTP-запрос не должен держать джобу вечно.
_SCAN_TIMEOUT_SECONDS = 300

# Перекрытие инкрементального окна с сохранённой позицией. API округляет
# границы до секунд, а запись, попавшая в хвост прошлого прохода, могла не
# успеть появиться в выдаче. Пяти минут хватает с запасом; строки из
# перекрытия дедуплицируются по id внутри прохода.
_CURSOR_OVERLAP_SECONDS = 300

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

    Текст строки приводится к нижнему регистру РОВНО один раз (раньше рядом
    с исходным списком жил второй список `haystacks` — двойная память на
    весь объём логов). Иглы тоже опускаются заранее, чтобы не делать это
    на каждой строке.
    """
    hits: dict[str, int] = {}
    samples: dict[str, int] = {}
    needles = [(label, secret.lower()) for label, secret in _secrets()]
    fragments_by_label: dict[str, list[str]] = {}
    for label, field in (
        ("treasury_mnemonic", "treasury_mnemonic"),
        ("treasury_testnet_mnemonic", "treasury_testnet_mnemonic"),
    ):
        secret = str(getattr(settings, field, "") or "")
        fragments = [fragment.lower() for fragment in _mnemonic_fragments(secret)]
        if fragments:
            fragments_by_label[label] = fragments
    for index, message in enumerate(messages, 1):
        text = message.lower()
        for label, needle in needles:
            if needle in text:
                hits[label] = hits.get(label, 0) + 1
                samples.setdefault(label, index)
        # Мнемоника: кроме всей фразы — три слова подряд (частая утечка).
        # Полная фраза входит в подсчёт фрагментов, как и раньше.
        for label, fragments in fragments_by_label.items():
            if any(fragment in text for fragment in fragments):
                hits[label] = hits.get(label, 0) + 1
                samples.setdefault(label, index)
    return {"hits": hits, "samples": samples, "scanned": len(messages)}


def _entry_time(raw: Any) -> datetime | None:
    """Момент из RFC3339 (`Z` или смещение); naive-время считается UTC.

    Годится и для штампов записей лога, и для позиции курсора.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _scan_plan(
    cursor: dict[str, Any] | None, now: datetime
) -> tuple[datetime, datetime, str]:
    """Границы прохода: (since, until, режим full/incremental).

    Полный проход читает всё окно render_logs_lookback_hours. Инкрементальный
    — от сохранённой позиции минус перекрытие, поэтому уже прочитанные строки
    не скачиваются заново. Полный нужен на первом проходе, после сброса или
    порчи позиции и раз в render_logs_full_rescan_hours: он ловит то, что
    инкремент мог пропустить из-за сбоя API или расхождения часов.

    Позиция из будущего (часы сервера ушли назад) не используется: вместо
    дыры читаем всё окно.
    """
    lookback_hours = max(1, int(settings.render_logs_lookback_hours))
    full_since = now - timedelta(hours=lookback_hours)
    until = _entry_time((cursor or {}).get("until"))
    full_at = _entry_time((cursor or {}).get("full_at"))
    rescan_hours = max(1, int(settings.render_logs_full_rescan_hours))
    full_due = (
        until is None
        or full_at is None
        or until > now
        or (now - full_at).total_seconds() >= rescan_hours * 3600
    )
    if full_due:
        return full_since, now, "full"
    # full_due уже отсеял None, но анализатору типов это не видно.
    assert until is not None
    since = until - timedelta(seconds=_CURSOR_OVERLAP_SECONDS)
    if since <= full_since:
        # Позиция старше окна: инкремент всё равно прочитал бы его целиком,
        # значит это полный проход и full_at пора обновить.
        return full_since, now, "full"
    return since, now, "incremental"


async def _load_cursor() -> dict[str, Any] | None:
    """Сохранённая позиция чтения логов; порча/сбой БД = полный проход."""
    from app.db import SessionLocal
    from app.models import WatcherState

    try:
        async with SessionLocal() as session:
            row = await session.get(WatcherState, RENDER_LOGS_CURSOR_KEY)
            if row is None:
                return None
            data = json.loads(row.value)
            return data if isinstance(data, dict) else None
    except Exception:
        # Позиция — оптимизация трафика, а не деньги: без неё проверка просто
        # читает всё окно, как раньше. Ронять из-за неё проход нельзя.
        return None


def _cursor_can_advance(
    cursor: dict[str, Any] | None,
    summary: dict[str, Any],
    error: str | None,
) -> bool:
    """Не оставит ли сдвиг позиции непрочитанную дыру.

    Позиция — это обещание «всё новее неё прочитано». Поэтому:
    - ошибка/таймаут — не двигаем никогда;
    - дошли до дна окна (пустая/короткая страница, штамп на границе) — можно;
    - прошлая позиция есть — нужно покрыть хотя бы её: самый старый
      прочитанный штамп не новее неё. Иначе между старой позицией и
      прочитанным куском осталась бы дыра;
    - прошлой позиции нет — можно, если пагинация кончилась сама или
      упёрлась в НАСТРОЕННЫЙ потолок (строки/страницы). Первый проход и так
      читает только новейший кусок, как до курсора; держать его вечно из-за
      потолка — значит навсегда остаться без инкремента.
    """
    if error:
        return False
    if summary.get("reached_floor"):
        return True
    previous = _entry_time((cursor or {}).get("until"))
    oldest = _entry_time(summary.get("oldest"))
    if previous is not None:
        return oldest is not None and oldest <= previous
    return bool(summary.get("capped"))


async def _save_cursor(
    until: str, *, full: bool, cursor: dict[str, Any] | None
) -> None:
    """Сохраняет позицию после прохода без дыры (см. _cursor_can_advance).

    full_at при инкременте не меняется: он отвечает на другой вопрос — когда
    последний раз проверялось всё окно, а не только новые строки.
    """
    from app.db import SessionLocal
    from app.models import WatcherState

    full_at = until
    if not full:
        previous_full = _entry_time((cursor or {}).get("full_at"))
        if previous_full is not None:
            full_at = previous_full.isoformat()
    payload = json.dumps({"until": until, "full_at": full_at})
    try:
        async with SessionLocal() as session:
            row = await session.get(WatcherState, RENDER_LOGS_CURSOR_KEY)
            if row is None:
                session.add(WatcherState(key=RENDER_LOGS_CURSOR_KEY, value=payload))
            else:
                row.value = payload
            await session.commit()
    except Exception:
        logger.warning("Позиция проверки логов не сохранилась", exc_info=True)


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


async def _fetch_log_page(
    params: dict[str, Any],
    since: datetime,
    upper: datetime,
    seen_ids: set[str],
) -> tuple[list[str], list[datetime], int, int]:
    """Одна страница логов Render (backward): (сообщения, штампы, свежие, размер).

    Следующая страница читается от самой старой записи предыдущей (exclusive),
    поэтому штампы нужны — они двигают границу `upper` назад.
    """
    params["startTime"] = (
        since.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    params["endTime"] = (
        upper.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    payload = await _api_get("/logs", params)
    entries = (
        payload if isinstance(payload, list) else (payload or {}).get("logs") or []
    )
    messages: list[str] = []
    fresh = 0
    page_stamps: list[datetime] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in seen_ids:
            continue
        if entry_id:
            seen_ids.add(entry_id)
            fresh += 1
        message = entry.get("message")
        if isinstance(message, str):
            messages.append(message)
        stamp = _entry_time(entry.get("timestamp"))
        if stamp is not None:
            page_stamps.append(stamp)
    return messages, page_stamps, fresh, len(entries)


async def fetch_logs(
    service_id: str,
    owner_id: str,
    since: datetime,
    limit: int,
    until: datetime | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Забирает тексты логов сервиса за окно назад — постранично НАЗАД.

    Логи Render листаются временем (курсора у списка нет): запрос с
    startTime/endTime отдаёт страницу записей, а следующая страница идёт
    от границы предыдущей. Направление — backward, от «сейчас» к началу
    окна: проверять надо СВЕЖИЕ логи (утечка в первых строках только что
    поднявшегося процесса), а не вчерашнее утро.

    until — верх окна (по умолчанию «сейчас»). Проверка передаёт момент
    старта прохода, чтобы сохранённая позиция не зависела от того, сколько
    шёл скан.

    Возвращает (тексты, сводка): сколько строк, какое окно реально покрыто
    и доводилось ли окно до конца. `reached_floor` — доказательство, что
    пагинация дошла до нижней границы: пустая/короткая страница или штамп
    на самой границе. Для инкремента это важнее доли времени: при тихих
    логах записей у границы может не быть вовсе.
    """
    last_error: Exception | None = None
    for candidate in _log_query_shapes(since):
        params: dict[str, Any] = {
            key: value for key, value in candidate.items() if value is not None
        }
        params["resource"] = service_id
        params["ownerId"] = owner_id
        params["direction"] = "backward"
        page_limit = int(params.get("limit") or 100)
        max_pages = max(1, int(settings.render_logs_max_pages))
        total_limit = max(page_limit, int(settings.render_logs_limit))
        messages: list[str] = []
        stamps: list[datetime] = []
        seen_ids: set[str] = set()
        scan_until = until or datetime.now(UTC)
        upper = scan_until
        pages = 0
        reached_floor = False
        provider_stalled = False
        try:
            while pages < max_pages and len(messages) < total_limit:
                page_messages, page_stamps, fresh, page_len = await _fetch_log_page(
                    params, since, upper, seen_ids
                )
                pages += 1
                if not page_len:
                    # Пустая страница — история в окне кончилась.
                    reached_floor = True
                    break
                messages.extend(page_messages)
                if page_stamps and min(page_stamps) <= since + timedelta(seconds=1):
                    reached_floor = True
                if not page_stamps or not fresh:
                    # Провайдер повторил страницу или не дал штампов: это не
                    # конец истории, а повод не доверять покрытию.
                    provider_stalled = True
                    break
                stamps.extend(page_stamps)
                upper = min(page_stamps) - timedelta(seconds=1)
                if page_len < page_limit:
                    # Короткая страница обычно значит «история кончилась»:
                    # окно пройдено до дна, даже если записей у дна не было.
                    # Для запроса на 1000 строк провайдер мог урезать ответ
                    # до 100, поэтому там полноту докажет только пустая
                    # страница: один лишний запрос дешевле потери строк.
                    if page_limit <= 100:
                        reached_floor = True
                        break
                    continue
        except Exception as exc:
            # Форма не подошла — пробуем следующую.
            last_error = exc
            continue
        now = datetime.now(UTC)
        capped = not provider_stalled and (
            pages >= max_pages or len(messages) >= total_limit
        )
        if stamps:
            oldest = min(stamps)
            newest = max(stamps)
            span = (now - since).total_seconds() or 1
            share = min(1.0, max(0.0, (now - oldest).total_seconds() / span))
            summary = {
                "pages": pages,
                "oldest": oldest.isoformat(),
                "newest": newest.isoformat(),
                "until": scan_until.isoformat(),
                "covered_share": round(share, 3),
                # Доля времени — запасной критерий: короткая/пустая страница
                # доказывает дно точнее, чем «старейшая запись близко к since»,
                # и не зависит от того, были ли у дна записи вообще.
                "window_covered": share >= 0.99 or reached_floor,
                "reached_floor": reached_floor,
                "capped": capped,
            }
        else:
            summary = {
                "pages": pages,
                "oldest": None,
                "newest": None,
                "until": scan_until.isoformat(),
                "covered_share": 0.0,
                "window_covered": True,
                "reached_floor": reached_floor,
                "capped": capped,
            }
        summary["query"] = ",".join(
            f"{key}={value}"
            for key, value in params.items()
            if key not in ("resource", "ownerId", "startTime", "endTime")
        )
        return messages, summary
    raise RuntimeError(f"ни одна форма запроса логов не подошла: {last_error}")


async def check_render_logs() -> dict[str, Any]:
    """Проверяет логи Render на секреты и пишет вердикт в watcher_state.

    Вердикт: {checked_at, scanned, hits, samples, service, query, window_hours,
    mode, cursor_from, window_start, until, error}. Значений секретов в нём
    нет — только счётчики.
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
        "until": None,
        "covered_share": 0.0,
        "window_covered": False,
        "reached_floor": False,
        "capped": False,
        "window_hours": int(settings.render_logs_lookback_hours),
        "mode": "full",
        "cursor_from": None,
        "window_start": None,
        "error": None,
    }
    cursor: dict[str, Any] | None = None
    mode = "full"
    try:
        async with asyncio.timeout(_SCAN_TIMEOUT_SECONDS):
            service_id, owner_id = await resolve_service()
            verdict["service"] = service_id
            cursor = await _load_cursor()
            since, until, mode = _scan_plan(cursor, datetime.now(UTC))
            verdict["mode"] = mode
            verdict["cursor_from"] = (cursor or {}).get("until")
            verdict["window_start"] = since.isoformat()
            messages, summary = await fetch_logs(
                service_id,
                owner_id,
                since,
                int(settings.render_logs_limit),
                until=until,
            )
            verdict.update(summary)
            verdict.update(scan_messages(messages))
    except TimeoutError:
        # Проход не уложился: вердикт с ошибкой всё равно пишется (проверка
        # должна оставлять след, а не исчезать молча), и следующий проход
        # повторит её — по расписанию, а не по сбою.
        verdict["error"] = (
            f"TimeoutError: проверка не уложилась в {_SCAN_TIMEOUT_SECONDS} с"
        )
        logger.warning("Проверка логов Render: %s", verdict["error"])
    except Exception as exc:
        verdict["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Проверка логов Render не удалась: %s", verdict["error"])
    until_stamp = str(verdict.get("until") or "")
    if until_stamp and _cursor_can_advance(cursor, verdict, verdict["error"]):
        await _save_cursor(until_stamp, full=(mode == "full"), cursor=cursor)
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
    if verdict.get("mode") == "incremental":
        start = str(verdict.get("window_start") or "")[:16].replace("T", " ")
        if verdict.get("window_covered"):
            coverage = f", докачано с {start}" if start else ", докачано"
        else:
            coverage = f", окно с {start} не пройдено до конца" if start else ""
    elif share is not None and window:
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
