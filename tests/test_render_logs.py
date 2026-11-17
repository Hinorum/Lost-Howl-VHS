"""Проверка собственных логов Render на секреты (app/render_logs.py).

Пункт чек-листа mainnet «мнемоника в логах» требовал ручного grep по
странице логов хостинга. Теперь бот проверяет это сам — и проверка не
должна превратиться в утечку: в вердикт попадают только счётчики.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.config import settings
from app.core.registry import RENDER_LOGS_CHECK_KEY, RENDER_LOGS_CURSOR_KEY
from app.db import SessionLocal
from app.models import WatcherState
from app.render_logs import (
    _mnemonic_fragments,
    resolve_service,
    scan_messages,
    verdict_line,
)

# Тестовая мнемоника. Собирается по-словами, а не пишется одной цитатой:
# сканер секретов ищет строку из 12 слов BIP-39, и тестовый вектор по виду
# неотличим от настоящей фразы (а это ровно тот случай, который сканер
# обязан ловить).
MNEMONIC = " ".join(
    [
        "abandon",
        "ability",
        "able",
        "about",
        "above",
        "absent",
        "absorb",
        "abstract",
        "absurd",
        "abuse",
        "access",
        "accident",
    ]
)


_STATE_KEYS = (RENDER_LOGS_CHECK_KEY, RENDER_LOGS_CURSOR_KEY)

_SERVICE = [
    {"service": {"id": "srv-1", "name": "bot", "type": "web", "ownerId": "own-1"}}
]


@pytest.fixture(autouse=True)
async def _clean_marker():
    async with SessionLocal() as session:
        await session.execute(delete(WatcherState).where(WatcherState.key.in_(_STATE_KEYS)))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(WatcherState).where(WatcherState.key.in_(_STATE_KEYS)))
        await session.commit()


async def _cursor_state() -> dict[str, str] | None:
    async with SessionLocal() as session:
        row = await session.get(WatcherState, RENDER_LOGS_CURSOR_KEY)
        if row is None:
            return None
        data = json.loads(row.value)
        return data if isinstance(data, dict) else None


async def _seed_cursor(until: datetime, full_at: datetime) -> None:
    await _seed_raw_cursor(json.dumps({"until": until.isoformat(), "full_at": full_at.isoformat()}))


async def _seed_raw_cursor(value: str) -> None:
    async with SessionLocal() as session:
        session.add(WatcherState(key=RENDER_LOGS_CURSOR_KEY, value=value))
        await session.commit()


def _stamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


@pytest.fixture
def secrets(monkeypatch):
    """Подставляет тестовые секреты в окружение."""
    for field in (
        "treasury_mnemonic",
        "treasury_testnet_mnemonic",
        "bot_token",
        "health_token",
        "webhook_secret",
        "referral_secret",
        "ton_api_key",
        "toncenter_api_key",
    ):
        monkeypatch.setattr(settings, field, "", raising=False)
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)
    monkeypatch.setattr(settings, "bot_token", "123456:TEST-BOT-TOKEN")
    for env_name in (
        "RENDER_API_KEY",
        "LLM_API_KEY",
        "GEMINI_API_KEY",
        "IMGBB_API_KEY",
        "POLLINATIONS_TOKEN",
        "BACKUP_PASSPHRASE",
    ):
        monkeypatch.delenv(env_name, raising=False)


def test_scan_finds_exact_secret(secrets) -> None:
    """Секрет в тексте лога — найден."""
    result = scan_messages([f"подпись: {settings.bot_token}"])
    assert result["hits"].get("bot_token") == 1
    assert result["samples"]["bot_token"] == 1
    assert result["scanned"] == 1


def test_scan_finds_mnemonic_fragment(secrets) -> None:
    """Часть мнемоники (три слова подряд) — тоже утечка."""
    result = scan_messages(["в логе: abandon ability able about"])
    assert result["hits"].get("treasury_mnemonic", 0) >= 1


def test_scan_clean_logs_have_no_hits(secrets) -> None:
    """Чистые логи — без находок."""
    result = scan_messages(["День 8 открыт", "очередь выплат пуста", "watcher: tonapi"])
    assert result["hits"] == {}
    assert result["scanned"] == 3


def test_scan_never_returns_secret_values(secrets) -> None:
    """Вердикт не содержит самих секретов — иначе проверка сама станет утечкой."""
    result = scan_messages([f"токен {settings.bot_token}"])
    dumped = json.dumps(result, ensure_ascii=False)
    assert settings.bot_token not in dumped
    assert MNEMONIC not in dumped


def test_mnemonic_fragments_are_consecutive_words() -> None:
    """Фрагменты — именно подряд идущие слова."""
    fragments = _mnemonic_fragments(MNEMONIC, 3)
    assert fragments
    assert fragments[0] == "abandon ability able"
    for fragment in fragments:
        assert fragment in MNEMONIC


def test_verdict_line_reports_hits_without_values(secrets) -> None:
    """Строка пульта показывает счётчики, а не секреты."""
    result = scan_messages([f"токен {settings.bot_token}"])
    line = verdict_line({**result, "checked_at": "2026-10-21T09:00:00+00:00"})
    assert "bot_token" in line
    assert settings.bot_token not in line


def test_verdict_line_reports_clean_and_error() -> None:
    """Чистый вердикт и ошибка проверки — разные строки."""
    clean = verdict_line(
        {"scanned": 10, "hits": {}, "checked_at": "2026-10-21T09:00:00+00:00", "window_hours": 24}
    )
    assert "секретов в логах нет" in clean
    assert "⚠️" not in clean
    failed = verdict_line({"error": "RuntimeError: 401", "hits": {}, "scanned": 0})
    assert "не удалась" in failed


async def test_resolve_service_requires_key(monkeypatch) -> None:
    """Без ключа проверка честно отказывает, а не молчит."""
    monkeypatch.setattr(settings, "render_api_key", "")
    with pytest.raises(RuntimeError):
        await resolve_service()


async def test_resolve_service_picks_single_web_service(monkeypatch) -> None:
    """Единственный web-сервис определяется сам, вместе с ownerId."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "")
    monkeypatch.setattr(settings, "render_service_name", "")

    async def fake_get(path, params=None):
        return [
            {"service": {"id": "srv-1", "name": "worker", "type": "worker"}},
            {
                "service": {
                    "id": "srv-2",
                    "name": "the-ways-bot",
                    "type": "web",
                    "ownerId": "own-1",
                }
            },
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    assert await resolve_service() == ("srv-2", "own-1")


async def test_resolve_service_refuses_to_guess(monkeypatch) -> None:
    """Несколько web-сервисов — честный отказ, а не проверка чужого."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "")
    monkeypatch.setattr(settings, "render_service_name", "")

    async def fake_get(path, params=None):
        return [
            {"service": {"id": "srv-1", "name": "a", "type": "web", "ownerId": "o"}},
            {"service": {"id": "srv-2", "name": "b", "type": "web", "ownerId": "o"}},
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    with pytest.raises(RuntimeError):
        await resolve_service()


async def test_resolve_service_requires_owner_id(monkeypatch) -> None:
    """Сервис без ownerId — честная ошибка: запрос логов без него не пройдёт."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "")
    monkeypatch.setattr(settings, "render_service_name", "")

    async def fake_get(path, params=None):
        return [{"service": {"id": "srv-2", "name": "bot", "type": "web"}}]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    with pytest.raises(RuntimeError):
        await resolve_service()


async def test_check_writes_verdict_without_secrets(monkeypatch, secrets) -> None:
    """Проверка пишет вердикт в watcher_state — и в нём нет секретов."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")

    async def fake_get(path, params=None):
        if path == "/logs":
            assert params.get("resource") == "srv-1"
            assert params.get("ownerId") == "own-1"
            return [
                {"id": "1", "message": f"токен бота {settings.bot_token}", "timestamp": "2026-10-21T09:00:00Z"},
                {"id": "2", "message": "День 8 открыт", "timestamp": "2026-10-21T09:00:01Z"},
            ]
        return [
            {
                "service": {
                    "id": "srv-1",
                    "name": "bot",
                    "type": "web",
                    "ownerId": "own-1",
                }
            }
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs, last_verdict

    verdict = await check_render_logs()
    assert verdict["scanned"] == 2
    assert verdict["hits"].get("bot_token") == 1
    assert verdict["error"] is None

    stored = await last_verdict()
    assert stored is not None
    assert settings.bot_token not in json.dumps(stored, ensure_ascii=False)


async def test_check_records_error_without_raising(monkeypatch) -> None:
    """Сбой проверки не поднимается наружу — вердикт говорит о сбое."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")

    async def boom(path, params=None):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("app.render_logs._api_get", boom)
    from app.render_logs import check_render_logs, last_verdict

    verdict = await check_render_logs()
    assert verdict["error"]
    assert "401" in verdict["error"]
    stored = await last_verdict()
    assert stored is not None and stored["error"]


