"""Платная смена выбора: форматы payload-ов и мемо, общие для Stars и TON."""

from __future__ import annotations

import re

REVOTE_MEMO_PREFIX = "rv:"

# Кошелёк не всегда доставляет комментарий байт-в-байт: вокруг фразы может
# оказаться мусор (перевод строки, неразрывные/нулевые пробелы, подпись
# кошелька, эмодзи). Ищем токен rv:<цифры> в любом месте нормализованного
# текста, а не требуем, чтобы он был всей строкой от первой позиции.
# \u200b/\u200c/\u200d — невидимые «нулевые» пробелы, которые кошельки иногда
# подмешивают внутрь скопированной фразы.
_ZERO_WIDTH = "\u200b\u200c\u200d"
_REVOTE_PATTERN = re.compile(rf"rv[{_ZERO_WIDTH}\s]*:[{_ZERO_WIDTH}\s]*(\d+)", re.IGNORECASE)

# Код подтверждения владения кошельком: bv:<код> в комментарии микро-перевода
# на казначея. Защита от сквата чужих публичных адресов — код выдан владельцу
# телеграм-аккаунта при привязке, а перевести с адреса может только его хозяин.
_VERIFY_PATTERN = re.compile(rf"bv[{_ZERO_WIDTH}\s]*:[{_ZERO_WIDTH}\s]*([a-z0-9]+)", re.IGNORECASE)


def revote_memo(round_id: int) -> str:
    """Комментарий к TON-переводу за смену выбора."""
    return f"{REVOTE_MEMO_PREFIX}{round_id}"


def build_revote_payload(round_id: int) -> str:
    """invoice_payload для счёта Telegram Stars."""
    return f"revote:{round_id}"


def parse_revote_payload(payload: str | None) -> int | None:
    parts = (payload or "").split(":")
    if len(parts) == 2 and parts[0] == "revote" and parts[1].isdigit():
        return int(parts[1])
    return None


def parse_revote_memo(memo: str | None) -> int | None:
    text = (memo or "").strip()
    if not text:
        return None
    # Прямое соответствие как раньше — быстро и без сюрпризов.
    lowered = text.lower()
    if lowered.startswith(REVOTE_MEMO_PREFIX):
        raw = text.lower()[len(REVOTE_MEMO_PREFIX):].strip()
        if raw.isdigit():
            return int(raw)
    # Кошелёк мог добавить пробелы до/после или обернул фразу иначе.
    match = _REVOTE_PATTERN.search(lowered)
    if match is not None:
        return int(match.group(1))
    return None


def parse_verify_memo(memo: str | None) -> str | None:
    """Проверочный код bv:<код> из комментария подтверждения кошелька.

    Находит токен в любом месте текста (кошелёк часто подмешивает мусор) и
    возвращает код верхним регистром — так, как он хранится при привязке.
    """
    match = _VERIFY_PATTERN.search(str(memo or ""))
    if match is None:
        return None
    return match.group(1).upper()


def parse_bank_memo(memo: str | None) -> bool:
    """Мемо капитала казны (bank:…) — внешнее пополнение, а не ставка.

    Кошелёк может подмешать мусор (нулевые пробелы, перевод строки): принимаем
    точное «bank» или слово после префикса bank:, без требований к остальному.
    Деньги, пришедшие с этим мемо, watcher не возвращает и не считает ставкой
    (банк дня не растёт), а пишет строкой входящего дохода — зеркало учитывает
    их в тождестве «в ноль».
    """
    text = str(memo or "").replace(_ZERO_WIDTH, "").strip().lower()
    return text == "bank" or text.startswith("bank:")
