"""Операционная наблюдаемость: снимок состояния для /health и алерты админу.

Всё, что раньше было видно только по логам, собирается в один JSON:
возраст последнего тика планировщика, курсор TON-watcher'а, глубина очереди
выплат, dead-letter хвост, текущий день. Аномалии (зависший watcher,
долгая очередь выплат, неустранимые failed) деликатно репортятся админу
не чаще раза в час на категорию — троттлинг живёт в watcher_state.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from aiogram import Bot
from sqlalchemy import func, select, text

from app.config import settings
from app.core.registry import (
    ALERT_BALANCE_KEY,
    ALERT_DEAD_KEY,
    ALERT_MIRROR_KEY,
    ALERT_QUEUE_KEY,
    ALERT_REFUND_KEY,
    ALERT_STAKE_KEY,
    ALERT_STUCK_KEY,
    ALERT_TICK_FAIL_KEY,
    ALERT_TICK_KEY,
    ALERT_WATCHER_KEY,
    BEAT_KEY,
    MONEY_MODE_KEY,
    OPS_ALERT_DIGEST_KEY,
    OPS_ALERT_TRACK_KEY,
    OPS_PROBLEMS_AT_KEY,
    OPS_PROBLEMS_KEY,
    PAUSE_KEY,
    PAUSE_REASON_KEY,
    STUCK_TX_KEY,
    TICK_FAIL_KEY,
    TICK_FAIL_LAST_KEY,
    TICK_KEY,
)
from app.db import SessionLocal
from app.models import Income, Payout, Round, RoundStatus, Stake, WatcherState

logger = logging.getLogger(__name__)

PROCESS_START = time.time()

# Список проблем, набранный текущим проходом check_anomalies: тревога уходит
# первой, иначе аномалию не увидят ни /health, ни /ops.
_problems_in_flight: list[str] = []
# Проблема → категория её тревоги, собирается по ходу проверок.
_problem_entry: dict[str, dict] = {}
_DIGITS_RE = re.compile(r"\d+")

_ALERT_COOLDOWN = timedelta(hours=1)
_WATCHER_STALE_AFTER = timedelta(minutes=30)
_QUEUE_OLD_AFTER = timedelta(minutes=30)
# Возврат ставки (refund) считается «застрявшим», когда ждёт отправки дольше
# этого окна: игрок остаётся с зависшими деньгами, хранителю нужно узнать.
_REFUND_OLD_AFTER = timedelta(minutes=30)
# Перевод, который так и не подтвердился в срок — необработанная ставка.
# Порог — двойное окно подтверждения ставки из настроек.
_STAKE_CONFIRM_STALE_MULT = 2
# Тики идут каждые 15 секунд: тишина дольше пары минут — процесс болен.
_TICK_STALE_AFTER = timedelta(minutes=5)
# Столько тиков подряд должны упасть, прежде чем тревога полетит админу.
# Один-два сбоя — обычная жизнь (гонка с закрытием дня, мигрантом БД), а
# третье подряд означает, что расписание не работает вовсе.
_TICK_FAIL_ALERT_AFTER = 3
# Допуск сверки баланса казначея с БД: сгоревший газ исходящих переводов
# и мелкий ручной вывод не должны будить админа ложной тревогой.
_BALANCE_TOLERANCE_NANO = 50_000_000  # 0.05 Gram

# Виды корректировок казны: ручной вывод хранителя / ручное пополнение.
# Пишутся в Income (unit_ref «manual:<uuid>»), попадают в формулу сверки.
MANUAL_OUT_KIND = "manual_out"
MANUAL_IN_KIND = "manual_in"


def _now() -> datetime:
    return datetime.now(UTC)


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (_now() - moment).total_seconds())


async def _get_state(session, key: str) -> str | None:
    row = await session.get(WatcherState, key)
    return row.value if row is not None else None


async def _set_state(session, key: str, value: str) -> None:
    row = await session.get(WatcherState, key)
    if row is None:
        session.add(WatcherState(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def claim_once(session, key: str) -> bool:
    """Атомарная cross-process метка «работу беру я» (watcher_state, PK).

    INSERT ... ON CONFLICT DO NOTHING в той же транзакции, что и сама работа:
    получает True ровно один процесс (rowcount == 1), остальные — False и
    выходят, не дублируя эффект. Откат транзакции отменяет метку вместе с
    работой — «полу-меток» не бывает, и повторная попытка после падения
    разрешена.

    Семантика at-most-once для рассылок/выплат через общий примитив,
    переживающий рестарты и несколько реплик. SQLite и Postgres понимают
    ON CONFLICT DO NOTHING одинаково (см. comment в leaderboard).

    Fail-fast на длину ключа: SQLite НЕ проверяет длину VARCHAR, Postgres
    кидает StringDataRightTruncationError. Инцидент 2026-09-17 — маркер
    refund:<tx_hash> (71 символ) упал в колонку VARCHAR(64) и зациклил
    обработку навсегда, а локальные тесты на SQLite его пропускали. Здесь
    оба движка проверяют одно и то же ограничение из схемы модели, чтобы
    расхождение оставалось тестовой ошибкой, а не прод-инцидентом.
    """
    limit = WatcherState.key.type.length
    if len(key) > limit:
        raise ValueError(f"claim-ключ {key!r} длиннее колонки key ({limit}) — см. registry/add_marker")
    result = await session.execute(
        text(
            "INSERT INTO watcher_state (key, value) VALUES (:k, '') "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"k": key},
    )
    return result.rowcount == 1


async def mark_tick() -> None:
    """Планировщик отмечается УСПЕШНЫМ тиком: /health видит зависший процесс.

    Смысл «после работы, а не до» — главный инцидент наблюдаемости: битие,
    поставленное в начале тика, подтверждало живость того же цикла, который
    тут же падал в лог. Дни не открывались, а /health отвечал «ok», и тревога
    «планировщик не тикает» не могла сработать по построению. Теперь зависший
    цикл честно старый, и его видит и мониторинг, и check_anomalies.
    """
    async with SessionLocal() as session:
        await _set_state(session, TICK_KEY, _now().isoformat())
        if await _get_state(session, TICK_FAIL_KEY) is not None:
            # Успех стирает счётчик падений: иначе три разовые аварии за
            # месяц накопились бы до «падает» и кричали бы вхолостую.
            await session.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([TICK_FAIL_KEY, TICK_FAIL_LAST_KEY])
                )
            )
            await session.commit()


async def mark_tick_failed(exc: BaseException | str, bot: Bot | None = None) -> int:
    """Тик упал: счётчик подряд, текст ошибки, тревога при затяжном падении.

    Отдельная точка входа вместо тихого `logger.exception` в теле тика. Сама
    тревога идёт отсюда, а не из check_anomalies, потому что её единственный
    носитель — тот же упавший цикл, и полагаться на него нельзя. Кулдаун
    час, как у остальных тревог; biение при этом честно стареет и через
    5 минут поднимает вторую, независимую сигнализацию.
    """
    detail = (
        f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    )[:300]
    count = 0
    try:
        async with SessionLocal() as session:
            raw = await _get_state(session, TICK_FAIL_KEY)
            try:
                count = int(raw or "0") + 1
            except ValueError:
                logger.debug("Счётчик падений тика не парсится (%r) — начинаем с нуля", raw)
                count = 1
            await _set_state(session, TICK_FAIL_KEY, str(count))
            await _set_state(session, TICK_FAIL_LAST_KEY, detail)
            if count < _TICK_FAIL_ALERT_AFTER or not await _throttled(
                session, ALERT_TICK_FAIL_KEY
            ):
                return count
        await notify_admins(
            bot,
            f"🚨 Главный тик падает {count} раз подряд: {detail}\n"
            "Дни не открываются и не закрываются, анонсы молчат — цикл повторяется "
            "каждые 15 секунд и падает снова. Степень: /health last_tick_age.\n"
            "Стоп-кран: /pause on (выплаты и watcher продолжат работать), "
            "дальше — по логам планировщика.",
        )
    except Exception:
        # Упавший тик не должен утащить за собой ещё и наблюдаемость.
        logger.exception("Не удалось записать падение главного тика")
    return count


async def snapshot() -> dict:
    """Состояние игры одним словарём для /health (мониторинг Render/UptimeRobot)."""
    async with SessionLocal() as session:
        latest = (
            await session.execute(select(Round).order_by(Round.day_index.desc()).limit(1))
        ).scalar_one_or_none()
        queue_count = (
            await session.execute(
                select(func.count())
                .select_from(Payout)
                .where(Payout.status.in_(["pending", "sending"]))
            )
        ).scalar_one()
        oldest_pending = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.status.in_(["pending", "sending"])
                )
            )
        ).scalar_one()
        dead_count = (
            await session.execute(
                select(func.count()).select_from(Payout).where(Payout.status == "failed")
            )
        ).scalar_one()
        # Разбивка очереди по типу выплаты: призы игрокам против возвратов ставок
        # и долей казны. Именно здесь видно, сколько игроков не получили ставку
        # обратно, не ныряя в /payouts.
        pending_by_kind = {
            kind: count
            for kind, count in (
                await session.execute(
                    select(Payout.kind, func.count())
                    .where(Payout.status.in_(["pending", "sending"]))
                    .group_by(Payout.kind)
                )
            ).all()
        }
        dead_by_kind = {
            kind: count
            for kind, count in (
                await session.execute(
                    select(Payout.kind, func.count())
                    .where(Payout.status == "failed")
                    .group_by(Payout.kind)
                )
            ).all()
        }
        # Необработанные переводы: ставки, что увидели в цепочке, но ещё не
        # подтвердили по возрасту (или зависли). Прямой ответ на «сколько
        # переводов висит в необработанных». Читаем из БД всегда: свёрка
        # казны (/panel) обязана показывать правду и при выключенных деньгах.
        pending_stakes = (
            await session.execute(
                select(func.count()).select_from(Stake).where(Stake.status == "pending")
            )
        ).scalar_one()
        cursor_iso = await _get_state(session, BEAT_KEY)
        # Счётчик падений главного тика: битие может быть свежим (сбой
        # короче 5 минут), а цикл при этом не отработал ни разу. Текст
        # последней ошибки в /health не отдаём — эндпоинт бывает без токена.
        try:
            tick_failures = int(await _get_state(session, TICK_FAIL_KEY) or "0")
        except ValueError:
            tick_failures = 0
        # Вердикт последней проверки аномалий из кэша sweeper'а, а не
        # результат опроса мониторинга: /health не должен сам ходить в сеть.
        problems_raw = await _get_state(session, OPS_PROBLEMS_KEY)
        problems = _parse_problems(problems_raw)
        track = _load_track(await _get_state(session, OPS_ALERT_TRACK_KEY))
        keys = {_problem_key(problem) for problem in problems}
        payload = {
            "status": "ok",
            "uptime_seconds": round(time.time() - PROCESS_START, 1),
            "last_tick_age": _age_seconds(await _get_state(session, TICK_KEY)),
            "tick_failures": tick_failures,
            "problems": problems,
            "problems_age": _age_seconds(await _get_state(session, OPS_PROBLEMS_AT_KEY)),
            # Сколько проблема держится и сколько раз её видели: «очередь стоит»
            # и «очередь стоит третий час» — разная срочность разбора.
            "problem_details": _detail(
                [(key, track[key]) for key in sorted(track) if key in keys]
            ),
            "round": None,
            "payout_queue": int(queue_count),
            "payout_pending_by_kind": pending_by_kind,
            "payout_dead_by_kind": dead_by_kind,
            "pending_stakes": int(pending_stakes),
            "oldest_payout_age": None,
            "dead_letter_payouts": int(dead_count),
            "watcher_beat_age": None,
            "watcher_source": None,
            # Диагностика окружения: видно, что реально дошло до процесса.
            "ton_enabled": bool(settings.ton_enabled),
            "ton_network": "testnet" if settings.is_testnet else "mainnet",
        }
        if oldest_pending is not None:
            moment = oldest_pending if oldest_pending.tzinfo else oldest_pending.replace(tzinfo=UTC)
            payload["oldest_payout_age"] = max(0.0, (_now() - moment).total_seconds())
        if settings.ton_enabled:
            payload["watcher_beat_age"] = _age_seconds(cursor_iso)
            # Локальный импорт: ton_watch тянет ставки/выплаты, ops должен
            # оставаться лёгким для /health при любых состояниях модулей.
            from app.ton_watch import SOURCE_KEY

            payload["watcher_source"] = await _get_state(session, SOURCE_KEY)
        if latest is not None:
            payload["round"] = {
                "id": latest.id,
                "day_index": latest.day_index,
                "status": latest.status.value if isinstance(latest.status, RoundStatus) else str(latest.status),
                "voting_ends_at": latest.voting_ends_at.isoformat(),
            }
        return payload


async def notify_admins(bot: Bot | None, text: str) -> None:
    """Разослать служебное сообщение всем хранителям; ошибки не мешают тику."""
    if bot is None or not settings.admin_id_set:
        return
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Служебный алерт админу %s не доставлен: %s", admin_id, exc)


async def _throttled(session, key: str) -> bool:
    """True, если по этой категории пора кричать (кулдаун раз в час).

    Длинная аномалия уходит в часовую сводку, а не в поток одинаковых
    сообщений: пять разных проверок держат одно и то же состояние по часу —
    админ получал пять сообщений с разными формулировками вместо одного
    «всё ещё сломано, уже второй час». Свёртка включается после того, как
    сводка реально ушла; если рассылка не сработала, повтор остаётся.
    """
    raw = await _get_state(session, key)
    if raw:
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            logger.debug("Кулдаун-метка %s не парсится (%r) — отсчёт заново", key, raw)
        else:
            if _now() - last < _ALERT_COOLDOWN:
                return False
            if await _sent_within(session, OPS_ALERT_DIGEST_KEY, _ALERT_COOLDOWN):
                # Час уже отчитался сводкой. Метку не двигаем: если сводка
                # потом отключится, повтор этой тревоги не потеряется.
                return False
    await _set_state(session, key, _now().isoformat())
    return True


async def _sent_within(session, key: str, window: timedelta) -> bool:
    """Метка времени лежит внутри окна. Битая метка — «не отправляли»."""
    raw = await _get_state(session, key)
    if not raw:
        return False
    try:
        return _now() - datetime.fromisoformat(raw) < window
    except ValueError:
        return False


def _load_track(raw: str | None) -> dict:
    """Разбор карты тревог. Мусор — пустая карта: пересчёт с нуля безопаснее
    выдуманной истории «проблема держится с какого-то момента»."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Битый JSON карты тревог (%r) — начинаем с чистого", (raw or "")[:128])
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, dict) and item.get("since")
    }


