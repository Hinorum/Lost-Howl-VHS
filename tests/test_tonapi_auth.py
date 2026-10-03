"""Заголовок авторизации должен соответствовать провайдеру.

TonAPI ждёт `Authorization: Bearer` и ключ в `X-API-Key` вовсе не считает:
запрос молча уходит в анонимный режим, где `/v2/blockchain` режет страницу до
100 записей. Никакой ошибки при этом не возвращается — просто приходит
анonymный ответ. Toncenter, наоборот, ждёт именно `X-API-Key`.

Последствия молчаливого ухода в анонимный режим в этом проекте стоили денег,
а не удобства:

* в сверке исходящих (`reconcile`) окно падало с 3072 переводов до 100, и
  выплата, чей memo вытеснили из окна, объявлялась неушедшей и отправлялась
  повторно — уже с новым seqno;
* бутстрап зеркала казны, спускающийся от головы к генезису, обрывался на
  глубине 100 переводов, и тишина обрыва выглядела как «истории больше нет».

Тест ловит именно эту ошибку — неверный заголовок на TonAPI и/или Toncenter.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import ton_pay, treasury_mirror
from app.config import settings
from app.ton_codec import api_headers, tonapi_headers
from app.ton_pay import reconcile, treasury
from app.ton_utils import normalize_address

TREASURY = normalize_address("EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF")
KEY = "secret-key"


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {"transactions": [], "balance": "0", "last": {"seqno": 1}}

    def raise_for_status(self) -> None:
        return None


class _Client:
    """Запоминает заголовки всех обращений."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def get(self, url, params=None, headers=None, **kwargs: Any) -> _Response:
        self.seen.append({"url": url, "headers": dict(headers or {})})
        return _Response()

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def provider_client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    """Клиент, запоминающий заголовки, на месте настоящего HTTP-клиента."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_api_key", KEY)
    monkeypatch.setattr(settings, "toncenter_api_key", KEY)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 1)
    client = _Client()
    monkeypatch.setattr(ton_pay, "get_http_client", lambda: client)
    monkeypatch.setattr(treasury_mirror, "get_http_client", lambda: client)
    return client


def _tonapi_calls(client: _Client) -> list[dict]:
    return [c for c in client.seen if "tonapi.io" in c["url"]]


def _toncenter_calls(client: _Client) -> list[dict]:
    return [c for c in client.seen if "toncenter" in c["url"]]


def test_tonapi_wants_bearer_and_toncenter_wants_x_api_key() -> None:
    """Сами помощники не перепутаны: это основа, на ней стоят все вызовы."""
    assert tonapi_headers(KEY) == {"Authorization": f"Bearer {KEY}"}
    assert "X-API-Key" not in tonapi_headers(KEY)
    assert api_headers(KEY) == {"X-API-Key": KEY}
    # Без ключа заголовков нет — провайдер сам уходит в анонимный режим.
    assert tonapi_headers("") == {}
    assert api_headers("") == {}


async def test_reconcile_tonapi_scan_uses_bearer(provider_client: _Client) -> None:
    """Сверка исходящих: TonAPI получает Bearer, а не X-API-Key."""
    await reconcile._tx_map_via_tonapi()

    calls = _tonapi_calls(provider_client)
    assert calls, "обращение к TonAPI не состоялось — тест ничего не проверил"
    for call in calls:
        assert call["headers"] == {"Authorization": f"Bearer {KEY}"}


async def test_masterchain_entropy_tonapi_uses_bearer(provider_client: _Client) -> None:
    """Энтропия закона дня: TonAPI получает Bearer."""
    await reconcile.fetch_masterchain_entropy()

    for call in _tonapi_calls(provider_client):
        assert call["headers"] == {"Authorization": f"Bearer {KEY}"}


async def test_treasury_account_state_splits_providers(provider_client: _Client) -> None:
    """Карточка аккаунта: у каждого провайдера свой заголовок."""
    await treasury._tonapi_account_raw(TREASURY)
    await treasury._toncenter_account(TREASURY)

    for call in _tonapi_calls(provider_client):
        assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
    for call in _toncenter_calls(provider_client):
        assert call["headers"] == {"X-API-Key": KEY}


async def test_treasury_mirror_splits_providers(provider_client: _Client) -> None:
    """Зеркало казны обходит оба провайдера — заголовки не должны смешиваться."""
    await treasury_mirror._fetch_page(before_lt=1, offset=0)

    for call in _tonapi_calls(provider_client):
        assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
        assert "X-API-Key" not in call["headers"]
    for call in _toncenter_calls(provider_client):
        assert call["headers"] == {"X-API-Key": KEY}
        assert "Authorization" not in call["headers"]
