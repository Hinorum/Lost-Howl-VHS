"""Дешёвый зонд головы истории: пропуск полного прохода в здоровом простое.

Контракт, который здесь ловится:
- совпали lt+hash+источник, голова свежа, курсор не двигался — полный проход
  не выполняется, но сердцебиение и подтверждение ставок всё равно идут;
- изменилась голова/сброшен курсор/истекла свежесть/есть stuck, дыра или
  буст — полный проход обязателен (экономия не стоит пропущенного перевода);
- отпечаток головы пишется только после полного здорового прохода и только
  по тому же источнику, что читал страницы;
- сбой зонда (ok=False) — это «не знаю», а не «голова пуста»: проход идёт.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app import ton_head, ton_watch
from app.config import settings
from app.core.registry import (
    BEAT_KEY,
    CURSOR_KEY,
    PARSE_FAIL_COUNT_KEY,
    PARSE_FAIL_LAST_KEY,
    SCAN_BOOST_KEY,
    SCAN_GAP_KEY,
    SOURCE_KEY,
    STUCK_TX_KEY,
    WATCH_HEAD_CHECK_KEY,
    WATCH_HEAD_KEY,
)
from app.db import SessionLocal
from app.models import WatcherState

_BASE = int(datetime.now(UTC).timestamp()) - 3_600
_PROBE = ton_head.HeadProbe(lt=90_000, hash="head-hash", source="tonapi", ok=True)
_KEYS = (
    BEAT_KEY,
    CURSOR_KEY,
    PARSE_FAIL_COUNT_KEY,
    PARSE_FAIL_LAST_KEY,
    SCAN_BOOST_KEY,
    SCAN_GAP_KEY,
    SOURCE_KEY,
    STUCK_TX_KEY,
    WATCH_HEAD_CHECK_KEY,
    WATCH_HEAD_KEY,
)


async def _clean_state() -> None:
    async with SessionLocal() as db:
        await db.execute(delete(WatcherState).where(WatcherState.key.in_(_KEYS)))
        await db.commit()


@pytest.fixture(autouse=True)
async def _probe_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_head_probe_enabled", True)
    monkeypatch.setattr(settings, "watch_full_rescan_seconds", 1800)
    await _clean_state()
    yield
    await _clean_state()


@pytest.fixture
def side_effects(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Подтверждение ставок/банк дня не должны тянуть БД в этих тестах."""
    fake = AsyncMock()
    monkeypatch.setattr(ton_watch, "_watch_confirm_side_effects", fake)
    return fake


async def _seed(
    cursor: int,
    *,
    head: tuple[int, str, str] | None,
    head_cursor: int | None = None,
) -> None:
    async with SessionLocal() as db:
        db.add(WatcherState(key=CURSOR_KEY, value=str(cursor)))
        await db.commit()
        if head is not None:
            lt, head_hash, source = head
            await ton_head.write_head(
                db,
                key=WATCH_HEAD_KEY,
                check_key=WATCH_HEAD_CHECK_KEY,
                lt=lt,
                head_hash=head_hash,
                source=source,
                cursor=cursor if head_cursor is None else head_cursor,
            )


async def _stored_head() -> dict | None:
    async with SessionLocal() as db:
        return await ton_head.read_head(db, WATCH_HEAD_KEY)


# ---------- Чистые правила ----------


def test_same_head_requires_lt_hash_and_source() -> None:
    state = {"lt": 5, "hash": "h", "source": "tonapi", "cursor": 0}
    assert ton_head.same_head(state, ton_head.HeadProbe(5, "h", "tonapi", True))
    assert not ton_head.same_head(state, ton_head.HeadProbe(6, "h", "tonapi", True))
    assert not ton_head.same_head(state, ton_head.HeadProbe(5, "other", "tonapi", True))
    assert not ton_head.same_head(state, ton_head.HeadProbe(5, "h", "toncenter", True))
    assert not ton_head.same_head(state, ton_head.HeadProbe(5, "h", "tonapi", False))


def test_head_is_fresh_boundaries() -> None:
    now = datetime.now(UTC)
    assert ton_head.head_is_fresh(now.isoformat(), 60)
    assert not ton_head.head_is_fresh((now - timedelta(minutes=5)).isoformat(), 60)
    assert not ton_head.head_is_fresh(None, 60)
    assert not ton_head.head_is_fresh("не-дата", 60)
    assert not ton_head.head_is_fresh(now.isoformat(), 0)


# ---------- Watcher: пропуск и полный проход ----------


async def test_watch_skips_full_pass_when_head_matches(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=(_PROBE.lt, _PROBE.hash, _PROBE.source))
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )

    await ton_watch.watch_once()

    collect.assert_not_awaited()
    side_effects.assert_awaited_once()
    async with SessionLocal() as db:
        beat = await db.get(WatcherState, BEAT_KEY)
        source = await db.get(WatcherState, SOURCE_KEY)
    assert beat is not None and beat.value
    assert source is not None and source.value == "tonapi"


