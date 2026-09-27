"""Исходящие выплаты победителям: расчёт в stakes.finalize_day_payouts,
отправка здесь — через pytoniq напрямую к лайтсерверам активной сети.
Казначейский кошелёк поддерживается в двух версиях контракта — v4r2 и
v5r1 (кошельки нового поколения): версия детектируется автоматически по
адресу казначея либо задаётся явно переменной TREASURY_WALLET_VERSION.

Жизненный цикл выплаты: pending → sending (зафиксировано ДО вещания, чтобы
падение сервиса не привело к двойной отправке) → sent / failed. Зависшие
sending и неуспешные failed с attempts < PAYOUT_MAX_ATTEMPTS оживают каждый
цикл автоматически; при исчерпании лимита админ получает алерт. Перед
ПОВТОРНОЙ отправкой (attempts > 1) очередь сверяется с memo недавних
исходящих казначея: если перевод уже ушёл в цепочку в прошлый раз, он
помечается sent без повтора — краш между вещанием и коммитом не задваивает
платёж. Ручной retry (/payout, resolve_dead_payout) счётчик попыток НЕ
сбрасывает: он сам мог повернуть в очередь уже ушедший перевод, и только
attempts >= 1 заставляет диспетчер сверяться с историей перед отправкой.

Призы и возвраты без получателя (игрок не привязал кошелёк к моменту
финализации) не тонут в failed: строки ждут в очереди, и когда игрок
привязывает адрес, диспетчер вставляет его в следующий же цикл и платёж
уходит сам. Доли казны без OWNER_WALLET_ADDRESS и переводы без игрока
честно падают в failed с причиной-действием.

tx_hash после отправки — метка вещания «bcast:<unix>»: лайтсервер не
возвращает хеш транзакции. Фактический перевод ищется в эксплорере по адресу
казначея и memo-комментарию вида way:<день>:<тип>#<id выплаты>.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, or_, select, update

from app.config import settings
from app.db import SessionLocal
from app.http_utils import get_http_client, http_get_with_retry, http_post_with_retry
from app.models import Income, Payout, Player, Round, RoundStatus, WatcherState
from app.stakes import finalize_day_payouts
from app.ton_codec import api_headers, extract_comment
from app.ton_utils import friendly_address, from_nano, normalize_address, to_nano

logger = logging.getLogger(__name__)

# Синглтон кошелька казначея: подключение к лайтсерверам дорогое, держим одно.
_wallet_lock = asyncio.Lock()
_provider = None
_wallet = None
_wallet_network: str | None = None

# Батч-контекст диспетчера: seqno кошелька, взятый ОДИН раз на цикл, и счётчик
# локально наращиваемый на каждый перевод пачки. Без него каждый вызов
# wallet.transfer() делал бы новый get_seqno() у сети: два перевода подряд
# (приз + рейк одного дня) получали бы ОДИН seqno, в блок входил бы только
# один, второй тихо терялся, хотя лайтсервер возвращал результат 1.
_batch_seqno: int | None = None

# Сериализация очереди выплат: dispatch_pending_payouts вызывают ЗАКРЫТИЕ дня
# (кик в тике), ton-settle (каждые 120 с) и ручные /finalize, /return,
# /refinalize. _reset_retriable оживляет строки sending → pending, поэтому
# без лока второй цикл, стартовавший, пока первый вещает, задвоил бы платёж.
_DISPATCH_LOCK = asyncio.Lock()

# Размер страницы истории казначея: одна страница (128 tx) слишком мелкая для
# анти-дубля в длинной очереди — memo «уже отправленного» уходит за окно, и
# сверка думает «перевода нет». Глубина покрытия задаётся настройками
# payout_reconcile_history_seconds / payout_reconcile_max_pages.
_RECONCILE_PAGE_LIMIT = 128
# Шаг пагинации: страница шагает НЕ на весь лимит, а с перекрытием хвоста
# (16 записей). В живой казне между двумя запросами может прийти новая
# транзакция, и граница ровно «128...256» сползут: memo на стыке уедет за край
# недосчитанной страницы. Перекрытие перечитывает стык — карта memo→хеш
# идемпотентна, лишнее перечтение безвредно, а дыры не бывает.
_RECONCILE_PAGE_OVERLAP = 16
# Доступна ли история казначея В ПОСЛЕДНЕМ опросе. Пусто set() в маркерах
# означает и «транзакций нет вообще», и «провайдеры молчат»; диспетчеру при
# повторе (>1 попытки) это различие критично: переотправка без возможности
# проверить memo = риск задвоить уже ушедший перевод. Флаг ставится в
# fetch_broadcast_tx_map реальными вызовами (True при успехе любого провайдера,
# False когда оба упали). Стартовое True = «история доступна»: до первого цикла
# диспетчер всё равно ходит за маркерами до ретраев.
_RECONCILE_HISTORY_OK = True
# Переключение казначея на HTTP-канал (лайтсерверы режутся окружением):
# запоминаем момент первого включения в процессе и стучим хранителю об этом
# не чаще раза в _HTTP_CHANNEL_ALERT_COOLDOWN, чтобы на появление канала в
# логах не сосать глаза, а статус был виден в чате.
_http_channel_engaged_at: datetime | None = None
_last_http_channel_alert_at: datetime | None = None
_HTTP_CHANNEL_ALERT_COOLDOWN = timedelta(hours=6)
# TONCENTER_API_KEY отсутствует: канал работает под анонимным лимитом и может
# получить 429. Кричать об этом в лог один раз за процесс, не каждую выплату.
_warned_no_toncenter_key = False


@asynccontextmanager
async def dispatch_lock():
    """Лок очереди выплат для ручных блокирующих операций (admin /refinalize):
    удаление/перемаркировка строк не должна попадать в цикл диспетчера."""
    async with _DISPATCH_LOCK:
        yield

# Глобальный идентификатор сети (конфиг #19 блокчейна): входит в wallet_id
# контракта v5, поэтому с одной мнемоникой тестнет- и мейннет-v5-кошельки
# имеют разные адреса.
NETWORK_GLOBAL_IDS = {"mainnet": -239, "testnet": -3}
# Поддерживаемые версии контракта казначея.
WALLET_VERSIONS = ("v4r2", "v5r1")


def _wallet_address(version: str, public_key: bytes, network_global_id: int, wc: int = 0) -> str:
    """Адрес кошелька данной версии для ключа — чистая локальная математика.

    Адрес = хеш StateInit(code + data), сеть не нужна. Data-ячейка v4 сеть не
    задаёт (адрес одинаков в обеих сетях), у v5 network_global_id входит в
    wallet_id внутри data.
    """
    from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE, WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5R1
    from pytoniq_core.tlb.account import StateInit

    if version == "v4r2":
        data = WalletV4R2.create_data_cell(public_key=public_key, wc=wc)
        code = WALLET_V4_R2_CODE
    elif version == "v5r1":
        data = WalletV5R1.create_data_cell(public_key=public_key, wc=wc, network_global_id=network_global_id)
        code = WALLET_V5_R1_CODE
    else:
        raise ValueError(f"Неизвестная версия кошелька казначея: {version}")
    state_init = StateInit(code=code, data=data)
    return f"{wc}:{state_init.serialize().hash.hex()}"


def _detect_wallet_version(
    public_key: bytes, treasury_address: str, network_global_id: int
) -> tuple[str | None, dict[str, str]]:
    """Версия кошелька, чей производный адрес совпал с настроенным.

    Возвращает (версия | None, {версия: адрес-кандидат}) — кандидаты идут в
    текст ошибки, чтобы расхождение мнемоники и адреса было видно сразу.
    """
    target = normalize_address(treasury_address)
    candidates = {
        version: _wallet_address(version, public_key, network_global_id)
        for version in WALLET_VERSIONS
    }
    for version, address in candidates.items():
        if normalize_address(address) == target:
            return version, candidates
    return None, candidates


async def pending_payout_count(session) -> int:
    """Сколько переводов ещё не ушли (обе сети, включая dead-letter failed).

    «sent» — единственное конечное состояние успеха; «dismissed» — ручной
    вердикт хранителя (спам-перевод с рекламой и т.п.), он деньгам игрокам
    не равен и сбросу не мешает. Всё остальное значит, что деньги игроку
    ещё должны: сброс игры обязан ждать, пока долг закрыт.
    """
    result = await session.execute(
        select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
    )
    return int(result.scalar_one())


async def resolve_dead_payout(session, payout_id: int, action: str) -> str | None:
    """Ручной разбор проблемной выплаты хранителем.

    action="spam" — статус «dismissed»: пыльный спам-перевод с рекламой,
    возврат которого не нужен или невозможен. Выплата исчезает из очереди,
    алертов и перестаёт блокировать /resetgame. action="retry" — обратно в
    очередь (настоящий долг игроку). Счётчик попыток НЕ сбрасываем: попытка
    могла реально уйти в цепочку (краш между вещанием и коммитом «sent»), и
    повтор без сверки с memo казначея задвоил бы платёж. Значение attempts
    >= 1 гарантирует, что диспетчер прогонит анти-дубль по истории исходящих.
    Возвращает новый статус или None, если выплаты нет либо она уже отправлена.
    """
    payout = await session.get(Payout, payout_id)
    if payout is None or payout.status == "sent":
        return None
    if action == "spam":
        payout.status = "dismissed"
    elif action == "retry":
        payout.status = "pending"
        payout.alerted = False
    else:
        return None
    await session.commit()
    logger.info("Выплата %d разобрана вручную: %s", payout_id, payout.status)
    return payout.status


async def _fetch_remote_json(url: str) -> dict:
    """Скачивает JSON (конфиг лайтсерверов) с редиректами."""
    client = get_http_client()
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _get_wallet():
    """Ленивая инициализация кошелька казначея для активной сети.

    Версия контракта — из TREASURY_WALLET_VERSION («auto» = детект по адресу).
    Проверка пары мнемоника/адрес выполняется ДО подключения к сети: если
    производный адрес не совпал, отправлять нельзя в принципе — падаем с
    внятной ошибкой, а не молчаливыми неудачными выплатами. Источник
    лайтсерверов: LITESERVER_CONFIG_URL (свежий JSON), иначе встроенный
    конфиг pytoniq для сети.
    """
    global _provider, _wallet, _wallet_network
    network = "testnet" if settings.is_testnet else "mainnet"
    async with _wallet_lock:
        if _wallet is not None and _wallet_network == network:
            return _wallet
        if not settings.active_treasury_mnemonic:
            raise ValueError("Нет мнемоники казначея для активной сети")
        if not settings.active_treasury_address:
            raise ValueError("Нет адреса казначея для активной сети")
        words = settings.active_treasury_mnemonic.replace("\n", " ").split()
        if len(words) < 12:
            raise ValueError("Мнемоника казначея неполная (нужно 24 слова)")

        from pytoniq import LiteBalancer
        from pytoniq.contract.wallets.wallet import WalletV4R2
        from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

        _, private_key = mnemonic_to_private_key(words)
        public_key = private_key_to_public_key(private_key)
        network_global_id = NETWORK_GLOBAL_IDS[network]

        requested = settings.treasury_wallet_version.strip().lower()
        if requested in WALLET_VERSIONS:
            derived = _wallet_address(requested, public_key, network_global_id)
            if normalize_address(derived) != normalize_address(settings.active_treasury_address):
                raise ValueError(
                    f"Адрес казначея не совпадает с производным от мнемоники "
                    f"(TREASURY_WALLET_VERSION={requested}): {derived}. "
                    "Проверь пару мнемоника/адрес или верни auto."
                )
            version = requested
        else:
            version, candidates = _detect_wallet_version(
                public_key, settings.active_treasury_address, network_global_id
            )
            if version is None:
                raise ValueError(
                    "Адрес казначея не совпадает ни с одной поддерживаемой версией "
                    f"кошелька для этой мнемоники: {candidates}. Проверь адрес и "
                    "мнемонику, либо задай TREASURY_WALLET_VERSION=v4r2|v5r1 явно."
                )

        if _provider is not None:
            try:
                await _provider.close_all()
            except Exception:
                logger.warning("Не удалось закрыть старый провайдер лайтсерверов", exc_info=True)
            _provider = None
            _wallet = None
        if settings.liteserver_config_url:
            config = await _fetch_remote_json(settings.liteserver_config_url)
            _provider = LiteBalancer.from_config(config)
            logger.info("Лайтсерверы: конфиг из LITESERVER_CONFIG_URL")
        elif network == "testnet":
            _provider = LiteBalancer.from_testnet_config()
        else:
            _provider = LiteBalancer.from_mainnet_config()
        await _provider.start_up()
        if version == "v5r1":
            _wallet = await WalletV5R1.from_private_key(
                _provider, private_key=private_key, wc=0, network_global_id=network_global_id
            )
        else:
            _wallet = await WalletV4R2.from_private_key(_provider, private_key, wc=0)
        _wallet_network = network
        logger.info("Кошелёк казначея готов (%s, контракт %s)", network, version)
        return _wallet


def _comment_cell(text: str):
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


def _payout_comment_candidates(payout) -> list[str]:
    """Все комментарии, которыми эта выплата МОГЛА уйти в цепочку.

    Служебное memo «way:<день>:<тип>#<id>» уникально глобально — на нём держится
    анти-дубль. Свободный текст переопределения (возвраты при паузе) общий для
    многих выплат: если слать его как есть, два возврата с одинаковым текстом
    становятся НЕРАЗЛИЧИМЫ для сверки — таймаут вещания одной строки прочитается
    как «уже ушла» по чужому переводу, и игрок не получит деньги.

    Поэтому переопределение дополняется служебным суффиксом (уникальный ключ
    сохраняется даже у строки на 120 символов — сам текст усекается), а
    первичный кандидат идёт в цепочку. Второй кандидат — сырой текст: это
    легаси-строки, отправленные ДО введения суффикса; их сверка должна уметь
    находить их в истории, иначе вернёт в очередь уже разосланное.
    """
    unique = f"way:{payout.round_id}:{payout.kind}#{payout.id}"
    override = payout.comment_override
    if not override:
        return [unique]
    suffix = f" | {unique}"
    available = max(0, 120 - len(suffix))
    return [f"{override[:available]}{suffix}", override]


async def _send_raw_with_seqno(wallet, seqno: int, dest_address: str, amount_nanotons: int, body) -> int:
    """Один перевод с ЗАДАННЫМ seqno (без get_seqno у сети).

    Собирает внутреннее сообщение, подписывает external-сообщение кошелька
    с явным seqno и вещает через лайтсерверы. Версии контракта отличаются
    параметром wallet_id: v5 держит network_global_id в wallet_id (тестнет и
    мейннет — разные адреса), v4 — константу. Используем wallet.wallet_id,
    который кошелёк сам знает из собственного state.
    """
    from pytoniq_core import Address

    internal = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=body,
    )
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[internal],
    )
    return await wallet.send_external(body=transfer_msg)


# ---------- Анти-дубль: сверка memo с историей казначея ----------


def _out_comments(item: dict) -> list[str]:
    """Комментарии исходящих сообщений одной транзакции.

    Единая расшифровка для TonAPI v2 и Toncenter v3 (app.ton_codec):
    decoded_body по op-имени → decoded_comment → base64 text →
    message_content.decoded (comment/text_comment) → короткий raw_message.
    """
    comments: list[str] = []
    for msg in item.get("out_msgs") or []:
        if not isinstance(msg, dict):
            continue
        comment = extract_comment(msg)
        if comment:
            comments.append(comment)
    return comments


# Совместимость имён: «формат-специфичные» экстракторы были двумя копиями
# одного декодера — тесты ходят по прежним именам (test_payout_dedupe).
_out_comments_tonapi = _out_comments
_out_comments_toncenter = _out_comments


async def _tx_map_via_tonapi(targets: set[str] | None = None) -> dict[str, str]:
    """memo исходящих казначея → реальный хеш (TonAPI v2), страницами вглубь.

    Одна страница (128 tx) — слишком мелкое окно: в длинной очереди слово
    «потерялся» выносится на пустом месте (memo уже отправленного легко лежит
    глубже 128 свежих транзакций), и сверка возвращает в очередь уже ушедший
    перевод. Ходим страницами (before_lt) вниз по времени, пока не накроем
    payout_reconcile_history_seconds или не упрёмся в пустую/повторную страницу.

    targets — кому это нужно: жадный полный скан (12 страниц) заменяется
    проходом до момента, когда ВСЕ цели найдены. Отсутствующая цель при этом
    вынуждает дойти до конца окна — отрицательный ответ остаётся честным.
    """
    if not settings.active_treasury_address:
        return {}
    url = (
        f"{settings.active_ton_api_base}/v2/blockchain/accounts/"
        f"{settings.active_treasury_address}/transactions"
    )
    headers = api_headers(settings.ton_api_key)
    tx_map: dict[str, str] = {}
    cutoff = time.time() - settings.payout_reconcile_history_seconds
    max_pages = max(1, settings.payout_reconcile_max_pages)
    before_lt: str | None = None
    first_hash: str | None = None
    client = get_http_client()
    for _ in range(max_pages):
        response = await client.get(
            url,
            params={"limit": _RECONCILE_PAGE_LIMIT, "sort_order": "desc", **({"before_lt": before_lt} if before_lt else {})},
            headers=headers,
        )
        response.raise_for_status()
        items = response.json().get("transactions") or []
        if not items:
            break
        page_first_hash = str(items[0].get("hash") or "")
        if page_first_hash and page_first_hash == first_hash:
            break  # пагинация не сдвинулась (провайдер не взял before_lt) — хватит
        first_hash = page_first_hash
        for item in items:
            tx_hash = str(item.get("hash") or "")
            if not tx_hash:
                continue
            # Каждая исходящая транзакция казначея имеет hash; комментарий
            # берём из её out_msgs. Если в одной транзакции несколько
            # переводов с разными memo — все попадают в карту.
            for comment in _out_comments_tonapi(item):
                tx_map[comment] = tx_hash
        if targets and targets <= set(tx_map):
            break  # цели найдены — дальше вглубь незачем (экономия запросов)
        oldest_utime = items[-1].get("utime")
        if oldest_utime is not None and float(oldest_utime) < cutoff:
            break  # окно истории покрыто
        if len(items) < _RECONCILE_PAGE_LIMIT:
            break  # неполная страница = хвост истории, следующая запрос пуста
        before_lt = str(items[-1].get("lt") or "")
        if not before_lt:
            break  # lt нет — следующий шаг невозможен, пагинация провалится
    return tx_map


async def _tx_map_via_toncenter(targets: set[str] | None = None) -> dict[str, str]:
    """memo исходящих казначея → реальный хеш (Toncenter v3), страницами вглубь.

    То же глубокое окно, что в _tx_map_via_tonapi, но через параметр offset
    резервного провайдера: анти-дубль не должен слепнуть там, где TonAPI молчит.
    targets — см. _tx_map_via_tonapi: досрочный стоп после нахождения всех целей.
    """
    if not settings.active_treasury_address:
        return {}
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    tx_map: dict[str, str] = {}
    cutoff = time.time() - settings.payout_reconcile_history_seconds
    max_pages = max(1, settings.payout_reconcile_max_pages)
    offset = 0
    first_hash: str | None = None
    client = get_http_client()
    for _ in range(max_pages):
        params = {
            "account": settings.active_treasury_address,
            "limit": _RECONCILE_PAGE_LIMIT,
            "sort": "desc",
            "offset": offset,
        }
        response = await http_get_with_retry(client, url, params=params, headers=headers)
        response.raise_for_status()
        items = response.json().get("transactions") or []
        if not items:
            break
        page_first_hash = str(items[0].get("hash") or "")
        if page_first_hash and page_first_hash == first_hash:
            break
        first_hash = page_first_hash
        for item in items:
            tx_hash = str(item.get("hash") or "")
            if not tx_hash:
                continue
            for comment in _out_comments_toncenter(item):
                tx_map[comment] = tx_hash
        if targets and targets <= set(tx_map):
            break  # цели найдены — дальше вглубь незачем (экономия запросов)
        oldest_utime = items[-1].get("utime")
        if oldest_utime is not None and float(oldest_utime) < cutoff:
            break
        if len(items) < _RECONCILE_PAGE_LIMIT:
            break  # неполная страница = хвост истории, дальше пусто
        offset += _RECONCILE_PAGE_LIMIT - _RECONCILE_PAGE_OVERLAP
    return tx_map


async def fetch_broadcast_tx_map(targets: set[str] | None = None) -> dict[str, str]:
    """memo последних исходящих казначея → реальный хеш транзакции.

    TonAPI → фолбэк Toncenter. Пустой результат при сбое сети значит
    «не знаем»: сверщик (confirm_broadcast_payouts) в этом случае НИЧЕГО
    не решает — ни подтверждает, ни возвращает в очередь (риск задвоить).

    targets (необязательно) — подмножество memo, ради которого ходим:
    скан останавливается, как только все цели найдены. Кому-то ещё нужен
    полный скан окна — зовут без targets и получают прежнее поведение.
    """
    global _RECONCILE_HISTORY_OK
    for fetch in (_tx_map_via_tonapi, _tx_map_via_toncenter):
        try:
            result = await fetch(targets)
            _RECONCILE_HISTORY_OK = True
            return result
        except Exception as exc:
            logger.warning("Карта исходящих казначея (%s) недоступна: %s", fetch.__name__, exc)
    _RECONCILE_HISTORY_OK = False
    return {}


async def fetch_masterchain_entropy() -> str | None:
    """«seqno:root_hash» последнего мастерхчейн-блока TON — честная энтропия ничьей.

    Блок уже лежит в цепочке в момент жеребьёвки: его нельзя подменить или
    подогнать задним числом, а каждый игрок может проверить seqno в эксплорере
    и пересчитать исход. TonAPI → фолбэк Toncenter. При выключенном TON или
    сбое обоих узлов возвращает None — день откатится на легаси-жребий (seed
    без энтропии), чтобы ничья никогда не «зависала» на сетевой ошибке.
    """
    if not settings.ton_enabled:
        return None
    candidates = (
        (
            f"{settings.active_ton_api_base}/v2/blockchain/masterchain-head",
            {"X-API-Key": settings.ton_api_key} if settings.ton_api_key else {},
            lambda data: data,
        ),
        (
            f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/masterchainInfo",
            {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {},
            lambda data: data.get("last") or data,
        ),
    )
    for url, headers, pick in candidates:
        try:
            client = get_http_client()
            response = await http_get_with_retry(client, url, headers=headers, max_retries=0, timeout=8.0)
            response.raise_for_status()
            block = pick(response.json())
            seqno = block.get("seqno")
            root_hash = block.get("root_hash")
            if seqno is not None and root_hash:
                return f"{seqno}:{root_hash}"
        except Exception as exc:
            logger.warning("Энтропия мастерчейна (%s) недоступна: %s", url, exc)
    return None


async def fetch_broadcast_markers() -> set[str]:
    """Memo недавних исходящих переводов казначея как set.

    Сверка перед ПОВТОРНОЙ отправкой: перевод мог уйти в цепочку в прошлый
    раз, но статус «sent» сохранить не успели (краш/таймаут сразу после
    вещания). Повтор такой выплаты — реальные чужие деньги дважды. Пустой
    результат при сбое сети значит «не знаем»: ведём себя как раньше и
    пытаемся отправить — узкое окно риска лучше постоянной блокировки очереди.
    """
    return set(await fetch_broadcast_tx_map())


# ---------- HTTP-канал отправки (fallback при мёртвых лайтсерверах) ----------
#
# ADNL/TCP до лайтсерверов может быть закрыт окружением (типовой тестнет под
# файрволом: чтение через REST-индексаторы работает, а вещание — нет, payout
# висит в очереди навсегда). Чтобы казна не «зависала», исходящие собираются
# ОФФЛАЙН (чистая локальная математика + seqno/публичный ключ через Toncenter
# v3 runGetMethod) и вещаются через HTTPS (Toncenter v2 jsonRPC sendBoc).
# Жизненный цикл строки и анти-дубль по memo не меняются: диспетчер перед
# повтором по-прежнему сверяется с историей исходящих.

# Дефолтный wallet_id контракта v4r2 (константа pytoniq; в data-ячейке v4).
_V4R2_WALLET_ID = 698983191


def _is_liteserver_down(exc: BaseException) -> bool:
    """Сбой именно лайтсерверного канала, а не самой выплаты.

    «have no alive peers» — LiteBalancer не поднял ни одного пира;
    TimeoutError — зависло ADNL-рукопожатие/отклик (порт режется файрволом).
    В этих случаях уходим в HTTP-вещание. Прочие ошибки (пара мнемоника/адрес,
    отказ контракта, нехватка средств) — настоящие: их показываем как есть.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return "no alive peers" in str(exc).lower()


