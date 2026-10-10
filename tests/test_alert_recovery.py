"""Восстановление и сводка: чем закончилась аномалия и как долго живёт.

Раньше тревога была только входом события: ушла аномалия — тишина. Хранитель
не знал, что починилось само, и продолжал чинить то, что уже работает; а
затянувшееся состояние приходило повтором раз в час с меняющимся числом минут
вместо честного «держится второй час». Здесь — оба сигнала.

Проверки идут напрямую через слой учёта (`_raise` + `_settle_alerts`): сеть,
TON и разбор реальных записей тут ни при чём, а снимок тревог в БД общий для
всех проверок сразу.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app import ops
from app.core.registry import (
    ALERT_QUEUE_KEY,
    ALERT_TICK_KEY,
    OPS_ALERT_DIGEST_KEY,
    OPS_ALERT_TRACK_KEY,
    OPS_PROBLEMS_KEY,
)
from app.db import SessionLocal
from app.models import Chat, Round, RoundStatus, WatcherState, WinRule


async def _closed_round(day_index: int) -> None:
    """Закрытый день, чьи итоги уже помечены отправленными (results_at)."""
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await db.execute(delete(Round).where(Round.day_index == day_index))
        await db.commit()
        db.add(
            Round(
                day_index=day_index,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title=f"День {day_index}",
                chapter_text="Сцена.",
                opens_at=now - timedelta(hours=30),
                voting_ends_at=now - timedelta(hours=20),
                tally_ends_at=now - timedelta(hours=19),
                results_at=now - timedelta(hours=19),
            )
        )
        await db.commit()


async def _drop_round(day_index: int) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Round).where(Round.day_index == day_index))
        await db.commit()


class _Bot:

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
async def _clean(monkeypatch):
    """Чистый БД и никакого TON: проверки должны быть про тревоги, а не про сеть."""
    monkeypatch.setattr(ops.settings, "ton_enabled", False)
    # Иначе notify_admins выйдет на «админов нет» и тревоги некуда слать.
    monkeypatch.setattr(ops.settings, "admin_ids", "42")
    ops._problems_in_flight.clear()
    ops._problem_entry.clear()
    async with SessionLocal() as db:
        await db.execute(WatcherState.__table__.delete())
        await db.commit()
    yield _Bot()
    ops._problems_in_flight.clear()
    ops._problem_entry.clear()


async def _sweep(bot, problems: list[str]) -> list[str]:
    """Один проход учёта: тревоги уже отправлены ветками проверок, здесь
    закрытие прохода — кто ушёл, кто держится."""
    async with SessionLocal() as session:
        await ops._settle_alerts(session, bot, problems)
    return list(getattr(bot, "sent", []))


async def _raise(bot, alert_key: str, problem: str) -> None:
    """Тревога по одной категории: сообщение уходит, если кулдаун истёк."""
    async with SessionLocal() as session:
        await ops._raise(session, bot, alert_key, problem, f"⚠️ {problem}")


async def _track() -> dict:
    async with SessionLocal() as session:
        return ops._load_track(await ops._get_state(session, OPS_ALERT_TRACK_KEY))


async def _state(key: str) -> str | None:
    async with SessionLocal() as db:
        row = await db.get(WatcherState, key)
        return row.value if row is not None else None


# ---------- восстановление ----------


async def test_resolved_problem_announces_recovery(_clean) -> None:
    """Аномалия пережила вторую проверку и ушла: молчание здесь означало бы,
    что хранитель так и не узнал, где чинить."""
    await _sweep(None, ["очередь выплат стоит 31 мин"])
    assert await _sweep(None, ["очередь выплат стоит 31 мин"]) == []
    sent = await _sweep(_clean, [])
    assert len(sent) == 1
    assert "ушла сама" in sent[0]
    assert "очередь выплат стоит 31 мин" in sent[0]


async def test_recovery_does_not_fire_twice(_clean) -> None:
    await _sweep(None, ["watcher молчит 40 мин"])
    await _sweep(None, ["watcher молчит 40 мин"])
    first = _Bot()
    assert len(await _sweep(first, [])) == 1
    second = _Bot()
    assert await _sweep(second, []) == []


async def test_renaming_text_is_the_same_problem(_clean) -> None:
    """«стоит 31 мин» и «стоит 32 мин» — одна и та же аномалия: без устойчивого
    ключа она мигала бы между проходами и никогда не отрапортовала о починке."""
    await _sweep(None, ["очередь выплат стоит 31 мин"])
    assert await _sweep(_clean, ["очередь выплат стоит 32 мин"]) == []
    track = await _track()
    (entry,) = track.values()
    assert entry["seen"] == 2
    # Свежий текст полезнее допотопного: в нём актуальное число минут.
    assert entry["text"] == "очередь выплат стоит 32 мин"


async def test_relapse_alerts_immediately_again(_clean) -> None:
    """Починили и сломали снова. Если не сбросить кулдаун категории, новое
    событие проглотит молчание до конца часа."""
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    async with SessionLocal() as db:
        db.add(WatcherState(key=ALERT_TICK_KEY, value=old))
        await db.commit()

    first = _Bot()
    await _raise(first, ALERT_TICK_KEY, "планировщик не тикает 31 мин")
    assert len(first.sent) == 1
    await _sweep(None, ["планировщик не тикает 31 мин"])
    assert (await _track())["планировщик не тикает # мин"]["alert"] == ALERT_TICK_KEY

    # Пока проблема держится, повтор молчит (кулдаун свежий).
    repeat = _Bot()
    await _raise(repeat, ALERT_TICK_KEY, "планировщик не тикает 32 мин")
    assert repeat.sent == []

    # Ушла — кулдаун её категории сброшен, и возвращение сразу слышно.
    await _sweep(None, ["планировщик не тикает 32 мин"])
    await _sweep(None, [])
    assert await _state(ALERT_TICK_KEY) is None
    again = _Bot()
    await _raise(again, ALERT_TICK_KEY, "планировщик не тикает 33 мин")
    assert len(again.sent) == 1


async def test_one_off_blip_is_not_reported_as_recovery(_clean) -> None:
    """Проблема, замеченная один раз и исчезнувшая до следующего прохода, — это
    всплеск, а не «починилось»: сообщение было бы шумом в личке."""
    await _sweep(None, ["ставок не обработано: 3"])
    assert await _sweep(_clean, []) == []


# ---------- сводка ----------


async def test_digest_reports_how_long_problem_holds(_clean) -> None:
    """Первое сообщение — «что-то сломалось». Следующий проход обязан сказать,
    что это не минутный всплеск и сколько проблема уже длится."""
    first = _Bot()
    await _raise(first, ALERT_QUEUE_KEY, "очередь выплат стоит 31 мин")
    assert first.sent == ["⚠️ очередь выплат стоит 31 мин"]
    assert await _sweep(None, ["очередь выплат стоит 31 мин"]) == []

    sent = await _sweep(_clean, ["очередь выплат стоит 32 мин"])
    assert len(sent) == 1
    assert "держится" in sent[0]
    assert "очередь выплат стоит 32 мин" in sent[0]
    assert await _state(OPS_ALERT_DIGEST_KEY) is not None


async def test_digest_is_hourly_not_per_sweep(_clean) -> None:
    await _sweep(None, ["очередь выплат стоит 31 мин"])
    await _sweep(_clean, ["очередь выплат стоит 32 мин"])
    for minutes in (33, 34, 35):
        assert await _sweep(_clean, [f"очередь выплат стоит {minutes} мин"]) == []


async def test_digest_replaces_per_category_repeats(_clean) -> None:
    """Три аномалии по часу — одно сообщение вместо трёх одинаковых."""
    problems = ["проблема аaa", "проблема bbb", "проблема ccc"]
    first = _Bot()
    for index, problem in enumerate(problems):
        await _raise(first, f"alert_test_{index}", problem)
    assert first.sent == [f"⚠️ {problem}" for problem in problems]
    assert await _sweep(None, problems) == []

    # Час спустя кулдауны истекли, но сводка уже отчиталась: молчим, чтобы не
    # сыпать тем же текстом снова, — вместо этого приходит один дайджест.
    later = _Bot()
    for index, problem in enumerate(problems):
        await _raise(later, f"alert_test_{index}", problem)
    assert later.sent == []
    sent = await _sweep(later, problems)
    assert len(sent) == 1
    assert all(problem in sent[0] for problem in problems)


# ---------- живучесть данных ----------


async def test_garbage_track_starts_from_clean_slate(_clean) -> None:
    async with SessionLocal() as db:
        db.add(WatcherState(key=OPS_ALERT_TRACK_KEY, value="не json"))
        await db.commit()
    await _sweep(None, ["очередь выплат стоит 31 мин"])
    (entry,) = (await _track()).values()
    assert entry["seen"] == 1


async def test_empty_corrupt_track_is_not_a_recovery(_clean) -> None:
    """Битая карта не должна выглядеть как «всё починилось» и устроить поток
    «✅ ушла сама» на каждую проблему."""
    async with SessionLocal() as db:
        db.add(WatcherState(key=OPS_ALERT_TRACK_KEY, value=json.dumps({"a": 1})))
        await db.commit()
    assert await _sweep(_clean, []) == []


async def test_check_anomalies_does_not_carry_verdict_between_sweeps(_clean) -> None:
    """Список проблем общий с ветками проверок и чистится в начале прохода:
    прошлый вердикт не должен переезжать в новый, а упавшая середина
    проверки — в следующий."""
    await _sweep(None, ["очередь выплат стоит 31 мин"])
    first = await ops.check_anomalies(None)
    second = await ops.check_anomalies(None)
    assert first is not second
    assert "очередь выплат стоит 31 мин" not in second
    assert await _state(OPS_PROBLEMS_KEY) is not None


async def test_record_delivery_writes_marker() -> None:
    """Метка доставки — это «доставлено/попыток», а не «рассылка прошла»."""
    from app.broadcast import record_delivery

    async with SessionLocal() as db:
        await db.execute(delete(WatcherState).where(WatcherState.key == "delivery:day:7"))
        await db.commit()
    try:
        await record_delivery("day:7", 3, 5)
        assert await _state("delivery:day:7") == "3/5"
        # Повтор перезаписывает, а не плодит строки.
        await record_delivery("day:7", 5, 5)
        assert await _state("delivery:day:7") == "5/5"
        # Пустой проход метку не оставляет: «0 из 0» — это не доставка.
        await record_delivery("day:8", 0, 0)
        assert await _state("delivery:day:8") is None
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(
                    WatcherState.key.in_(["delivery:day:7", "delivery:day:8"])
                )
            )
            await db.commit()


async def test_zero_delivery_raises_alarm(_clean) -> None:
    """Рассылка, не дошедшая ни до кого, — тревога.

    Раньше день, ушедший пустым, был неотличим от доставленного всем: успешный
    проход рассылки и доставка игроку — разные утверждения, и второе не проверял
    никто. Тик при этом оставался живым, так что /health говорил «всё хорошо».
    """
    async with SessionLocal() as db:
        db.add(WatcherState(key="delivery:day:9", value="0/4"))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert any("ни одно сообщение не доставлено" in p for p in problems), problems
        assert any("day:9" in p for p in problems), problems
        assert _clean.sent, "тревога должна уйти админу"
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(WatcherState.key == "delivery:day:9")
            )
            await db.commit()


async def test_partial_delivery_is_not_an_alarm(_clean) -> None:
    """Частичная потеря — норма, а не авария: заблокировавшие бота есть всегда.

    Порог по доле кричал бы постоянно и приучил бы игнорировать тревоги.
    """
    async with SessionLocal() as db:
        db.add(WatcherState(key="delivery:day:10", value="3/4"))
        db.add(WatcherState(key="delivery:day:11", value="1/9"))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert not any("не доставлено" in p for p in problems), problems
        assert not any("Рассылка не дошла" in text for text in _clean.sent), _clean.sent
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(
                    WatcherState.key.in_(["delivery:day:10", "delivery:day:11"])
                )
            )
            await db.commit()


async def test_unparsable_delivery_marker_is_ignored(_clean) -> None:
    """Битая метка не должна ронять проверку и не должна выдумывать аварию."""
    async with SessionLocal() as db:
        db.add(WatcherState(key="delivery:day:12", value="мусор"))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert not any("не доставлено" in p for p in problems), problems
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(WatcherState.key == "delivery:day:12")
            )
            await db.commit()


async def test_record_delivery_keeps_reason_only_while_zero() -> None:
    """Причина нулевой доставки живёт рядом с меткой и гаснет вместе с нулём."""
    from app.broadcast import record_delivery

    try:
        await record_delivery("day:15", 0, 4, {"доступ закрыт": 3, "сеть": 1})
        assert await _state("delivery:day:15") == "0/4"
        reason = await _state("delivery_reason:day:15")
        assert reason is not None, "причина нулевой доставки обязана сохраниться"
        assert "доступ закрыт (×3)" in reason and "сеть (×1)" in reason, reason
        # Доставка пошла — история прошлого прохода не должна уехать в
        # следующую тревогу задним числом.
        await record_delivery("day:15", 4, 4, {"доступ закрыт": 4})
        assert await _state("delivery:day:15") == "4/4"
        assert await _state("delivery_reason:day:15") is None
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(
                    WatcherState.key.in_(["delivery:day:15", "delivery_reason:day:15"])
                )
            )
            await db.commit()


async def test_delivery_alarm_carries_last_reason(_clean) -> None:
    """Тревога «не дошла» называет причину, а не только факт.

    Факт по логам не разобрать (там рассылка всегда «успешна»), а выбор между
    «kicked» и «сеть на Render» решает, куда идти — в права чатов или в /health.
    """
    async with SessionLocal() as db:
        db.add(WatcherState(key="delivery:day:16", value="0/4"))
        db.add(
            WatcherState(
                key="delivery_reason:day:16",
                value="доступ к чату закрыт (kicked/бот заблокирован) (×4)",
            )
        )
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert any("day:16" in p for p in problems), problems
        # problems несёт только заметку; полный текст тревоги — в отправке.
        sent = "\n".join(_clean.sent)
        assert "Причины последнего прохода" in sent, sent
        assert "закрыт" in sent, sent
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(
                    WatcherState.key.in_(
                        ["delivery:day:16", "delivery_reason:day:16"]
                    )
                )
            )
            await db.commit()


async def test_empty_announce_audience_raises_alarm(_clean) -> None:
    """Анонс дня без единого получателя — тревога, а не «успех».

    Claim дня (announced_at) уже стоит, а метка delivery:* при «0 из 0» не
    пишется (см. record_delivery) — то есть рассылка, не дошедшая ни до кого,
    раньше была неотличима от рассылки, дошедшей до всех.
    """
    from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY

    async with SessionLocal() as db:
        db.add(WatcherState(key=ANNOUNCE_EMPTY_DAY_KEY, value="42"))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert any("анонс дня 42 не дошёл ни до кого" in p for p in problems), problems
        assert _clean.sent, "тревога должна уйти админу"
        assert any("/start" in text and "Добавь бота" in text for text in _clean.sent), _clean.sent
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(WatcherState).where(WatcherState.key == ANNOUNCE_EMPTY_DAY_KEY)
            )
            await db.commit()


async def test_empty_announce_alarm_clears_when_chat_returns(_clean) -> None:
    """Привязали чат — метка пустоты гаснет на ближайшем же проходе.

    Иначе тревога висела бы до самого следующего анонса (до суток) после того,
    как хранитель уже всё починил, и «починилось само» никто бы не услышал.
    """
    from app.core.registry import ANNOUNCE_EMPTY_DAY_KEY

    chat = -100_555_001
    async with SessionLocal() as db:
        db.add(WatcherState(key=ANNOUNCE_EMPTY_DAY_KEY, value="43"))
        db.add(Chat(id=chat, type="channel", active=True))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert not any("не дошёл ни до кого" in p for p in problems), problems
        async with SessionLocal() as db:
            assert await db.get(WatcherState, ANNOUNCE_EMPTY_DAY_KEY) is None
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(WatcherState).where(WatcherState.key == ANNOUNCE_EMPTY_DAY_KEY))
            await db.execute(delete(Chat).where(Chat.id == chat))
            await db.commit()


async def test_missing_delivery_marker_raises_alarm(_clean) -> None:
    """День отмечен рассылкой, а метки доставки нет — тревога.

    Ноль означает «дошло немного», отсутствие метки — «не знаем». Тишина опаснее:
    проход мог умереть до записи метки, и это выглядело как успех.
    """
    await _closed_round(21)
    try:
        problems = await ops.check_anomalies(_clean)
        assert any("нет отметки о доставке" in p and "21" in p for p in problems), problems
    finally:
        await _drop_round(21)


async def test_existing_day_marker_silences_missing_alarm(_clean) -> None:
    """Метка за этот день есть — тревоги нет (иначе она кричала бы каждый цикл)."""
    await _closed_round(22)
    async with SessionLocal() as db:
        db.add(WatcherState(key="delivery:results:22", value="4/4"))
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert not any("нет отметки о доставке" in p for p in problems), problems
    finally:
        await _drop_round(22)


async def test_no_closed_round_means_no_missing_alarm(_clean) -> None:
    """Свежая база без закрытых дней тревоги не поднимает: нечего проверять.

    Закрытые дни заводят и другие тесты (тестовая БД общая), поэтому убираем их
    явно: иначе тест держался бы на том, что этот файл идёт первым по алфавиту.
    """
    async with SessionLocal() as db:
        await db.execute(delete(Round).where(Round.results_at.is_not(None)))
        await db.commit()
    problems = await ops.check_anomalies(_clean)
    assert not any("нет отметки о доставке" in p for p in problems), problems


async def test_closed_round_without_results_is_not_alarmed(_clean) -> None:
    """День закрыт, но итоги ещё не отправлялись — метки нет законно.

    Это и есть настоящий риск ложной тревоги: ручной переход закрывает день
    раньше, чем что-то уходит игрокам. Тревога на «нет метки» без оглядки на
    results_at кричала бы на каждом таком дне.
    """
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await db.execute(delete(Round).where(Round.day_index == 23))
        await db.commit()
        db.add(
            Round(
                day_index=23,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="День 23",
                chapter_text="Сцена.",
                opens_at=now - timedelta(hours=30),
                voting_ends_at=now - timedelta(hours=20),
                tally_ends_at=now - timedelta(hours=19),
                results_at=None,
            )
        )
        await db.commit()
    try:
        problems = await ops.check_anomalies(_clean)
        assert not any("нет отметки о доставке" in p for p in problems), problems
    finally:
        await _drop_round(23)
