from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings

_ROMAN = ("I", "II", "III")


def _now() -> datetime:
    return datetime.now(UTC)


def utc_aware(value: datetime) -> datetime:
    """Гарантирует tzinfo=UTC у даты из БД.

    Postgres с TIMESTAMPTZ возвращает aware-даты, но SQLite игнорирует
    timezone=True и отдаёт наивные значения — без нормализации любое
    сравнение «дата из базы против _now()» падает на локальных прогонах.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _next_hour_slot(after: datetime, hour: int) -> datetime:
    """Ближайший момент «after-дня или позже» ровно в hour:00 UTC."""
    hour %= 24
    candidate = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def catchup_cutoff() -> datetime:
    """Граница «догоняем только недавние дни».

    Восстановители по маркерам (awards_at / results_at / announced_at /
    payouts_finalized) ищут дни с NULL-маркером. Такой отбор без границы
    после миграции, добавившей колонку без бэкфилла, находит ВСЮ историю
    закрытых дней: первый же тик новой версии заново рассылает их итоги,
    начисляет очки и пересоздаёт выплаты. Прошёл день — его маркер «не
    обработан» больше не значит «нужно догнать».

    Догон существует ради крашей и рестартов (час-дни), поэтому ловим дни,
    чья граница (закрытие/открытие) попала в последние catchup_window_hours.
    """
    return _now() - timedelta(hours=settings.catchup_window_hours)


def announce_claim_cutoff() -> datetime:
    """Граница «claim анонса брошен»: метка старше окна — можно перехватить.

    claim_announcement коммитит announced_at ДО рассылки. Исключение снимает
    метку (unclaim), но жёсткий краш — kill -9, деплой, пропажа инстанса —
    нет: тик догона ищет только announced_at IS NULL, и день остался бы без
    поста навсегда. Окно обязано быть заведомо больше самой долгой живой
    рассылки (см. announce_claim_timeout_seconds): иначе догон заберёт claim
    у работающего вещателя и день уйдёт в чаты дважды.
    """
    return _now() - timedelta(seconds=settings.announce_claim_timeout_seconds)


def results_claim_cutoff() -> datetime:
    """Граница «claim рассылки итогов брошен»: метка старше — можно перехватить.

    _claim_results коммитит results_claimed_at ДО бродкаста. Исключение снимает
    метку (unclaim), но жёсткий краш — kill -9, деплой, пропажа инстанса — нет:
    восстановитель ищет дни без results_at, и без перехвата день остался бы без
    итогов навсегда. Окно обязано быть заведомо больше самой долгой живой
    рассылки (см. results_claim_timeout_seconds): иначе догон заберёт claim у
    работающего вещателя и итоги уйдут в чаты дважды.
    """
    return _now() - timedelta(seconds=settings.results_claim_timeout_seconds)


def _day_window(opens_at: datetime) -> tuple[datetime, datetime]:
    """Границы голосования и подсчёта дня на сетке UTC.

    Голосование закрывается ровно в day_close_hour_utc — сразу после
    закрытия следуют мгновенный подсчёт и итоги. Первый день стартует сразу
    после создания/сброса, дальше дни идут строго по сетке 11:00 UTC даже
    после простоя бота.
    """
    # Бесшовный день: голосование закрывается в DAY_CLOSE_HOUR_UTC,
    # подсчёт и итоги — сразу (секунды), новый день открывается следом
    # из заготовки. TALLYING как час простоя больше не существует;
    # поле tally_ends_at сохранено для совместимости схемы.
    voting_ends_at = _next_hour_slot(opens_at, settings.day_close_hour_utc)
    if voting_ends_at - opens_at < timedelta(hours=6):
        # Открылись слишком близко к границе — короткий день никому не нужен,
        # переносим закрытие на следующие сутки (сетка сохраняется).
        voting_ends_at += timedelta(days=1)
    return voting_ends_at, voting_ends_at