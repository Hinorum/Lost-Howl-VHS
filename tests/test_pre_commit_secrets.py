"""Тесты pre-commit хука scripts/hooks/check_secrets.py.

Покрывают три категории обнаружения и проверяют, что легитимные адреса
и tx-hash не вызывают ложных срабатываний.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "scripts" / "hooks" / "check_secrets.py"


def _load_module():
    """Загрузить модуль хука из абсолютного пути (не как пакет)."""
    spec = importlib.util.spec_from_file_location("check_secrets", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_secrets"] = module
    spec.loader.exec_module(module)
    return module


def test_mnemonic_24_words_is_detected():
    cs = _load_module()
    line = (
        'MNEMONIC = "abandon ability able about above absent absorb abstract absurd '
        'abuse access accident account accuse achieve acid acoustic acquire '
        'across act action actor actress actual"'
    )
    findings = cs._scan_line(line)
    assert any("BIP-39" in f for f in findings), findings


def test_hex_64_with_priv_key_context_is_detected():
    cs = _load_module()
    line = 'PRIV_KEY_HEX = "' + "a" * 64 + '"'
    findings = cs._scan_line(line)
    assert any("hex" in f for f in findings), findings


def test_ton_address_is_not_flagged():
    """`0:<hex64>` — публичный адрес, безопасный для коммита."""
    cs = _load_module()
    line = 'OWNER_WALLET_ADDRESS = "0:ca6e321c7cce9ecedf0a8ca2492ec8592494aa5fb5ce0387dff96ef6af982a3e"'
    assert cs._scan_line(line) == []


def test_tx_hash_is_not_flagged():
    """64-hex хеш транзакции — публичная информация из эксплорера TON."""
    cs = _load_module()
    line = 'TX_HASH = "' + "ab" * 32 + '"'  # 64 hex без ключевого слова
    assert cs._scan_line(line) == []


def test_address_friendly_format_is_not_flagged():
    """EQ/UQ-адрес (user-friendly форма TON) — безопасен."""
    cs = _load_module()
    line = 'USER_FRIENDLY = "EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF"'
    assert cs._scan_line(line) == []


def test_short_hex_is_not_flagged():
    """Короткие hex-строки (id, hash, версии) — безопасны."""
    cs = _load_module()
    for line in (
        'ROUND_ID = "abc123"',
        'REV = "5f70c7b44dbc"',
        'COMMIT = "f5408c2"',
    ):
        assert cs._scan_line(line) == [], line


def test_random_english_text_is_not_flagged():
    """Обычный английский текст в docstring/comment — безопасен."""
    cs = _load_module()
    line = '"""Pre-commit hook: защищает от утечки TREASURY_MNEMONIC или приватного ключа."""'
    assert cs._scan_line(line) == []


def test_scan_file_handles_binary_safely(tmp_path: Path):
    """Бинарь-псевдо-файл не должен ломать хук."""
    cs = _load_module()
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00\x01\x02\xff" * 64)
    assert cs._scan_file(binary) == []
