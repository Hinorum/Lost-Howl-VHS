"""SSRF-гейт fetch_remote_json: https + публичный хост.

LITESERVER_CONFIG_URL приходит из env деплоера, но редиректы включены —
легитимный хост может увести на 169.254.169.254 (метаданные облака) или
127.0.0.1. Гейт стоит на схеме и IP-литералах.
"""

from __future__ import annotations

import pytest

from app.ton_pay.wallet import _assert_public_https_url, fetch_remote_json


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
