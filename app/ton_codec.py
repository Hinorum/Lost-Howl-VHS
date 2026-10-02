"""Чистые кодеки TON-сообщений: единая реализация для ton_watch и ton_pay.

Watcher входящих и диспетчер выплат держали свои копии расшифровки
комментариев, нормализации хешей и заголовков API (дрейф форматов после
правок одного из контуров — реальный источник точечных багов). Здесь —
единственный источник: поведение зафиксировано тестами (test_ops,
test_watch_sources, test_payout_dedupe, test_payout_reconcile).
Никакой сети, БД и настроек: чистые функции над примитивами индексаторов.
"""

from __future__ import annotations

import base64

# Кошелёк изредка добавляет к комментарию невидимые символы (нулевая ширина,
# неразрывные пробелы, BOM) — они ломали бы строгий разбор rv:-memo.
_COMMENT_NOISE = str.maketrans(
    {
        "\ufeff": "",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def api_headers(api_key: str) -> dict:
    """Заголовок ключа для Toncenter; пустой ключ — без заголовка.

    Только Toncenter: v3 принимает `X-API-Key`. У TonAPI другой стандарт —
    см. tonapi_headers.
    """
    return {"X-API-Key": api_key} if api_key else {}


def tonapi_headers(api_key: str) -> dict:
    """Заголовок ключа для TonAPI; пустой ключ — без заголовка.

    TonAPI авторизует по `Authorization: Bearer`, и `X-API-Key` вовсе не считает:
    ключ, отправленный не тем заголовком, молча уходит в анонимный режим. На
    практике это выглядит как «сломанный ключ», хотя он валиден — растолковывать
    приходится по телу ответа. Признак анонимного режима — `limit must not
    exceed 100: anonymous tier`: /v2/blockchain режет страницу до 100, тогда как
    с ключом проходит и 1000, плюс снимается анонимный троттлинг 1 rps.
    """
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def clean_comment(text: str) -> str:
    """Убирает невидимые символы из комментария, сохраняя остальное как есть."""
    return str(text).translate(_COMMENT_NOISE)


def extract_comment(msg: dict) -> str:
    """Текст комментария сообщения в формате TonAPI v2 / Toncenter v3.

    Поля, где провайдеры прячут текст: decoded_body (TonAPI по op-имени),
    msg_data.decoded_comment, msg_data.text (base64), message_content.decoded
    (Toncenter v3) и raw_message — только если это короткий текст, а не BoC.
    """
    if not isinstance(msg, dict):
        return ""
    decoded_body = msg.get("decoded_body")
    if isinstance(decoded_body, dict) and msg.get("decoded_op_name") == "text_comment":
        return clean_comment(str(decoded_body.get("text") or ""))
    msg_data = msg.get("msg_data")
    if isinstance(msg_data, dict):
        decoded = msg_data.get("decoded_comment")
        if decoded:
            return clean_comment(str(decoded))
        b64 = msg_data.get("text")
        if b64:
            text = _text_from_b64(b64)
            if text:
                return text
    content = msg.get("message_content")
    if isinstance(content, dict):
        decoded = content.get("decoded")
        if isinstance(decoded, dict) and decoded.get("@type") in ("comment", "text_comment"):
            text = str(decoded.get("comment") or "")
            if text:
                return clean_comment(text)
    raw = str(msg.get("raw_message") or "")
    if raw and len(raw) < 200:
        return clean_comment(raw)
    return ""


def _text_from_b64(b64: str) -> str:
    try:
        return clean_comment(base64.b64decode(str(b64)).decode("utf-8", "ignore"))
    except Exception:
        return ""


def norm_tx_hash(raw: str) -> str:
    """Единая форма хеша транзакции для всех провайдеров — hex lowercase.

    TonAPI отдаёт base64url, Toncenter v3 — стандартный base64 с паддингом.
    Идемпотентность ставок/возвратов строится на tx_hash, поэтому одна и та же
    транзакция, увиденная разными источниками, обязана дать одну строку.
    Разобрать не удалось — возвращаем как есть (в нижнем регистре).
    """
    candidate = raw.strip()
    # Только правдоподобные длины хеша транзакции: hex-64 либо base64
    # тридцати двух байт (43 без паддинга / 44 с ним). Прочие строки —
    # служебные метки тестов и логов — проходят насквозь нетронутыми.
    if len(candidate) == 64:
        try:
            int(candidate, 16)
            return candidate.lower()
        except ValueError:
            # Не hex — значит, это base64-вид, пробуем его ниже. Это проверка
            # формата, а не сбой: исключение здесь часть разбора адреса.
            pass
    if len(candidate) not in (43, 44):
        return candidate.lower()
    b64 = candidate.replace("-", "+").replace("_", "/")
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        return base64.b64decode(padded, validate=True).hex()
    except Exception:
        return candidate.lower()