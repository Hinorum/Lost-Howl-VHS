"""Кошелёк казначея: детект версии контракта, ленивая инициализация, оффлайн-сборка.

Казначей живёт на контракте v4r2 или v5r1. Версия детектируется автоматически
по адресу либо задаётся явно TREASURY_WALLET_VERSION. Пара мнемоника/адрес
валидируется ДО подключения к сети: производный адрес должен совпасть с
настроенным, иначе отправлять нельзя в принципе.

Два строителя:
- get_wallet() — сетевой: лайтсерверы, готовый кошелёк для batch-переводов.
- build_offline_wallet() — без сети: для HTTP-канала (лайтсерверы мёртвые,
  окружение режет ADNL/TCP). StateInit и адрес собираются локально.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import settings
from app.ton_utils import normalize_address

from . import state

logger = logging.getLogger(__name__)

# Глобальный идентификатор сети (конфиг #19 блокчейна): входит в wallet_id
# контракта v5, поэтому с одной мнемоникой тестнет- и мейннет-v5-кошельки
# имеют разные адреса.
NETWORK_GLOBAL_IDS = {"mainnet": -239, "testnet": -3}
# Поддерживаемые версии контракта казначея.
WALLET_VERSIONS = ("v4r2", "v5r1")
# Дефолтный wallet_id контракта v4r2 (константа pytoniq; в data-ячейке v4).
_V4R2_WALLET_ID = 698983191

# Сколько редиректов при загрузке конфига лайтсерверов готовы пройти вручную,
# проверяя каждый хоп SSRF-гейтом (см. fetch_remote_json).
_MAX_CONFIG_REDIRECTS = 5


def wallet_address(version: str, public_key: bytes, network_global_id: int, wc: int = 0) -> str:
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


def detect_wallet_version(
    public_key: bytes, treasury_address: str, network_global_id: int
) -> tuple[str | None, dict[str, str]]:
    """Версия кошелька, чей производный адрес совпал с настроенным.

    Возвращает (версия | None, {версия: адрес-кандидат}) — кандидаты идут в
    текст ошибки, чтобы расхождение мнемоники и адреса было видно сразу.
    """
    target = normalize_address(treasury_address)
    candidates = {
        version: wallet_address(version, public_key, network_global_id)
        for version in WALLET_VERSIONS
    }
    for version, address in candidates.items():
        if normalize_address(address) == target:
            return version, candidates
    return None, candidates


def _assert_public_https_url(url: str) -> None:
    """SSRF-гейт: https + публичный хост, без loopback/private/link-local.

    URL приходит из LITESERVER_CONFIG_URL (env деплоера). Проверяем схему и
    IP-литералы; DNS-имена не резолвим (гейт на старте, а не на каждый тик —
    дорого и ненадёжно). Редиректы автоматически НЕ следуются: каждый
    следующий хоп обязан быть https и публичным (см. fetch_remote_json), иначе
    легитимный хост увёл бы запрос на 169.254.169.254 (метаданные облака) или
    127.0.0.1 в обход проверки исходного URL.
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(
            f"LITESERVER_CONFIG_URL обязан быть https:// (получено: {parsed.scheme or 'без схемы'})"
        )
    host = parsed.hostname
    if not host:
        raise ValueError(f"LITESERVER_CONFIG_URL без хоста: {url}")
    lowered = host.lower()
    blocked_hosts = {
        "localhost",
        "metadata.google.internal",
        "instance-data",
    }
    if lowered in blocked_hosts:
        raise ValueError(f"SSRF-гейт: хост {host} заблокирован")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # DNS-имя — не IP-литерал, пропускаем
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(f"SSRF-гейт: адрес {host} не является публичным")