def build_offline_wallet(
    mnemonic: str,
    address: str,
    network_global_id: int,
    forced_version: str | None = None,
):
    """Кошелёк БЕЗ провайдера по мнемонике: чистая локальная математика.

    Общий строитель для HTTP-канала (лайтсерверы мертвы, сеть не нужна):
    версия контракта детектится по привязанному адресу, StateInit собирается
    из кода контракта и data ровно как в _get_wallet/_wallet_address (пара
    мнемоника/адрес валидируется тем же детектом). Возвращает (wallet, version).
    forced_version — «v4r2»|«v5r1» принудительно (как TREASURY_WALLET_VERSION);
    иначе авто-детект.
    """
    from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE, WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5R1
    from pytoniq_core import Address, StateInit
    from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

    words = mnemonic.replace("\n", " ").split()
    if len(words) < 12:
        raise ValueError("Мнемоника неполная (нужно 24 слова)")
    _, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)

    requested = (forced_version or "").strip().lower()
    if requested in WALLET_VERSIONS:
        derived = _wallet_address(requested, public_key, network_global_id)
        if normalize_address(derived) != normalize_address(address):
            raise ValueError(
                f"Адрес не совпадает с производным от мнемоники "
                f"(версия {requested}): {derived}. Проверь пару мнемоника/адрес "
                "или верни auto."
            )
        version = requested
    else:
        version, candidates = _detect_wallet_version(public_key, address, network_global_id)
        if version is None:
            raise ValueError(
                "Адрес не совпадает ни с одной поддерживаемой версией кошелька "
                f"для этой мнемоники: {candidates}. Проверь адрес и мнемонику, "
                "либо задай TREASURY_WALLET_VERSION=v4r2|v5r1 явно."
            )

    if version == "v5r1":
        data = WalletV5R1.create_data_cell(public_key=public_key, wc=0, network_global_id=network_global_id)
        code = WALLET_V5_R1_CODE
        wallet_class = WalletV5R1
    else:
        data = WalletV4R2.create_data_cell(public_key=public_key, wc=0, wallet_id=_V4R2_WALLET_ID)
        code = WALLET_V4_R2_CODE
        wallet_class = WalletV4R2
    state_init = StateInit(code=code, data=data)
    address_obj = Address((0, state_init.serialize().hash))
    if version == "v5r1":
        # wallet_id — свойство, читающее data из self.state; кода на аккаунте
        # для этого не нужно, но self.state обязан быть заполнен.
        wallet = wallet_class(
            provider=None,
            address=address_obj,
            state_init=state_init,
            private_key=private_key,
        )
        wallet.state = state_init
    else:
        # v4: wallet_id — read-only свойство, читающее data из self.state
        # (константа контракта внутри data-ячейки); kwarg'ом задать нельзя.
        wallet = wallet_class(
            provider=None,
            address=address_obj,
            state_init=state_init,
            private_key=private_key,
        )
        wallet.state = state_init
    return wallet, version


