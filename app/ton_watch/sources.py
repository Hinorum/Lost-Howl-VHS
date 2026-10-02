"""Источники входящих переводов казначея: TonAPI (основной) и Toncenter (фолбэк).

Страницы, разбор транзакций в Transfer, слияние источников без дублей. Кеш
индексатора не различаем: «пусто» бывает и правдой, и поломкой, поэтому
проверяем карточку аккаунта."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import NamedTuple

# Клиент и алиасы ton_codec живут в корне пакета. Обращаемся к ним через
# _pkg.<имя> в момент вызова, а не через from-import: тесты подменяют
# app.ton_watch.get_http_client и кодек-алиасы скриптованным клиентом, и
# подмена обязана доходить до этого модуля. Прямой импорт заморозил бы
# ссылку на момент загрузки пакета.
from app import ton_watch as _pkg
from app.config import settings
from app.http_utils import http_get_with_retry

logger = logging.getLogger(__name__)

# Джеттон-уведомления одинаковые от всех держателей: достаточно предупредить
# один раз на адрес, иначе watcher засыпает логами.
_warned_jettons: set[str] = set()

# Метка последнего предупреждения о деградации основного индексатора, чтобы не
# сыпать одинаковое сообщение каждый цикл.
_last_fallback_warning_at = 0.0

_PAGE_LIMIT = max(1, settings.watch_page_limit)

_MAX_PAGES = max(1, settings.watch_max_pages)

_EMPTY_STOP = 2


@dataclass
class Transfer:
    tx_hash: str
    source: str
    value_nanotons: int
    comment: str
    utime: int
    # Пагинационный ключ провайдера (Toncenter v3 требует lt, TonAPI — хеш);
    # для TonAPI-переводов остаётся пустым.
    provider_ref: str = ""


class PassResult(NamedTuple):
    """Итог одного прохода по страницам одного провайдера.

    reliable — страницы отдались живо (деградация провайдера делает проход
    непригодным как опору для курсора).
    complete — проход дошёл до курсора или до конца истории. False бывает двух
    видов: провайдер лежал (тогда reliable=False) и бюджет страниц исчерпан
    раньше курсора (тогда reliable=True, но окно прочитано не целиком).
    floor_utime — utime самой старой увиденной транзакции: ею помечается дыра,
    чтобы её можно было назвать, а не потерять молча.
    """

    transfers: list[Transfer]
    reliable: bool
    complete: bool
    floor_utime: int | None

async def fetch_recent_transfers(since_utime: int, before_lt: str | None = None) -> tuple[list[Transfer], bool]:
    """Страница входящих переводов казначея активной сети (новые сверху).

    Ошибки сети не поднимают исключение: возвращается (пусто, False), чтобы
    цикл знал, что проверка не состоялась, и не ставил сердцебиение.
    before_lt — пагинация вглубь по логическому времени (lt): каждая следующая
    страница строго старше последнего lt предыдущей.

    Честная работа с 404: раньше «нет истории» считалось здоровьем, и падение
    индексатора TonAPI маскировалось под тихую цепочку (реальный инцидент:
    ставки не находятся, а /health зелёный). Теперь 404 перепроверяется по
    /v2/accounts/{адрес}: если аккаунт активен и у него есть активность после
    курсора — история TonAPI врёт, цикл считается несостоявшимся (False),
    и _collect_transfers переключается на фолбэк Toncenter v3.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], True
    url = (
        f"{settings.active_ton_api_base}/v2/blockchain/accounts/"
        f"{settings.active_treasury_address}/transactions"
    )
    headers = getattr(_pkg, "_tonapi_headers", getattr(_pkg, "_api_headers_tonapi", _pkg._api_headers))(settings.ton_api_key)
    try:
        client = _pkg.get_http_client()
        response = await http_get_with_retry(
            client,
            url,
            params={
                "limit": _PAGE_LIMIT,
                "sort_order": "desc",
                **({"before_lt": before_lt} if before_lt else {}),
            },
            headers=headers,
        )
        if response.status_code == 404:
            # Пустая история бывает у двух причин: кошелёк правда молчал
            # или индексатор потерял историю. Различаем честно.
            return await _resolve_tonapi_empty_history(since_utime)
        response.raise_for_status()
        items = response.json().get("transactions", [])
    except Exception as exc:
        logger.warning("TonAPI недоступен: %s", exc)
        return [], False
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_tx_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, True