async def fetch_remote_json(url: str) -> dict:
    """Скачивает JSON (конфиг лайтсерверов), проверяя каждый хоп редиректа.

    Общий httpx-клиент ходит с follow_redirects=True, поэтому одной проверки
    исходного URL недостаточно: легитимный хост мог ответить 302 на
    169.254.169.254 или 127.0.0.1, и запрос ушёл бы туда мимо гейта. Здесь
    редиректы не следуются автоматически — каждый Location резолвится
    относительно текущего адреса и проверяется _assert_public_https_url до
    следующего запроса. Потолок редиректов — _MAX_CONFIG_REDIRECTS.
    """
    from urllib.parse import urljoin

    from app.http_utils import get_http_client

    client = get_http_client()
    current = url
    for hop in range(_MAX_CONFIG_REDIRECTS + 1):
        _assert_public_https_url(current)
        response = await client.get(current, follow_redirects=False)
        if not response.is_redirect:
            response.raise_for_status()
            return response.json()
        location = response.headers.get("location")
        if not location:
            raise ValueError(f"Редирект без Location при загрузке {current}")
        if hop == _MAX_CONFIG_REDIRECTS:
            break
        current = urljoin(str(response.url), location)
    raise ValueError(
        f"Слишком много редиректов при загрузке конфига лайтсерверов: {url}"
    )


def notify_liteserver_down(exc: BaseException) -> None:
    """Сбой лайтсерверного канала: серия подряд → пересборка пула.

    Больной узел в pytoniq остаётся «живым» (высокий кэш masterchain seqno —
    приоритет выбора выше), а execute_method на ошибку-класс 651 пиры не
    перебирает: без пересборки каждый ретрай снова попадает в ту же ноду, и
    «больное окно» длится часами. После порога _LITESERVER_REBUILD_AFTER
    сбоев подряд (не чаще раза в кулдаун) выставляется запрос пересборки —
    его исполняет get_wallet при следующем обращении. Флаг
    TON_PROVIDER_REBUILD выключен — функция ничего не делает (поведение
    деплоя не меняется).

    Честная оговорка: пересборка не гарантирует смену пиров — свежий пул
    может снова выбрать ту же ноду (она отвечает на пинг и держит высокий
    seqno). Это шанс, а не лечение; нормальный режим при больном канале —
    HTTP-фолбэк.
    """
    if not settings.ton_provider_rebuild:
        return
    state._liteserver_down_streak += 1
    if state._liteserver_down_streak < state._LITESERVER_REBUILD_AFTER:
        return
    now = datetime.now(UTC)
    if (
        state._last_provider_reset_at is not None
        and now - state._last_provider_reset_at < state._PROVIDER_RESET_COOLDOWN
    ):
        # Кулдаун после недавней пересборки. Серию НЕ сбрасываем: сразу после
        # кулдауна один сбой снова попросит пересборку, а не будет ждать
        # полный порог заново.
        return
    logger.warning(
        "Лайтсерверы: %d сбоев подряд (%s) — пересоберу пул при следующем обращении",
        state._liteserver_down_streak,
        exc,
    )
    state._liteserver_down_streak = 0
    state._last_provider_reset_at = now
    state._provider_reset_requested = True


def notify_liteserver_ok() -> None:
    """Успешное чтение/вещание по лайтсерверам: серия сбоев оборвалась."""
    state._liteserver_down_streak = 0


