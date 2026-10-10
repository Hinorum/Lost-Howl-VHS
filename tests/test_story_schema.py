"""Контракт сюжетной кассеты: месяцы 28–31 день, структура, лимиты, табу.

Кассета обязана описывать ровно один календарный месяц («YYYY-MM») и содержать
ровно столько дней, сколько в этом месяце по календарю. Жёсткие ошибки
отвергают кассету; привязка эха prev и стиль — мягкие замечания.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.story.schema import (
    FIELD_LIMITS,
    ValidationResult,
    validate_file,
    validate_payload,
)


def _day(index: int, **overrides) -> dict:
    data = {
        "day_index": index,
        "station": f"Станция {index}",
        "chapter_title": f"Глава {index}",
        "chapter_text": "Стая собирается у котла. Огонь лижет бак, дождь трогает крыши. Пути ждут до рассвета.",
        "dilemma": "Куда пойдёт стая на рассвете?",
        "cards": [
            {
                "position": 0,
                "title": f"Путь А {index}",
                "consequence": "Стая пошла путём А и нашла свет.",
                "tag": "care",
            },
            {
                "position": 1,
                "title": f"Путь Б {index}",
                "consequence": "Стая ушла путём Б и нашла тень.",
                "tag": "care",
            },
            {
                "position": 2,
                "title": f"Путь В {index}",
                "consequence": "Стая осталась и дождалась утра.",
                "tag": "care",
            },
        ],
    }
    data.update(overrides)
    return data


def _payload(month: str, n_days: int, **overrides) -> dict:
    payload = {
        "cassette_id": "test-kasseta",
        "month": month,
        "title": "Тестовая кассета",
        "logline": "проверка контракта.",
        "days": [_day(i) for i in range(1, n_days + 1)],
    }
    payload.update(overrides)
    return payload


def test_month_lengths_28_to_31() -> None:
    # Февраль 2027 — 28 дней, февраль 2024 (високосный) — 29, апрель — 30, январь — 31.
    for month, n in (
        ("2027-02", 28),
        ("2024-02", 29),
        ("2026-04", 30),
        ("2026-01", 31),
    ):
        result = validate_payload(_payload(month, n))
        assert result.ok, result.errors
        assert result.cassette is not None
        assert len(result.cassette.days) == n


def test_wrong_days_count_for_month_rejected() -> None:
    result = validate_payload(_payload("2027-02", 30))
    assert not result.ok
    assert any("2027-02" in error for error in result.errors)


def test_duplicate_day_index_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][4]["day_index"] = 1
    result = validate_payload(payload)
    assert not result.ok


def test_missing_day_rejected() -> None:
    result = validate_payload(_payload("2026-01", 29))  # в январе 31 день
    assert not result.ok


def test_bad_cards_positions_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][1]["position"] = 0
    result = validate_payload(payload)
    assert not result.ok
    assert any("позиции 0, 1, 2" in error for error in result.errors)


def test_bad_month_format_rejected() -> None:
    assert not validate_payload(_payload("2026-13", 30)).ok
    assert not validate_payload(_payload("okt-2026", 30)).ok
    assert not validate_payload(_payload("", 30)).ok


def test_field_length_limits_rejected() -> None:
    too_long = "а" * (FIELD_LIMITS["chapter_title"] + 1)
    result = validate_payload(
        _payload("2026-04", 30, days=[_day(1, chapter_title=too_long)])
    )
    assert not result.ok

    too_long_chapter = "а" * (FIELD_LIMITS["chapter_text"] + 1)
    result = validate_payload(
        _payload("2026-04", 30, days=[_day(1, chapter_text=too_long_chapter)])
    )
    assert not result.ok

    too_long_card = "а" * (FIELD_LIMITS["card_title"] + 1)
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][0]["title"] = too_long_card
    assert not validate_payload(payload).ok

    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][0]["consequence"] = "с" * (FIELD_LIMITS["card_consequence"] + 1)
    assert not validate_payload(payload).ok


def test_dilemma_required_and_limited() -> None:
    """Блок дилеммы обязателен (одна структура дня для всех кассет) и не
    длиннее лимита: пост обязан показывать его целиком."""
    payload = _payload("2026-04", 30)
    del payload["days"][0]["dilemma"]
    assert not validate_payload(payload).ok

    payload = _payload("2026-04", 30)
    payload["days"][0]["dilemma"] = "Куда пойдёт стая: к воде или за топливом?"
    assert validate_payload(payload).ok

    payload["days"][0]["dilemma"] = "д" * (FIELD_LIMITS["dilemma"] + 1)
    assert not validate_payload(payload).ok


def test_empty_card_fields_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["cards"][0]["title"] = ""
    assert not validate_payload(payload).ok


def test_taboo_word_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["chapter_text"] = "Наши граммы дают хороший доход стае."
    result = validate_payload(payload)
    assert not result.ok
    assert any("стоп-слова" in error for error in result.errors)


def test_missing_attribution_is_soft_warning() -> None:
    """Отсутствие клейма (attribution) — warning, а не ошибка (см. промпт §5)."""
    result = validate_payload(_payload("2026-04", 30))
    assert result.ok
    assert result.cassette is not None
    assert not (result.cassette.attribution or "").strip()
    assert any("attribution" in warning for warning in result.warnings)


def test_field_limits_tighten_to_display() -> None:
    """Капы схемы = потолкам показа в app/broadcast.py: пост дня не режется «…»."""
    assert FIELD_LIMITS["chapter_title"] == 80
    assert FIELD_LIMITS["card_title"] == 80
    assert FIELD_LIMITS["card_consequence"] == 220


def test_style_chapter_long_warns() -> None:
    """Глава заметно выше канона (мягкий порог) — кадр тянется."""
    payload = _payload("2026-04", 30)
    payload["days"][0]["chapter_text"] = "а" * 605
    result = validate_payload(payload)
    assert result.ok
    assert any("мягкий порог" in warning for warning in result.warnings)


def test_style_chapter_single_sentence_warns() -> None:
    """Глава одним предложением выглядит заголовком, а не кадром дня."""
    payload = _payload("2026-04", 30)
    payload["days"][0]["chapter_text"] = "Стая молчит у котла."
    result = validate_payload(payload)
    assert result.ok
    assert any("одним предложением" in warning for warning in result.warnings)


def test_style_clean_payload_no_style_warnings() -> None:
    """Свежий день без перегибов не тянет стилевых замечаний."""
    result = validate_payload(_payload("2026-04", 30))
    assert result.ok
    assert not any(
        word in warning
        for word in ("мягкий порог", "одним предложением")
        for warning in result.warnings
    )


def test_active_day_matches_calendar_day() -> None:
    result = validate_payload(_payload("2026-10", 31))
    assert result.cassette is not None
    day = result.cassette.active_day(date(2026, 10, 15))
    assert day is not None and day.day_index == 15
    # Последний день месяца играется (октябрь — 31 день, на 31-е числа есть день).
    assert result.cassette.active_day(date(2026, 10, 31)).day_index == 31
    # Другой месяц — кассета не играется (стоп на стыке).
    assert result.cassette.active_day(date(2026, 9, 15)) is None


def test_active_day_keeps_full_scene_text() -> None:
    """Глава кассеты — текст сцены (не крючок): сохраняется как есть, целиком."""
    result = validate_payload(_payload("2026-10", 31))
    day = result.cassette.active_day(date(2026, 10, 1))
    assert day is not None
    assert day.chapter_text == _day(1)["chapter_text"]


def test_validate_file_reads_and_validates(tmp_path) -> None:
    cassette = "cassettes"
    good_dir = tmp_path / cassette
    good_dir.mkdir()
    good = good_dir / "ok.json"
    good.write_text(
        json.dumps(_payload("2026-04", 30), ensure_ascii=False), encoding="utf-8"
    )
    result: ValidationResult = validate_file(good)
    assert result.ok

    broken = good_dir / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    result = validate_file(broken)
    assert not result.ok
    assert any("не JSON" in error for error in result.errors)

    missing = good_dir / "nope.json"
    result = validate_file(missing)
    assert not result.ok
    assert any("не прочитать" in error for error in result.errors)


def test_bom_is_tolerated(tmp_path) -> None:
    raw = json.dumps(_payload("2026-04", 30), ensure_ascii=False).encode("utf-8")
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    assert validate_file(path).ok


def test_prev_echo_and_diary_accepted() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][1]["prev"] = {
        0: "Вчера стая пошла на свет.",
        2: "Вчера стая ждала утра.",
    }
    payload["days"][1]["diary"] = "Щенок записал: мама почти выздоровела."
    result = validate_payload(payload)
    assert result.ok, result.errors
    assert result.cassette is not None
    assert result.cassette.days[1].diary
    assert result.cassette.days[1].prev == {0: "Вчера стая пошла на свет.", 2: "Вчера стая ждала утра."}


def test_prev_bad_keys_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][1]["prev"] = {0: "текст", 5: "текст"}
    result = validate_payload(payload)
    assert not result.ok
    assert any("ключи prev" in error for error in result.errors)


def test_prev_empty_value_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][1]["prev"] = {0: "   "}
    result = validate_payload(payload)
    assert not result.ok


def test_prev_value_too_long_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][1]["prev"] = {0: "а" * (FIELD_LIMITS["prev_value"] + 1)}
    result = validate_payload(payload)
    assert not result.ok


def test_diary_too_long_rejected() -> None:
    payload = _payload("2026-04", 30)
    payload["days"][0]["diary"] = "д" * (FIELD_LIMITS["diary"] + 1)
    result = validate_payload(payload)
    assert not result.ok


def _fork_payload() -> dict:
    payload = _payload("2026-11", 30)
    branches = []
    for name, winner in (("b", 1), ("c", 2)):
        days = [_day(i) for i in range(28, 31)]
        days[0]["prev"] = {winner: "Вчера стая выбрала свой путь."}
        branches.append({"to": name, "at_day": 28, "winner": winner, "days": days})
    payload["switch"] = branches
    return payload


def test_fork_duplicate_at_day_winner_rejected() -> None:
    """Пара (at_day, winner) должна быть уникальна: вторая дорога в road() теряется."""
    payload = _fork_payload()
    payload["switch"].append(
        {"to": "b2", "at_day": 28, "winner": 1, "days": [_day(i) for i in range(28, 31)]}
    )
    result = validate_payload(payload)
    assert not result.ok
    assert any("at_day=28" in error and "winner=1" in error for error in result.errors)


def test_fork_dead_prev_key_on_entrance_warns() -> None:
    """Первый день дороги помнит ТОЛЬКО своего победителя: чужие ключи мёртвые."""
    payload = _fork_payload()
    payload["switch"][0]["days"][0]["prev"] = {
        0: "Вчера стая шла на свет.",
        1: "Вчера стая выбрала свой путь.",
        2: "Вчера стая ждала утра.",
    }
    result = validate_payload(payload)
    assert result.ok
    assert any("мёртвые" in warning for warning in result.warnings)


def test_fork_only_own_prev_key_on_entrance_is_clean() -> None:
    payload = _fork_payload()
    result = validate_payload(payload)
    assert result.ok
    assert not any("мёртвые" in warning for warning in result.warnings)


# --- Линтер привязки эха prev к своему дню ---------------------------------
#
# prev дня N — это «как стая вспомнит победителя дня N−1», и рендерится оно в
# начале дня N. Самая частая ошибка при наборе кассеты на месяц — написать в
# prev дня N пересказ последствий карт САМОГО дня N. Игрок тогда читает в
# начале дня последствия выбора, которого ещё не сделал. Линтер ловит это
# сравнением текста эха с последствиями своего и предыдущего дня.

_ЭХО_ДНЯ1 = "Котельная полыхала всю ночь, и к утру от запаса осталось два рассказа."
_ЭХО_ДНЯ2 = "Шиповник вымостил тропу сухими ветками, и дорога домой стала ровной."


def _payload_with_echoes() -> dict:
    """Кассета, где у дней 1 и 2 РАЗНЫЕ последствия (иначе сравнение бессмысленно).

    День 1 — котельная/лампа/огонь, день 2 — шиповник/овраг/лёд, дальше фоновое
    _day(). Поэтому «эхо про котельную» и «эхо про шиповник» различимы.
    """
    payload = _payload("2026-01", 31)
    by_day = {
        1: [
            "Котельная полыхала всю ночь, и к утру от запаса осталось два рассказа.",
            "Лампа вспыхнула на два часа и погасла, будто её обманули.",
            "Стая легла спать у настоящего огня, а Карандаш считал голоса вслух.",
        ],
        2: [
            "Шиповник вымостил тропу сухими ветками, и дорога домой стала ровной.",
            "Обогнули овраг по верху, и голос остался подо льдом.",
            "Сошли по льду, и ночью из трещины пила вся стая.",
        ],
    }
    for day_index, texts in by_day.items():
        cards = payload["days"][day_index - 1]["cards"]
        for card, text in zip(cards, texts, strict=True):
            card["consequence"] = text
    return payload


def test_prev_echo_of_yesterday_is_clean() -> None:
    """Эхо дня N про день N−1 — норма: никакого предупреждения."""
    payload = _payload_with_echoes()
    payload["days"][1]["prev"] = {0: _ЭХО_ДНЯ1}  # день 2 помнит котельную (день 1)
    result = validate_payload(payload)
    assert result.ok
    assert not any("СВОЕГО" in warning and "день 2" in warning for warning in result.warnings)


def test_prev_echo_of_own_day_is_flagged() -> None:
    """Эхо дня N про день N — сдвиг на +1: это и есть баг кассеты."""
    payload = _payload_with_echoes()
    payload["days"][1]["prev"] = {0: _ЭХО_ДНЯ2}  # день 2 «помнит» шиповник (свой день)
    result = validate_payload(payload)
    assert result.ok, result.errors  # мягкое замечание, не отказ
    hits = [w for w in result.warnings if "СВОЕГО" in w and "день 2" in w]
    assert hits, result.warnings
    assert "ждёт дня на +1" in hits[0]


def test_own_day_echo_is_found_in_a_long_cassette() -> None:
    """Сдвиг ловится и в полной кассете: сравнение только с соседним днём."""
    payload = _payload_with_echoes()
    payload["days"][1]["prev"] = {0: _ЭХО_ДНЯ2}
    result = validate_payload(payload)
    assert result.ok
    flagged = [w for w in result.warnings if "СВОЕГО" in w and "день 2" in w]
    assert flagged, result.warnings
    # и остальной месяц линтер не сочтит сдвигом
    assert len(flagged) == 1, flagged


def test_repeated_prev_block_is_flagged() -> None:
    """Дословно повторённый блок prev — забытое эхо другого дня."""
    payload = _payload_with_echoes()
    block = {0: _ЭХО_ДНЯ1, 1: "Лампа погасла, и стая осталась совсем без огня."}
    payload["days"][2]["prev"] = dict(block)
    payload["days"][8]["prev"] = dict(block)
    result = validate_payload(payload)
    assert result.ok
    assert any("блок prev дословно повторяет день 3" in w for w in result.warnings), (
        result.warnings
    )


def test_repeated_single_prev_key_is_flagged() -> None:
    """Повтор одного ключа при других новых — тоже забытое эхо (так был день 19)."""
    payload = _payload_with_echoes()
    payload["days"][2]["prev"] = {0: _ЭХО_ДНЯ1}
    payload["days"][8]["prev"] = {0: _ЭХО_ДНЯ1, 1: "Совсем другой текст для ключа один."}
    result = validate_payload(payload)
    assert result.ok
    assert any("prev[0] дословно повторяет день 3" in w for w in result.warnings), (
        result.warnings
    )


def test_prev_of_first_day_is_not_judged() -> None:
    """У первого дня нет вчерашнего: эхо не судим (иначе вечное ложное срабатывание)."""
    payload = _payload_with_echoes()
    payload["days"][0]["prev"] = {0: _ЭХО_ДНЯ2}
    result = validate_payload(payload)
    assert result.ok
    assert not any("СВОЕГО" in w and "день 1" in w for w in result.warnings), result.warnings


def test_fork_first_day_echo_compared_against_main_road() -> None:
    """Первый день ветки помнит день at_day−1 ГЛАВНОЙ дороги, а не свой."""
    payload = _payload("2026-01", 31)
    main_text = "Главная дорога ушла в тупик, и стая вернулась с подарком."
    for card in payload["days"][26]["cards"]:  # день 27 — накануне развилки
        card["consequence"] = main_text
    fork_text = "Ветка ушла в тупик, и стая засветила фонарь на всех."
    payload["switch"] = [
        {"to": "b", "at_day": 28, "winner": 1, "days": [_day(i) for i in range(28, 32)]}
    ]
    for card in payload["switch"][0]["days"][0]["cards"]:
        card["consequence"] = fork_text
    payload["switch"][0]["days"][0]["prev"] = {1: "Главная дорога ушла в тупик."}
    result = validate_payload(payload)
    assert result.ok
    assert not any("СВОЕГО" in w and "день 28" in w for w in result.warnings), result.warnings


def test_fork_day_echoing_its_own_day_is_flagged() -> None:
    """Сдвиг ловится и внутри ветки — там дней меньше, но игрок видит то же эхо."""
    payload = _payload("2026-01", 31)
    payload["switch"] = [
        {"to": "b", "at_day": 28, "winner": 1, "days": [_day(i) for i in range(28, 32)]}
    ]
    fork_days = payload["switch"][0]["days"]
    own_text = "Ветка обогнула овраг по верху, и голос остался подо льдом."
    for card in fork_days[1]["cards"]:  # день 29
        card["consequence"] = own_text
    fork_days[1]["prev"] = {0: "Ветка обогнула овраг по верху, и голос остался подо льдом."}
    result = validate_payload(payload)
    assert result.ok, result.errors
    assert any("СВОЕГО" in w and "день 29" in w for w in result.warnings), result.warnings


def test_real_cassettes_have_no_hard_errors() -> None:
    """Вся библиотека проходит жёсткий контракт (мягкие замечания — не ошибки)."""
    library = Path(__file__).resolve().parents[1] / "app" / "story" / "cassettes"
    files = sorted(library.glob("*.json"))
    assert files, "библиотека кассет пуста"
    for path in files:
        result = validate_file(path)
        assert result.ok, f"{path.name}: {result.errors}"
