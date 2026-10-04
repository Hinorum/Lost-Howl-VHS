"""Починка привязки эха `prev` к своему дню — поимённо, с сохранением прозы.

Что делает
----------
`prev` дня N — эхо выбора дня N−1 (см. DayModel.prev, bay._prev_echo). Если
автор написал в `prev` дня N пересказ последствий карт САМОГО дня N, эхо
показывается игроку на день раньше нужного: он читает последствия выбора,
которого ещё не сделал.

Скрипт находит такие дни ЛЕКСИКО (тем же способом, что и
schema.prev_alignment_warnings) и переносит их эхо в день N+1. Проза не
переписывается: тексты переезжают как есть.

Ключевая деталь реализации
--------------------------
План сдвига строится ЦЕЛИКОМ в отдельном словаре «куда какое эхо встаёт», а не
правкой на месте. У сбитых дней, идущих подряд (а идут они подряд почти всегда),
правка на месте каскадно стирает результат: день N отдаёт эхо дню N+1, а на
следующем шаге день N+1 отдаёт своё и стирает только что полученное. На реальных
кассетах это давало 20 дней без эха и мнимое «25 → 1 предупреждение».

Гарантии
--------
* по умолчанию отчёт (без `--apply` ничего не пишется);
* перед записью кассета перевалидируется, и число предупреждений о `prev`
  обязано уменьшиться, иначе запись отменяется;
* день, у которого эхо переехало к соседу, остаётся без эха — такие дни
  перечисляются, чтобы автор дописал их сам;
* эхо последнего дня не переносится (дня N+1 не существует) и остаётся на
  месте: терять текст хуже, чем оставить автора перед очевидным предупреждением.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.story.schema import _prev_overlap, validate_payload  # noqa: E402

LIBRARY = Path(__file__).resolve().parents[1] / "app" / "story" / "cassettes"

# Пороги линтера, продублированы намеренно: скрипт должен давать тот же ответ,
# что и schema.prev_alignment_warnings, иначе «починили» бы не то.
MARGIN = 0.05
FLOOR = 0.25


def _safe_console() -> None:
    """Консоль Windows в cp1251 роняет «→» и «ё» UnicodeEncodeError'ом."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def misaligned(day: dict, prior: dict, own_cards: dict, prior_cards: dict) -> bool:
    """Эхо дня `day` привязано к своему дню, а не к предыдущему.

    Карты передаются словарями по position: в кассете карты хранятся списком,
    а эхо адресует их по position, поэтому индексация списка по порядку не годится.
    """
    best_own = 0.0
    best_prior = 0.0
    for raw_position, text in (day.get("prev") or {}).items():
        # В JSON ключи prev приходят строками ("0"), в модели pydantic — ints.
        position = int(raw_position)
        if position not in own_cards or position not in prior_cards:
            continue
        best_own = max(best_own, _prev_overlap(text, own_cards[position]["consequence"]))
        best_prior = max(
            best_prior, _prev_overlap(text, prior_cards[position]["consequence"])
        )
    return best_own > best_prior + MARGIN or best_own >= FLOOR


def repair_road(days: list[dict], road: str) -> tuple[list[dict], list[dict], list[int]]:
    """Перенести эхо сбитых дней на следующий, ничего не теряя.

    Возвращает (новые дни, отчёт по переносам, дни с неуехавшим эхом).
    """
    out = json.loads(json.dumps(days))
    by_position = [{card["position"]: card for card in day["cards"]} for day in days]

    landing: dict[int, dict] = {}
    origin: dict[int, int] = {}
    kept: list[int] = []

    for index, day in enumerate(days):
        if not day.get("prev"):
            continue
        broken = index > 0 and misaligned(
            day, days[index - 1], by_position[index], by_position[index - 1]
        )
        if broken and index + 1 >= len(days):
            kept.append(int(day["day_index"]))
        target = index if (not broken or index + 1 >= len(days)) else index + 1
        landing[target] = day["prev"]
        origin[target] = index

    moved: list[dict] = []
    for index, day in enumerate(out):
        if index in landing:
            day["prev"] = landing[index]
            if origin[index] != index:
                moved.append(
                    {
                        "road": road,
                        "from": int(days[origin[index]]["day_index"]),
                        "to": int(day["day_index"]),
                        "keys": sorted(landing[index]),
                    }
                )
        else:
            day.pop("prev", None)
    return out, moved, kept


def repair(payload: dict) -> tuple[dict, list[dict], list[int], list[int]]:
    result = json.loads(json.dumps(payload))
    moved: list[dict] = []
    kept: list[int] = []

    result["days"], main_moved, main_kept = repair_road(result["days"], "главная")
    moved.extend(main_moved)
    kept.extend(main_kept)
    for fork in result.get("switch") or []:
        fork["days"], fork_moved, fork_kept = repair_road(fork["days"], f'ветка «{fork["to"]}»')
        moved.extend(fork_moved)
        kept.extend(fork_kept)
    return result, moved, kept


def prev_warnings(payload: dict) -> list[str]:
    result = validate_payload(payload)
    if not result.ok:
        raise SystemExit(f"контракт нарушен: {result.errors}")
    return [w for w in result.warnings if "prev" in w]


def main() -> int:
    _safe_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать правку в файлы")
    args = parser.parse_args()

    touched = 0
    for path in sorted(LIBRARY.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        before = len(prev_warnings(payload))
        result, moved, kept = repair(payload)
        if not moved:
            print(f"{path.name}: сдвигать нечего (предупреждений о prev: {before})")
            continue

        after = len(prev_warnings(result))
        if after >= before:
            raise SystemExit(
                f"{path.name}: после починки предупреждений не стало меньше "
                f"({before} → {after}), запись отменена"
            )
        if args.apply:
            path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        touched += 1

        print(f"{path.name}: {'ПОЧИНЕНО' if args.apply else 'было бы починено'}")
        print(f"    предупреждений о prev: {before} → {after}")
        for item in moved:
            print(f"    эхо дня {item['from']} → день {item['to']} ({item['road']})")
        shifted_from = {item["from"] for item in moved}
        shifted_to = {item["to"] for item in moved}
        lost = sorted(shifted_from - shifted_to)
        if lost:
            print(f"    остались без эха (дописать автору): {lost}")
        if kept:
            print(f"    эхо не уехало, осталось на месте: дни {kept}")

    if touched and not args.apply:
        print("\nЭто был отчёт. Повторить с --apply, чтобы записать.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())