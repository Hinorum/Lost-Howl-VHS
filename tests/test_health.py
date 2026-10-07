"""/health: живость без лжи — код 503 вместо «200 при degraded».
/ready — тот же вердикт без токена и без данных, для внешнего watchdog."""

import json
from types import SimpleNamespace

from app import main as main_module


async def test_health_returns_snapshot_payload(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_require_token", False)
    async def good_snapshot():
        return {"status": "ok", "last_tick_age": 1.5}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(SimpleNamespace())
    assert response.status == 200
    assert b'"last_tick_age"' in response.body


async def test_health_reports_honest_503_when_snapshot_fails(monkeypatch) -> None:
    """Переходное окно (например, инвалидация планов после миграции) не должно
    ронять эндпоинт молчанием: тело честно degraded, а код — 503.

    Раньше здесь был 200, и внешний watchdog не мог отличить «всё хорошо»
    от «снимок недоступен»: проверять было нечем. Проба живости Render
    смотрит на /alive, поэтому деградация не превращается в рестарт сервиса.
    """

    async def broken_snapshot():
        raise RuntimeError("cached statement plan is invalid")

    monkeypatch.setattr("app.config.settings.health_require_token", False)
    monkeypatch.setattr("app.ops.snapshot", broken_snapshot)
    response = await main_module.health(SimpleNamespace())
    assert response.status == 503
    assert b'"degraded"' in response.body
    assert b'"ok"' not in response.body


async def test_health_503_when_verdict_degraded(monkeypatch) -> None:
    """Авторизованный чекер получает 503, когда снимок сам о себе говорит
    degraded: тревоги есть или тики падали. Тело при этом остаётся полным —
    под токеном видны и причина, и возраст."""
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")

    async def degraded_snapshot():
        return {"status": "degraded", "problems": ["очередь выплат стоит 42 мин"]}

    monkeypatch.setattr("app.ops.snapshot", degraded_snapshot)
    response = await main_module.health(
        _request(headers={"Authorization": "Bearer s3cret"})
    )
    assert response.status == 503
    assert json.loads(response.body)["problems"] == ["очередь выплат стоит 42 мин"]


def _request(
    headers: dict | None = None,
    query: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        headers={} if headers is None else headers,
        query={} if query is None else query,
    )


async def test_health_authorized_by_bearer(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(_request(headers={"Authorization": "Bearer s3cret"}))
    assert response.status == 200
    assert b'"ok"' in response.body


async def test_health_rejects_without_token(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    response = await main_module.health(_request())
    assert response.status == 401
    assert response.body == b"unauthorized"


async def test_health_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    response = await main_module.health(_request(headers={"Authorization": "Bearer nope"}))
    assert response.status == 401


async def test_health_rejects_query_token(monkeypatch) -> None:
    """Токен в строке запроса больше не авторизует снимок.

    Query-форма кладёт секрет в access-логи прокси и CDN, в историю браузера и
    в заголовок Referer при переходе со страницы, открытой в браузере. Кто
    читает логи — читает и токен. Легитимного потребителя у этой формы нет:
    self-ping ходит с заголовком, проба живости Render идёт на /alive.
    """
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(_request(query={"token": "s3cret"}))
    assert response.status == 401
    assert response.body == b"unauthorized"


async def test_query_token_ignored_even_when_header_present(monkeypatch) -> None:
    """Правильный заголовок работает, даже когда в query лежит мусорный токен."""
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(
        _request(headers={"Authorization": "Bearer s3cret"}, query={"token": "wrong"})
    )
    assert response.status == 200
    assert b'"ok"' in response.body


async def test_health_require_token_with_empty_token_locked(monkeypatch) -> None:
    """Runtime-гард: health_require_token=true и пустой токен → 401 для всех.
    Несогласованный конфиг ловится ещё fail-fast в validate_config, но эндпоинт
    не должен молча открываться, если конфиг меняется на лету/в тестах."""
    monkeypatch.setattr("app.config.settings.health_require_token", True)
    monkeypatch.setattr("app.config.settings.health_token", "")
    response = await main_module.health(_request())
    assert response.status == 401
    assert response.body == b"unauthorized"


async def test_alive_open_without_token(monkeypatch) -> None:
    """Проба Render ходит без заголовков: /alive отвечает и при пустом токене,
    поэтому секрет не нужно держать в healthCheckPath репозитория."""
    monkeypatch.setattr("app.config.settings.health_token", "")
    monkeypatch.setattr("app.config.settings.health_require_token", True)
    response = await main_module.alive(_request())
    assert response.status == 200
    assert b'alive' in response.body


async def test_alive_never_leaks_snapshot(monkeypatch) -> None:
    """Живость без данных: очередь выплат/тревоги в /alive попадать не должны."""
    async def exploding_snapshot():
        raise AssertionError("/alive не должен дёргать снимок БД")

    monkeypatch.setattr("app.ops.snapshot", exploding_snapshot)
    response = await main_module.alive(_request())
    assert response.status == 200
    for leaked in (b"problems", b"queue", b"last_tick_age", b"payout"):
        assert leaked not in response.body


# ---------- /ready: вердикт для watchdog без заголовков ----------


async def test_ready_is_open_without_token(monkeypatch) -> None:
    """Чекер, не умеющий слать Authorization, всё равно видит вердикт.

    Токен задан — значит, /health для такого мониторинга закрыт (там 401),
    а /ready отвечает. Именно ради этого мониторинга эндпоинт и существует:
    до него единственным открытым был /alive, который молчит про мёртвые
    тики при живом процессе — ровно тот инцидент, что и случился.
    """
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")

    async def good_snapshot():
        return {"status": "ok", "problems": [], "payout_queue": 3}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.ready(_request())
    assert response.status == 200
    assert b'"ok"' in response.body
    for leaked in (b"problems", b"payout", b"last_tick", b"queue", b"watcher"):
        assert leaked not in response.body


async def test_ready_returns_503_when_degraded(monkeypatch) -> None:
    """Мёртвые тики при живом процессе:503 наружу, причина — только под токеном."""
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")

    async def degraded_snapshot():
        return {"status": "degraded", "problems": ["тиков нет второй день"]}

    monkeypatch.setattr("app.ops.snapshot", degraded_snapshot)
    response = await main_module.ready(_request())
    assert response.status == 503
    # Вердикт без подробностей: причина тревоги остаётся под токеном в /health.
    assert response.body == b'{"status": "degraded"}'


async def test_ready_degraded_when_snapshot_fails(monkeypatch) -> None:
    """Снимок недоступен — не «ok»: иначе watchdog молчит ровно в тот
    момент, когда данные пропали вместе с ним."""
    async def broken_snapshot():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.ops.snapshot", broken_snapshot)
    response = await main_module.ready(_request())
    assert response.status == 503
    assert b'"degraded"' in response.body


async def test_ready_survives_locked_health_config(monkeypatch) -> None:
    """health_require_token=true при пустом токене закрывает /health для всех
    (fail closed) — /ready обязан отвечать, иначе неверный конфиг убивает и
    watchdog, то есть мониторинг пропадает вместе с тем, что он охраняет."""
    monkeypatch.setattr("app.config.settings.health_require_token", True)
    monkeypatch.setattr("app.config.settings.health_token", "")

    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.ready(_request())
    assert response.status == 200
    assert response.body == b'{"status": "ok"}'
