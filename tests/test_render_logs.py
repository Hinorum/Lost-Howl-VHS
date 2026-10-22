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
from app.core.registry import RENDER_LOGS_CHECK_KEY
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


@pytest.fixture(autouse=True)
async def _clean_marker():
    async with SessionLocal() as session:
        await session.execute(
            delete(WatcherState).where(WatcherState.key == RENDER_LOGS_CHECK_KEY)
        )
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(
            delete(WatcherState).where(WatcherState.key == RENDER_LOGS_CHECK_KEY)
        )
        await session.commit()


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