def _problem_key(text: str) -> str:
    """Ключ проблемы, устойчивый к её собственной формулировке.

    Тексты аномалий меняются от цикла к циклу («очередь стоит 31 мин» →
    «очередь стоит 32 мин»), а сам факт аномалии остаётся тем же. Поэтому в
    ключе все числа — плейсхолдер: без этого «проблема» мигает, и ни
    восстановления, ни подсчёта часов не получить.
    """
    return _DIGITS_RE.sub("#", text.strip().lower())


def _tracked_text(entry: dict) -> str:
    return str(entry.get("text") or "")


def _tracked_since(entry: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(entry.get("since")))
    except (TypeError, ValueError):
        return None


def _humanize(seconds: float) -> str:
    """Длительность словами: «12 с», «14 мин», «2 ч 14 мин»."""
    if seconds < 60:
        return f"{int(seconds)} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    return f"{int(seconds // 3600)} ч {int((seconds % 3600) // 60)} мин"


async def _store_track(session, track: dict) -> None:
    raw = json.dumps(track, ensure_ascii=False)
    row = await session.get(WatcherState, OPS_ALERT_TRACK_KEY)
    if row is None:
        session.add(WatcherState(key=OPS_ALERT_TRACK_KEY, value=raw))
    else:
        row.value = raw
    await session.commit()


async def _clear_alert_stamp(session, alert_key: str | None) -> None:
    """Сбросить кулдаун тревоги: аномалия ушла сама и вернулась — это новое
    событие, и молчать о нём ещё час было бы худшей версией молчания."""
    if not alert_key:
        return
    row = await session.get(WatcherState, alert_key)
    if row is not None:
        await session.delete(row)
        await session.commit()


async def _raise(session, bot, alert_key: str, problem: str, notice: str) -> None:
    """Единая точка тревоги: проблема в список + первое сообщение + учёт.

    Собирать это вручную в восьми местах — значит забыть учёт где-то одном,
    и карта тревог молча разъезжается с реальностью.
    """
    if problem in _problems_in_flight:
        # Та же аномалия уже в списке: две проверки о ней знают, а тревога и
        # кулдаун должны остаться одними.
        return
    _problems_in_flight.append(problem)
    if await _throttled(session, alert_key):
        await notify_admins(bot, notice)
    # Категория тревоги помнит, какой проблемой она стала: без этой связи
    # восстановление не знает, чей кулдаун сбрасывать, и повтор после
    # починки молчал бы ещё час.
    record = _problem_key(problem)
    entry = _problem_entry.get(record)
    if entry is not None:
        entry["alert"] = alert_key
    else:
        _problem_entry[record] = {"alert": alert_key}


def _detail(entries: list[tuple[str, dict]]) -> list[dict]:
    """Проблемы для /ops и снимка: текст, сколько держится, сколько раз видели."""
    return [
        {
            "text": _tracked_text(entry),
            "seen": int(entry.get("seen") or 1),
            "age": _age_seconds(entry.get("since")),
        }
        for _key, entry in entries
    ]


async def _settle_alerts(session, bot, problems: list[str]) -> None:
    """Восстановление и сводка: чем закончилась аномалия и как долго живёт.

    Раньше тревога была только входом события: ушла аномалия — тишина, и
    хранитель не знал, что починилось сам (и продолжал чинить то, что уже
    работает), а затянувшееся состояние приходило повтором раз в час с
    меняющимся числом минут вместо «держится второй час».
    """
    previous = _load_track(await _get_state(session, OPS_ALERT_TRACK_KEY))
    now: dict[str, dict] = {}
    moment = _now()
    stamp = moment.isoformat()
    for problem in problems:
        key = _problem_key(problem)
        # Категория тревоги записана этой же веткой проверки (_raise). Подставляем
        # её сюда: вердикт «проблема держалась 2 часа» должен уметь погасить
        # кулдаун именно этой тревоги.
        alert_key = str(_problem_entry.get(key, {}).get("alert") or "")
        entry = previous.get(key)
        if entry is None:
            now[key] = {"text": problem, "since": stamp, "seen": 1, "alert": alert_key}
        else:
            # Текст берём свежий: «32 мин» полезнее допотопного «31 мин».
            now[key] = {
                **entry,
                "text": problem,
                "seen": int(entry.get("seen") or 1) + 1,
                "alert": alert_key or entry.get("alert", ""),
            }

    for key, entry in previous.items():
        if key in now:
            continue
        if int(entry.get("seen") or 1) < 2:
            # Проблема, замеченная один раз и исчезнувшая до следующего прохода, —
            # не «починка», а всплеск. Сообщение «ушла сама» о нём было бы шумом
            # в личке, а кулдаун и так свежий.
            continue
        since = _tracked_since(entry)
        held = (moment - since).total_seconds() if since is not None else 0.0
        text = _tracked_text(entry) or key
        await notify_admins(
            bot,
            f"✅ Аномалия ушла сама: {text} (держалась {_humanize(held)}, "
            "замечена в прошлых проверках).",
        )
        await _clear_alert_stamp(session, entry.get("alert"))

    # Сводка — раз в час и только по тем, кто пережил больше одной проверки:
    # первое сообщение о новой аномалии уже отправлено веткой выше.
    repeated = [
        (key, entry)
        for key, entry in now.items()
        if int(entry.get("seen") or 1) > 1 and entry.get("alert")
    ]
    if repeated and not await _sent_within(session, OPS_ALERT_DIGEST_KEY, _ALERT_COOLDOWN):
        await _set_state(session, OPS_ALERT_DIGEST_KEY, stamp)
        lines = []
        for _key, entry in sorted(repeated, key=lambda pair: str(pair[1].get("since"))):
            since = _tracked_since(entry)
            held = (moment - since).total_seconds() if since is not None else 0.0
            lines.append(f"• {_tracked_text(entry)} — держится {_humanize(held)}")
        await notify_admins(
            bot,
            f"🔁 Аномалии без изменений ({len(repeated)}):\n" + "\n".join(lines),
        )
    await _store_track(session, now)


def _load_only_stuck(raw: str | None) -> dict:
    """Разбор JSON stuck-списка watcher'а без импорта ton_watch (loop-безопасно).

    Пустой/битый JSON — пустой словарь: отсутствие записей не тревога.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        logger.debug("Битый stuck-JSON (%r) — начинаем с чистого списка", (raw or "")[:128])
    return {}


def _parse_problems(raw: str | None) -> list[str]:
    """Снимок проблем из JSON. Мусор — пустой список: он не повод тревожить."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Битый JSON снимка аномалий (%r) — список пуст", (raw or "")[:128])
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


async def _store_problems(session, problems: list[str]) -> None:
    """Кэш вердикта проверок для /health и /ops (одна транзакция на оба ключа)."""
    for key, value in (
        (OPS_PROBLEMS_KEY, json.dumps(problems, ensure_ascii=False)),
        (OPS_PROBLEMS_AT_KEY, _now().isoformat()),
    ):
        row = await session.get(WatcherState, key)
        if row is None:
            session.add(WatcherState(key=key, value=value))
        else:
            row.value = value
    await session.commit()


async def problems_snapshot() -> tuple[list[str], float | None, list[dict]]:
    """(проблемы, возраст снимка в секундах, подробности) из кэша sweeper'а.

    Без сети и без пересчёта: /health опрашивают мониторингом постоянно, а
    check_anomalies ходит в БД и (при TON) в зеркало. Возраст снимка —
    отдельный сигнал: он растёт, если сам sweeper перестал ходить. Подробности
    отвечают на вопрос «а давно оно?» — из карты тревог, без похода в БД.
    """
    async with SessionLocal() as session:
        raw = await _get_state(session, OPS_PROBLEMS_KEY)
        age = _age_seconds(await _get_state(session, OPS_PROBLEMS_AT_KEY))
        track = _load_track(await _get_state(session, OPS_ALERT_TRACK_KEY))
    problems = _parse_problems(raw)
    details = _detail([(key, track[key]) for key in track if key in {_problem_key(p) for p in problems}])
    return problems, age, details


async def check_anomalies(bot: Bot | None) -> list[str]:
    """Фоновые проверки раз в минутный цикл обслуживания; возвращает список проблем."""
    # Проблемы собираются во время проверок, а не в конце: тревога должна уйти
    # до сохранения снимка — иначе сломанная рассылка оставила бы /health
    # говорить «всё хорошо» после того, как аномалия замечена. Список
    # общий с _raise и чистится в начале прохода: прошлый вердикт не должен
    # переезжать в новый, а упавшая середина проверки — в следующий.
    problems = _problems_in_flight
    problems.clear()
    _problem_entry.clear()
    async with SessionLocal() as session:
        # 0. Планировщик молчит — дни не закрываются, выплаты не уходят.
        tick_age = _age_seconds(await _get_state(session, TICK_KEY))
        if tick_age is not None and tick_age > _TICK_STALE_AFTER.total_seconds():
            note = f"планировщик не тикает {int(tick_age // 60)} мин"
            await _raise(
                session,
                bot,
                ALERT_TICK_KEY,
                note,
                f"⚠️ {note.capitalize()}. Дни не переключаются, смотрите логи сервиса.",
            )
        # 1. Watcher не завершает успешные циклы — ставки перестают находиться.
        # Сердцебиение ставит каждый цикл с живым TonAPI, даже если переводов
        # нет: тишина в цепочке — здоровье, а не простой. Курсор для этого
        # не годится: он двигается только переводами.
        if settings.ton_enabled:
            from app.ton_watch import BEAT_KEY

            beat_age = _age_seconds(await _get_state(session, BEAT_KEY))
            watcher_note: str | None = None
            if beat_age is None:
                watcher_note = "watcher ещё ни разу не завершал цикл"
            elif beat_age > _WATCHER_STALE_AFTER.total_seconds():
                watcher_note = f"watcher молчит {int(beat_age // 60)} мин"
            if watcher_note is not None:
                await _raise(
                    session,
                    bot,
                    ALERT_WATCHER_KEY,
                    watcher_note,
                    f"⚠️ TON-watcher отстаёт: {watcher_note}. Ставки копятся необработанными.",
                )
        # 2. Очередь выплат старше получаса — казначей застрял или сеть лежит.
        oldest = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.status.in_(["pending", "sending"]),
                    Payout.amount_nanotons > 0,
                )
            )
        ).scalar_one()
        if oldest is not None:
            moment = oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)
            age_min = int((_now() - moment).total_seconds() // 60)
            if age_min >= _QUEUE_OLD_AFTER.total_seconds() // 60:
                reason = ""
                if not settings.ton_enabled:
                    reason = " Причина: TON выключен (TON_ENABLED=false), ретраи не идут."
                elif not settings.active_treasury_mnemonic:
                    reason = " Причина: нет мнемоники казначея для активной сети (TREASURY_TESTNET_MNEMONIC?)."
                await _raise(
                    session,
                    bot,
                    ALERT_QUEUE_KEY,
                    f"очередь выплат стоит {age_min} мин",
                    f"⚠️ Очередь выплат не двигается {age_min} мин.{reason} Проверь казначея/сеть.",
                )
        # 3. Dead-letter выплаты существуют — деньги ждут ручного разбора.
        dead = (
            await session.execute(
                select(Payout.id, Payout.last_error).where(Payout.status == "failed").limit(20)
            )
        ).all()
        if dead:
            reasons = [f"#{row_id} {reason}" for row_id, reason in dead[:2] if reason]
            reason_note = f": {'; '.join(reasons)}" if reasons else ""
            await _raise(
                session,
                bot,
                ALERT_DEAD_KEY,
                f"dead-letter выплат: {len(dead)}",
                "⚠️ Есть безнадёжные выплаты (failed после всех ретраев): "
                f"{', '.join(str(row_id) for row_id, _r in dead[:10])}{reason_note}. "
                "Разбор: /payouts → /payout <id> retry|spam перед сбросом игры.",
            )
        # 3b. Возврат ставки (refund) висит неотправленным: игрок не получил
        #     деньги обратно. Отдельный целевой алерт, а не общий «очередь стоит».
        oldest_refund = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.kind == "refund",
                    Payout.status.in_(["pending", "sending"]),
                    Payout.amount_nanotons > 0,
                )
            )
        ).scalar_one()
        refund_dead = (
            await session.execute(
                select(func.count())
                .select_from(Payout)
                .where(Payout.kind == "refund", Payout.status == "failed")
            )
        ).scalar_one()
        if oldest_refund is not None:
            moment = (
                oldest_refund
                if oldest_refund.tzinfo
                else oldest_refund.replace(tzinfo=UTC)
            )
            age_min = int((_now() - moment).total_seconds() // 60)
            if age_min >= _REFUND_OLD_AFTER.total_seconds() // 60:
                tail = ""
                if refund_dead:
                    tail = f" Плюс {refund_dead} безнадёжных возврата (failed)."
                await _raise(
                    session,
                    bot,
                    ALERT_REFUND_KEY,
                    f"возврат ставки ждёт {age_min} мин",
                    "⚠️ Возврат ставки не доставлен: игрок не получил деньги "
                    f"обратно уже {age_min} мин.{tail} Разбор: /payouts",
                )
        # 3c. Необработанные переводы-ставки висят дольше двойного окна
        #     подтверждения — ставки копятся, никто не получает ни статус, ни
        #     возврат. Прямой ответ на «сколько переводов не обработано».
        if settings.ton_enabled:
            stale_threshold = settings.stake_confirm_seconds * _STAKE_CONFIRM_STALE_MULT
            oldest_stake = (
                await session.execute(
                    select(func.min(Stake.created_at)).where(Stake.status == "pending")
                )
            ).scalar_one()
            if oldest_stake is not None:
                moment = (
                    oldest_stake
                    if oldest_stake.tzinfo
                    else oldest_stake.replace(tzinfo=UTC)
                )
                if (_now() - moment).total_seconds() >= stale_threshold:
                    pending_stakes_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(Stake)
                            .where(Stake.status == "pending")
                        )
                    ).scalar_one()
                    await _raise(
                        session,
                        bot,
                        ALERT_STAKE_KEY,
                        f"ставок не обработано: {pending_stakes_count}",
                        "⚠️ Переводы-ставки не обрабатываются: "
                        f"{pending_stakes_count} висят без подтверждения дольше "
                        "положенного. /stakes",
                    )
        # 3d. Stuck-список watcher'а непуст — сбойные переводы зависли в казне.
        #     Авто-лечение точит их само, но пока запись живёт, админ должен
        #     видеть, что деньги не затерялись молча (инцидент Kote: 1 G казны
        #     висел из-за схема-бага, а сообщения не приходило).
        if settings.ton_enabled:
            stuck_row = await session.get(WatcherState, STUCK_TX_KEY)
            stuck = _load_only_stuck(stuck_row.value if stuck_row is not None else None)
            if stuck:
                stuck_count = len(stuck)
                await _raise(
                    session,
                    bot,
                    ALERT_STUCK_KEY,
                    f"сбойных переводов в stuck: {stuck_count} (в т.ч. брошенных: "
                    f"{sum(1 for r in stuck.values() if isinstance(r, dict) and r.get('reported'))})",
                    "⚠️ Сбойные переводы не обработаны: "
                    f"{stuck_count} в stuck-списке (watcher_state[{STUCK_TX_KEY}]). "
                    "Авто-лечение запущено, но записи дольше часа требуют "
                    "взгляда: /blockchain",
                )