async def get_wallet():
    """Ленивая инициализация кошелька казначея для активной сети.

    Версия контракта — из TREASURY_WALLET_VERSION («auto» = детект по адресу).
    Проверка пары мнемоника/адрес выполняется ДО подключения к сети: если
    производный адрес не совпал, отправлять нельзя в принципе — падаем с
    внятной ошибкой, а не молчаливыми неудачными выплатами. Источник
    лайтсерверов: LITESERVER_CONFIG_URL (свежий JSON), иначе встроенный
    конфиг pytoniq для сети. Запрошенная пересборка пула (см.
    notify_liteserver_down) исполняется здесь: старый провайдер закрывается,
    при следующем обращении поднимается свежий.
    """
    network = "testnet" if settings.is_testnet else "mainnet"
    async with state._wallet_lock:
        if state._provider_reset_requested:
            # Фолбэк-путь заметил серию сбоев лайтсерверов: больной узел
            # остаётся «живым» и приоритетным, ретраи бесполезны — обнуляем
            # кошелёк, и блок ниже закроет старый пул и поднимет свежий
            # (шанс сменить набор пиров). Флаг снимаем всегда: если кошелёк
            # уже None, следующий блок сам пересоберёт провайдера, а снятый
            # флаг не слал бы пересборку на каждом обращении.
            state._provider_reset_requested = False
            if state._wallet is not None:
                state._wallet = None
                logger.info("Пересборка пула лайтсерверов: серия сбоев исчерпала порог")
        if state._wallet is not None and state._wallet_network == network:
            return state._wallet
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
            derived = wallet_address(requested, public_key, network_global_id)
            if normalize_address(derived) != normalize_address(settings.active_treasury_address):
                raise ValueError(
                    f"Адрес казначея не совпадает с производным от мнемоники "
                    f"(TREASURY_WALLET_VERSION={requested}): {derived}. "
                    "Проверь пару мнемоника/адрес или верни auto."
                )
            version = requested
        else:
            version, candidates = detect_wallet_version(
                public_key, settings.active_treasury_address, network_global_id
            )
            if version is None:
                raise ValueError(
                    "Адрес казначея не совпадает ни с одной поддерживаемой версией "
                    f"кошелька для этой мнемоники: {candidates}. Проверь адрес и "
                    "мнемонику, либо задай TREASURY_WALLET_VERSION=v4r2|v5r1 явно."
                )

        if state._provider is not None:
            try:
                await state._provider.close_all()
            except Exception:
                logger.warning("Не удалось закрыть старый провайдер лайтсерверов", exc_info=True)
            state._provider = None
            state._wallet = None
        if settings.liteserver_config_url:
            config = await fetch_remote_json(settings.liteserver_config_url)
            state._provider = LiteBalancer.from_config(config)
            logger.info("Лайтсерверы: конфиг из LITESERVER_CONFIG_URL")
        elif network == "testnet":
            state._provider = LiteBalancer.from_testnet_config()
        else:
            state._provider = LiteBalancer.from_mainnet_config()
        await state._provider.start_up()
        if version == "v5r1":
            state._wallet = await WalletV5R1.from_private_key(
                state._provider, private_key=private_key, wc=0, network_global_id=network_global_id
            )
        else:
            state._wallet = await WalletV4R2.from_private_key(state._provider, private_key, wc=0)
        state._wallet_network = network
        logger.info("Кошелёк казначея готов (%s, контракт %s)", network, version)
        return state._wallet


def build_offline_wallet(
    mnemonic: str,
    address: str,
    network_global_id: int,
    forced_version: str | None = None,
):
    """Кошелёк БЕЗ провайдера по мнемонике: чистая локальная математика.

    Общий строитель для HTTP-канала (лайтсерверы мертвы, сеть не нужна):
    версия контракта детектится по привязанному адресу, StateInit собирается
    из кода контракта и data ровно как в get_wallet/wallet_address (пара
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
        derived = wallet_address(requested, public_key, network_global_id)
        if normalize_address(derived) != normalize_address(address):
            raise ValueError(
                f"Адрес не совпадает с производным от мнемоники "
                f"(версия {requested}): {derived}. Проверь пару мнемоника/адрес "
                "или верни auto."
            )
        version = requested
    else:
        version, candidates = detect_wallet_version(public_key, address, network_global_id)
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


def build_offline_treasury_wallet():
    """Кошелёк казначея БЕЗ провайдера: build_offline_wallet от настроек.

    Вариант для HTTP-канала: лайтсерверы мертвы, поэтому экземпляр кошелька
    создаётся без подключения (provider=None), а StateInit собирается из кода
    контракта и data ровно как в get_wallet/wallet_address (пары мнемоника/
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