async def test_check_times_out_and_leaves_a_trace(monkeypatch) -> None:
    """Проход, который не уложился, оставляет след.

    Раньше проверка жила внутри ops-sweep — единственного детектора аномалий:
    зависший запрос к API хостинга держал его десятки минут, и тревоги админу
    не уходили. Теперь у проверки своя джоба и потолок по времени; но и при
    таймауте вердикт обязан сохраниться — иначе след сбоя исчезает, и
    следующий проход выглядит как первый.
    """
    import asyncio

    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr("app.render_logs._SCAN_TIMEOUT_SECONDS", 0.05)

    async def hang(path, params=None):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr("app.render_logs._api_get", hang)
    from app.render_logs import check_render_logs, last_verdict

    verdict = await check_render_logs()
    assert "TimeoutError" in verdict["error"]
    stored = await last_verdict()
    assert stored is not None and "TimeoutError" in stored["error"]


async def test_fetch_logs_walks_window_backwards(monkeypatch) -> None:
    """Страницы идут НАЗАД от «сейчас»: проверять надо свежие логи.

    Утечка в первых строках только что поднявшегося процесса — в хвосте
    окна, а не в начале: обход назад от текущего момента доходит до них
    первыми.
    """
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_max_pages", 5)
    # Страница логов — до 100 строк; в тесте делаем её короче, чтобы обход
    # уложился в две страницы.
    monkeypatch.setattr(
        "app.render_logs._log_query_shapes", lambda since: [{"limit": 2}]
    )

    pages = [
        [
            {"id": "1", "message": "свежее", "timestamp": "2026-10-21T09:10:00Z"},
            {"id": "2", "message": "свежее2", "timestamp": "2026-10-21T09:00:00Z"},
        ],
        [
            {"id": "3", "message": "старое", "timestamp": "2026-10-21T08:30:00Z"},
        ],
    ]
    seen: list[dict] = []

    async def fake_get(path, params=None):
        if path != "/logs":
            return [
                {"service": {"id": "srv-1", "name": "bot", "type": "web", "ownerId": "own-1"}}
            ]
        seen.append(dict(params or {}))
        index = min(len(seen) - 1, len(pages) - 1)
        return pages[index]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import fetch_logs

    since = datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)
    messages, summary = await fetch_logs("srv-1", "own-1", since, 2000)

    assert messages == ["свежее", "свежее2", "старое"]
    assert summary["pages"] == 2
    assert summary["oldest"].startswith("2026-10-21T08:30:00")
    assert summary["newest"].startswith("2026-10-21T09:10:00")
    # Первая страница — от начала окна; вторая — всё, что СТАРШЕ самой ранней
    # записи первой (endTime отсекает хвост).
    assert seen[0]["startTime"].startswith("2026-10-21T08:00:00")
    assert seen[1]["endTime"].startswith("2026-10-21T08:59:59")


