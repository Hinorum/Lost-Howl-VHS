"""Мнемоника в логи не попадает — ни текстом сообщения, ни трейсбеком.

Пункт чек-листа «мнемоника не появляется в логах» проверялся grep'ом, а grep
проверяет состояние на момент проверки и ничего не запрещает: любой новый
вызов или чужая строка внутри исключения открывают путь заново. Здесь
проверяется сам запрет — на всём, что уходит в лог.
"""

from __future__ import annotations

import io
import logging

from app import log_scrub
from app.config import settings

MNEMONIC = " ".join(f"word{i:02d}" for i in range(24))
TESTNET_MNEMONIC = " ".join(f"net{i:02d}" for i in range(15))


def test_redact_replaces_the_whole_phrase(monkeypatch) -> None:
    """Фраза целиком и без учёта регистра: её могли залогировать в верхнем
    регистре или с хвостом от формата сообщения."""
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)

    text = log_scrub.redact(f"подпись упала: {MNEMONIC.upper()} — конец")

    assert "word00" not in text.lower()
    assert log_scrub.REDACTED in text
    assert "конец" in text


def test_redact_survives_env_line_wrapping(monkeypatch) -> None:
    """Разделители в настройке и в логе могут не совпадать: `.env` переносит
    строку иначе, чем она записана в коде. Красим по любым пробелам."""
    monkeypatch.setattr(settings, "treasury_mnemonic", "\n\t".join(MNEMONIC.split()))

    text = log_scrub.redact("ключ: " + "   ".join(MNEMONIC.split()))

    assert "word00" not in text
    assert log_scrub.REDACTED in text


def test_testnet_mnemonic_is_redacted_too(monkeypatch) -> None:
    """Тестнет-ключ — тоже ключ кошелька, и он лежит в той же переменной окружения."""
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", TESTNET_MNEMONIC)

    text = log_scrub.redact(f"testnet: {TESTNET_MNEMONIC}")

    assert "net00" not in text
    assert log_scrub.REDACTED in text


def test_single_word_alone_is_not_redacted(monkeypatch) -> None:
    """По одному слову красить нельзя: слова мнемоники обычные английские, и
    замена «the» на метку сломала бы лог, ничего не защитив."""
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)
    line = "повторил word05 и пошёл дальше"

    assert log_scrub.redact(line) == line


def test_short_value_is_not_treated_as_mnemonic(monkeypatch) -> None:
    """Короче 12 слов — не мнемоника BIP-39, а небрежный конфиг: красить такое
    значит расфокусировать внимание на пустяке."""
    monkeypatch.setattr(settings, "treasury_mnemonic", "the and for")

    assert log_scrub.redact("the end for all") == "the end for all"


def test_no_mnemonic_configured_changes_nothing(monkeypatch) -> None:
    """Дефолт (пусто) — краска выключена и не трогает обычные сообщения."""
    monkeypatch.setattr(settings, "treasury_mnemonic", "")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "")

    assert log_scrub.redact("обычное сообщение 42") == "обычное сообщение 42"


def test_scrub_clears_args_so_formatter_cannot_rebuild(monkeypatch) -> None:
    """Аргументы гасим вместе с текстом: иначе форматтер соберёт сообщение
    заново из `%s` и вернёт слова на место."""
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)
    record = logging.LogRecord(
        "way", logging.INFO, __file__, 1, "ключ: %s", (MNEMONIC,), None
    )

    log_scrub.scrub(record)

    assert MNEMONIC not in record.getMessage()
    assert record.args == ()
    assert log_scrub.REDACTED in record.getMessage()


def test_formatter_redacts_traceback_text(monkeypatch) -> None:
    """`exc_text` собирается при форматировании и через `record.msg` не виден:
    чужая строка «invalid mnemonic ...» внутри исключения обязана исчезнуть."""
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        log_scrub.redacting_formatter(logging.Formatter("%(levelname)s:%(message)s"))
    )
    logger = logging.getLogger("way.log_scrub.traceback")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        try:
            raise RuntimeError(f"invalid mnemonic: {MNEMONIC}")
        except RuntimeError:
            logger.exception("сбой подписи")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()

    assert MNEMONIC not in output
    assert log_scrub.REDACTED in output
    assert "сбой подписи" in output


def test_installed_factory_redacts_for_later_handlers(monkeypatch, caplog) -> None:
    """Фабрика записей покрывает и те хендлеры, которых на момент install() ещё
    не было — caplog из pytest как раз такой."""
    monkeypatch.setattr(settings, "treasury_mnemonic", MNEMONIC)
    log_scrub.install()

    with caplog.at_level(logging.INFO, logger="way.log_scrub.e2e"):
        logging.getLogger("way.log_scrub.e2e").info("ключ: %s", MNEMONIC)

    assert MNEMONIC not in caplog.text
    assert log_scrub.REDACTED in caplog.text
    assert "ключ" in caplog.text


def test_install_is_idempotent() -> None:
    """Повторный вызов не должен вешать краску второй раз на ту же фабрику."""
    log_scrub.install()
    factory = logging.getLogRecordFactory()
    log_scrub.install()

    assert logging.getLogRecordFactory() is factory


def test_production_entrypoint_installs_scrub() -> None:
    """Точка входа обязана включить краску при импорте — до старта задач,
    которые и могут утащить ключ в лог."""
    from app import main  # noqa: F401  (импорт и есть проверяемое действие)

    assert log_scrub._factory_installed