async def _resolve_tonapi_empty_history(since_utime: int) -> tuple[list[Transfer], bool]:
    """404 истории транзакций: «правда пусто» или «индекс сломан»?

    Сверяемся с карточкой аккаунта: активный кошелёк с активностью после
    курсора при пустой истории — деградация индексатора. Не сумели проверить
    (сеть/не-200) — тоже считаем цикл несостоявшимся: лучше лишний проход
    через фолбэк, чем пропущенная ставка.
    """
    info = await _tonapi_account_info()
    if not isinstance(info, dict):
        logger.warning(
            "TonAPI отдал 404 истории транзакций, но карточка аккаунта недоступна — "
            "цикл не признаётся успешным, переводы пойдут через фолбэк"
        )
        return [], False
    status = str(info.get("status") or "").strip().lower()
    try:
        last_activity = int(info.get("last_activity") or 0)
    except (TypeError, ValueError):
        last_activity = 0
    if status == "active" and last_activity > since_utime:
        logger.warning(
            "TonAPI отдал 404 истории транзакций при активном казначее с активностью %s "
            "(курсор %s) — индекс истории деградировал",
            last_activity,
            since_utime,
        )
        return [], False
    return [], True

async def _tonapi_account_info() -> dict | None:
    """Карточка казначея в TonAPI (/v2/accounts/{адрес}) или None при сбое."""
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}"
    try:
        client = _pkg.get_http_client()
        hdr = getattr(
            _pkg,
            "_tonapi_headers",
            getattr(_pkg, "_api_headers_tonapi", _pkg._api_headers),
        )(settings.ton_api_key)
        response = await http_get_with_retry(client, url, headers=hdr)
    except Exception as exc:
        logger.warning("TonAPI не ответил на запрос карточки аккаунта: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None

_JETTON_OPCODES = {"0x7362d09c"}

def _is_jetton_notification(in_msg: dict) -> bool:
    opcode = str(in_msg.get("opcode") or "").strip().lower()
    if opcode in _JETTON_OPCODES:
        return True
    msg_data = in_msg.get("msg_data")
    if isinstance(msg_data, dict):
        decoded_op = str(msg_data.get("decoded_op") or "").strip().lower()
        if decoded_op == "transfer_notification":
            return True
    return False

def _parse_tx_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция страницы -> Transfer либо None (старая/джеттон/мусор).

    Джеттон-уведомление — это НЕ ставка: value внутри обёртки — копейки
    газа, источник — jetton-кошелёк игрока. Такой перевод нельзя ни
    зачесть, ни автоматически вернуть, поэтому он пропускается целиком,
    без пыльного refund-payout: токены ждут ручного возврата с казначея.
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("utime", 0))
        if utime <= since_utime:
            return None
        if _is_jetton_notification(in_msg):
            tx_hash = str(item.get("hash") or "")
            if tx_hash and tx_hash not in _warned_jettons:
                if len(_warned_jettons) > 256:
                    _warned_jettons.clear()
                _warned_jettons.add(tx_hash)
                logger.warning(
                    "Входящий перевод %s… — токен (jetton), а не нативный Gram/TON. Ставкой не становится "
                    "и автоматически не возвращается: верни вручную с казначея.",
                    tx_hash[:16],
                )
            return None
        source = ((in_msg.get("source") or {}).get("address")) or ""
        value = int(in_msg.get("value") or 0)
        if value <= 0 or not source:
            return None
        return Transfer(
            tx_hash=_pkg._norm_tx_hash(str(item.get("hash") or "")),
            source=source,
            value_nanotons=value,
            comment=_pkg._decode_comment(in_msg),
            utime=utime,
            provider_ref=str(item.get("lt") or ""),
        )
    except Exception as exc:
        logger.warning("Странная транзакция пропущена: %s", exc)
        return None

def _parse_toncenter_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция Toncenter v3 -> Transfer либо None (старая/пустая).

    Джеттон-уведомления в выборку по аккаунту казначея не попадают вовсе
    (они садятся на jetton-кошелёк отправителя), поэтому отдельного фильтра,
    как у TonAPI, здесь не нужно. Комментарий приходит декодированным в
    message_content.decoded с типом «comment».
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("now") or 0)
        if utime <= since_utime:
            return None
        source = in_msg.get("source") or ""
        if isinstance(source, dict):
            source = source.get("address") or ""
        value = int(str(in_msg.get("value") or 0))
        if value <= 0 or not source:
            return None
        decoded = (in_msg.get("message_content") or {}).get("decoded") or {}
        comment = ""
        if isinstance(decoded, dict) and decoded.get("@type") in ("comment", "text_comment"):
            comment = _pkg._clean_comment(str(decoded.get("comment") or ""))
        return Transfer(
            tx_hash=_pkg._norm_tx_hash(str(item.get("hash") or "")),
            source=str(source),
            value_nanotons=value,
            comment=comment,
            utime=utime,
            provider_ref=str(item.get("lt") or ""),
        )
    except Exception as exc:
        logger.warning("Странная транзакция Toncenter пропущена: %s", exc)
        return None

