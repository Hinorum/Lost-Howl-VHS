"""Тесты двух источников истории переводов: честный 404 TonAPI и фолбэк Toncenter v3.

Реальный инцидент: TonAPI-тестнет отдавал 404 истории транзакций активного
казначея, watcher считал это «пустой цепочкой», ставил сердцебиение — и ставки
тихо не находились при зелёном /health. Здесь проверяется, что такое состояние
теперь распознаётся, а переводы читаются через резервный источник.
"""

from __future__ import annotations

import asyncio
import base64
import os
from unittest.mock import AsyncMock

import pytest

from app import ton_watch
from app.config import settings
from app.ton_utils import to_nano

TREASURY = "0:" + "ab" * 32
SENDER = "0:" + "cd" * 32

_HISTORY = f"/v2/blockchain/accounts/{TREASURY}/transactions"
_ACCOUNT = f"/v2/accounts/{TREASURY}"
_V3 = "/api/v3/transactions"


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def install_http(monkeypatch: pytest.MonkeyPatch, routes: dict[str, list]) -> list[tuple]:
    """Скриптованный httpx.AsyncClient внутри ton_watch.

    routes: фрагмент URL -> очередь ответов (_Response или Exception);
    порядок ключей важен: совпадение ищется по первому подходящему фрагменту.
    Возвращает журнал вызовов [(url, params), ...].
    """
    calls: list[tuple] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def get(self, url, params=None, headers=None, **kwargs):
            calls.append((url, dict(params or {})))
            for fragment, queue in routes.items():
                if fragment in url:
                    assert queue, f"заглушка исчерпана: {fragment}"
                    item = queue.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
            raise AssertionError(f"нет заглушки для {url}")

    monkeypatch.setattr(ton_watch, "get_http_client", lambda: _Client())
    return calls


@pytest.fixture(autouse=True)
def _enabled_treasury(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)


def _api_tx(utime: int, value_nano: int) -> dict:
    """Транзакция в формате TonAPI v2."""
    raw = os.urandom(32)
    return {
        "hash": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "lt": utime,
        "utime": utime,
        "in_msg": {"source": {"address": SENDER}, "value": str(value_nano), "raw_message": ""},
    }


def _v3_tx(utime: int, value_nano: int, comment: str | None = None) -> dict:
    """Транзакция в формате Toncenter v3."""
    raw = os.urandom(32)
    decoded = (
        {"@type": "comment", "comment": comment}
        if comment is not None
        else {"@type": "empty_cell"}
    )
    return {
        "hash": base64.b64encode(raw).decode(),
        "lt": str(utime),
        "now": utime,
        "in_msg": {
            "source": SENDER,
            "value": str(value_nano),
            "message_content": {"decoded": decoded},
        },
    }


async def test_tonapi_success_reads_verify_comment(monkeypatch) -> None:
    item = _api_tx(1500, to_nano(0.03))
    item["in_msg"].update(
        raw_message="b5ee9c72",
        decoded_op_name="text_comment",
        decoded_body={"text": "bv:ABC123"},
    )
    install_http(monkeypatch, {_HISTORY: [_Response(200, {"transactions": [item]})]})
    transfers, ok = await ton_watch.fetch_recent_transfers(1000)
    assert ok is True
    assert len(transfers) == 1
    assert transfers[0].comment == "bv:ABC123"


