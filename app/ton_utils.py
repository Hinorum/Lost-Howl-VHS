"""Утилиты TON: валидация адресов, конвертация нанотон, хук доверия к кошельку."""

from __future__ import annotations

import base64

NANO = 1_000_000_000

# Префиксы user-friendly адресов: бит сети в теге даёт 0Q/kQ (тестнет),
# остальные — EQ/UQ (mainnet).
_TESTNET_TAGS = ("kQ", "0Q")
_MAINNET_TAGS = ("EQ", "UQ")


def address_network(address: str) -> str | None:
    """Сеть адреса по его user-friendly тегу: "mainnet"/"testnet"/None.

    None — по адресу сеть не видна: raw-форма (wc:hex) несёт только
    workchain, а неразбираемая строка — ничего.

    Это важно для v5-кошельков: у них сеть зашита в data cell, то есть
    тестнетовый и mainnet-овый адреса — РАЗНЫЕ аккаунты. Адрес чужой сети,
    привязанный в базе, вёл бы себя тихо: переводы с него не видны в сети
    бота, кошелёк не подтверждается, ставки не засчитываются, а приз ждёт
    в очереди выплат.
    """
    candidate = address.strip()
    if len(candidate) != 48:
        return None
    tag = candidate[:2]
    if tag in _TESTNET_TAGS:
        return "testnet"
    if tag in _MAINNET_TAGS:
        return "mainnet"
    return None


def is_valid_ton_address(address: str) -> bool:
    """Принимает user-friendly base64url (EQ/UQ/kQ/0Q, 48 символов) и raw hex."""
    candidate = address.strip()
    if len(candidate) == 48 and candidate[:2] in {"EQ", "UQ", "kQ", "0Q"}:
        try:
            normalized = candidate.replace("-", "+").replace("_", "/")
            decoded = base64.b64decode(normalized, validate=True)
            if len(decoded) != 36:
                return False
            # CRC16-XMODEM: байты 34-35 должны совпадать с контрольной суммой
            expected_crc = _crc16_xmodem(decoded[:34])
            actual_crc = int.from_bytes(decoded[34:36], "big")
            return expected_crc == actual_crc
        except Exception:
            return False
    if len(candidate) == 66 and candidate[1] == ":":
        hex_part = candidate[2:]
        try:
            int(hex_part, 16)
            return True
        except ValueError:
            return False
    return False


def normalize_address(address: str) -> str:
    """Приводит к единому виду для сравнения (raw hex без флагов).

    User-friendly раскладка: байт 0 — тег, байт 1 — воркчейн (знаковый),
    байты 2..33 — хеш аккаунта, байты 34..35 — CRC.

    Разбираемая строка всегда канонизируется, неразбираемая возвращается
    как есть (в нижнем регистре): функция вызывается и на данных из
    блокчейна, и на пользовательском вводе, и на легаси-записях БД —
    исключение здесь ломало бы watcher/миграции на мусорной строке.
    CRC не проверяется: сравнение адресов это допускает (валидность
    контролирует is_valid_ton_address на входе от игрока).
    """
    candidate = address.strip()
    if len(candidate) == 48 and candidate[:2] in {"EQ", "UQ", "kQ", "0Q"}:
        normalized = candidate.replace("-", "+").replace("_", "/")
        try:
            decoded = base64.b64decode(normalized, validate=True)
        except Exception:
            return candidate.lower()
        if len(decoded) != 36:
            return candidate.lower()
        wc = decoded[1] - 256 if decoded[1] >= 0x80 else decoded[1]
        return f"{wc}:{decoded[2:34].hex()}"
    return candidate.lower()


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def friendly_address(raw: str, *, testnet: bool = False, bounceable: bool = False) -> str:
    """Обратное к normalize_address: raw hex -> user-friendly base64url.

    Нужен для показа игроку и платёжных ссылок: хранить адрес в raw удобно
    для сверки, читать его человеку — нет. Тег по умолчанию 0x51
    (non-bounceable); тестнет-бит 0x80 даёт знакомые по кошелькам префиксы
    0Q/kQ. Если raw разобрать не удалось, возвращаем как есть.
    """
    candidate = raw.strip().lower()
    try:
        wc_part, hex_part = candidate.split(":", 1)
        wc = int(wc_part)
        account = bytes.fromhex(hex_part)
    except ValueError:
        return raw
    if len(account) != 32 or not -128 <= wc <= 127 or len(hex_part) != 64:
        return raw
    tag = 0x11 if bounceable else 0x51
    if testnet:
        # Тестнет-бит 0x80: даёт префиксы 0Q/kQ как в тестнет-кошельках.
        tag |= 0x80
    payload = bytes([tag, wc & 0xFF]) + account
    payload += _crc16_xmodem(payload[:34]).to_bytes(2, "big")
    return base64.b64encode(payload).decode().replace("+", "-").replace("/", "_")


def to_nano(amount_ton: float) -> int:
    return int(round(amount_ton * NANO))


def from_nano(amount_nanotons: int) -> float:
    return amount_nanotons / NANO