# 4. Сверка баланса казначея с учётом БД. Две беды разного рода:
    #    дефицит под очередь (пополни — и всё уйдёт само) и расхождение
    #    с ожиданиями (ручной вывод, потерянные средства, чужой доступ).
    if settings.ton_enabled and settings.active_treasury_address:
        balance_note = await _treasury_balance_anomaly(session)
        if balance_note is not None:
            await _raise(
                session,
                bot,
                ALERT_BALANCE_KEY,
                balance_note,
                f"⚠️ Казначей: {balance_note}. Детали: /treasury и /payouts.\n"
                "Это был твой ручной перевод — закрой расхождение: /adjust",
            )
        # 4b. Зеркало казны: ежедневная сверка «в ноль». Тождество измеряется
        # зеркалом на каждом цикле синка (без сети здесь — читаем результат из
        # watcher_state), поэтому сбой виден сразу, без маскировки допуском на
        # газ: Σ движений зеркала должна равняться живому балансу ровно.
        mirror_note = await _treasury_mirror_anomaly(session)
        if mirror_note is not None:
            await _raise(session, bot, ALERT_MIRROR_KEY, mirror_note, mirror_note + " Разбор: /treasury")
    # Итог прохода: кто из тревог ушёл сам, кто держится дольше часа.
    await _settle_alerts(session, bot, problems)
    # Снимок для /health и /ops. Кэш, а не пересчёт на каждый опрос: одно и то
    # же «что сломано» отвечают и мониторинг, и команда хранителя.
    await _store_problems(session, problems)
    return list(problems)


