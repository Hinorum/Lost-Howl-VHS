"""Мнемоника и прочие секреты не должны дойти до логов — красим независимо от того, кто их залогировал.

Пункт чек-листа «мнемоника не появляется в логах» проверяется grep'ом, а grep
проверяет состояние на момент проверки: любой новый вызов, любой сторонний
`raise ValueError(f"... {mnemonic}")`, любой трейсбек с чужой строкой внутри
открывают путь заново, и следующая проверка — уже после инцидента. Здесь
утечка закрыта в принципе: каждая запись лога проходит через `redact()` до
того, как её увидит хоть один хендлер, а форматтер хендлера красит и то, что
лежит отдельно от текста сообщения — `exc_text`.

Красятся два вида секретов:
- мнемоники (кошелёк) — целая фраза настроенных слов; слова мнемоники обычные
  английские, замена «the» на «[скрыто]» сделала бы лог нечитаемым, ничего не
  защитив. Фраза ищется без учёта регистра и с любыми разделителями между
  словами: `.env` мог перенести её иначе, чем она записана в коде. Короче
  12 слов не красим: это уже не мнемоника BIP-39, а хендлер или чей-то
  небрежный конфиг (порог тот же, что в `scripts/hooks/check_secrets.py`).
- opaque-секреты (токены, API-ключи) — точное вхождение настроенного значения;
  это непрозрачные строки без «слов», ловятся только полным совпадением.
  Короче 8 символов не красим: пустяк вроде «true» ломал бы лог без пользы.
"""

from __future__ import annotations

import logging
import re

from app.config import settings

# Остаётся в логах вместо слов: метка узнаваема, чтобы инцидент было видно.
REDACTED = "[мнемоника скрыта]"
REDACTED_SECRET = "[секрет скрыта]"
_MIN_WORDS = 12
_MIN_SECRET_LEN = 8

_mnemonic_cache_key: tuple[str, str] | None = None
_mnemonic_cache: list[re.Pattern[str]] = []
_secret_cache_key: tuple[str, ...] | None = None
_secret_cache: list[re.Pattern[str]] = []
_factory_installed = False


def _compile(value: str) -> re.Pattern[str] | None:
    words = value.split()
    if len(words) < _MIN_WORDS:
        return None
    return re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)


def _mnemonic_patterns() -> list[re.Pattern[str]]:
    """Кэш паттернов: настройки в тестах меняются на лету, читать их на каждую
    запись лога дорого, поэтому пересобираем только при смене значения."""
    global _mnemonic_cache_key, _mnemonic_cache
    key = (settings.treasury_mnemonic, settings.treasury_testnet_mnemonic)
    if key != _mnemonic_cache_key:
        _mnemonic_cache_key = key
        _mnemonic_cache = [p for p in map(_compile, key) if p is not None]
    return _mnemonic_cache


def _secret_values() -> tuple[str, ...]:
    return (
        settings.bot_token,
        settings.health_token,
        settings.webhook_secret,
        settings.ton_api_key,
        settings.toncenter_api_key,
        settings.referral_secret,
    )


def _compile_secret(value: str) -> re.Pattern[str] | None:
    stripped = value.strip()
    if len(stripped) < _MIN_SECRET_LEN:
        return None
    return re.compile(re.escape(stripped))


def _secret_patterns() -> list[re.Pattern[str]]:
    global _secret_cache_key, _secret_cache
    key = _secret_values()
    if key != _secret_cache_key:
        _secret_cache_key = key
        _secret_cache = [p for p in map(_compile_secret, key) if p is not None]
    return _secret_cache


def redact(text: str) -> str:
    """Убирает из строки мнемоники и opaque-секреты настроенных значений."""
    for pattern in _mnemonic_patterns():
        text = pattern.sub(REDACTED, text)
    for pattern in _secret_patterns():
        text = pattern.sub(REDACTED_SECRET, text)
    return text


def scrub(record: logging.LogRecord) -> None:
    """Красит запись лога до того, как её увидит любой хендлер.

    Работает и когда аргументы ушли не текстом, а в `args`: красим уже
    собранное сообщение и гасим `args`, иначе форматтер соберёт его заново.
    """
    if not _mnemonic_patterns() and not _secret_patterns():
        return
    try:
        message = record.getMessage()
    except Exception:
        # Сломанные args не должны валить логирование (и краску): остаётся
        # как есть, форматтер сам разберётся, краска ниже его не догонит.
        return
    clean = redact(message)
    if clean != message:
        record.msg = clean
        record.args = ()


class _RedactingFormatter(logging.Formatter):
    """Форматтер, красящий уже собранный вывод — включая трейсбек.

    `record.msg` этого не покрывает: текст исключения собирается отдельно, при
    форматировании, и именно так в лог попадают чужие строки вида
    «invalid mnemonic: <24 слова>» из сторонней библиотеки.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))


def redacting_formatter(inner: logging.Formatter | None = None) -> logging.Formatter:
    """Обёртка для форматтера хендлера; без аргумента — обёртка дефолтного."""
    if isinstance(inner, _RedactingFormatter):
        return inner
    return _RedactingFormatter(inner or logging.Formatter())


def install() -> None:
    """Включает краску для всего, что пишется в лог дальше. Идемпотентно.

    Два уровня, потому что один не покрывает всё:
    - фабрика записей — для любых хендлеров, включая добавленные позже
      (их форматтеры на момент install() ещё не существуют);
    - форматтеры текущих хендлеров — для `exc_text`, который фабрика не видит.
    """
    global _factory_installed
    if not _factory_installed:
        base = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = base(*args, **kwargs)
            scrub(record)
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True
    for handler in logging.getLogger().handlers:
        handler.setFormatter(redacting_formatter(handler.formatter))
