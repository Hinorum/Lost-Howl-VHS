"""Маркер-гард живого e2e-контура (scripts/e2e_testnet.py).

Полный прогон ставка → подсчёт → выплаты → зеркало идёт только против живого
тестнета и включается явно: E2E_TESTNET=1 плюс тестнет-ключи (охранный гейт
сам перечислит недостающее). Живой тест помечен `e2e` и в регулярном прогоне
молча скапывается (skipif) — сеть и чужие деньги не трогаются. Охранный гейт
всегда тестируется без сети отдельными юнит-тестами.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts.e2e_testnet import guard

_LIVE = os.environ.get("E2E_TESTNET") == "1" and not guard()


def test_guard_rejects_dev_checkout(monkeypatch) -> None:
    """В «пустом» окружении гейт обязан назвать причины, а не стартовать.

    Тест агностичен к лежащему в репо .env (gitignored): сносим все контурные
    ключи явно и ждём причин по ним же.
    """
    for key in (
        "TON_NETWORK",
        "DATABASE_URL",
        "TREASURY_TESTNET_ADDRESS",
        "TREASURY_TESTNET_MNEMONIC",
        "OWNER_WALLET_ADDRESS",
        "E2E_PLAYER_ID",
        "E2E_PLAYER_MNEMONIC",
    ):
        monkeypatch.delenv(key, raising=False)
    text = " ".join(guard())
    assert "TON_NETWORK" in text
    assert "E2E_PLAYER_MNEMONIC" in text


def test_guard_rejects_mainnet_even_with_all_keys(monkeypatch) -> None:
    """На mainnet скрипт не запустится даже с полным набором ключей — это не баг."""
    monkeypatch.setenv("TON_NETWORK", "mainnet")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("TREASURY_TESTNET_ADDRESS", "kQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setenv("TREASURY_TESTNET_MNEMONIC", "a b c d e f g h i j k l")
    monkeypatch.setenv("OWNER_WALLET_ADDRESS", "kQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setenv("E2E_PLAYER_ID", "1")
    monkeypatch.setenv("E2E_PLAYER_MNEMONIC", "a b c d e f g h i j k l")
    assert any("TON_NETWORK" in reason for reason in guard())


def test_standalone_stake_phase_refuses_on_mainnet() -> None:
    """Отдельная фаза stake обязана упираться в гейт (реальные переводы!),
    а не молча слать деньги в mainnet."""
    env = dict(os.environ)
    env["TON_NETWORK"] = "mainnet"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_testnet", "stake"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 2, f"ожидался отказ гейта (код 2):\n{result.stdout}\n{result.stderr}"
    # logging пишет в stderr: причина отказа обязана быть видна
    assert "TON_NETWORK" in result.stdout + result.stderr


@pytest.mark.e2e
@pytest.mark.skipif(
    not _LIVE,
    reason="E2E_TESTNET=1 + готовый тестнет-контур не активированы (это живой прогон против testnet)",
)
def test_live_testnet_check_phase() -> None:
    """Живой гейт: при E2E_TESTNET=1 скрипт обязан пройти check без ошибок."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_testnet", "check"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"e2e check не прошёл:\n{result.stdout}\n{result.stderr}"
