"""/health: живость без лжи — degraded вместо «ok» без данных."""

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


async def test_health_stays_green_and_honest_when_snapshot_fails(monkeypatch) -> None:
    """Переходное окно (например, инвалидация планов после миграции) не должно
    ронять эндпоинт: Render видит живой процесс, а статус честно degraded."""

    async def broken_snapshot():
        raise RuntimeError("cached statement plan is invalid")

    monkeypatch.setattr("app.config.settings.health_require_token", False)
    monkeypatch.setattr("app.ops.snapshot", broken_snapshot)
    response = await main_module.health(SimpleNamespace())
    assert response.status == 200
    assert b'"degraded"' in response.body
    assert b'"ok"' not in response.body


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


async def test_health_accepts_query_token(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.health_token", "s3cret")
    async def good_snapshot():
        return {"status": "ok"}

    monkeypatch.setattr("app.ops.snapshot", good_snapshot)
    response = await main_module.health(_request(query={"token": "s3cret"}))
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
