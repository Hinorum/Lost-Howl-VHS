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

import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

RENDER_YAML = Path(__file__).resolve().parents[1] / "render.yaml"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Ключи, которых нет в Settings, но которые нужны окружению:
# - E2E_* — живой тестнет-прогон (scripts/e2e_testnet.py), читается скриптом;
# - PYTHON_VERSION — переменная сборки Render, не настройка бота.
NON_SETTING_KEYS = {
    "E2E_PLAYER_ID",
    "E2E_PLAYER_MNEMONIC",
    "E2E_STAKE_TIMEOUT_SECONDS",
    "E2E_TESTNET",
    "PYTHON_VERSION",
}

# Рычаги, которые render.yaml объявляет «те же, что в app/config.py».
MONEY_LEVERS = (
    ("PAYOUT_FEE_GRAM", "payout_fee_gram"),
    ("MIN_PAYOUT_GRAM", "min_payout_gram"),
    ("REFUND_MIN_GRAM", "refund_min_gram"),
    ("REFUND_FEE_RATIO", "refund_fee_ratio"),
    ("REFERRAL_PCT", "referral_pct"),
    ("REFERRAL_MIN_PAYOUT_GRAM", "referral_min_payout_gram"),
    # Правила зачёта тоже двигают деньги: порог недели решает, кому вообще
    # достаётся приз, а потолок верности — как делится зачёт. Указ b754d15
    # (4 → 1) разошёлся с манифестом и .env.example и жил так неделю ровно
    # потому, что этих строк здесь не было.
    ("WEEKLY_MIN_DAYS", "weekly_min_days"),
    ("LEADERBOARD_CORRECT_CAP", "leaderboard_correct_cap"),
    # Флаг пачки v5r1 — не сумма, а выключатель денежного пути: расхождение
    # «в render.yaml true, в коде false» — та же тихая дивергенция копий,
    # ради которой этот тест и существует.
    ("PAYOUT_V5_BATCH", "payout_v5_batch"),
    # Второй вещатель HTTP-канала — тоже выключатель денежного пути: при
    # «render.yaml true, код false» отказавший Toncenter не будет продублирован
    # через TonAPI, и выплаты встанут там, где задумано было не вставать.
    ("HTTP_TONAPI_FALLBACK", "http_tonapi_fallback"),
    # Пересборка пула лайтсерверов по серии сбоев: включена в Render, но
    # выключена в коде — «больное окно» лайтсерверов останется вечным, а
    # оператор будет думать, что лечение уже включено.
    ("TON_PROVIDER_REBUILD", "ton_provider_rebuild"),
    # Границы ставки и тай-брейк ничьей — тоже денежные рычаги: минимум
    # определяет, что вообще станет ставкой, потолок — что не станет, а
    # tie_break_by_votes меняет, кто получит банк при равных суммах.
    ("STAKE_MIN_TON", "stake_min_ton"),
    ("STAKE_MAX_TON", "stake_max_ton"),
    ("TIE_BREAK_BY_VOTES", "tie_break_by_votes"),
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


def _manifest_keys() -> set[str]:
    """ВСЕ ключи render.yaml — включая те, у которых нет значения (sync: false)."""
    data = yaml.safe_load(RENDER_YAML.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for service in data.get("services", []):
        for entry in service.get("envVars", []) or []:
            key = entry.get("key")
            if key:
                keys.add(str(key))
    return keys


def _env_example_keys() -> set[str]:
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", env, re.M))


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
    default = Settings.model_fields[field].default
    if isinstance(default, bool):
        assert str(actual).strip().lower() == str(default).strip().lower(), (
            f"{env_key} {where} = {actual}, "
            f"а в app/config.py = {default}"
        )
        return
    assert float(actual) == pytest.approx(
        float(default)
    ), (
        f"{env_key} {where} = {actual}, "
        f"а в app/config.py = {default}"
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


def test_every_setting_is_documented_in_env_example() -> None:
    """Каждая настройка бота есть в .env.example — файл, который копируют.

    Раньше паритет проверял только 16 «рычагов» денег, а остальные настройки
    могли отсутствовать в файле годами: оператор копирует .env.example и
    не знает, что настройка вообще существует (а потолок возвратов или
    параметры проверки логов хостинга — это не «рычаг», а защита).

    Исключения — ключи, которых в Settings нет по определению (E2E_* —
    живой прогон, PYTHON_VERSION — переменная сборки Render).
    """
    settings_keys = {name.upper() for name in Settings.model_fields}
    env_keys = _env_example_keys()
    missing = sorted(settings_keys - env_keys)
    assert not missing, f"настройки не описаны в .env.example: {missing}"


def test_env_example_has_no_dead_keys() -> None:
    """В .env.example нет ключей, которых бот не читает.

    Мёртвый ключ — это опечатка (бота он не настроит) или секрет ради
    ничего (в окружении лежит, а код его не смотрит). И то и другое —
    источник расхождений: «я выставил LLM_API_KEY, а генерации не
    изменились».
    """
    settings_keys = {name.upper() for name in Settings.model_fields}
    env_keys = _env_example_keys()
    unknown = sorted(env_keys - settings_keys - NON_SETTING_KEYS)
    assert not unknown, f"в .env.example есть ключи, которых бот не читает: {unknown}"


def test_manifest_has_no_unknown_keys() -> None:
    """В render.yaml нет ключей, которых бот не читает.

    Опечатка в манифесте (PAYOUT_FEE_GARM) молча оставляет дефолт, и
    оператор думает, что выставил значение. Мёртвый ключ — секрет в
    окружении ради ничего.
    """
    settings_keys = {name.upper() for name in Settings.model_fields}
    manifest_keys = _manifest_keys()
    unknown = sorted(manifest_keys - settings_keys - NON_SETTING_KEYS)
    assert not unknown, f"в render.yaml есть ключи, которых бот не читает: {unknown}"


def test_money_safety_knobs_are_declared_in_manifest() -> None:
    """Рычаги безопасности денег объявлены в манифесте деплоя.

    Потолок авто-возвратов и таймаут проверки баланса — не «рычаги» в
    смысле распределения призов, а защита казны от сгоревшего газа. Если
    их нет в render.yaml, прод работает на дефолтах кода, и оператор об
    этом не знает.
    """
    manifest = _manifest_keys()
    for key in (
        "REFUND_MAX_PER_SENDER_DAY",
        "REFUND_MAX_TOTAL_DAY",
        "PAYOUT_BALANCE_PROBE_TIMEOUT_SECONDS",
    ):
        assert key in manifest, f"{key} не объявлен в render.yaml"


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