_TONCENTER_MAX_LIMIT = 256

async def _toncenter_page(since_utime: int, before_lt: str | None = None) -> tuple[list[Transfer], str]:
    """Страница переводов казначея через Toncenter API v3 (фолбэк TonAPI).

    Контракт как у fetch_recent_transfers, но вместо булева — состояние
    страницы (_PAGE_OK/_PAGE_DEGRADED): фолбэк вызывается, только когда
    основной источник деградировал. Пагинация вглубь по before_lt.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], _PAGE_OK
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    params: dict = {
        "account": settings.active_treasury_address,
        "limit": min(_PAGE_LIMIT, _TONCENTER_MAX_LIMIT),
        "sort": "desc",
    }
    if before_lt:
        params["before_lt"] = before_lt
    try:
        client = _pkg.get_http_client()
        hdr = _pkg._api_headers(settings.toncenter_api_key)
        response = await http_get_with_retry(client, url, params=params, headers=hdr)
        response.raise_for_status()
        items = response.json().get("transactions") or []
    except Exception as exc:
        logger.warning("Toncenter v3 недоступен: %s", exc)
        return [], _PAGE_DEGRADED
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_toncenter_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, _PAGE_OK

_PAGE_OK = "ok"

_PAGE_DEGRADED = "degraded"

_FALLBACK_WARN_EVERY_SECONDS = 600.0

def _warn_degraded_primary() -> None:
    global _last_fallback_warning_at
    now = time.monotonic()
    if now - _last_fallback_warning_at < _FALLBACK_WARN_EVERY_SECONDS:
        return
    _last_fallback_warning_at = now
    logger.warning(
        "TonAPI деградировал (ошибка сети или 404 истории при живом казначее) — "
        "переводы читаются через фолбэк Toncenter v3"
    )

async def _tonapi_page(since_utime: int, before_lt: str | None) -> tuple[list[Transfer], str]:
    """Адаптер основного источника под единый контракт (список, состояние)."""
    # fetch_recent_transfers -- через корень пакета: тесты подменяют его
    # скриптованным источником, и подмена обязана дойти до этого адаптера.
    transfers, ok = await _pkg.fetch_recent_transfers(since_utime, before_lt=before_lt)
    return transfers, (_PAGE_OK if ok else _PAGE_DEGRADED)

async def _deep_collect(
    fetch_page,
    cursor_of,
    since: int,
    max_pages: int | None = None,
) -> PassResult:
    """Глубокий проход по страницам одного провайдера.

    Возвращает PassResult: переводы, надёжен ли проход, дошёл ли он до курсора
    и где лежит дно прохода. Ненадёжная страница обрывает проход: частичный
    результат сохраняется, но вызывающий обязан не считать такой цикл успешным.
    Уходим вглубь до max_pages × _PAGE_LIMIT переводов; пустое место (история
    короче страницы или две страницы подряд без новых переводов) завершает проход
    досрочно. Курсор хранится в БД, поэтому покрытие кумулятивно: после простоя
    накопившийся хвост догоняется за несколько минут.

    Отдельный случай — бюджет страниц исчерпан, а до курсора не дошли: проход
    надёжен, но окно ниже floor_utime осталось непрочитанным. Раньше такой
    проход объявлялся полным (complete=True), и цикл двигал курсор вперёд по
    самой свежей транзакции — непрочитанное окно исчезало навсегда: тихо, без
    записи и без тревоги. Теперь complete=False, а floor_utime становится
    границей покрытия: курсор встаёт на неё, и следующий проход продолжает
    читать вглубь оттуда.
    """
    transfers: list[Transfer] = []
    seen: set[str] = set()
    before: str | None = None
    empty_pages = 0
    floor_utime: int | None = None
    for _page in range(max_pages or _MAX_PAGES):
        page, state = await fetch_page(since, before)
        if state != _PAGE_OK:
            return PassResult(transfers, False, False, floor_utime)
        fresh = [t for t in page if t.tx_hash and t.tx_hash not in seen]
        for item in fresh:
            seen.add(item.tx_hash)
        transfers.extend(fresh)
        if fresh:
            page_floor = min(item.utime for item in fresh)
            floor_utime = page_floor if floor_utime is None else min(floor_utime, page_floor)
        if not page or len(page) < _PAGE_LIMIT:
            return PassResult(transfers, True, True, floor_utime)  # история кончилась — глубже пусто
        oldest = page[-1]
        if oldest.utime <= since:
            return PassResult(transfers, True, True, floor_utime)  # страница дотянулась до курсора
        if not fresh:
            empty_pages += 1
            if empty_pages >= _EMPTY_STOP:
                return PassResult(transfers, True, True, floor_utime)  # подряд страницы без новых переводов
        else:
            empty_pages = 0
        before = cursor_of(page)
        await asyncio.sleep(0.12)  # бережём лимиты API на глубоком проходе
    # Бюджет страниц исчерпан, курсор не достигнут: окно ниже floor_utime не
    # прочитано. Это дыра, а не полный проход — вызывающий обязан её учесть.
    return PassResult(transfers, True, False, floor_utime)

def _merge_unique(batches: list[list[Transfer]]) -> list[Transfer]:
    """Слияние результатов источников без дублей, по возрастанию utime."""
    seen: set[str] = set()
    merged: list[Transfer] = []
    for batch in batches:
        for transfer in batch:
            if transfer.tx_hash and transfer.tx_hash not in seen:
                seen.add(transfer.tx_hash)
                merged.append(transfer)
    return sorted(merged, key=lambda item: item.utime)

async def _collect_transfers(
    since: int, max_pages: int | None = None
) -> tuple[list[Transfer], bool, str, int | None]:
    """Все переводы после курсора: основной источник + фолбэк Toncenter.

    Основной проход TonAPI'ем; если он не удался (сеть легла или индекс отдаёт
    404 истории при живом кошельке) — тот же проход повторяется по Toncenter v3,
    результаты сливаются без дублей. Источник успешного прохода возвращается
    третьим значением для /health.

    Четвёртое значение — utime дна дыры, когда проход надёжен, но не вычитал
    окно до курсора (кончился бюджет страниц). Это НЕ «провал цикла»: новые
    переводы обработаны, а прочитанное покрывает всё выше этого дна. Курсор
    встаёт ровно на него (граница покрытия), и следующий проход стартует оттуда
    же, уходя вглубь — поэтому бэклог догоняется, а не теряется. Пока дыра
    жива, тревогу по ней держит ops.py.
    """
    # Страницы достаём из пакета в момент вызова, а не из своих глобалов:
    # тесты подменяют app.ton_watch._tonapi_page / ._toncenter_page (сценарий
    # «оба индексатора лежат»), и подмена обязана дойти до этого цикла.
    from app.ton_watch import _tonapi_page, _toncenter_page

    primary = await _deep_collect(
        _tonapi_page, lambda page: page[-1].provider_ref or page[-1].tx_hash, since, max_pages
    )
    if primary.complete:
        return primary.transfers, True, "tonapi", None
    if not primary.reliable:
        _warn_degraded_primary()
    fallback = await _deep_collect(
        _toncenter_page, lambda page: page[-1].provider_ref or page[-1].tx_hash, since, max_pages
    )
    merged = _merge_unique([primary.transfers, fallback.transfers])
    if fallback.complete:
        return merged, True, "toncenter", None
    if not primary.reliable and not fallback.reliable:
        logger.error(
            "ОБА индексатора (%s) недоступны: переводы не читаются! "
            "Проверь TonAPI/Toncenter или перезапусти сервис.",
            "testnet" if settings.is_testnet else "mainnet",
        )
        return merged, False, "none", None
    # Хотя бы один проход надёжен — цикл успешен, но окно прочитано не целиком.
    # Дно дыры — самая глубокая граница, до которой дошёл живой проход: выше
    # неё покрытие полное, курсор встанет на неё, и бэклог догонится глубже.
    reachable = [
        result.floor_utime
        for result in (primary, fallback)
        if result.reliable and result.floor_utime is not None
    ]
    gap_at = min(reachable) if reachable else None
    logger.error(
        "Проход не вычитал окно входящих до utime %s при курсоре %s: бюджет страниц исчерпан, "
        "часть окна не прочитана (курсор за дыру не сдвинется, бюджет страниц поднят)",
        gap_at,
        since,
    )
    return merged, True, ("tonapi" if primary.reliable else "toncenter"), gap_at