async def test_tonapi_pagination_walks_by_lt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Полная страница уводит TonAPI-проход вглубь курсором before_lt.

    Курсор — младший lt страницы (тот же механизм, что у Toncenter v3),
    а не offset: /v2/blockchain/accounts/.../transactions такого параметра
    не имеет.
    """
    limit = ton_watch._PAGE_LIMIT
    page_one = [_api_tx(2000 - index, to_nano(0.01)) for index in range(limit)]
    page_two = [_api_tx(1500, to_nano(0.02))]
    calls = install_http(
        monkeypatch,
        {_HISTORY: [_Response(200, {"transactions": page_one}), _Response(200, {"transactions": page_two})]},
    )

    result = await ton_watch._deep_collect(
        ton_watch._tonapi_page,
        lambda page: page[-1].provider_ref or page[-1].tx_hash,
        1000,
    )

    assert result.complete is True
    assert len(result.transfers) == limit + 1
    v2_calls = [params for url, params in calls if "/v2/blockchain/" in url]
    assert len(v2_calls) == 2
    assert "before_lt" not in v2_calls[0] and "offset" not in v2_calls[0]
    # Курсор второй страницы — младший lt первой (сортировка desc).
    assert v2_calls[1]["before_lt"] == str(page_one[-1]["lt"])


async def test_tonapi_http_error_falls_back(monkeypatch) -> None:
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(403)],
            _V3: [_Response(200, {"transactions": [_v3_tx(1500, to_nano(0.03))]})],
        },
    )
    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)
    assert ok is True
    assert source == "toncenter"
    assert gap is None
    assert len(transfers) == 1


@pytest.mark.parametrize("kind", ["comment", "text_comment"])
def test_toncenter_verify_comment_formats(kind) -> None:
    item = _v3_tx(1500, to_nano(0.03), comment="\ufeffbv:ABC\u200b123")
    item["in_msg"]["message_content"]["decoded"]["@type"] = kind
    transfer = ton_watch._parse_toncenter_item(item, 1000)
    assert transfer.comment == "bv:ABC123"


# ---------- Честная трактовка 404 ----------


async def test_uninitialized_treasury_404_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий казначей без единой транзакции: 404 — здоровье, фолбэк не нужен."""
    calls = install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "uninitialized"})],
            _V3: [AssertionError("фолбэк не должен вызываться")],
        },
    )
    transfers, ok = await ton_watch.fetch_recent_transfers(1000)
    assert ok is True and transfers == []
    assert any("/api/v3/" not in url for url, _params in calls)


async def test_active_account_without_recent_activity_404_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Активность кошелька старше курсора — история за окном и правда пуста."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "active", "last_activity": 900})],
        },
    )
    transfers, ok = await ton_watch.fetch_recent_transfers(1000)
    assert ok is True and transfers == []


async def test_stale_index_404_is_degraded_and_toncenter_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Инцидент: 404 истории при живом кошельке с свежей активностью.

    Цикл не считается успешным по TonAPI, переводы приходят из Toncenter,
    источник успешного прохода помечается как «toncenter».
    """
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "active", "last_activity": 1500})],
            _V3: [_Response(200, {"transactions": [_v3_tx(1500, to_nano(1.67))]})],
        },
    )
    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)
    assert ok is True and source == "toncenter"
    assert len(transfers) == 1
    assert transfers[0].value_nanotons == to_nano(1.67)
    assert transfers[0].utime == 1500
    assert transfers[0].comment == ""


async def test_tonapi_network_error_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """TonAPI лежит целиком (не только 404) — фолбэк спасает цикл."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [RuntimeError("connect timeout")],
            _V3: [
                _Response(
                    200,
                    {"transactions": [_v3_tx(1600, to_nano(0.5), comment="привет")]},
                )
            ],
        },
    )
    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)
    assert ok is True and source == "toncenter"
    assert transfers[0].comment == "привет"


async def test_both_sources_down_means_failed_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оба источника недоступны — цикл неуспешен, сердцебиение не встанет."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [RuntimeError("tonapi молчит")],
            _V3: [RuntimeError("toncenter молчит")],
        },
    )
    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)
    assert transfers == [] and ok is False and source == "none" and gap is None


# ---------- Бюджет страниц кончился раньше курсора ----------


def _deep_pages(make_tx, top: int, count: int) -> tuple[list, int]:
    """count страниц строго новее курсора: проход упрётся в бюджет страниц.

    Страницы непустые и не повторяются (хеши случайные), каждая глубже
    предыдущей, но ни одна не достигает курсора — то есть проход физически
    не может завершиться, кроме как исчерпанием бюджета. Возвращает
    (ответы, ожидаемое дно дыры).
    """
    limit = ton_watch._PAGE_LIMIT
    pages = [
        _Response(
            200,
            {
                "transactions": [
                    make_tx(top - page * limit - index, to_nano(0.01)) for index in range(limit)
                ]
            },
        )
        for page in range(count)
    ]
    return pages, top - (count - 1) * limit - (limit - 1)


