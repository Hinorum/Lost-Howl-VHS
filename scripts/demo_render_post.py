"""Демо: что игрок видит в Telegram после правок (столбик кнопок, без
🧠-кнопки памяти и без миграции memory_hits).

Скрипт НЕ часть прод-кода. Не дёргает БД, не эмулирует SQLAlchemy — просто
повторяет логику status_text() и cards_keyboard() из app/broadcast.py на
данных кассеты tuman-na-viale.json. Для удобства выводит «как в Telegram».
"""
from __future__ import annotations

import html
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

POSITIONS = ("I", "II", "III")
_BUTTON_TEXT_MAX = 64


def _clamp(text: str, limit: int) -> str:
    """Дословно из app/broadcast.py:81 — чтобы числа в обоих местах совпадали."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    tail = cut.rsplit(" ", 1)
    if len(tail) == 2:
        cut = tail[0]
    return cut.rstrip(" ,.;:") + "…"


def status_text(cassette_day: dict) -> str:
    """Упрощённый status_text без БД, TON-банка и эхо (для дня 1 prev пуст)."""
    title = html.escape(cassette_day["chapter_title"], quote=False)
    head = f"🜂 {title}\n\n"
    story = f"{html.escape(cassette_day['chapter_text'], quote=False)}\n\n"
    dilemma = html.escape(cassette_day["dilemma"], quote=False)
    # phase + дедлайн — без БД; для демо берём дефолт «сразу».
    now = datetime.now(UTC)
    voting_at = now + timedelta(hours=18)
    phase = "🎬 Сцена дня: большинство решит. Счёт скрыт до конца сцены."
    deadline = f"🗳 Голосование до {voting_at:%H:%M} UTC — итоги и новый день придут сразу после"
    tail = f"\n\n{phase}\n{deadline}"
    return head + story + dilemma + tail


def cards_keyboard(cassette_day: dict) -> list[tuple[str, str]]:
    """Возвращает [(text, callback_data), …] для каждой кнопки в столбике."""
    titles = {card["position"]: card["title"] for card in cassette_day["cards"]}
    rows = []
    for position in range(3):
        label = titles.get(position) or f"Сцена {POSITIONS[position]}"
        if len(label) > _BUTTON_TEXT_MAX:
            label = _clamp(label, _BUTTON_TEXT_MAX - 1)
        rows.append((label, f"vote:9001:{position}"))
    return rows


def main() -> None:
    cassette_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "story"
        / "cassettes"
        / "tuman-na-viale.json"
    )
    cassette = json.loads(cassette_path.read_text(encoding="utf-8"))
    print("=" * 60)
    print(f"КАССЕТА: {cassette.get('title', '?')}  ·  месяц {cassette['month']}")
    print("=" * 60)

    for day_index in (1, 2):
        day = cassette["days"][day_index - 1]
        print(f"\n========== ДЕНЬ {day_index} ==========")
        print("\nТЕКСТ ПОСТА (status_text):")
        body = status_text(day)
        for line in body.split("\n"):
            print(f"| {line}")

        print("\nКЛАВИАТУРА (cards_keyboard — столбик из 3 кнопок):")
        for text, callback in cards_keyboard(day):
            print("+----------------------------------------")
            print(f"| {text}   -> callback_data = {callback!r}")
        print("+----------------------------------------")
        print()


if __name__ == "__main__":
    main()