async def test_watch_runs_pass_and_records_head_when_head_changed(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=(_PROBE.lt - 1, "old-hash", "tonapi"))
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )

    await ton_watch.watch_once()

    collect.assert_awaited_once()
    assert await _stored_head() == {
        "lt": _PROBE.lt,
        "hash": _PROBE.hash,
        "source": "tonapi",
        "cursor": _BASE,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (STUCK_TX_KEY, '{"bad": {"utime": 1, "fails": 1}}'),
        (SCAN_GAP_KEY, '{"since": 1, "floor": 2, "max_pages": 100}'),
        (SCAN_BOOST_KEY, "2"),
    ],
)
async def test_watch_probe_not_even_queried_when_contour_is_unhealthy(
    key: str,
    value: str,
    side_effects: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(_BASE, head=(_PROBE.lt, _PROBE.hash, _PROBE.source))
    async with SessionLocal() as db:
        db.add(WatcherState(key=key, value=value))
        await db.commit()
    probe = AsyncMock(return_value=_PROBE)
    monkeypatch.setattr(ton_head, "fetch_head_probe", probe)
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)

    await ton_watch.watch_once()

    probe.assert_not_awaited()
    collect.assert_awaited_once()


async def test_watch_probe_failure_means_full_pass(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=(_PROBE.lt, _PROBE.hash, _PROBE.source))
    monkeypatch.setattr(
        ton_head,
        "fetch_head_probe",
        AsyncMock(return_value=ton_head.HeadProbe(0, "", "none", False)),
    )
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)

    await ton_watch.watch_once()

    collect.assert_awaited_once()
    # Неудачный зонд не переписывает старую голову.
    assert (await _stored_head())["lt"] == _PROBE.lt


async def test_watch_manual_cursor_reset_invalidates_head(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Голова записана для старого курсора; сырой курсор уехал вперёд.
    await _seed(
        _BASE + 500,
        head=(_PROBE.lt, _PROBE.hash, _PROBE.source),
        head_cursor=_BASE,
    )
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )

    await ton_watch.watch_once()

    collect.assert_awaited_once()
    assert (await _stored_head())["cursor"] == _BASE + 500


async def test_watch_stale_head_forces_periodic_full_pass(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=(_PROBE.lt, _PROBE.hash, _PROBE.source))
    async with SessionLocal() as db:
        row = await db.get(WatcherState, WATCH_HEAD_CHECK_KEY)
        assert row is not None
        row.value = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await db.commit()
    collect = AsyncMock(return_value=([], True, "tonapi", None, 0))
    monkeypatch.setattr(ton_watch, "_collect_transfers", collect)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )

    await ton_watch.watch_once()

    collect.assert_awaited_once()
    # Страховочный проход обновляет метку свежести — следующий цикл снова
    # может пропускаться.
    async with SessionLocal() as db:
        row = await db.get(WatcherState, WATCH_HEAD_CHECK_KEY)
    assert row is not None
    stamp = datetime.fromisoformat(row.value)
    assert datetime.now(UTC) - stamp < timedelta(minutes=1)


async def test_watch_head_not_written_on_partial_pass(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=None)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )
    monkeypatch.setattr(
        ton_watch,
        "_collect_transfers",
        AsyncMock(return_value=([], True, "tonapi", _BASE - 60, 0)),
    )

    await ton_watch.watch_once()

    assert await _stored_head() is None


async def test_watch_head_not_written_when_source_differs(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=None)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )
    # Зонд сходил в TonAPI, а полный проход дочитал через Toncenter: головы
    # разных индексаторов могут отставать друг от друга — отпечаток не пишем.
    monkeypatch.setattr(
        ton_watch,
        "_collect_transfers",
        AsyncMock(return_value=([], True, "toncenter", None, 0)),
    )

    await ton_watch.watch_once()

    assert await _stored_head() is None


async def test_watch_head_not_written_on_parse_failures(
    side_effects: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(_BASE, head=None)
    monkeypatch.setattr(
        ton_head, "fetch_head_probe", AsyncMock(return_value=_PROBE)
    )
    monkeypatch.setattr(
        ton_watch,
        "_collect_transfers",
        AsyncMock(return_value=([], True, "tonapi", None, 1)),
    )

    await ton_watch.watch_once()

    assert await _stored_head() is None


# ---------- Зонд: реальный запрос ----------


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


async def test_fetch_head_probe_falls_back_to_toncenter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", "0:" + "ab" * 32)
    calls: list[str] = []

    async def fake_get(client, url, params=None, headers=None):
        calls.append(url)
        if "tonapi" in url:
            raise RuntimeError("tonapi down")
        return _Response(
            {"transactions": [{"lt": 777, "hash": "head-hash-toncenter"}]}
        )

    monkeypatch.setattr(ton_head, "http_get_with_retry", fake_get)
    monkeypatch.setattr(ton_head, "get_http_client", lambda: object())

    probe = await ton_head.fetch_head_probe()

    assert probe.ok and probe.lt == 777 and probe.source == "toncenter"
    assert any("tonapi" in url for url in calls)
    assert any("toncenter" in url for url in calls)


async def test_fetch_head_probe_reports_failure_instead_of_empty_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", "0:" + "ab" * 32)

    async def broken(client, url, params=None, headers=None):
        raise RuntimeError("оба лежат")

    monkeypatch.setattr(ton_head, "http_get_with_retry", broken)
    monkeypatch.setattr(ton_head, "get_http_client", lambda: object())

    probe = await ton_head.fetch_head_probe()

    assert probe.ok is False
    assert probe.lt == 0 and probe.hash == ""


def test_probe_constants_are_sane() -> None:
    # Страховочный проход обязан быть чаще, чем «никогда»: 0 выключил бы
    # периодическую проверку и оставил бы только реакцию на голову.
    assert settings.watch_full_rescan_seconds > 0
    assert settings.treasury_mirror_full_rescan_seconds > 0
