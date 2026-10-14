"""SSRF-гейт fetch_remote_json: https + публичный хост.

LITESERVER_CONFIG_URL приходит из env деплоера. Гейт стоит на схеме и
IP-литералах; редиректы не следуются автоматически — каждый хоп проверяется
тем же гейтом (легитимный хост иначе увёл бы запрос на 169.254.169.254 или
127.0.0.1 в обход проверки исходного URL).
"""

from __future__ import annotations

import httpx
import pytest

from app.ton_pay.wallet import (
    _MAX_CONFIG_REDIRECTS,
    _assert_public_https_url,
    fetch_remote_json,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://ton.org/config.json",
        "ftp://ton.org/config.json",
        "file:///etc/passwd",
        "ton.org/config.json",  # без схемы
        "https://127.0.0.1/config.json",
        "https://localhost/config.json",
        "https://[::1]/config.json",
        "https://10.0.0.1/config.json",
        "https://172.16.0.1/config.json",
        "https://192.168.1.1/config.json",
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://0.0.0.0/config.json",
        "https://[fd00::1]/config.json",  # unique-local
        "https:///config.json",  # без хоста
    ],
)
def test_ssrf_gate_blocks_dangerous_urls(url: str) -> None:
    with pytest.raises(ValueError):
        _assert_public_https_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://ton.org/global.config.json",
        "https://example.com/config.json",
        "https://8.8.8.8/config.json",  # публичный IP
        "https://cdn.example.org/liteservers.json",
    ],
)
def test_ssrf_gate_allows_public_https(url: str) -> None:
    _assert_public_https_url(url)  # не должен бросить


async def test_fetch_remote_json_calls_gate() -> None:
    """fetch_remote_json обязан гейтить URL до сетевого вызова."""
    with pytest.raises(ValueError, match="https"):
        await fetch_remote_json("http://evil.example/config.json")


class _FakeClient:
    """Мини-клиент httpx: отдаёт заранее заготовленные ответы и пишет вызовы."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, bool]] = []

    async def get(
        self, url: str, *, follow_redirects: bool = True, **kwargs: object
    ) -> httpx.Response:
        self.calls.append((str(url), follow_redirects))
        return self._responses.pop(0)


def _redirect(location: str, *, url: str) -> httpx.Response:
    return httpx.Response(
        302,
        headers={"location": location},
        request=httpx.Request("GET", url),
    )


def _ok(payload: dict, *, url: str) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


async def test_fetch_remote_json_does_not_follow_redirect_to_private(monkeypatch) -> None:
    """Редирект на метаданные облака отвергается, второй хоп не запрашивается."""
    client = _FakeClient(
        [
            _redirect(
                "https://169.254.169.254/latest/meta-data/",
                url="https://cdn.example.org/config.json",
            )
        ]
    )
    monkeypatch.setattr("app.http_utils.get_http_client", lambda: client)
    with pytest.raises(ValueError):
        await fetch_remote_json("https://cdn.example.org/config.json")
    # Первый запрос обязан идти без авто-редиректов, а второй хоп — не состояться.
    assert client.calls == [("https://cdn.example.org/config.json", False)]


async def test_fetch_remote_json_follows_public_redirect(monkeypatch) -> None:
    """Публичный редирект (в т.ч. относительный) проходит с ручной проверкой."""
    client = _FakeClient(
        [
            _redirect("/real.json", url="https://cdn.example.org/config.json"),
            _ok({"ok": True}, url="https://cdn.example.org/real.json"),
        ]
    )
    monkeypatch.setattr("app.http_utils.get_http_client", lambda: client)
    data = await fetch_remote_json("https://cdn.example.org/config.json")
    assert data == {"ok": True}
    assert client.calls == [
        ("https://cdn.example.org/config.json", False),
        ("https://cdn.example.org/real.json", False),
    ]


async def test_fetch_remote_json_rejects_redirect_chain(monkeypatch) -> None:
    """Бесконечная цепочка редиректов упирается в потолок."""
    client = _FakeClient(
        [
            _redirect("/next", url="https://cdn.example.org/config.json")
            for _ in range(_MAX_CONFIG_REDIRECTS + 1)
        ]
    )
    monkeypatch.setattr("app.http_utils.get_http_client", lambda: client)
    with pytest.raises(ValueError, match="редиректов"):
        await fetch_remote_json("https://cdn.example.org/config.json")


async def test_fetch_remote_json_rejects_redirect_without_location(monkeypatch) -> None:
    """3xx без Location — не редирект на неизвестный хост, а явная ошибка."""
    client = _FakeClient(
        [httpx.Response(302, request=httpx.Request("GET", "https://cdn.example.org/config.json"))]
    )
    monkeypatch.setattr("app.http_utils.get_http_client", lambda: client)
    with pytest.raises(ValueError, match="Location"):
        await fetch_remote_json("https://cdn.example.org/config.json")