def _build_offline_treasury_wallet():
    """Кошелёк казначея БЕЗ провайдера: build_offline_wallet от настроек.

    Вариант для HTTP-канала: лайтсерверы мертвы, поэтому экземпляр кошелька
    создаётся без подключения (provider=None), а StateInit собирается из кода
    контракта и data ровно как в _get_wallet/_wallet_address (пары мнемоника/
    адрес валидируются тем же детектом версии). Сеть не трогается: seqno и
    публичный ключ онлайн-путь берут у liteclient, здесь их читаем по HTTP.
    Возвращает (wallet, version).
    """
    network = "testnet" if settings.is_testnet else "mainnet"
    requested = settings.treasury_wallet_version.strip().lower() if settings.treasury_wallet_version else ""
    forced = requested if requested in WALLET_VERSIONS else None
    return build_offline_wallet(
        settings.active_treasury_mnemonic,
        settings.active_treasury_address,
        NETWORK_GLOBAL_IDS[network],
        forced_version=forced,
    )


def _parse_run_method_seqno(data: dict) -> int | None:
    """seqno из ответа toncenter v3 runGetMethod (метод «seqno»).

    exit_code 0 → число из первой записи стека (значение бывает hex «0x…» и
    десятичным); иной exit_code — метод не выполнился (uninit/unactive либо
    чужая версия контракта) → None.
    """
    if data.get("exit_code") != 0:
        return None
    stack = data.get("stack") or []
    if not stack:
        return None
    value = (stack[0] or {}).get("value")
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _http_get_wallet_seqno(wallet, *, address: str | None = None) -> int:
    """seqno для оффлайн-подписи: чтение через Toncenter v3.

    address=None — казначей (статус сначала прощупывается fetch_account_state):
    активный аккаунт с развёрнутым кодом — число из get-метода «seqno» (единое
    имя для v4r2 и v5r1); нет контракта (uninit/unactive) — 0: внешнее сообщение
    с init задеплоит кошелёк и выполнится в одной транзакции; если казначёй
    активен, а seqno прочитать не удалось — падаем (слать «вслепую» нельзя).

    address задан — generic-путь (кошелёк игрока/e2e): статус не прощупывается,
    exit_code != 0 трактуется как uninit → 0 (send_wallet_transfer_http в этом
    случае разворачивает контракт init-external'ом). Для тест-сценария это
    достаточно: активный, но нечитаемый метод на игроке — редкость, и перевод
    просто не пройдёт подтверждение watcher'ом.
    """
    target = address or settings.active_treasury_address
    if address is None:
        try:
            _, status, _ = await fetch_account_state()
            active = status == "active"
        except Exception:
            status = None
            active = False
        if status is not None and not active:
            return 0
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/runGetMethod"
    headers = api_headers(settings.toncenter_api_key)
    client = get_http_client()
    response = await http_post_with_retry(
        client,
        url,
        json={"address": target, "method": "seqno", "stack": []},
        headers=headers,
    )
    response.raise_for_status()
    seqno = _parse_run_method_seqno(response.json())
    if seqno is not None:
        return seqno
    if address is None and active:
        raise RuntimeError(
            "казначёй активен, но seqno не читается (toncenter runGetMethod "
            f"exit_code={response.json().get('exit_code')}) — отправка отложена"
        )
    return 0


