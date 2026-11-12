"""Дешёвый зонд головы истории казначея: один транзакционный item вместо страницы.

Watcher входящих и зеркало казны в простое читали по странице истории
(25 транзакций ≈ 70 КБ) каждые несколько секунд/минут, хотя голова цепочки
не менялась. Тот же endpoint умеет отдать последнюю транзакцию (`limit=1`,
≈3 КБ): сравнение lt+hash+источника доказывает, что новых переводов выше
головы нет, и полный проход можно пропустить.

Почему отдельный модуль: зонд нужен двум контурам — `app.ton_watch` и
`app.treasury_mirror`. Общая реализация держит один набор правил (TonAPI →
Toncenter, нормализация хеша), а не две копии, которые разъедутся.

Важно: зонд — только условие ПРОПУСКА. Полный проход всё равно остаётся
страховкой по расписанию (`WATCH_FULL_RESCAN_SECONDS`,
`TREASURY_MIRROR_FULL_RESCAN_SECONDS`), потому что индексатор может
«дослать» старую транзакцию ниже неизменной головы. Совпадение головы
означает «сверху ничего нового», а не «история заморожена навсегда».
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import NamedTuple

from app.config import settings
from app.http_utils import get_http_client, http_get_with_retry
from app.models import WatcherState
from app.ton_codec import api_headers, norm_tx_hash, tonapi_headers

logger = logging.getLogger(__name__)


class HeadProbe(NamedTuple):
    """Голова истории: lt, хеш и источник, который её отдал.

    ok=False — оба индексатора не ответили или ответ не разобрался. Это не
    «голова пуста»: вызывающий обязан сделать полный проход, а не пропустить
    цикл. Пустая история — это ok=True с lt=0 и пустым хешем.
    """

    lt: int
    hash: str
    source: str
    ok: bool


def same_head(state: dict | None, probe: HeadProbe) -> bool:
    """Совпадает ли сохранённая голова с зондом (lt + хеш + источник).

    Источник сравнивается намеренно: при переключении TonAPI ↔ Toncenter
    индексаторы могут отдавать разные задержки головы, и один полный проход
    после переключения дешевле, чем доверие к чужому курсору.
    """
    if not state or not probe.ok:
        return False
    try:
        return (
            int(state.get("lt") or 0) == probe.lt
            and str(state.get("hash") or "") == probe.hash
            and str(state.get("source") or "") == probe.source
        )
    except (TypeError, ValueError):
        return False


def head_is_fresh(stamp: str | None, max_age_seconds: int) -> bool:
    """Не пора ли на страховочный полный проход.

    Битая/отсутствующая метка — «пора»: пропускать цикл без доказанной
    свежести сохранённой головы нельзя.
    """
    if not stamp or max_age_seconds <= 0:
        return False
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() <= max_age_seconds


async def fetch_head_probe() -> HeadProbe:
    """Последняя транзакция истории казначея: TonAPI, при сбое — Toncenter.

    Ошибки не поднимаются: неудачный зонд возвращает ok=False, и вызывающий
    делает обычный полный проход. Так сбой зонда не превращается в
    пропущенный цикл, а лишь отменяет оптимизацию.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return HeadProbe(0, "", "none", False)
    treasury = settings.active_treasury_address
    client = get_http_client()
    candidates = (
        (
            "tonapi",
            f"{settings.active_ton_api_base.rstrip('/')}/v2/blockchain/accounts/"
            f"{treasury}/transactions",
            {"limit": 1, "sort_order": "desc"},
            tonapi_headers(settings.ton_api_key),
        ),
        (
            "toncenter",
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions",
            {"account": treasury, "limit": 1, "sort": "desc"},
            api_headers(settings.toncenter_api_key),
        ),
    )
    for source, url, params, headers in candidates:
        try:
            response = await http_get_with_retry(
                client, url, params=params, headers=headers
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            items = payload.get("transactions") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                # 200 без списка — сбой формы ответа, а не «история пуста».
                continue
            if not items:
                return HeadProbe(0, "", source, True)
            item = items[0]
            if not isinstance(item, dict):
                continue
            raw_hash = str(item.get("hash") or "")
            if not raw_hash:
                continue
            return HeadProbe(
                int(item.get("lt") or 0),
                norm_tx_hash(raw_hash),
                source,
                True,
            )
        except Exception as exc:
            # Тихий debug: полный проход ниже сам расскажет о деградации
            # индексатора, а зонд не должен удваивать шум каждый цикл.
            logger.debug("Зонд головы %s недоступен: %s", source, exc)
    return HeadProbe(0, "", "none", False)


# ---------- Хранение отпечатка головы (watcher_state) ----------


async def read_head(session, key: str) -> dict | None:
    """Отпечаток головы из watcher_state или None.

    Битая запись — None (как отсутствие): пропускать цикл по непроверенной
    голове нельзя. Поле cursor (0 по умолчанию) ловит ручной сброс курсора
    там, где он есть.
    """
    row = await session.get(WatcherState, key)
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        logger.warning("Отпечаток головы %s повреждён (%r) — будет полный проход", key, row.value[:128])
        return None
    if not isinstance(value, dict):
        return None
    try:
        return {
            "lt": int(value.get("lt") or 0),
            "hash": str(value.get("hash") or ""),
            "source": str(value.get("source") or ""),
            "cursor": int(value.get("cursor") or 0),
        }
    except (TypeError, ValueError):
        return None


async def read_check(session, key: str) -> str | None:
    """ISO-время последнего полного прохода (None — прохода ещё не было)."""
    row = await session.get(WatcherState, key)
    return row.value if row is not None else None


async def write_head(
    session,
    *,
    key: str,
    check_key: str,
    lt: int,
    head_hash: str,
    source: str,
    cursor: int,
) -> None:
    """Запомнить голову и время полного прохода одним коммитом.

    Пишется ТОЛЬКО после полного успешного прохода (без дыры и деградации):
    иначе пропуск следующего цикла опирался бы на непрочитанное окно.
    """
    payload = json.dumps(
        {"lt": int(lt), "hash": head_hash, "source": source, "cursor": int(cursor)},
        ensure_ascii=False,
    )
    stamp = datetime.now(UTC).isoformat()
    for name, value in ((key, payload), (check_key, stamp)):
        row = await session.get(WatcherState, name)
        if row is None:
            session.add(WatcherState(key=name, value=value))
        else:
            row.value = value
    await session.commit()