class TreasuryDrift(NamedTuple):
    """Снимок сверки «баланс цепочки ↔ ожидания БД» одним объектом."""

    balance_nanotons: int
    expected_nanotons: int
    unpaid_nanotons: int
    tolerance_nanotons: int

    @property
    def drift_nanotons(self) -> int:
        """Положительный — на цепи МЕНЬШЕ ожиданий (вывод/пропажа),
        отрицательный — больше (незаметное пополнение)."""
        return self.expected_nanotons - self.balance_nanotons

    @property
    def beyond_tolerance(self) -> bool:
        return abs(self.drift_nanotons) > self.tolerance_nanotons


async def treasury_expected_state(session) -> TreasuryDrift | None:
    """Баланс цепочки против ожиданий БД; None — баланс недоступен.

    Ожидаемый остаток = все входящие переводы казны (ставки и revote-оплата,
    это строки Income kind="ton") + ручные пополнения − ручные выводы − все
    выплаты (sent уже ушли, pending/sending ещё уйдут). Допуск покрывает
    сгоревший газ исходящих переводов.

    ВАЖНО: подтверждённые ставки отдельно НЕ суммируем — каждый входящий
    перевод уже создаёт строку Income kind="ton" (ton_watch._ledger_incoming /
    _process_revote), и ставка дважды посчиталась бы (двойной учёт 1 Gram —
    неправдоподобные «пропажи» на ровном месте).
    """
    from app.ton_pay import fetch_account_state

    try:
        balance, _status, _source = await fetch_account_state()
    except Exception as exc:
        logger.warning("Баланс казначея для сверки не прочитан: %s", exc)
        return None
    if balance is None:
        return None
    network = "testnet" if settings.is_testnet else "mainnet"
    unpaid = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                    Payout.status.in_(["pending", "sending"]),
                    Payout.network == network,
                )
            )
        ).scalar_one()
    )
    sent = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                    Payout.status == "sent", Payout.network == network,
                )
            )
        ).scalar_one()
    )
    sent_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.status == "sent", Payout.network == network,
                )
            )
        ).scalar_one()
    )
    revotes = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == "ton",
                    Income.network == network,
                )
            )
        ).scalar_one()
    )
    manual_in = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == MANUAL_IN_KIND,
                    Income.network == network,
                )
            )
        ).scalar_one()
    )
    manual_out = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == MANUAL_OUT_KIND,
                    Income.network == network,
                )
            )
        ).scalar_one()
    )
    expected = revotes + manual_in - manual_out - sent - unpaid
    # Газ сгорает на каждом исходящем переводе; допуск = база + запас по числу.
    from app.ton_utils import to_nano

    tolerance = _BALANCE_TOLERANCE_NANO + sent_count * 2 * to_nano(settings.payout_fee_gram)
    return TreasuryDrift(
        balance_nanotons=balance,
        expected_nanotons=expected,
        unpaid_nanotons=unpaid,
        tolerance_nanotons=tolerance,
    )


