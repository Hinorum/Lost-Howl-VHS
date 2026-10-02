"""Единый стиль сообщений Стаи: тон, формулировки и дозированные эмодзи.

Эмодзи здесь — приправа, а не еда: одна марка на смысловой блок. Ассортимент
широкий (по 10+ вариантов на пул), выбор детерминированный по ключу контекста:
один и тот же день/карта всегда выглядит одинаково, но дни отличаются между
собой. Никаких эмодзи в адресах, мемо, командах и цифрах.
"""

from __future__ import annotations

import hashlib
import re

# Разметка, которую Telegram окно колбэка (callback.answer) не рендерит:
# она приходит клиенту как сырой текст. Такой текст чистим перед показом.
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def strip_html(text: str) -> str:
    """Убирает HTML-теги из текста для окон без поддержки разметки.

    Содержимое (bv:КОД, адреса, числа) сохраняется — теги только отдаются.
    """
    return _HTML_TAG_RE.sub("", text)

# Обложка дня и заголовки канона.
DAY_MARKS = [
    "🌑", "🌒", "🌓", "🌘", "🌙", "✨", "🌌", "🌀",
    "🔮", "🕯️", "🌫️", "⚡", "💫", "🐾", "🐺", "🦴",
]

# Карты путей по тегам: риск / забота / хитрость.
PATH_MARKS = {
    "risk": ["🔥", "⚔️", "💥", "🌪️", "🩸", "🎢", "🌋", "🗡️", "⛈️", "🧨"],
    "care": ["🌿", "🍲", "🛖", "💚", "☀️", "🤲", "🌾", "🔔", "🫂", "🐣"],
    "cunning": ["🎭", "🗝️", "🃏", "🦊", "🕸️", "🎲", "🪙", "🧠", "🐍", "🪤"],
}

# Деньги, ставки, кошельки.
MONEY_MARKS = ["💎", "💸", "💰", "🪙", "🔗", "📈", "🏦", "🧾"]

# Подсказки, справка, навигация.
HINT_MARKS = ["🧭", "🗺️", "📌", "ℹ️", "👣", "🫖"]

# Предупреждения и отказы — без пугающих красных везде, только по делу.
WARN_MARKS = ["⚠️", "🕳️", "🌩️", "🚧", "🥀"]

# Тихие подтверждения.
OK_MARKS = ["✅", "🐾", "👌", "🕊️"]


def _pick(pool: list[str], key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def day_mark(key: str) -> str:
    return _pick(DAY_MARKS, f"day:{key}")


def path_mark(tag: str, key: str) -> str:
    pool = PATH_MARKS.get(tag) or PATH_MARKS["care"]
    return _pick(pool, f"path:{tag}:{key}")


def money_mark(key: str) -> str:
    return _pick(MONEY_MARKS, f"money:{key}")


def hint_mark(key: str) -> str:
    return _pick(HINT_MARKS, f"hint:{key}")


def warn_mark(key: str) -> str:
    return _pick(WARN_MARKS, f"warn:{key}")


def ok_mark(key: str = "") -> str:
    return _pick(OK_MARKS, f"ok:{key}")