async def test_check_reports_uncovered_window(monkeypatch, secrets) -> None:
    """Если окно не покрыто целиком — вердикт говорит об этом прямо.

    Метка записи строится от «сейчас», а не пишется константой: покрытие
    считается как (now − oldest) / (now − since), где since = now − окно.
    Захардкоженная метка делает тест бомбой — он валиден ровно 24 часа
    после момента написания, потом метка проваливается за since, числитель
    перерастает знаменатель, min(1.0, …) зажимает долю в 1.0, и тест падает
    на ровном месте. Отсчёт от now не даёт такой границы: запись на 12 ч
    назад при окне 24 ч всегда даёт долю 0.5 и сохраняет смысл проверки —
    одна страница окно не покрывает.
    """
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_max_pages", 1)
    monkeypatch.setattr(settings, "render_logs_lookback_hours", 24)
    # Страница ровно в лимит: скан упирается в потолок страниц, а не в дно
    # истории. Короткая страница означала бы «история кончилась» — тогда окно
    # честно считается покрытым.
    monkeypatch.setattr("app.render_logs._log_query_shapes", lambda since: [{"limit": 1}])
    halfway = (datetime.now(UTC) - timedelta(hours=12)).isoformat()

    async def fake_get(path, params=None):
        if path != "/logs":
            return [
                {"service": {"id": "srv-1", "name": "bot", "type": "web", "ownerId": "own-1"}}
            ]
        return [
            {"id": "1", "message": "строка", "timestamp": halfway},
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()
    assert verdict["error"] is None
    assert verdict["scanned"] == 1
    assert verdict["covered_share"] < 1.0
    assert verdict["window_covered"] is False


async def test_first_scan_reads_full_window_and_saves_cursor(monkeypatch, secrets) -> None:
    """Первый проход читает всё окно и запоминает его верх.

    Пока позиции нет, экономить не на чем: прочитать надо всё
    render_logs_lookback_hours, как до курсора.
    """
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_lookback_hours", 8)
    monkeypatch.setattr(settings, "render_logs_max_pages", 5)
    now = datetime.now(UTC)
    seen: list[dict] = []

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        seen.append(dict(params or {}))
        stamp = now - timedelta(hours=8) + timedelta(seconds=30)
        return [{"id": "1", "message": "чисто", "timestamp": stamp.isoformat()}]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    before = datetime.now(UTC)
    verdict = await check_render_logs()
    after = datetime.now(UTC)

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    assert verdict["window_covered"] is True
    start = _stamp(seen[0]["startTime"])
    assert abs((start - (before - timedelta(hours=8))).total_seconds()) <= 5
    cursor = await _cursor_state()
    assert cursor is not None
    until = _stamp(cursor["until"])
    assert before <= until <= after
    assert cursor["full_at"] == cursor["until"]


async def test_incremental_scan_overlaps_cursor_without_gap(monkeypatch, secrets) -> None:
    """Повторный проход читает только новые строки, но с перекрытием.

    Начало окна — позиция минус пять минут: запись на стыке проходов не
    теряется, а уже прочитанные часы не скачиваются заново.
    """
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_full_rescan_hours", 24)
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=3)
    full_at = now - timedelta(hours=1)
    await _seed_cursor(cursor_until, full_at)
    seen: list[dict] = []

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        seen.append(dict(params or {}))
        return [
            {
                "id": "new",
                "message": "чисто",
                "timestamp": (now - timedelta(minutes=1)).isoformat(),
            }
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "incremental"
    assert verdict["cursor_from"] == cursor_until.isoformat()
    assert verdict["error"] is None
    assert verdict["window_covered"] is True
    start = _stamp(seen[0]["startTime"])
    expected = cursor_until - timedelta(seconds=300)
    assert abs((start - expected).total_seconds()) <= 1
    assert _stamp(seen[0]["endTime"]) >= now
    cursor = await _cursor_state()
    assert cursor is not None
    assert _stamp(cursor["until"]) > cursor_until
    assert cursor["full_at"] == full_at.isoformat()


async def test_scan_error_keeps_cursor(monkeypatch) -> None:
    """Сбой API не двигает позицию: непрочитанное повторит следующий проход."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=2)
    full_at = now - timedelta(hours=1)
    await _seed_cursor(cursor_until, full_at)

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["error"] and "401" in verdict["error"]
    assert await _cursor_state() == {
        "until": cursor_until.isoformat(),
        "full_at": full_at.isoformat(),
    }


async def test_scan_timeout_keeps_cursor(monkeypatch) -> None:
    """Таймаут не двигает позицию: окно не доказано пройденным."""
    import asyncio

    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr("app.render_logs._SCAN_TIMEOUT_SECONDS", 0.05)
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=2)
    full_at = now - timedelta(hours=1)
    await _seed_cursor(cursor_until, full_at)

    async def hang(path, params=None):
        if path != "/logs":
            return _SERVICE
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr("app.render_logs._api_get", hang)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["error"] and "TimeoutError" in verdict["error"]
    assert await _cursor_state() == {
        "until": cursor_until.isoformat(),
        "full_at": full_at.isoformat(),
    }


async def test_partial_incremental_window_keeps_cursor(monkeypatch) -> None:
    """Если пагинация упёрлась в потолок страниц, позиция стоит на месте."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_max_pages", 1)
    monkeypatch.setattr("app.render_logs._log_query_shapes", lambda since: [{"limit": 2}])
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=3)
    full_at = now - timedelta(hours=1)
    await _seed_cursor(cursor_until, full_at)

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        return [
            {"id": "1", "message": "a", "timestamp": (now - timedelta(minutes=9)).isoformat()},
            {"id": "2", "message": "b", "timestamp": (now - timedelta(minutes=10)).isoformat()},
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "incremental"
    assert verdict["error"] is None
    assert verdict["window_covered"] is False
    assert await _cursor_state() == {
        "until": cursor_until.isoformat(),
        "full_at": full_at.isoformat(),
    }


async def test_first_capped_scan_still_saves_cursor(monkeypatch) -> None:
    """Первый проход упёрся в потолок — позицию всё равно сохраняем.

    При потоке выше render_logs_limit полное окно не покрывается никогда.
    Если в этом случае не двигать позицию, инкремент не включится: каждый
    проход будет перечитывать те же самые строки. Дыры после прошлой позиции
    нет — позиции ещё нет, а старый код читал ровно этот новейший кусок.
    """
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_max_pages", 1)
    monkeypatch.setattr("app.render_logs._log_query_shapes", lambda since: [{"limit": 2}])
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        return [
            {"id": "1", "message": "a", "timestamp": (now - timedelta(minutes=1)).isoformat()},
            {"id": "2", "message": "b", "timestamp": (now - timedelta(minutes=2)).isoformat()},
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    assert verdict["window_covered"] is False
    assert verdict["capped"] is True
    cursor = await _cursor_state()
    assert cursor is not None and cursor["until"]


async def test_full_rescan_missing_previous_cursor_keeps_it(monkeypatch) -> None:
    """Перескан не дошёл до прошлой позиции — сдвиг оставил бы дыру."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_lookback_hours", 8)
    monkeypatch.setattr(settings, "render_logs_full_rescan_hours", 24)
    monkeypatch.setattr(settings, "render_logs_max_pages", 1)
    monkeypatch.setattr("app.render_logs._log_query_shapes", lambda since: [{"limit": 2}])
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=6)
    full_at = now - timedelta(hours=25)
    await _seed_cursor(cursor_until, full_at)

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        return [
            {"id": "1", "message": "a", "timestamp": (now - timedelta(minutes=1)).isoformat()},
            {"id": "2", "message": "b", "timestamp": (now - timedelta(minutes=2)).isoformat()},
        ]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    assert verdict["capped"] is True
    assert await _cursor_state() == {
        "until": cursor_until.isoformat(),
        "full_at": full_at.isoformat(),
    }


async def test_periodic_full_rescan_ignores_cursor(monkeypatch) -> None:
    """Раз в render_logs_full_rescan_hours читается всё окно, а не хвост."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_lookback_hours", 8)
    monkeypatch.setattr(settings, "render_logs_full_rescan_hours", 24)
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=3)
    full_at = now - timedelta(hours=25)
    await _seed_cursor(cursor_until, full_at)
    seen: list[dict] = []

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        seen.append(dict(params or {}))
        stamp = now - timedelta(hours=8) + timedelta(seconds=30)
        return [{"id": "1", "message": "чисто", "timestamp": stamp.isoformat()}]

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    start = _stamp(seen[0]["startTime"])
    expected = now - timedelta(hours=8)
    assert abs((start - expected).total_seconds()) <= 5
    cursor = await _cursor_state()
    assert cursor is not None
    assert _stamp(cursor["until"]) > cursor_until
    assert _stamp(cursor["full_at"]) > full_at