async def _treasury_balance_anomaly(session) -> str | None:
    """Сравнивает баланс цепочки с ожиданиями БД. None — всё сходится.

    Две беды разного рода: дефицит под очередь выплат (пополни казначея)
    и расхождение с ожиданиями (ручной вывод или пропажа — закрывается
    командой /adjust). TON-строки Income размечены сетью (network), поэтому
    сверка идёт по активному контуру mainnet/testnet без смешения меток.
    """
    state = await treasury_expected_state(session)
    if state is None:
        return None
    if state.balance_nanotons < state.unpaid_nanotons:
        return (
            f"баланса {state.balance_nanotons / 1e9:.4f} Gram не хватит на очередь выплат "
            f"({state.unpaid_nanotons / 1e9:.4f} Gram) — пополни казначея"
        )
    if state.balance_nanotons + state.tolerance_nanotons < state.expected_nanotons:
        return (
            f"баланс {state.balance_nanotons / 1e9:.4f} Gram ниже ожиданий БД "
            f"(~{state.expected_nanotons / 1e9:.4f} Gram): ручной вывод или пропажа средств? "
            f"Закрыть расхождение: /adjust"
        )
    return None


async def _treasury_mirror_anomaly(session) -> str | None:
    """Ежедневная сверка «в ноль»: зеркало казны против цепочки. None — всё сходится.

    Читает результат тождества, который синк зеркала кладёт в watcher_state
    каждым циклом (без лишнего запроса к индексатору здесь). Расхождение
    «Σ движений ≠ живой баланс» — инцидент, который прежний допуск на газ
    мог маскировать неделями. Замерший синк — тревога в ЛЮБОМ состоянии:
    у не-выстроенного зеркала это «бутстрап остановился», у выстроенного —
    «протухший CHECK» (последний «зелёный» результат устарел, расхождение
    может расти без контроля). Живой бутстрап — тихая работа, не тревога.
    """
    from app.treasury_mirror import treasury_mirror_stats

    stats = await treasury_mirror_stats(session)
    bootstrapped = stats["bootstrapped"]
    beat_age: float | None = None
    beat_iso = stats.get("beat_iso")
    if beat_iso:
        beat_age = _age_seconds(beat_iso)
    if beat_age is None or beat_age > _WATCHER_STALE_AFTER.total_seconds():
        if bootstrapped:
            return (
                "зеркало казны не обновляется: синк замер при выстроенном "
                "зеркале — последняя сверка устарела, смотри /treasury"
            )
        return "зеркало казны не выстроено и циклы не идут — индексаторы молчат?"
    if not bootstrapped:
        return None  # бутстрап идёт: тихая работа, не тревога
    check = stats["check"]
    if not check:
        return "зеркало казны: тождество ещё не измерено"
    if check.get("exact") is True:
        return None
    diff = int(check.get("diff_nanotons") or 0)
    return (
        f"зеркало казны расходится с цепочкой на {diff / 1e9:+.4f} Gram: "
        "∑ движений ≠ живой баланс"
    )


