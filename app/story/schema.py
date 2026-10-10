"""Контракт и валидация сюжетной кассеты.

Стандарт кассеты — docs/lost_howl_prompts.md (§1 — контракт кассеты). Ядро (bay.py) ничего
не генерирует: единственный источник сюжета дня — валидный файл кассеты.

Кассета описывает ровно один календарный месяц (поле `month`: «YYYY-MM»),
а дней в файле — сколько в этом месяце по календарю (28–31): день месяца
= day_index кассеты. Поля и лимиты зеркалят то, что ест движок
(app/rounds/rendering.py::_plan_and_render + _materialize_round + модели).

Помимо главной дороги (`days`) кассета может нести перемотки (`switch`):
ветки месяца, включаемые честным победителем движка за день до развилки.
Решение принимает не кассета, а ядро — кассета только объявляет условия.

Проверка делится на жёсткую (кассета отвергнута) и мягкую (warning):
жёстко — структура, длины, позиции карт, уникальность дорог и пар
(at_day, winner) перемоток, стоп-слова; мягко — мёртвые ключи prev на входе
дороги перемотки, привязка эха prev к тому дню, которому оно принадлежит
(сдвиг на день и копипаст блока), стилевые замечания (описания одним экраном,
кадр не тянется, заголовок карты не повторяет дословно своё описание).
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# Лимиты из движка (см. docs/story_world_manifest.md, раздел «Формат полей»).
# Схема не выпускает текст, который рендер поста не сможет показать целиком:
# лимит равен обрезке в app/broadcast.py — глава до 1500 → мы жёстче 700,
# dilemma 400 == показу 400, заголовки 80 == показу 80 (ничего не режется
# многоточием); consequence 220 — итог пути звучит коротко, а не пересказом
# дня. Поля card.description больше нет (снято указом владельца): в пост дня
# оно не шло, варианты несут кнопки — названия карт.
FIELD_LIMITS = {
    "chapter_title": 80,
    "chapter_text": 700,
    "dilemma": 400,
    "card_title": 80,
    "card_consequence": 220,
    "attribution": 200,
    "track_name": 32,
    "diary": 200,
    "prev_value": 160,
}

# Максимум перемоток (развилок) в одной кассете: ветвление месяца держим
# строго «домашним» — 2–4 вилки на месяц, чтобы сюжет оставался обозримым.
MAX_FORKS = 4

# Стилевые пороги (мягкие warning'и, не ошибки). Калибр — текущая библиотека
# кассет (главы до ~550 знаков), поэтому пороги ловят ЗАМЕТНО более тяжёлый
# текст, а не нюансы «хорошего слога».
_CHAPTER_TOO_LONG = 600  # выше этого кадр тянется (жёсткий кап — 700 из FIELD_LIMITS)
_CHAPTER_TOO_SHORT = 140  # короче и в одно предложение — «заголовок», не кадр

_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+")

# Линтер эха prev (мягкие warning'и). prev — рукописный пересказ, и главная его
# беда — сдвиг на день: автор пишет «что стая вспомнит про СВОИ карты» и кладёт
# текст в prev этого же дня, а рендер показывает его в начале СЛЕДУЮЩЕГО, где
# оно читается как спойлер и разрыв преемственности. Ловим сравнением текста
# эха с последствиями: своего дня (ошибка) против предыдущего (как надо).
#
# Мера — доля общих значимых слов (Жаккар). Грубая: пересказ словами не
# повторяет, поэтому пороги выбраны по разбору действующей библиотеки так,
# чтобы ловить уверенные случаи и молчать на неоднозначных. На текущих
# кассетах: 47 дней уверенно «свой день», 0 ложных срабатываний.
_PREV_WORD_RE = re.compile(r"[а-яёa-z0-9]+")
_PREV_MIN_WORD_LEN = 4  # короче — служебные слова («стая», «путь» не отличить)
_PREV_STOPWORDS = frozenset(
    """это как все они она его её им их но а то из за у же бы вы по при для от этот эта эти
    того этом если под над без через чтобы один свой своих своей всего к уже ещё еще так вот
    там тут где когда лишь даже либо том тем нас вас ней нему них пусть будто после перед между
    через""".split()
)
_PREV_SAME_DAY_MARGIN = 0.05  # «свой день» увереннее «предыдущего» хотя бы на столько
_PREV_SAME_DAY_FLOOR = 0.25  # либо совпадение со своим днём настолько сильное, что сомнений нет


def _prev_words(text: str) -> set[str]:
    return {
        word
        for word in _PREV_WORD_RE.findall((text or "").casefold())
        if len(word) >= _PREV_MIN_WORD_LEN and word not in _PREV_STOPWORDS
    }


def _prev_overlap(left: str, right: str) -> float:
    """Доля общих значимых слов двух текстов: 0 — ничего общего, 1 — один текст."""
    a, b = _prev_words(left), _prev_words(right)
    return len(a & b) / len(a | b) if a and b else 0.0


# Стоп-слова: реальные бренды/криптобиржи/обещания дохода. Канон «Lost Dogs:
# The Way» (Догтаун, имена персонажей) ДОЗВОЛЕН: кассеты — открытый фанфик,
# его обязательное клеймо живёт в attribution. Эвристика — подстрока в нижнем
# регистре; совпадение = кассета отвергнута.
TABOO_WORDS = (
    "woof",
    "notcoin",
    "$not",
    "$bones",
    "binance",
    "bybit",
    "okx",
    "airdrop",
    "токен",
    "инвестици",
    "доход",
    "заработ",
    "прибыль",
    "памп",
    "криптобирж",
    "сиквел",
    "официальн",
)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def days_in_month(month: str) -> int:
    """Число дней календарного месяца «YYYY-MM»: 28..31 (февраль — по годам)."""
    year, month_number = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, month_number)[1]


class CardModel(BaseModel):
    """Одна карта пути (Position 0, 1 или 2) внутри дня."""

    position: int
    title: str = Field(min_length=1, max_length=FIELD_LIMITS["card_title"])
    consequence: str = Field(min_length=1, max_length=FIELD_LIMITS["card_consequence"])
    tag: str = "care"

    @field_validator("position")
    @classmethod
    def _position_in_range(cls, value: int) -> int:
        if not 0 <= value <= 2:
            raise ValueError("position должен быть 0, 1 или 2")
        return value


class DayModel(BaseModel):
    """Один день пути: станция, глава и ровно три карты голосования."""

    day_index: int = Field(ge=1)
    station: str = Field(min_length=1)
    chapter_title: str = Field(min_length=1, max_length=FIELD_LIMITS["chapter_title"])
    chapter_text: str = Field(min_length=1, max_length=FIELD_LIMITS["chapter_text"])
    dilemma: str = Field(
        min_length=1,
        max_length=FIELD_LIMITS["dilemma"],
        description="Блок «что предстоит решить»: проблема/дилемма/вопрос дня. "
        "Пост дня — эхо → сюжет → этот блок; сами варианты живут только "
        "в кнопках (подписи — названия карт), без последствий.",
    )
    cards: list[CardModel] = Field(min_length=3, max_length=3)
    prev: dict[int, str] | None = Field(
        default=None,
        description="Эхо вчерашнего выбора стаи: {позиция победителя: как стая "
        "вспомнит его последствие}. Рендерится в начале главы следующего дня.",
    )
    diary: str | None = Field(
        default=None,
        max_length=FIELD_LIMITS["diary"],
        description="Запись дневника (ПОВ-контраст к эпической главе): звучит "
        "в итогах дня после канона.",
    )

    @field_validator("prev")
    @classmethod
    def _prev_echo_sane(cls, value: dict[int, str] | None) -> dict[int, str] | None:
        if value is None:
            return value
        for position, text in value.items():
            if position not in (0, 1, 2):
                raise ValueError("ключи prev — только позиции карт 0, 1 или 2")
            if not text.strip():
                raise ValueError("текст эха prev не может быть пустым")
            if len(text) > FIELD_LIMITS["prev_value"]:
                raise ValueError(
                    f"эхо prev не длиннее {FIELD_LIMITS['prev_value']} знаков"
                )
        return value

    @model_validator(mode="after")
    def _cards_positions_complete(self) -> DayModel:
        positions = sorted(card.position for card in self.cards)
        if positions != [0, 1, 2]:
            raise ValueError("три карты дня должны занимать позиции 0, 1, 2 без дублей")
        return self


class SwitchModel(BaseModel):
    """Перемотка месяца: развилка на альтернативную дорогу.

    Решение кассета НЕ принимает сама: она спрашивает честного победителя
    движка. Если в день `at_day - 1` стая пошла картой `winner` (позиция
    0..2), то с дня `at_day` и до конца месяца играется дорога `to` —
    список дней этого ответвления (day_index ровно at_day..N). Перемотка
    возможна только по уже закрытому кадру: ветка не может появиться
    раньше, чем движок объявил победителя предыдущего дня.
    """

    to: str = Field(min_length=1, max_length=FIELD_LIMITS["track_name"])
    at_day: int = Field(ge=2)
    winner: int
    days: list[DayModel]

    @field_validator("winner")
    @classmethod
    def _winner_in_range(cls, value: int) -> int:
        if not 0 <= value <= 2:
            raise ValueError("winner должен быть 0, 1 или 2")
        return value


class Cassette(BaseModel):
    """Валидированная кассета месяца: месяц «YYYY-MM» + ровно N дней месяца.

    `days` — главная дорога (main) на весь месяц. `switch` — перемотки:
    альтернативные дороги, которые включаются, только если по итогам дня
    до развилки движок отдал нужную карту. `attribution` — клеймо плёнки
    (фанатский фанфик, не канон).
    """

    cassette_id: str = Field(min_length=1)
    month: str
    title: str = Field(min_length=1)
    logline: str | None = None
    attribution: str | None = Field(default=None, max_length=FIELD_LIMITS["attribution"])
    switch: list[SwitchModel] = Field(default_factory=list)
    days: list[DayModel]

    @field_validator("month")
    @classmethod
    def _month_is_calendar(cls, value: str) -> str:
        if not MONTH_RE.fullmatch(value):
            raise ValueError("month: ожидается «YYYY-MM»")
        year, month_number = int(value[:4]), int(value[5:7])
        try:
            datetime(year, month_number, 1)
        except ValueError:
            raise ValueError(f"несуществующий месяц: {value}") from None
        return value

    @model_validator(mode="after")
    def _days_match_month(self) -> Cassette:
        expected = days_in_month(self.month)
        indices = [day.day_index for day in self.days]
        if len(indices) != expected:
            raise ValueError(
                f"дней в кассете {len(indices)}, а в месяце {self.month} по календарю"
                f" {expected} (28..31)"
            )
        if indices != list(range(1, expected + 1)):
            raise ValueError("day_index должны идти подряд 1..N без пропусков и дублей")
        if len(self.switch) > MAX_FORKS:
            raise ValueError(
                f"перемоток {len(self.switch)}, а положено не больше {MAX_FORKS}"
            )
        roads: set[str] = {fork.to for fork in self.switch}
        if len(roads) != len(self.switch):
            raise ValueError("дороги перемоток не должны дублироваться")
        windows: set[tuple[int, int]] = set()
        for fork in self.switch:
            window = (fork.at_day, fork.winner)
            if window in windows:
                raise ValueError(
                    f"пара (at_day={fork.at_day}, winner={fork.winner}) дублируется: "
                    "вторая такая дорога в road() молча теряется — её дни не отыграются"
                )
            windows.add(window)
        for fork in self.switch:
            if fork.at_day > expected:
                raise ValueError(
                    f"перемотка «{fork.to}»: at_day {fork.at_day} за пределами месяца "
                    f"({expected} дней)"
                )
            want = list(range(fork.at_day, expected + 1))
            got = [day.day_index for day in fork.days]
            if got != want:
                raise ValueError(
                    f"перемотка «{fork.to}»: дни дороги должны идти {want[0]}..{want[-1]} "
                    "без пропусков и дублей"
                )
        return self

    def active_day(self, today: date) -> DayModel | None:
        """День главной дороги для даты, или None, если кассета молчит.

        Кассета активна, только когда месяц (YYYY-MM) кассеты == месяцу даты:
        день = день календарного месяца. За пределами своих N дней (в том
        числе на стыке месяцев) кассета молчит — движок играет шаблон.
        """
        if today.strftime("%Y-%m") != self.month:
            return None
        return self.day_for(today.day, "main")

    def road(self, today_day: int, winners: dict[int, int]) -> str:
        """Активная дорога на календарный день месяца.

        По умолчанию «main». Перемотка включается, если день до её черелка
        (at_day - 1) выигран картой `winner` — тогда с at_day дорога меняется
        на `to`. Перемотки независимы и смотрят только на честного победителя
        движка; несколько сработавших каскадятся по датам — учитывается
        последняя на этот день.
        """
        current = "main"
        for fork in sorted(self.switch, key=lambda item: item.at_day):
            if fork.at_day > today_day:
                continue
            if winners.get(fork.at_day - 1) == fork.winner:
                current = fork.to
        return current

    def day_for(self, today_day: int, road: str) -> DayModel | None:
        """День месяца на конкретной дороге, None — такой дороги/кадра нет."""
        if road == "main":
            if 1 <= today_day <= len(self.days):
                return self.days[today_day - 1]
            return None
        for fork in self.switch:
            if fork.to != road:
                continue
            offset = today_day - fork.at_day
            if 0 <= offset < len(fork.days):
                return fork.days[offset]
            return None
        return None

    def dead_prev_warnings(self) -> list[str]:
        """Мёртвые ключи эха на входе дороги перемотки (warning).

        Первый день дороги (at_day) помнит победителя дня at_day − 1, а это РОВНО
        фиксированный winner перемотки — ключи prev, не равные ему, не покажутся
        никогда: эхо рендерится только под честного победителя, которым может быть
        только этот winner. Дубликаты (at_day, winner) запрещены жёстко (см.
        _days_match_month); здесь — предупреждение о мёртвом тексте.
        """
        warnings: list[str] = []
        for fork in self.switch:
            first_day = fork.days[0] if fork.days else None
            if first_day is None or not first_day.prev:
                continue
            dead = sorted(key for key in first_day.prev if key != fork.winner)
            if dead:
                warnings.append(
                    f"перемотка «{fork.to}» (at_day={fork.at_day}): ключи prev первого "
                    f"дня {dead} — мёртвые, дорога играет только при winner={fork.winner}"
                )
        return warnings

    def prev_alignment_warnings(self) -> list[str]:
        """Эхо prev, привязанное не к тому дню (warning, не ошибка).

        Контракт `prev` жёсткий: поле дня N — «как стая вспомнит ПОБЕДИТЕЛЯ
        ДНЯ N−1», и рендерится оно в начале главы дня N (см. DayModel.prev и
        bay._prev_echo). Текст в prev дня N, пересказывающий последствия карт
        САМОГО дня N, — это эхо, поставленное на день раньше нужного: игрок
        читает последствия выбора, которого ещё не сделал, и приписанные
        вчерашнему победителю.

        Ловим две разновидности, обе — обычная ошибка при рукописном наборе
        кассеты на месяц:

        * сдвиг (основное): текст эха дня N совпадает с последствиями карт дня N
          заметнее, чем с последствиями дня N−1;
        * копипаст: эхо под ту же карту повторяет эхо более раннего дня этой же
          дороги — «забытый» текст. Ловим и целый блок, и отдельные ключи: в
          библиотеке был день, у которого повторялся только ключ "0", а ключи
          "1"/"2" были дописаны заново, и блок целиком потому не совпал.

        Проверяются ВСЕ дороги, включая главную: dead_prev_warnings смотрит
        только на вход в ветку, а сдвиг живёт именно на главной дороге, где
        дней больше всего.

        Мера лексическая и поэтому не видит слабый пересказ: день, который
        пересказывает прошлое чужими словами, может остаться непойманным.
        Линтер — не приговор, а повод автору глазами прочитать перечисленные
        дни.
        """
        warnings: list[str] = []
        roads: list[tuple[str, list[DayModel], bool]] = [("главная", self.days, False)]
        roads += [(f"ветка «{fork.to}»", fork.days, True) for fork in self.switch]
        for road, days, is_fork in roads:
            seen_blocks: dict[tuple, int] = {}
            seen_fields: dict[tuple[int, str], int] = {}
            for index, day in enumerate(days):
                block = day.prev or {}
                if not block:
                    continue

                normalized = {pos: " ".join(text.split()) for pos, text in block.items()}
                signature = tuple(sorted(normalized.items()))
                if signature in seen_blocks:
                    warnings.append(
                        f"{road} день {day.day_index}: блок prev дословно повторяет "
                        f"день {seen_blocks[signature]} — это эхо чужого дня"
                    )
                else:
                    seen_blocks[signature] = day.day_index
                for position, text in normalized.items():
                    field_key = (position, text)
                    if field_key in seen_fields:
                        warnings.append(
                            f"{road} день {day.day_index}: prev[{position}] дословно "
                            f"повторяет день {seen_fields[field_key]} — эхо под карту "
                            f"{position} не может быть одинаковым в разные дни"
                        )
                    else:
                        seen_fields[field_key] = day.day_index

                if index == 0 and not is_fork:
                    continue  # день 1: вчерашнего дня в кассете нет, эха нет и не нужно
                if index == 0:
                    # Первый день ветки помнит победителя дня at_day − 1 главной дороги.
                    prior = self.days[day.day_index - 2]
                else:
                    prior = days[index - 1]

                own_best = 0.0
                prior_best = 0.0
                for position, text in block.items():
                    if position not in (0, 1, 2):
                        continue
                    own_best = max(
                        own_best, _prev_overlap(text, day.cards[position].consequence)
                    )
                    prior_best = max(
                        prior_best, _prev_overlap(text, prior.cards[position].consequence)
                    )

                if (
                    own_best > prior_best + _PREV_SAME_DAY_MARGIN
                    or own_best >= _PREV_SAME_DAY_FLOOR
                ):
                    warnings.append(
                        f"{road} день {day.day_index}: prev пересказывает последствия "
                        f"СВОЕГО дня ({own_best:.2f} против {prior_best:.2f} за вчера) — "
                        "эхо ждёт дня на +1; сегодня игрок прочтёт последствия "
                        "несделанного выбора"
                    )
        return warnings

    def style_warnings(self) -> list[str]:
        """Стилевые замечания (warning, не ошибка): глава дня читается легко.

        * Кадр дня: глава не растянута (жёсткий кап — FIELD_LIMITS, тут мягкий
          порог) и не выглядит заголовком (короче порога одним предложением).
        """
        warnings: list[str] = []

        def _sentence_count(text: str) -> int:
            parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
            return max(1, len(parts))

        def _guard_day(day: DayModel, label: str) -> None:
            chapter = len(day.chapter_text)
            if chapter > _CHAPTER_TOO_LONG:
                warnings.append(
                    f"{label}: глава {chapter} знаков (мягкий порог "
                    f"{_CHAPTER_TOO_LONG}, жёсткий — {FIELD_LIMITS['chapter_text']}) — "
                    "кадр тянется, игрок читает пост дня целиком"
                )
            if chapter < _CHAPTER_TOO_SHORT and _sentence_count(day.chapter_text) == 1:
                warnings.append(
                    f"{label}: глава одним предложением — выглядит заголовком, "
                    "добавь 1–3 предложения обстановки, чтобы кадр заработал"
                )

        for day in self.days:
            _guard_day(day, f"день {day.day_index}")
        for fork in self.switch:
            for day in fork.days:
                _guard_day(day, f"перемотка «{fork.to}», день {day.day_index}")
        return warnings


@dataclass
class ValidationResult:
    """Итог проверки кассеты: валидна ли, жёсткие ошибки и мягкие замечания."""

    cassette: Cassette | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.cassette is not None


def _taboo_hits(cassette: Cassette) -> list[str]:
    found: set[str] = set()
    all_days = list(cassette.days)
    for fork in cassette.switch:
        all_days.extend(fork.days)
    for day in all_days:
        parts = [day.chapter_title, day.chapter_text, day.station]
        if day.dilemma:
            parts.append(day.dilemma)
        for card in day.cards:
            parts.extend((card.title, card.consequence))
        haystack = " ".join(parts).lower()
        for word in TABOO_WORDS:
            if word.lower() in haystack:
                found.add(word)
    return sorted(found)


def validate_payload(payload: dict) -> ValidationResult:
    """Проверяет словарь кассеты (из JSON) против контракта.

    Успех → cassette заполнен; иначе ошибки в errors. Мягкие замечания
    (привязка эха, стиль) всегда в warnings.
    """
    warnings: list[str] = []
    try:
        cassette = Cassette.model_validate(payload)
    except ValidationError as exc:
        rows: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            rows.append(f"{location}: {error['msg']}")
        return ValidationResult(cassette=None, errors=rows)
    taboo = _taboo_hits(cassette)
    if taboo:
        return ValidationResult(
            cassette=None,
            errors=[f"стоп-слова: {', '.join(taboo)}"],
        )
    warnings.extend(cassette.dead_prev_warnings())
    warnings.extend(cassette.prev_alignment_warnings())
    warnings.extend(cassette.style_warnings())
    if not (cassette.attribution or "").strip():
        warnings.append(
            "attribution не указано — клеймо плёнки-фанфика («по мотивам …») желательно"
        )
    return ValidationResult(cassette=cassette, errors=[], warnings=warnings)


def validate_file(path: str | Path) -> ValidationResult:
    """Читает и валидирует файл кассеты (*.json, UTF-8, допускается BOM)."""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return ValidationResult(cassette=None, errors=[f"не прочитать файл: {exc}"])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ValidationResult(cassette=None, errors=[f"не JSON: {exc}"])
    if not isinstance(payload, dict):
        return ValidationResult(cassette=None, errors=["корень кассеты — объект JSON"])
    return validate_payload(payload)