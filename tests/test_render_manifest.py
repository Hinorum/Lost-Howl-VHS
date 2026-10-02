"""render.yaml — манифест деплоя не должен хранить секреты открытым текстом.

Раньше healthCheckPath был /health?token=<HEALTH_TOKEN>, и значение токена
лежало в репозитории дважды, а репозиторий публичный. Теперь проба Render
смотрит на /alive (без секрета и без снимка), а все чувствительные переменные
объявлены sync: false. Эти тесты не дают вернуть секрет в манифест.
"""

from pathlib import Path

import yaml

RENDER_YAML = Path(__file__).resolve().parents[1] / "render.yaml"

SECRET_KEYS = {
    "ADMIN_IDS",
    "BOT_TOKEN",
    "DATABASE_URL",
    "GEMINI_API_KEY",
    "HEALTH_TOKEN",
    "LLM_API_KEY",
    "OWNER_WALLET_ADDRESS",
    "TON_API_KEY",
    "TONCENTER_API_KEY",
    "TREASURY_ADDRESS",
    "TREASURY_MNEMONIC",
    "TREASURY_TESTNET_ADDRESS",
    "TREASURY_TESTNET_MNEMONIC",
    "WEBHOOK_SECRET",
}


def _service() -> dict:
    manifest = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    return manifest["services"][0]


def test_secret_env_vars_are_not_committed() -> None:
    offenders = [
        entry["key"]
        for entry in _service()["envVars"]
        if entry["key"] in SECRET_KEYS and ("value" in entry or entry.get("sync") is not False)
    ]
    assert not offenders, f"секреты в render.yaml: {offenders}"


def test_health_check_path_carries_no_secret() -> None:
    path = _service()["healthCheckPath"]
    assert path == "/alive"
    assert "token" not in path.lower()


def test_health_require_token_pinned_in_manifest() -> None:
    values = {entry["key"]: entry.get("value") for entry in _service()["envVars"]}
    assert values["HEALTH_REQUIRE_TOKEN"] == "true"