def _fast_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убрать паузу между страницами: тест гоняет десятки страниц.

    Пауза 0.12 с между страницами нужна живому циклу, чтобы не словить лимит
    индексатора; в тесте она превратила бы прогон в десятки секунд ожидания.
    """
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


async def test_page_budget_exhausted_is_not_a_complete_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Полные страницы всё глубже курсора, бюджет исчерпан — проход НЕ полный.

    Именно этот случай раньше возвращал complete=True, и цикл двигал курсор
    вперёд по utime самой нижней увиденной транзакции: окно между ней и старым
    курсором выпадало из чтения навсегда — тихо, без записи и без тревоги.
    Теперь дыра возвращается наружу, а курсор за неё не двигается.
    """
    limit = ton_watch._PAGE_LIMIT
    budget = ton_watch._MAX_PAGES
    _fast_pace(monkeypatch)
    history, history_floor = _deep_pages(_api_tx, 7000, budget)
    fallback, _ = _deep_pages(_v3_tx, 7000, budget)
    calls = install_http(monkeypatch, {_HISTORY: history, _V3: fallback})

    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)

    assert ok is True  # провайдеры живы, прочитанное обработано
    assert source == "tonapi"
    assert len(transfers) == 2 * budget * limit
    assert gap == history_floor  # дно дыры — самая старая увиденная
    v2_calls = [params for url, params in calls if "/v2/blockchain/" in url]
    assert len(v2_calls) == budget  # бюджет честно исчерпан


async def test_page_budget_truncation_keeps_deepest_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Оба прохода усечены — дыра называется по самой глубокой достигнутой границе."""
    budget = ton_watch._MAX_PAGES
    _fast_pace(monkeypatch)
    history, _ = _deep_pages(_api_tx, 7000, budget)
    fallback, fallback_floor = _deep_pages(_v3_tx, 6500, budget)
    install_http(monkeypatch, {_HISTORY: history, _V3: fallback})

    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)

    assert ok is True
    assert source == "tonapi"
    assert gap == fallback_floor  # фолбэк зашёл дальше — дыра там
    assert transfers


async def test_full_pass_reports_no_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проход, дошедший до курсора, дыры не возвращает — false positives не будет."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(200, {"transactions": [_api_tx(1500, to_nano(0.02))]})],
            _V3: [AssertionError("фолбэк не должен вызываться")],
        },
    )

    transfers, ok, source, gap = await ton_watch._collect_transfers(1000)

    assert (ok, source, gap) == (True, "tonapi", None)
    assert len(transfers) == 1


# ---------- Парсер Toncenter v3 ----------


def test_v3_comment_decoded_and_old_transactions_skipped() -> None:
    fresh = _v3_tx(1500, to_nano(0.42), comment="rv:7")
    transfer = ton_watch._parse_toncenter_item(fresh, since_utime=1000)
    assert transfer is not None
    assert transfer.comment == "rv:7"
    assert transfer.provider_ref == "1500"

    assert ton_watch._parse_toncenter_item(_v3_tx(500, to_nano(1)), since_utime=1000) is None
    empty = _v3_tx(1500, 0)
    assert ton_watch._parse_toncenter_item(empty, since_utime=1000) is None


def test_tx_hash_normalized_across_providers() -> None:
    """Один хеш в разных кодировках провайдеров — одна строка идемпотентности."""
    raw = bytes(range(32))
    urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    standard = base64.b64encode(raw).decode()
    expected = raw.hex()
    assert ton_watch._norm_tx_hash(urlsafe) == expected
    assert ton_watch._norm_tx_hash(standard) == expected
    assert ton_watch._norm_tx_hash(expected.upper()) == expected
    # Мусор проходит насквозь, не падая.
    assert ton_watch._norm_tx_hash("c-0") == "c-0"


# ---------- Пагинация фолбэка ----------


async def test_toncenter_pagination_walks_by_lt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Полная страница уводит проход вглубь с курсором before_lt."""
    limit = ton_watch._PAGE_LIMIT
    page_one = [_v3_tx(2000 - index, to_nano(0.01)) for index in range(limit)]
    page_two = [_v3_tx(1500, to_nano(0.02)), _v3_tx(1400, to_nano(0.03))]
    calls = install_http(
        monkeypatch,
        {_V3: [_Response(200, {"transactions": page_one}), _Response(200, {"transactions": page_two})]},
    )

    result = await ton_watch._deep_collect(
        ton_watch._toncenter_page,
        lambda page: page[-1].provider_ref or page[-1].tx_hash,
        1000,
    )

    assert result.complete is True
    assert len(result.transfers) == limit + 2
    v3_calls = [params for url, params in calls if _V3 in url]
    assert len(v3_calls) == 2
    assert v3_calls[0]["account"] == TREASURY
    assert "before_lt" not in v3_calls[0]
    # Курсор второй страницы — младший lt первой (страница отсортирована desc).
    assert v3_calls[1]["before_lt"] == page_one[-1]["lt"]