async def _http_broadcast_external(wallet, seqno: int, internal_msg) -> None:
    """Подписывает внешнее сообщение оффлайн и вещает через Toncenter sendBoc.

    seqno == 0 (казна ещё не развёрнута) → сообщение несёт state_init и
    деплоит кошелёк той же транзакцией. Возвращает None; неуспех — исключение
    с текстом от провайдера.
    """
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[internal_msg],
    )
    external = wallet.create_external_msg(
        src=None,
        dest=wallet.address,
        state_init=wallet.state_init if seqno == 0 else None,
        body=transfer_msg,
    )
    boc_b64 = base64.b64encode(external.serialize().to_boc()).decode()
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v2/jsonRPC"
    headers = api_headers(settings.toncenter_api_key)
    client = get_http_client()
    response = await http_post_with_retry(
        client,
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "sendBoc", "params": {"boc": boc_b64}},
        headers=headers,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("ok"):
        reason = str((data.get("error") if isinstance(data, dict) else None) or response.text)
        raise RuntimeError(f"Toncenter не принял сообщение ({response.status_code}): {reason[:160]}")


async def _send_ton_transfer_http(dest_address: str, amount_nanotons: int, comment: str) -> str:
    """Оффлайн-подпись + вещание через HTTPS (fallback лайтсерверов).

    Держит батч-счётчик _batch_seqno тем же инвариантом, что и лайтсерверный
    путь: один seqno на пачку, локально наращивается только при УСПЕХЕ
    вещания. Два подряд перевода (приз + рейк) не получат одинаковый seqno.
    """
    wallet, _ = _build_offline_treasury_wallet()
    from pytoniq_core import Address

    global _batch_seqno, _warned_no_toncenter_key
    if not settings.toncenter_api_key and not _warned_no_toncenter_key:
        _warned_no_toncenter_key = True
        logger.warning(
            "TONCENTER_API_KEY пуст: HTTP-канал работает под анонимным лимитом "
            "Toncenter — при частых выплатах возможны 429; задай ключ в .env"
        )
    if _batch_seqno is None:
        _batch_seqno = await _http_get_wallet_seqno(wallet)
    internal_msg = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=_comment_cell(comment),
    )
    await _http_broadcast_external(wallet, _batch_seqno, internal_msg)
    _batch_seqno += 1
    global _http_channel_engaged_at
    if _http_channel_engaged_at is None:
        _http_channel_engaged_at = datetime.now(UTC)
    marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
    logger.info(
        "Перевод %d нанотонов к …%s разослан через HTTP (toncenter, seqno=%d)",
        amount_nanotons,
        dest_address[-6:],
        _batch_seqno - 1,
    )
    return marker


async def send_wallet_transfer_http(
    wallet, *, dest_address: str, amount_nanotons: int, comment: str
) -> str:
    """Оффлайн-подпись + HTTPS-вещание для ПРОИЗВОЛЬНОГО кошелька.

    Generic-путь HTTP-канала (кошелёк игрока из build_offline_wallet): seqno
    читается через runGetMethod по адресу кошелька, seqno==0 разворачивает
    контракт init-external'ом в одной транзакции. Возвращает метку bcast.
    """
    if not wallet.private_key:
        raise ValueError("Кошелёк без приватного ключа — оффлайн-подпись невозможна")
    from pytoniq_core import Address

    seqno = await _http_get_wallet_seqno(
        wallet,
        address=wallet.address.to_str(is_user_friendly=False, is_bounceable=False, is_url_safe=True),
    )
    internal_msg = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=_comment_cell(comment),
    )
    await _http_broadcast_external(wallet, seqno, internal_msg)
    marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
    logger.info(
        "Перевод %d нанотонов к …%s разослан через HTTP (toncenter, seqno=%d)",
        amount_nanotons,
        dest_address[-6:],
        seqno,
    )
    return marker


async def send_ton_transfer(dest_address: str, amount_nanotons: int, comment: str) -> str | None:
    """Отправляет перевод с казначея. Возвращает метку вещания или None.

    None — только когда отправка невозможна в принципе (TON выключен или нет
    мнемоники): вызывающий диспетчер сам запишет понятную причину в
    payouts.last_error. Реальные ошибки (пара мнемоника/адрес, лайтсерверы,
    seqno) ПРОПАГАЦИЯТСЯ исключением — диспетчер кладёт их текст в
    last_error, и причина видна в /payouts и алертах без раскопок логов.
    Успех фиксируется лайтсервером (результат 1); фактический хеш транзакции
    смотрится в эксплорере по memo-комментарию.
    """
    if not settings.ton_enabled or not settings.active_treasury_mnemonic:
        logger.warning("TON выключен или нет мнемоники: выплата к …%s не отправлена", dest_address[-6:])
        return None
    try:
        wallet = await _get_wallet()
        global _batch_seqno
        if _batch_seqno is not None:
            # Диспетчер держит seqno из одного get_seqno() на цикл: два подряд
            # перевода не получают одинаковый seqno (иначе один молча потеряется).
            # Инкремент — только при УСПЕХЕ вещания; при сбое батч отменяется:
            # последующие переводы получат свежий seqno из нового get_seqno().
            seqno = _batch_seqno
            try:
                result = await _send_raw_with_seqno(
                    wallet, seqno, dest_address, amount_nanotons, _comment_cell(comment)
                )
            except (Exception, asyncio.CancelledError):
                # Таймаут диспетчера (asyncio.wait_for) обрывает корутину через
                # CancelledError — это НЕ Exception, и без явного перехвата
                # _batch_seqno остался бы протухшим: следующий перевод батча
                # переиспользовал бы уже разосланный seqno и молча потерялся.
                _batch_seqno = None
                raise
            if result != 1:
                _batch_seqno = None
                raise RuntimeError(f"Лайтсерверы не приняли перевод (результат {result})")
            _batch_seqno += 1
        else:
            result = await wallet.transfer(
                destination=dest_address,
                amount=amount_nanotons,
                body=_comment_cell(comment),
            )
        if result != 1:
            raise RuntimeError(f"Лайтсерверы не приняли перевод (результат {result})")
        marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
        logger.info("Перевод %d нанотонов к …%s разослан (%s)", amount_nanotons, dest_address[-6:], comment[:40])
        return marker
    except asyncio.CancelledError:
        # Таймаут диспетчера: он сам решит, что делать со строкой. HTTP-канал
        # сюда не цепляем — рваную корутину «добивать» нельзя.
        raise
    except Exception as exc:
        if not _is_liteserver_down(exc):
            raise
        # Лайтсерверы мертвы (ADNL/TCP режется окружением), а деньги слать
        # надо: оффлайн-подпись + HTTPS-вещание через Toncenter.
        logger.warning(
            "Лайтсерверы недоступны (%s) — переключаюсь на HTTP-канал (toncenter)",
            exc,
        )
        return await _send_ton_transfer_http(dest_address, amount_nanotons, comment)


