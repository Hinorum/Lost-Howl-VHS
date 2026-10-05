"""Денежные пороги не должны расходиться между кодом и манифестом деплоя.

`render.yaml` объявляет, что это «единственное место, где прод видит эти
рычаги»: оператор правит его, а не `app/config.py`. Пока обе копии держались
в согласии, это было правдой. Но `PAYOUT_FEE_GRAM` в манифесте был 0.002, а в
коде 0.005 — и комиссия за один исходящий перевод оказывалась разной в двух
местах, которые обе считают деньги:

* из призового пула вычитается до раздачи (`pokes` при финализации дня);
* по ней же `ops` считает допуски сверки казны с БД.

Молчаливого признака такой ошибки нет: газ уходит на выплаты, отчёт о
расхождении казны сходится, а игроки получают чуть меньше или казна чуть
меньше обещает. Поэтому равенство держим тестом, а не комментарием.

Копий этих чисел три, не две: `render.yaml`, `.env.example` и дефолт в коде.
Третью мы долго не сверяли, а именно её оператор копирует в `.env` при
первом развороте — расхождение там тише всех остальных и доживает до
боевого деплоя, потому что цифра уже не отличается от задуманной.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import Settings

RENDER_YAML = Path(__file__).resolve().parents[1] / "render.yaml"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Рычаги, которые render.yaml объявляет «те же, что в app/config.py».
MONEY_LEVERS = (
    ("PAYOUT_FEE_GRAM", "payout_fee_gram"),
    ("MIN_PAYOUT_GRAM", "min_payout_gram"),
    ("REFUND_MIN_GRAM", "refund_min_gram"),
    ("REFUND_FEE_RATIO", "refund_fee_ratio"),
    ("REFERRAL_PCT", "referral_pct"),
    ("REFERRAL_MIN_PAYOUT_GRAM", "referral_min_payout_gram"),
)


def _manifest_values() -> dict[str, str]:
    data = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    values: dict[str, str] = {}
    for service in data.get("services", []):
        for entry in service.get("envVars", []):
            key = entry.get("key")
            if key is not None and entry.get("value") is not None:
                values[key] = str(entry["value"])
    return values


def _env_example_values() -> dict[str, str]:
    """KEY=VALUE из .env.example, без комментариев и пустых строк."""
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _assert_matches_default(env_key: str, field: str, actual: str, where: str) -> None:
    assert float(actual) == pytest.approx(
        float(Settings.model_fields[field].default)
    ), (
        f"{env_key} {where} = {actual}, "
        f"а в app/config.py = {Settings.model_fields[field].default}"
    )


@pytest.mark.parametrize(("env_key", "field"), MONEY_LEVERS)
def test_money_lever_matches_code_default(env_key: str, field: str) -> None:
    """Порог из манифеста деплоя обязан совпадать с дефолтом в коде."""
    manifest = _manifest_values()
    assert env_key in manifest, f"{env_key} пропал из render.yaml — рычаг стал невидимым на деплое"
    _assert_matches_default(env_key, field, manifest[env_key], "в render.yaml")


@pytest.mark.parametrize(("env_key", "field"), MONEY_LEVERS)
def test_money_lever_matches_env_example(env_key: str, field: str) -> None:
    """Третья копия рычагов — .env.example — обязана совпадать с кодом.

    Именно её оператор копирует в `.env` при первом развороте, и именно её
    никто раньше не сверял. Расхождение здесь тише остальных: числа в
    render.yaml перечитываются перед деплоем, а копию читают один раз в
    начале и дальше уже не отличают от задуманного.
    """
    env = _env_example_values()
    assert env_key in env, f"{env_key} пропал из .env.example — рычаг стал невидимым при развороте"
    _assert_matches_default(env_key, field, env[env_key], "в .env.example")


def test_mainnet_liteserver_url_is_not_testnet() -> None:
    """mainnet не должен ходить за конфигом тестнетовых лайтсерверов.

    URL подставляется в pytoniq с приоритетом над TON_NETWORK. Пока в
    манифесте стоял `testnet-global.config.json` при `TON_NETWORK=mainnet`,
    `get_seqno()` читал seqno mainnet-адреса на testnet-нодах: выплаты
    подписывались неверным seqno, возвращали `result == 1`, помечались sent и
    после пяти попыток уходили в dead-letter. Игрокам не платили вообще, а
    `/health` был зелёный — ошибка не проявлялась ни одним симптомом, кроме
    невыплаченных денег.
    """
    manifest = _manifest_values()
    network = manifest.get("TON_NETWORK", "").strip().lower()
    url = manifest.get("LITESERVER_CONFIG_URL", "")
    assert url, "LITESERVER_CONFIG_URL пуст — лайтсерверы берутся из встроенного мёртвого конфига"
    if network == "testnet":
        assert "testnet" in url.lower(), f"testnet ходит за конфигом mainnet: {url}"
    else:
        assert "testnet" not in url.lower(), f"mainnet ходит за конфигом testnet: {url}"