async def test_cursor_older_than_lookback_reads_full_window(monkeypatch) -> None:
    """Позиция старше окна не заставляет читать глубже настроенной глубины."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    monkeypatch.setattr(settings, "render_logs_lookback_hours", 8)
    now = datetime.now(UTC).replace(microsecond=0)
    cursor_until = now - timedelta(hours=10)
    full_at = now - timedelta(hours=1)
    await _seed_cursor(cursor_until, full_at)
    seen: list[dict] = []

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        seen.append(dict(params or {}))
        return []

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    assert verdict["window_covered"] is True
    start = _stamp(seen[0]["startTime"])
    expected = now - timedelta(hours=8)
    assert abs((start - expected).total_seconds()) <= 5


async def test_broken_cursor_falls_back_to_full_scan(monkeypatch) -> None:
    """Порча позиции — не повод пропустить окно: читаем всё, как в первый раз."""
    monkeypatch.setattr(settings, "render_api_key", "KEY")
    monkeypatch.setattr(settings, "render_service_id", "srv-1")
    await _seed_raw_cursor("{not json")

    async def fake_get(path, params=None):
        if path != "/logs":
            return _SERVICE
        return []

    monkeypatch.setattr("app.render_logs._api_get", fake_get)
    from app.render_logs import check_render_logs

    verdict = await check_render_logs()

    assert verdict["mode"] == "full"
    assert verdict["error"] is None
    assert verdict["window_covered"] is True
    cursor = await _cursor_state()
    assert cursor is not None and cursor["until"]


def test_verdict_line_reports_incremental_position() -> None:
    """Строка /ops при инкременте говорит, с какого места докачано."""
    complete = verdict_line(
        {
            "scanned": 5,
            "hits": {},
            "mode": "incremental",
            "window_start": "2026-11-17T09:00:00+00:00",
            "window_covered": True,
            "checked_at": "2026-11-17T12:00:00+00:00",
        }
    )
    assert "докачано с 2026-11-17 09:00" in complete
    incomplete = verdict_line(
        {
            "scanned": 5,
            "hits": {},
            "mode": "incremental",
            "window_start": "2026-11-17T09:00:00+00:00",
            "window_covered": False,
            "checked_at": "2026-11-17T12:00:00+00:00",
        }
    )
    assert "не пройдено до конца" in incomplete