async def confirm_broadcast_payouts(bot: Bot | None = None) -> int:
    """Сверяет «sent»-выплаты с реальным блокчейном и чинит потерю перевода.

    Метка вещания bcast:<unix> фиксирует только «запрос принят лайтсервером»,
    а не «транзакция в блоке»: при гонке двух быстрых переводов (приз + рейк
    одного дня) один из них может не попасть в цепочку, хотя результат=1
    вернулся. База остаётся с sent-статусом и несуществующим переводом —
    игрок не получает приз, никто не переотправит.

    Каждый цикл:
      • memo, найденное в истории казначея → пишем реальный хеш вместо bcast;
      • memo, которого НЕТ в истории дольше payout_confirm_timeout_seconds →
        строка возвращается в pending (перевод в цепочку не ушёл, анти-дубль
        при повторной отправке не сработает — мемо там нет);
      • карта истории пуста (оба провайдера молчат) → НЕ трогаем строки:
        «не знаем» не имеет права ни подтверждать, ни возвращать в очередь.
    """
    network = "testnet" if settings.is_testnet else "mainnet"
    async with _DISPATCH_LOCK:
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(Payout).where(
                            Payout.status == "sent",
                            Payout.network == network,
                            or_(
                                Payout.tx_hash.is_(None),
                                Payout.tx_hash.like("bcast:%"),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            # Сверять нужно ТОЛЬКО эти memo (sent-без-реального-хеша): ищем их,
            # а не шерстим всю историю слепо. Как только все найдены — стоп:
            # запросов в квартал провайдера минимум, а «нет в истории» остаётся
            # правдивым (отсутствующая цель дожимает скан до конца окна).
            targets = {
                candidate for payout in rows for candidate in _payout_comment_candidates(payout)
            }
            tx_map = await fetch_broadcast_tx_map(targets=targets)
            if not tx_map:
                logger.warning("История казначея недоступна — сверка sent-выплат пропущена")
                return 0
            confirmed = 0
            requeued = 0
            # Сравнение в naive UTC: Postgres (timezone=True) вернёт aware,
            # SQLite — naive; снос tzinfo с обеих сторон даёт один масштаб.
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=settings.payout_confirm_timeout_seconds
            )
            for payout in rows:
                real_hash = next(
                    (
                        tx_map[candidate]
                        for candidate in _payout_comment_candidates(payout)
                        if candidate in tx_map
                    ),
                    None,
                )
                if real_hash:
                    payout.tx_hash = real_hash
                    confirmed += 1
                    continue
                sent_at = payout.sent_at.replace(tzinfo=None) if payout.sent_at is not None else None
                if sent_at is not None and sent_at > cutoff:
                    # Свежая вещация: блокчейн мог ещё не успеть — даём время.
                    continue
                # memo нет в истории, окно верификации истекло — перевод не ушёл.
                payout.status = "pending"
                payout.attempts += 1
                payout.last_error = (
                    f"memo «{_payout_comment_candidates(payout)[0][:40]}» не найдено в блокчейне "
                    f"за {settings.payout_confirm_timeout_seconds} с после вещания — повторная отправка"
                )
                requeued += 1
            await session.commit()
    if requeued:
        logger.warning("Сверка: %d выплат подтверждены, %d возвращены в очередь для ретрая", confirmed, requeued)
    elif confirmed:
        logger.info("Сверка: %d выплат подтверждены реальными хешами", confirmed)
    return confirmed + requeued


async def _reset_retriable(session, network: str) -> None:
    """Оживляем зависшие sending/failed, пока не исчерпан лимит попыток.

    failed возвращается в очередь сразу (в мёртвой строке никто не «живёт»);
    sending — ТОЛЬКО если клейм заведомо «мёртв»: он старше
    payout_send_timeout_seconds + 30 c. Живое вещание (другая копия
    диспетчера держит строку до таймаута вещания) не перехватывается —
    иначе та копия на следующем цикле забрала бы строку и перевела деньги
    второй раз; memo-антидубль не поможет — перевод ещё не в цепочке.
    claimed_at IS NULL (строки, упавшие до появления колонки) считаем
    зависшими: живой клейм всегда пишет claimed_at сейчас. Сверка в naive
    UTC: Postgres вернёт aware, SQLite — naive (см. confirm_broadcast_payouts).
    """
    rows = (
        await session.execute(
            select(Payout.id, Payout.status, Payout.claimed_at).where(
                Payout.status.in_(["failed", "sending"]),
                Payout.attempts < settings.payout_max_attempts,
                Payout.dest_address != "",
                Payout.network == network,
            )
        )
    ).all()
    if not rows:
        return
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=settings.payout_send_timeout_seconds + 30
    )
    reset_ids = [payout_id for payout_id, status, _claimed in rows if status == "failed"]
    for payout_id, status, claimed_at in rows:
        if status != "sending":
            continue
        if claimed_at is None:
            reset_ids.append(payout_id)
        elif claimed_at.replace(tzinfo=None) <= cutoff:
            reset_ids.append(payout_id)
    if reset_ids:
        await session.execute(
            update(Payout).where(Payout.id.in_(reset_ids)).values(status="pending")
        )


async def _alert_http_channel_switch(bot: Bot | None, network: str) -> None:
    """Разово (не чаще раза в кулдаун) сообщает хранителю: казначей пишет
    исходящие через HTTP-канал, лайтсерверы недоступны. Без bot — тихо."""
    global _last_http_channel_alert_at
    if bot is None or _http_channel_engaged_at is None:
        return
    now = datetime.now(UTC)
    if _last_http_channel_alert_at is not None:
        if now - _last_http_channel_alert_at < _HTTP_CHANNEL_ALERT_COOLDOWN:
            return
    _last_http_channel_alert_at = now
    try:
        from app.ops import notify_admins  # локально: ops не импортируется наверху

        await notify_admins(
            bot,
            "⚠️ Казначей: лайтсерверы недоступны (ADNL/TCP режется окружением) — "
            f"исходящие идут через HTTP-канал (оффлайн-подпись + Toncenter sendBoc) "
            f"[{network}]. Вернусь к лайтсерверам сам, когда они оживут.",
        )
    except Exception as exc:
        logger.warning("Алерт о переключении на HTTP-канал не отправлен: %s", exc)


async def _alert_admin(bot: Bot | None, network: str) -> None:
    """Алерты о failed-выплатах. Дедуп — колонка payouts.alerted в БД:
    переживает рестарт и безопасен при нескольких инстансах. В текст идут
    причины из last_error — разбор начинается без открытия логов."""
    if bot is None:
        return
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Payout.id, Payout.last_error).where(
                        Payout.status == "failed",
                        Payout.alerted.is_(False),
                        Payout.network == network,
                    )
                )
            )
            .all()
        )
        if not rows:
            return
        # Условная пометка alerted=True: два процесса-диспетчера увидят одни
        # и те же failed-строки, но алерт по строке создаёт только тот, чей
        # UPDATE вернул rowcount=1 (второй уже видит alerted=True).
        claimed = []
        for payout_id, reason in rows:
            marked = (
                await session.execute(
                    update(Payout)
                    .where(Payout.id == payout_id, Payout.alerted.is_(False))
                    .values(alerted=True)
                )
            ).rowcount
            if marked:
                claimed.append((payout_id, reason))
        await session.commit()
        if not claimed:
            return
        sample = "; ".join(
            f"#{payout_id}: {reason}" if reason else f"#{payout_id}"
            for payout_id, reason in claimed[:3]
        )
        text = (
            f"⚠️ Выплаты не ушли ({len(claimed)} шт., сеть {network}). {sample}. "
            "Разбор: /payouts (причина видна у каждой строки)."
        )
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)


# Доли казны без игрока: адрес получателя — OWNER_WALLET_ADDRESS.
_TREASURY_KINDS = {"rake", "leaderboard"}