async def record_manual_adjustment(
    session,
    kind: str,
    amount_nanotons: int,
    note: str = "",
) -> Income:
    """Корректировка казны: «это был мой ручной вывод» либо «пропажа/пополнение».

    Пишет строку в Income-леджер (unit_ref «manual:<uuid>» — уникальность
    бесплатно), после чего формула ожиданий в treasury_expected_state
    сходится с реальностью и часовой алерт замолкает сам.
    """
    if kind not in {MANUAL_OUT_KIND, MANUAL_IN_KIND}:
        raise ValueError(f"Неизвестный вид корректировки: {kind}")
    amount = int(amount_nanotons)
    if amount <= 0:
        raise ValueError("Сумма корректировки должна быть положительной")
    row = Income(
        kind=kind,
        amount_nanotons=amount,
        player_id=None,
        round_id=None,
        network="testnet" if settings.is_testnet else "mainnet",
        unit_ref=f"manual:{uuid.uuid4().hex}",
        note=(note or "")[:200],
    )
    session.add(row)
    await session.commit()
    logger.info(
        "Корректировка казны: %s %.4f Gram (%s)", kind, amount / 1e9, note or "без комментария"
    )
    return row


# ---------- Пауза игры (стоп-кран хранителя) ----------


async def is_game_paused(session) -> bool:
    """True, пока игра остановлена командой /pause или кнопкой пропажи."""
    return bool(await _get_state(session, PAUSE_KEY))