async def _hydrate_player_dests(session, network: str) -> int:
    """Оживляет выплаты без получателя, когда кошелёк уже привязан.

    Призы и возвраты игроков без привязанного кошелька на момент финализации
    не должны тонуть в failed (деньги спят, пока админ не разберёт вручную).
    Строка остаётся в очереди, а как только игрок привязывает адрес (/wallet),
    следующий же цикл диспетчера всталяет его в dest_address и платёж уходит
    сам — retry из /payouts не нужен. Доли казны (rake/leaderboard) без
    OWNER_WALLET_ADDRESS и выплаты без игрока (player_id пуст) оживлять нечем:
    честный failed с причиной-действием, как раньше.

    Возвращает число оживших строк (они поедут в пик этого же цикла).
    """
    rows = list(
        (
            await session.execute(
                select(Payout).where(
                    Payout.dest_address == "",
                    Payout.status == "pending",
                    Payout.network == network,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    player_ids = {p.player_id for p in rows if p.player_id is not None}
    wallet_map: dict[int, str] = {}
    verified_map: dict[int, bool] = {}
    if player_ids:
        players = await session.execute(
            select(Player.id, Player.wallet_address, Player.wallet_verified).where(Player.id.in_(player_ids))
        )
        for pid, addr, verified in players.all():
            wallet_map[pid] = addr
            verified_map[pid] = verified
    revived = 0
    for payout in rows:
        if payout.kind in _TREASURY_KINDS:
            if settings.owner_wallet_address:
                payout.dest_address = normalize_address(settings.owner_wallet_address)
                payout.last_error = None
                revived += 1
            else:
                payout.status = "failed"
                payout.last_error = "нет адреса получателя: для доли казны задай OWNER_WALLET_ADDRESS"
        elif payout.player_id is None:
            payout.status = "failed"
            payout.last_error = "нет адреса получателя (кошелёк игрока не найден)"
        else:
            addr = wallet_map.get(payout.player_id) or ""
            is_verified = verified_map.get(payout.player_id, False)
            if addr and is_verified:
                payout.dest_address = addr
                payout.last_error = None
                revived += 1
            elif addr and not is_verified:
                payout.last_error = "кошелёк привязан, но не подтверждён (игрок должен отправить bv:<код>)"
            else:
                payout.last_error = "нет адреса получателя: кошелёк игрока ещё не привязан"
    await session.commit()
    return revived


async def dispatch_pending_payouts(limit: int = 50, bot: Bot | None = None) -> int:
    """Разгребает очередь выплат. Весь цикл под _DISPATCH_LOCK: только один
    диспетчер в эвентлупе вещает, _reset_retriable не восстанавливает строки,
    которые другой цикл взял в работу (иначе двойная рассылка)."""
    async with _DISPATCH_LOCK:
        return await _dispatch_pending_payouts_impl(limit=limit, bot=bot)


async def _dispatch_pending_payouts_impl(limit: int, bot: Bot | None) -> int:
    sent = 0
    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as session:
        # Ретрай: зависшие failed с неисчерпанным лимитом снова в очередь.
        await _reset_retriable(session, network)
        await session.commit()
        # Призы без кошелька оживают сами, когда игрок привязал адрес: это
        # отдельный проход, а НЕ статус failed, иначе строки тонули бы в
        # мёртвых письмах, а игрок терял бы деньги без веской причины.
        await _hydrate_player_dests(session, network)
        result = await session.execute(
            select(Payout)
            .where(
                Payout.status == "pending",
                # Пустые получатели в пик не берём: они либо оживают выше в этом
                # же цикле, либо ждут кошелёк. Иначе они съедали бы лимит из
                # 50 строк и голодали настоящие выплаты.
                Payout.dest_address != "",
                Payout.amount_nanotons > 0,
                Payout.network == network,
            )
            .order_by(Payout.id.asc())
            .limit(limit)
        )
        payouts = list(result.scalars().all())
        # Предохранитель баланса: не вещаем переводы, которые сеть отвергнет
        # из-за нехватки средств на казначее. Fail-fast с понятной причиной:
        # статус остаётся pending, попытки НЕ сгорают — после пополнения
        # очередь уйдёт сама, без ручного retry и без мёртвых писем.
        #
        # Если баланс недоступен (оба индексатора молчат) — логируем, но
        # ПРОБУЕМ отправить: liteclient работает через прямое TCP-соединение
        # к liteserver, а не через HTTP API. Пусть liteserver отвергнет сам,
        # если средств мало — это надёжнее, чем висеть в очереди навсегда.
        sendable = [payout for payout in payouts if payout.dest_address]
        if (
            sendable
            and settings.active_treasury_address
            and settings.active_treasury_mnemonic
        ):
            try:
                balance, _status, _source = await fetch_account_state()
            except Exception as exc:
                logger.warning("Баланс казначея перед циклом не прочитан: %s", exc)
                balance = None
            if balance is None:
                logger.warning(
                    "Баланс казначея недоступен (оба индексатора молчат) — "
                    "попытка отправки через liteclient напрямую (%d выплат)",
                    len(sendable),
                )
            if balance is not None:
                fee_nano = to_nano(settings.payout_fee_gram)
                needed = sum(p.amount_nanotons for p in sendable) + fee_nano * len(sendable)
                if balance < needed:
                    reason = (
                        f"казначей подкачан: нужно {needed / 1e9:.4f} Gram (с газом), "
                        f"есть {balance / 1e9:.4f} — пополни баланс, очередь уйдёт сама"
                    )
                    logger.warning("Диспетчер: %s", reason)
                    for payout in sendable:
                        payout.last_error = reason[:200]
                    await session.commit()
                    return 0
        # Атомарный клейм «взятых в работу» ДО вещания. Условный UPDATE по
        # status='pending' — единственный процесс (одна копия диспетчера)
        # переведёт строку в sending: rowcount==1. Вторая копия (двойной
        # процесс: закрытие дня + ton-settle + ручной кик) получит 0 и НЕ
        # возьмёт строку — иначе оба вещали бы один перевод и задваивали
        # трату казны. Падение после клейма обратимо: _reset_retriable вернёт
        # sending → pending на следующем цикле, а memo-антидубль (attempts>1)
        # уберёт повтор уже ушедшего перевода.
        claimed_ids: set[int] = set()
        for payout in payouts:
            if not payout.dest_address:
                continue
            gate = await session.execute(
                update(Payout)
                .where(Payout.id == payout.id, Payout.status == "pending")
                .values(status="sending")
            )
            if gate.rowcount != 1:
                # Строку уже забрала другая копия — не трогаем и не вещаем.
                continue
            payout.attempts += 1
            payout.status = "sending"
            payout.claimed_at = datetime.now(UTC)
            claimed_ids.add(payout.id)
        await session.commit()
        # Работаем только строками, что реально забрали мы: сама рассылка
        # (claimed). Строки другой копии диспетчера в эту сессию НЕ трогаем.
        payouts = [p for p in payouts if p.id in claimed_ids]
        # Сверка с историей: если комментарий уже есть среди недавних
        # исходящих казначея — перевод ушёл в прошлом цикле (краш между
        # вещанием и коммитом). Повторная отправка задвоила бы платёж.
        # Доступность истории считается ЗАНОВО для этого цикла: сбой в прошлом
        # цикле не должен вечно замораживать повторы — история могла ожить.
        # Реальный сбой fetch_broadcast_markers ниже снова выставит False.
        global _RECONCILE_HISTORY_OK
        _RECONCILE_HISTORY_OK = True
        markers: set[str] = set()
        if payouts:
            markers = await fetch_broadcast_markers()
        # Батч: берём seqno кошелька ОДИН раз на цикл и наращиваем его локально
        # на каждую рассылку. Иначе каждый перевод делал бы свой get_seqno(),
        # и два подряд перевода (приз + рейк одного дня) получили бы ОДИН и тот
        # же seqno — в блок входил бы только один, второй тихо терялся.
        global _batch_seqno
        _batch_seqno = None
        if payouts and settings.ton_enabled and settings.active_treasury_mnemonic:
            try:
                wallet_for_batch = await _get_wallet()
                _batch_seqno = await wallet_for_batch.get_seqno()
            except Exception:
                # Сбой не критичен: выродимся в старый путь, где send_ton_transfer
                # сам получает seqno (а её тред-безопасность отдельная история).
                _batch_seqno = None
                logger.warning("Не удалось получить seqno для батч-отправки — отправлю по одному", exc_info=True)
        try:
            for payout in payouts:
                # Свободный комментарий (возвраты при паузе) дополняется
                # служебным суффиксом «way:<день>:<тип>#<id>», чтобы анти-дубль
                # не спотыкался на одинаковом тексте разных возвратов.
                candidates = _payout_comment_candidates(payout)
                comment = candidates[0]
                if any(candidate in markers for candidate in candidates):
                    # Перевод уже ушёл в цепочку раньше, но статус тогда не
                    # сохранился (краш/таймаут после вещания). Повтор задвоил бы
                    # платёж — фиксируем доставку без новой отправки.
                    payout.tx_hash = None
                    payout.status = "sent"
                    payout.sent_at = datetime.now(UTC)
                    payout.last_error = None
                    sent += 1
                    logger.warning(
                        "Выплата %d уже разослана ранее (memo найдено у казначея) — помечена sent без повтора",
                        payout.id,
                    )
                    continue
                if (
                    payout.attempts > 1
                    and not any(candidate in markers for candidate in candidates)
                    and not _RECONCILE_HISTORY_OK
                ):
                    # Повтор (>1 попытки) и история казначея НЕДОСТУПНА: не знаем,
                    # не ушёл ли этот перевод тем же memo в прошлом цикле (краш
                    # между вещанием и коммитом). Пустой ответ маркеров в этом
                    # случае означает «молчат оба провайдера», а НЕ «перевода
                    # нет». Переотправка в «не знаю» = реальный двойной платёж.
                    # Замораживаем строку с видимой причиной: история вернётся —
                    # сверка повторится сама, без ручного retry.
                    payout.status = "pending"
                    payout.last_error = (
                        "история казначея недоступна — повтор отложен (анти-дубль), "
                        "сверка с memo невозможна"
                    )
                    logger.warning("Выплата %d: повтор отложен — история казначея недоступна", payout.id)
                    continue
                try:
                    tx_hash = await asyncio.wait_for(
                        send_ton_transfer(
                            payout.dest_address,
                            payout.amount_nanotons,
                            comment=comment,
                        ),
                        timeout=settings.payout_send_timeout_seconds,
                    )
                except TimeoutError:
                    # Зависший лайтсервер не имеет права замораживать цикл:
                    # таймаут — обычный ретрай с видимой причиной.
                    logger.warning("Выплата %s: таймаут вещания >%ss", payout.id, settings.payout_send_timeout_seconds)
                    payout.last_error = f"таймаут вещания (>{settings.payout_send_timeout_seconds} с)"
                    tx_hash = None
                except Exception as exc:
                    # «no alive peers» сюда уже не доходит: send_ton_transfer
                    # перехватывает сбой лайтсерверного канала и уходит в HTTP
                    # (тот при неуспехе падает текстом провайдера — он и виден).
                    reason = str(exc)
                    logger.warning("Выплата %s не ушла: %s", payout.id, exc)
                    payout.last_error = reason[:200]
                    tx_hash = None
                if tx_hash is None and payout.last_error is None:
                    # Единственный путь сюда — guard выключенного TON/мнемоники.
                    payout.last_error = "отправка недоступна: TON выключен или нет мнемоники казначея"
                if tx_hash:
                    payout.tx_hash = tx_hash
                    payout.status = "sent"
                    payout.sent_at = datetime.now(UTC)
                    payout.attempts = 0
                    payout.last_error = None
                    sent += 1
                elif payout.attempts >= settings.payout_max_attempts:
                    payout.status = "failed"
                else:
                    # Лимит не исчерпан — вернётся в очередь следующего цикла;
                    # last_error сохраняем: причина видна в /payouts уже сейчас.
                    payout.status = "pending"
        finally:
            _batch_seqno = None
        await session.commit()
    dead = [p.id for p in payouts if p.status == "failed"]
    if dead:
        logger.warning("Выплаты окончательно не отправлены: %s", dead)
    # Алерт по ВСЕМ неотправленным без предупреждения (включая найденные
    # после рестарта): дедуп внутри _alert_admin по колонке alerted.
    await _alert_admin(bot, network)
    # Отдельная история: выплаты ушли, но через HTTP-канал — хранитель должен
    # знать, что лайтсерверы за стеной (дедуп по времени, см. хелпер).
    await _alert_http_channel_switch(bot, network)
    return sent


async def settle_closed_rounds(bot: Bot | None = None) -> int:
    """Финализирует фонды закрытых дней и разбирает очередь выплат."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Round.id).where(
                Round.status == RoundStatus.CLOSED,
                Round.payouts_finalized.is_(False),
            )
        )
        round_ids = [row[0] for row in result.all()]
    created = 0
    for round_id in round_ids:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            if round_row is not None:
                created += await finalize_day_payouts(session, round_row)
    await dispatch_pending_payouts(bot=bot)
    return created


# ---------- Диагностика казначея для хранителя (/treasury) ----------


async def _tonapi_account_raw(address: str) -> dict:
    url = f"{settings.active_ton_api_base}/v2/accounts/{address}"
    headers = api_headers(settings.ton_api_key)
    client = get_http_client()
    response = await http_get_with_retry(client, url, headers=headers)
    response.raise_for_status()
    return response.json()


async def _toncenter_account(address: str) -> dict:
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/accountInformation"
    headers = api_headers(settings.toncenter_api_key)
    # v3 ждёт query-параметр «account», а не «address» (как в /api/v3/transactions).
    client = get_http_client()
    response = await http_get_with_retry(client, url, params={"account": address}, headers=headers)
    response.raise_for_status()
    return response.json()


async def fetch_account_state() -> tuple[int | None, str | None, str]:
    """(баланс в нанотонах | None, статус аккаунта | None, источник данных).

    TonAPI → фолбэк Toncenter v3; оба молчат — (None, None, "none").
    """
    address = settings.active_treasury_address
    try:
        data = await _tonapi_account_raw(address)
        balance = int(str(data.get("balance") or 0))
        status = str(data.get("status") or "")
        return balance, (status or None), "tonapi"
    except Exception as exc:
        logger.warning("Баланс казначея через TonAPI недоступен: %s", exc)
    try:
        data = await _toncenter_account(address)
        return int(str(data.get("balance") or 0)), None, "toncenter"
    except Exception as exc:
        logger.warning("Баланс казначея через Toncenter недоступен: %s", exc)
    return None, None, "none"


def treasury_pair_check_text() -> str:
    """Сверка пары мнемоника/адрес без выхода в сеть: v4r2/v5r1 → адрес."""
    from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

    words = settings.active_treasury_mnemonic.replace("\n", " ").split()
    if len(words) < 12:
        return f"мнемоника неполная ({len(words)} слов вместо 24) ⚠️"
    try:
        _, private_key = mnemonic_to_private_key(words)
    except Exception:
        return "мнемоника невалидна ⚠️"
    public_key = private_key_to_public_key(private_key)
    network = "testnet" if settings.is_testnet else "mainnet"
    version, _candidates = _detect_wallet_version(
        public_key, settings.active_treasury_address, NETWORK_GLOBAL_IDS[network]
    )
    if version is not None:
        return f"{version} ✓ (детект по адресу)"
    return (
        "ни v4r2, ни v5r1 не дают настроенный адрес ⚠️ — "
        "проверь пару мнемоника/адрес или задай TREASURY_WALLET_VERSION явно"
    )


async def treasury_diagnostics() -> str:
    """Полный отчёт по казначею одной строкой-текстом для /treasury."""
    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"🏛 Казначей ({network})"]
    if not settings.ton_enabled:
        lines.append("TON выключен (TON_ENABLED=false): ставки и выплаты не работают.")
        return "\n".join(lines)
    if settings.active_treasury_address:
        shown = friendly_address(settings.active_treasury_address, testnet=settings.is_testnet)
        lines.append(f"Адрес: <code>{shown}</code>")
    else:
        lines.append("Адрес не задан ⚠️")
    lines.append("Мнемоника: " + ("задана ✓" if settings.active_treasury_mnemonic else "НЕ задана ⚠️"))
    lines.append(
        "OWNER_WALLET_ADDRESS: "
        + ("задан ✓" if settings.owner_wallet_address else "не задан — доли казны (рейк/копилка) уйти не могут ⚠️")
    )
    if (
        settings.active_treasury_address
        and settings.owner_wallet_address
        and normalize_address(settings.owner_wallet_address)
        == normalize_address(settings.active_treasury_address)
    ):
        lines.append("⚠️ OWNER_WALLET_ADDRESS совпадает с казначеем: рейк уходит «сам себе» — задай отдельный кошелёк владельца.")
    if settings.active_treasury_address and settings.active_treasury_mnemonic:
        try:
            lines.append(f"Пара мнемоника/адрес: {treasury_pair_check_text()}")
        except Exception as exc:
            lines.append(f"Пара мнемоника/адрес: не проверена ({exc})")
        balance, status, source = await fetch_account_state()
        if balance is None:
            lines.append("Баланс: недоступен (оба индексатора молчат) ⚠️")
        else:
            note = f", статус {status}" if status else ""
            lines.append(f"Баланс: {from_nano(balance):.4f} Gram{note} · источник {source}")
            if balance <= 0:
                lines.append("Баланс пуст: пополнить через @testgiver_ton_bot (testnet).")
    # Сверка с ожиданиями БД, корректировки казны и стоп-кран (/adjust, /pause).
    from app.ops import (
        MANUAL_IN_KIND,
        MANUAL_OUT_KIND,
        is_game_paused,
        paused_reason,
        treasury_expected_state,
    )

    try:
        async with SessionLocal() as session:
            drift_state = await treasury_expected_state(session)
            adjustments = (
                await session.execute(
                    select(
                        Income.kind,
                        func.count(),
                        func.coalesce(func.sum(Income.amount_nanotons), 0),
                    )
                    .where(Income.kind.in_([MANUAL_OUT_KIND, MANUAL_IN_KIND]))
                    .group_by(Income.kind)
                )
            ).all()
            paused = await is_game_paused(session)
            reason = await paused_reason(session)
    except Exception:
        logger.warning("Сверка казны для /treasury не собралась", exc_info=True)
        drift_state, adjustments, paused, reason = None, [], False, None
    if paused:
        lines.append(
            f"⏸ Игра на паузе ({reason or 'техработы'}): входящие переводы "
            "возвращаются отправителям. Снять: /resume"
        )
    if adjustments:
        parts = [
            f"{'−' if kind == MANUAL_OUT_KIND else '+'}{from_nano(total):.4f} Gram ({count})"
            for kind, count, total in adjustments
        ]
        lines.append("Корректировки казны: " + " · ".join(parts))
    if drift_state is not None:
        if drift_state.beyond_tolerance:
            lines.append(
                f"Ожидания БД: ~{drift_state.expected_nanotons / 1e9:.4f} Gram · "
                f"расхождение {drift_state.drift_nanotons / 1e9:+.4f} Gram ⚠️ — "
                "закрой: /adjust"
            )
        else:
            lines.append(
                f"Сверка с БД сходится ✓ (ожидается ~{drift_state.expected_nanotons / 1e9:.4f} Gram)"
            )
    # Зеркало казны: независимая копия истории кошелька со сверкой «в ноль».
    try:
        from app.treasury_mirror import treasury_mirror_block

        mirror_text = await treasury_mirror_block()
        if mirror_text:
            lines.append("")
            lines.append(mirror_text)
    except Exception as exc:
        logger.warning("Блок «Зеркало казны» в /treasury не собрался: %s", exc)
    async with SessionLocal() as session:
        waiting = (
            await session.execute(
                select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
            )
        ).scalar_one()
        dead = (
            await session.execute(select(func.count()).select_from(Payout).where(Payout.status == "failed"))
        ).scalar_one()
        # Глазами watcher'а: куда смотрит, когда последний раз видел цепочку
        # и где стоит курсор. Одна команда отвечает на «почему не видно пополнений».
        from app.ton_watch import BEAT_KEY, CURSOR_KEY, SOURCE_KEY

        beat_iso = None
        source = None
        cursor_raw = None
        for key, slot in ((BEAT_KEY, "b"), (SOURCE_KEY, "s"), (CURSOR_KEY, "c")):
            row = await session.get(WatcherState, key)
            if row is not None:
                if slot == "b":
                    beat_iso = row.value
                elif slot == "s":
                    source = row.value
                else:
                    cursor_raw = row.value
    lines.append(f"Очередь выплат: ожидает {waiting} · failed {dead}")
    if waiting or dead:
        lines.append("Разбор: /payouts — причина видна у каждой строки.")
    now = datetime.now(UTC)
    lines.append("Watcher:")
    if not settings.active_treasury_address:
        lines.append("  адрес не задан — смотреть не на что ⚠️")
    else:
        lines.append(f"  смотрит на: {settings.active_treasury_address[:8]}…{settings.active_treasury_address[-6:]} ({network})")
    beat_age = None
    if beat_iso:
        try:
            beat_moment = datetime.fromisoformat(beat_iso)
            if beat_moment.tzinfo is None:
                beat_moment = beat_moment.replace(tzinfo=UTC)
            beat_age = int((now - beat_moment).total_seconds())
        except ValueError:
            pass
    lines.append(
        f"  успешный цикл: {'never' if beat_age is None else f'{beat_age} с назад'}"
        + (f" · источник {source}" if source else "")
    )
    if beat_age is not None and beat_age > 180:
        lines.append("  ⚠️ циклы не проходят >3 мин: индексаторы недоступны или процесс спит")
    if cursor_raw and cursor_raw.isdigit():
        cursor_dt = datetime.fromtimestamp(int(cursor_raw), tz=UTC)
        lag = int((now - cursor_dt).total_seconds())
        lines.append(f"  курсор: {cursor_dt:%d.%m %H:%M} UTC ({lag:+d} с от текущего времени)")
        if lag < -60:
            lines.append("  ⚠️ курсор В БУДУЩЕМ: новые переводы отсекаются как «старые» — обнули ключ ton_watch_cursor_utime в watcher_state")
    elif settings.ton_enabled:
        lines.append("  курсора нет — стартует с отката 12 ч")
    return "\n".join(lines)


async def blockchain_diagnostics() -> str:
    """Аудит блокчейн-контура одной строкой-текстом для /blockchain.

    Показывает глазами всей связки watcher → очередь выплат → казначей:
    курсор и источник, stuck-список сбойных входящих, очередь по статусам,
    неопознанные «sent» (ждут сверки), глубину сверки истории и флаг её
    доступности в последнем цикле. Поиск «куда делось» начинается здесь,
    без перебора логов и запросов к индексаторам руками.
    """
    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"⛓ Блокчейн-контур ({network})"]
    if not settings.ton_enabled:
        lines.append("TON выключен (TON_ENABLED=false): ставки и выплаты не работают.")
        return "\n".join(lines)
    # Watcher-состояние одним запросом (курсор, источник, сердцебиение, stuck).
    from app.ton_watch import (  # локально: ton_watch не импортируется сверху
        BEAT_KEY,
        CURSOR_KEY,
        SOURCE_KEY,
    )
    from app.ton_watch import (
        STUCK_TX_KEY as STUCK_KEY,
    )

    cursor_raw: str | None = None
    source: str | None = None
    beat_iso: str | None = None
    stuck: dict = {}
    queue: dict[str, int] = {}
    sent_unconfirmed = 0
    waiting_dest = 0
    verified_wallets = 0
    async with SessionLocal() as session:
        for key, slot in ((BEAT_KEY, "b"), (SOURCE_KEY, "s"), (CURSOR_KEY, "c")):
            row = await session.get(WatcherState, key)
            if row is None:
                continue
            if slot == "b":
                beat_iso = row.value
            elif slot == "s":
                source = row.value
            else:
                cursor_raw = row.value
        stuck_row = await session.get(WatcherState, STUCK_KEY)
        if stuck_row is not None:
            try:
                stuck = json.loads(stuck_row.value) or {}
            except (ValueError, TypeError):
                stuck = {}
            if not isinstance(stuck, dict):
                stuck = {}
        for status in ("pending", "sending", "sent", "failed"):
            n = (
                await session.execute(
                    select(func.count()).select_from(Payout).where(
                        Payout.status == status, Payout.network == network
                    )
                )
            ).scalar_one()
            queue[status] = n
        sent_unconfirmed = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.status == "sent",
                    Payout.network == network,
                    or_(Payout.tx_hash.is_(None), Payout.tx_hash.like("bcast:%")),
                )
            )
        ).scalar_one()
        waiting_dest = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.dest_address == "", Payout.network == network
                )
            )
        ).scalar_one()
        verified_wallets = (
            await session.execute(
                select(func.count()).select_from(Player).where(Player.wallet_verified.is_(True))
            )
        ).scalar_one()
    # Курсор: лаг от текущего времени (тот же расчёт, что в /treasury).
    now = datetime.now(UTC)
    if cursor_raw and cursor_raw.isdigit():
        cursor_dt = datetime.fromtimestamp(int(cursor_raw), tz=UTC)
        lines.append(
            f"Watcher: курсор {cursor_dt:%d.%m %H:%M} UTC "
            f"({int((now - cursor_dt).total_seconds()):+d} с)"
            + (f" · источник {source}" if source else "")
        )
    else:
        lines.append("Watcher: курсора нет — стартует с отката 12 ч")
    if beat_iso:
        try:
            beat_moment = datetime.fromisoformat(beat_iso)
            if beat_moment.tzinfo is None:
                beat_moment = beat_moment.replace(tzinfo=UTC)
            beat_age = int((now - beat_moment).total_seconds())
        except ValueError:
            beat_age = None
        lines.append(f"Watcher: успешный цикл {beat_age if beat_age is not None else '?'} с назад")
        if beat_age is not None and beat_age > 180:
            lines.append("  ⚠️ циклы не проходят >3 мин: индексаторы недоступны или процесс спит")
    stuck_entries = sum(1 for rec in stuck.values() if isinstance(rec, dict) and not rec.get("reported"))
    stuck_sample = ""
    if stuck_entries:
        hashes = [h[:10] for h in list(stuck.keys())[:3]]
        stuck_sample = "· " + ", ".join(hashes) + ("…" if stuck_entries > 3 else "")
    lines.append(f"Stuck-входящих: {stuck_entries} {stuck_sample} (ключ watcher_state[{STUCK_KEY}])")
    lines.append(
        f"Очередь выплат: pending {queue.get('pending', 0)} · sending {queue.get('sending', 0)} · "
        f"sent {queue.get('sent', 0)} (сверки ждут {sent_unconfirmed}) · failed {queue.get('failed', 0)}"
    )
    if waiting_dest:
        lines.append(f"  {waiting_dest} строк ждут кошелёк игрока (dest пустой) — уйдут после /wallet")
    lines.append(f"Кошельков verified: {verified_wallets}")
    balance, _status, balance_source = await fetch_account_state()
    if balance is not None:
        lines.append(f"Баланс казначея: {balance / 1e9:.4f} Gram ({balance_source})")
    else:
        lines.append("Баланс казначея: недоступен (оба индексатора молчат)")
    lines.append(
        f"Сверка истории: {settings.payout_reconcile_history_seconds / 86400:g} сут · "
        f"до {settings.payout_reconcile_max_pages} стр · "
        f"история {'доступна' if _RECONCILE_HISTORY_OK else 'НЕДОСТУПНА ⚠️ повторы выплат заморожены'}"
    )
    return "\n".join(lines)