async def paused_reason(session) -> str | None:
    """Причина паузы (для пульта и сообщений игрокам) или None."""
    reason = await _get_state(session, PAUSE_REASON_KEY)
    return reason or None


async def set_game_paused(session, paused: bool, reason: str = "") -> bool:
    """Включает/снимает паузу. Возвращает True, если состояние изменилось.

    Повторная установка того же состояния — no-op: двойное нажатие кнопки
    не рассылает игрокам второе уведомление и не затирает причину.
    """
    if paused == await is_game_paused(session):
        return False
    if paused:
        await _set_state(session, PAUSE_KEY, _now().isoformat())
        if reason:
            await _set_state(session, PAUSE_REASON_KEY, reason[:200])
    else:
        await _set_state(session, PAUSE_KEY, "")
        await _set_state(session, PAUSE_REASON_KEY, "")
    logger.info("Пауза игры: %s (%s)", "включена" if paused else "снята", reason or "—")
    return True


# ---------- Версия игры: со ставками / без ставок (рубильник хранителя) ----------


async def money_mode_enabled(session) -> bool:
    """True = денежная версия (ставки TON, платная смена выбора).

    Рубильник из /panel живёт в watcher_state; отсутствие ключа = включено.
    Реальные дни снимают РЕЖИМ в Round.money_mode на своё открытие, поэтому
    переключение вступает в силу со следующего дня, а не посреди текущего.
    """
    return await _get_state(session, MONEY_MODE_KEY) != "0"


async def set_money_mode(session, enabled: bool) -> bool:
    """Переключает версию игры. True, если состояние реально изменилось —
    повторный тап той же кнопки не рассылает лишних подтверждений."""
    if enabled == await money_mode_enabled(session):
        return False
    await _set_state(session, MONEY_MODE_KEY, "1" if enabled else "0")
    logger.info(
        "Версия игры: %s",
        "денежная (ставки + смена выбора за плату)" if enabled else "без ставок",
    )
    return True
