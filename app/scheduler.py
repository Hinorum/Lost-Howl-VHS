from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.async_utils import spawn
from app.broadcast import announce_new_day
from app.config import settings
from app.db import SessionLocal
from app.models import RoundStatus, WatcherState
from app.rounds import (
    _now,
    claim_announcement,
    close_voting,
    ensure_current_round,
    finish_tally,
    get_active_round,
    get_latest_round,
    utc_aware,
)
from app.tally import award_pending_points, award_points

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


async def tick(bot: Bot | None = None) -> None:
    bot = bot or _bot
    from app.metrics import timed

    # Тик ловит свой сбой сам, поэтому результат отмечается вручную (span.fail):
    # проглоченное исключение не должно попасть в метрики как успех.
    async with timed("way-tick") as span:
        await _tick_body(bot, span)


async def _tick_body(bot: Bot | None, span) -> None:
    """Одно тело тика. Вынесено из tick(), чтобы обернуть его учётом времени."""
    from app.ops import is_game_paused, mark_tick, mark_tick_failed

    # Стоп-кран: дни не открываются и не закрываются, анонсы молчат.
    # Стоп-кран: дни не открываются и не закрываются, анонсы молчат.
    # Watcher (отдельная джоба) продолжает возвращать входящие переводы,
    # а очередь выплат — разгребаться: чужие деньги зависнуть не должны.
    # Битие здесь честное: игра стоит по команде, а не упала.
    async with SessionLocal() as session:
        if await is_game_paused(session):
            await mark_tick()
            return
    async with SessionLocal() as session:
        try:
            previous = await get_latest_round(session)
            current = await ensure_current_round(session)

            # Самолечение: дни, застрявшие не-закрытыми позади актуального
            from app.rounds import heal_stale_rounds

            healed = await heal_stale_rounds(session)
            if healed:
                logger.warning("Вылечено застрявших дней: %d", healed)

            # Прогрев кэшей для синхронных постов: якорь забега.
            from app.rounds import get_run_anchor

            await get_run_anchor(session)

            # Первый запуск или только что созданный день — анонсим без итогов.
            if previous is None or current.id > previous.id:
                await _announce_round(session, current, bot)

            now = _now()
            if current.status == RoundStatus.OPEN and now >= utc_aware(current.voting_ends_at):
                await close_voting(session, current)
            if current.status == RoundStatus.TALLYING and now >= utc_aware(current.tally_ends_at):
                finished, closed_here = await finish_tally(session, current)
                if closed_here:
                    await award_points(session, finished)
                    from app.stakes import finalize_day_payouts

                    await finalize_day_payouts(session, finished)
                    spawn(_payout_dispatch_job(), "payout_dispatch")
                    results_task = spawn(_announce_results_job(finished.id), "announce_results")
                    # Новый день ждёт рассылку итогов: без ожидания финализация
                    # с нейро-контентом обгоняла итоги (флуд-паузы по retry_after)
                    # и хронология в чатах ломалась.
                    spawn(
                        _finalize_new_day_job(finished.id, wait_results=results_task),
                        "finalize_new_day",
                    )
            # Краш между коммитом finish_tally и award_points оставляет день
            # CLOSED без очков, а heal_stale_rounds лечит только OPEN/TALLYING:
            # маркер awards_at догоняет такие дни идемпотентно (claim отсекает
            # двойное списание и параллельных финализаторов).
            await award_pending_points(session)
            # То же для выплат: краш между коммитом finish_tally и
            # finalize_day_payouts оставляет CLOSED-день без призов, и копилки
            # недели/месяца ждут его вечно (leaderboard читает
            # payouts_finalized). Догон идемпотентен — claim финализации
            # пускает одного.
            from app.stakes import finalize_pending_payouts

            await finalize_pending_payouts(session)
            # Достраховка итогов: CLOSED-день без маркера results_at (краш между
            # коммитом закрытия и джобой рассылки, вылеченные дни тоже сюда) —
            # досылается фоном, claim-метка не даёт дублей.
            spawn(_retry_results_job(), "retry_results")
            # Достраховка анонса нового дня: OPEN-день без метки announced_at
            # (краш/сбой сети между claim_announcement и вещанием) объявляется
            # восстановителем — новый день не теряется молча.
            spawn(_retry_new_day_job(), "retry_new_day")
        except Exception as exc:
            logger.exception("тик закрытия дня упал — откат транзакции")
            await session.rollback()
            span.fail()
            # Битие НЕ обновляем: иначе падающий каждые 15 секунд цикл
            # подтверждал бы собственную живость, /health отвечал бы «ok»,
            # а тревога «планировщик не тикает» не срабатывала бы никогда.
            # Счётчик падений и админский алерт — в mark_tick_failed; стирает
            # их следующий успешный тик.
            await mark_tick_failed(exc, bot)
            return
    # До сюда доходим только успешным тиком: сердцебиение = «цикл отработал».
    await mark_tick()


async def _announce_results_job(finished_id: int) -> None:
    """Мгновенная рассылка сухих итогов дня (без эпилога и нового дня).

    Дёргается отдельной джобой сразу после вскрытия, чтобы не ждать
    нейро-контент нового дня. Своя сессия — запущена из тика после закрытия
    его собственной транзакции.
    """
    try:
        from app.broadcast import announce_player_results, announce_results
        from app.models import Round

        async with SessionLocal() as session:
            finished = (
                await session.execute(
                    select(Round).where(Round.id == finished_id).options(selectinload(Round.cards))
                )
            ).scalar_one_or_none()
            if finished is None:
                logger.warning("Итоги дня %s: раунд не найден", finished_id)
                return
            if not await _claim_results(session, finished_id):
                # Восстановитель уже взял этот день (гонка джоб) — дублей нет.
                return
            # Личный доказ «за что голосовал и чем кончилось» — следом за общими
            # итогами, чтобы игрок сначала увидел сводку дня, потом свой исход.
            try:
                await announce_results(_bot, finished)
                await announce_player_results(_bot, finished)
            except Exception:
                await session.rollback()  # маркер снят — восстановитель дошлёт
                raise
            await session.commit()
    except Exception:
        logger.exception("Рассылка итогов дня упала (id=%s)", finished_id)


async def _claim_results(session: AsyncSession, finished_id: int) -> bool:
    """Атомарная метка «итоги этого дня разношу я» (results_at = токен).

    Ставится ДО бродкаста, в той же транзакции, что и сама рассылка: откат
    транзакции снимает маркер (крах в середине разрешает повтор — at-least-once),
    а прав на одну рассылку ровно один (гонка джоб/реплик — at-most-once).
    """
    from app.models import Round

    claimed = await session.execute(
        update(Round)
        .where(Round.id == finished_id, Round.results_at.is_(None))
        .values(results_at=_now())
    )
    return claimed.rowcount == 1


async def _announce_round(session: AsyncSession, round_row, bot: Bot | None) -> None:
    """Объявляет новый день под claim-меткой announced_at.

    claim_announcement коммитит метку ДО вещания (атомарно, ровно один
    вещатель в гонке джоб/реплик). Если вещание падает — метка снимается
    (unclaim), и _retry_new_day_job на ближайшем тике объявит день снова:
    потеря поста нового дня хуже редкого дубля (at-least-once), а дубль всё
    равно исключён, пока метка стоит.
    """
    if not await claim_announcement(session, round_row):
        return
    try:
        await announce_new_day(bot, round_row)
    except Exception:
        from app.rounds import unclaim_announcement

        await unclaim_announcement(session, round_row.id)
        raise


async def _retry_new_day_job() -> None:
    """Досылка анонса новых дней, не доставленного прошлым циклом.

    Краш или сбой сети между claim_announcement и announce_new_day (или анонс
    day-1 при самом первом запуске) оставляют OPEN-день без поста: тик видит
    только переход previous→current, а /advance помечает день навсегда. Здесь
    открытые дни без announced_at объявляются заново (лимит 5 за тик).

    Ловим только НЕДАВНИЕ дни (catchup_cutoff): announced_at добавлен
    миграцией без бэкфилла, так что у всей истории маркер NULL — без границы
    первый же тик новой версии объявил бы заново каждый старый день.
    """
    try:
        from app.models import Round
        from app.ops import is_game_paused
        from app.rounds import catchup_cutoff

        async with SessionLocal() as session:
            if await is_game_paused(session):
                return
            pending = (
                await session.execute(
                    select(Round.id)
                    .where(
                        Round.status == RoundStatus.OPEN,
                        Round.announced_at.is_(None),
                        Round.opens_at >= catchup_cutoff(),
                    )
                    .order_by(Round.day_index.asc())
                    .limit(5)
                )
            ).all()
            for (round_id,) in pending:
                day = await session.get(Round, round_id)
                if day is None:
                    continue
                try:
                    await _announce_round(session, day, _bot)
                except Exception as exc:
                    # Метку _announce_round уже снял — день повторится завтрашним
                    # тиком; больной день не должен обрушить остальные.
                    logger.warning(
                        "Повторный анонс дня %s упал (повторится): %s",
                        day.day_index, exc,
                    )
    except Exception:
        logger.exception("Повтор анонса нового дня упал")


async def _retry_results_job() -> None:
    """Восстановитель рассылки итогов: CLOSED-дни без маркера results_at.

    Краш между коммитом закрытия дня (finish_tally) и spawn'ом джобы итогов
    оставлял день без единого поста навсегда — heal 'ли закрывает OPEN/TALLYING
    и ничего не анонсирует. Здесь такие дни дохожу: общий пост + личные, откат
    транзакции снимает маркер и повтор разрешён. Кап 3 дня за тик — налёт
    заваленных деплоем дней не валит бота флудом.

    Ловим только НЕДАВНИЕ дни (catchup_cutoff): results_at добавлен
    миграцией без бэкфилла, поэтому у КАЖДОГО исторического закрытого дня
    маркер NULL. Без границы первый же тик новой версии считал всю историю
    игры недоставленной и рассылал её заново — 27 закрытых дней флудом в
    чат за пару минут. Догон нужен только для свежих крашей.
    """
    try:
        from app.broadcast import announce_player_results, announce_results
        from app.models import Round
        from app.rounds import catchup_cutoff

        async with SessionLocal() as session:
            missing = (
                await session.execute(
                    select(Round)
                    .where(
                        Round.status == RoundStatus.CLOSED,
                        Round.results_at.is_(None),
                        Round.voting_ends_at >= catchup_cutoff(),
                    )
                    .order_by(Round.day_index.asc())
                    .limit(3)
                    .options(selectinload(Round.cards))
                )
            ).scalars().all()
            # Снимок до цикла: session.rollback() в except-блоке истекает ВСЕ
            # инстансы сессии, и чтение атрибутов после отката предыдущего дня —
            # это lazy load вне greenlet (MissingGreenlet). Второй exception
            # тогда вылетал прямо из except, continue не выполнялся и до
            # остальных дней тика восстановитель не доходил. Id и день держим
            # простыми int, инстанс достаём заново через session.get — как в
            # rounds/lifecycle.py.
            batch = [(finished.id, finished.day_index) for finished in missing]
            for finished_id, day_index in batch:
                if not await _claim_results(session, finished_id):
                    continue
                finished = await session.get(
                    Round, finished_id, options=[selectinload(Round.cards)]
                )
                if finished is None:
                    continue
                try:
                    await announce_results(_bot, finished)
                    await announce_player_results(_bot, finished)
                except Exception:
                    await session.rollback()  # маркер снят — повтор разрешён
                    logger.exception(
                        "Восстановитель итогов дня %s упал — повторит в следующем тике",
                        day_index,
                    )
                    continue
                await session.commit()
                logger.info(
                    "Итоги дня %s досланы восстановителем после краха",
                    day_index,
                )
    except Exception:
        logger.exception("Восстановитель итогов упал целиком")


async def _finalize_new_day_job(
    finished_id: int, wait_results: asyncio.Task | None = None
) -> None:
    """Тяжёлая доработка нового дня — фоном, по готовности.

    Итоги уже разосланы отдельно (_announce_results_job); если wait_results
    передан, анонс нового дня откладывается до полной доставки итогов — чтобы
    игроки видели сначала хронологический итог, а не рассказ следующего дня.
    Здесь: write_epilogue (бэкафилл канона в БД) → флаг лидерборда (последний день
    недели/месяца) → новый день (инлайн-генерация) → анонс. Канон дней уже записан
    при закрытии раунда (```lifecycle.finish_tally```) и разошёлся в посте итогов;
    write_epilogue лишь страхует дни без эпилога.
    Свои краткоживущие сессии (нельзя переиспользовать сессию тика — она
    за пределами этого контекста).
    """
    from app.models import Round

    try:
        from app.rounds import create_next_round_detailed, write_epilogue

        # 1. Эпилог подтверждает выбор и закрепляется в БД (идемпотентно).
        # cards грузим сразу: write_epilogue ходит по ним синхронно, ленивая
        # подгрузка вне await дала бы MissingGreenlet.
        async with SessionLocal() as session:
            finished = (
                await session.execute(
                    select(Round).where(Round.id == finished_id).options(selectinload(Round.cards))
                )
            ).scalar_one_or_none()
            if finished is None:
                logger.warning("Доработка дня %s: раунд не найден", finished_id)
                return
            # Индекс дня берём из живой сессии: ниже finished расцепляется —
            # читать его day_index из отвязанного объекта было бы ошибкой.
            finished_day_index = finished.day_index
            await write_epilogue(session, finished)
            # Если последний день недели/месяца — ставим флаг готовности лидерборда.
            from app.leaderboard import mark_leaderboards_for_finished

            await mark_leaderboards_for_finished(session, finished)
        # 2. Материализуем и открываем новый день. День рендерится сразу
        # целиком по известному итогу «вчера» — без заготовки из часа подсчёта.
        # Финализация открывает ровно день после закрытого (N+1), а не
        # latest+1: так тик, уже создавший N+1, не провоцирует эскалацию в N+2
        # (двойной день, потерянные итоги N+1).
        async with SessionLocal() as session:
            nxt, created = await create_next_round_detailed(
                session, base_day_index=finished_day_index
            )
        if created:
            if wait_results is not None:
                await wait_results
            # finished не передаём: итоги уже разосланы отдельным постом.
            # Анонс идёт ПОД claim-меткой: иначе восстановитель новых дней
            # принял бы этот день за неанонсированный (announced_at IS NULL)
            # и объявил бы его второй раз.
            async with SessionLocal() as session:
                fresh = await session.get(Round, nxt.id)
                if fresh is None or fresh.status != RoundStatus.OPEN:
                    logger.warning("День %s для анонса не найден или закрыт", nxt.id)
                else:
                    await _announce_round(session, fresh, _bot)
    except Exception:
        logger.exception("Финализация нового дня упала (id=%s)", finished_id)


async def _payout_dispatch_job() -> None:
    """Немедленная отправка вознаграждений после вскрытия итогов."""
    try:
        from app.ton_pay import dispatch_pending_payouts

        sent = await dispatch_pending_payouts(bot=_bot)
        logger.info("Диспетчер выплат (kick): отправлено %d", sent)
    except Exception:
        logger.exception("Kick выплат не удался (ретраи продолжатся по расписанию)")


async def _watch_job() -> None:
    """Watcher ставок с ботом: игрок получает личное о судьбе перевода."""
    from app.ton_watch import watch_once

    await watch_once(bot=_bot)


async def _watch_job_guarded() -> None:
    """Обёртка _watch_job с алертом при падении."""
    await _alert_guarded("ton-watch", _watch_job)


async def _treasury_mirror_job() -> None:
    """Зеркало казны: инкрементальный синк истории активного кошелька.

    Отдельная джоба от watcher'а (тот ищет новые ставки и живёт готовым
    курсором): зеркало бутстрапится от генезиса и не должно вставать на
    долгую паузу из-за сбоев окна watcher'а — у сверки «в ноль» свой ритм.
    """
    from app.treasury_mirror import sync_treasury_mirror

    await sync_treasury_mirror()


async def _treasury_mirror_guarded() -> None:
    """Обёртка _treasury_mirror_job с алертом при падении."""
    await _alert_guarded("treasury-mirror", _treasury_mirror_job)


async def _ton_maintenance() -> None:
    """Финализация дней, очередь выплат, ретраи, копилки недели и месяца."""
    from app.leaderboard import settle_month_if_due, settle_week_if_due
    from app.ton_pay import confirm_broadcast_payouts, settle_closed_rounds

    try:
        # Сверка «sent»-выплат с блокчейном: bcast-метка не гарантирует, что
        # перевод попал в блок (гонка двух быстрых переводов). Потерянные memo
        # возвращаются в очередь, подтверждённые получают реальный хеш.
        await confirm_broadcast_payouts(bot=_bot)
    except Exception:
        logger.exception("confirm_broadcast_payouts упал (повторится через 120с)")
    try:
        await settle_closed_rounds(bot=_bot)
    except Exception:
        logger.exception("settle_closed_rounds упал (повторится через 120с)")
    try:
        await settle_week_if_due(bot=_bot)
    except Exception:
        logger.exception("settle_week_if_due упал")
    try:
        await settle_month_if_due(bot=_bot)
    except Exception:
        logger.exception("settle_month_if_due упал")
    # Проверка аномалий уехала в отдельную джобу ops-sweep: она обязана
    # работать и при TON_ENABLED=false, где раньше не запускалась вовсе.


async def _ops_sweep() -> None:
    """Тревоги по расписанию и данным — раз в 120с, при любой конфигурации.

    Раньше проверка жила внутри ton-settle, а та регистрируется только при
    TON_ENABLED=true. В текущем проде деньги выключены, значит не работали
    ВСЕ тревоги разом: ни «очередь стоит», ни «безнадёжные выплаты», ни
    «планировщик не тикает». Своя джоба честно отделена от денежного контура.
    """
    from app.ops import check_anomalies

    problems = await check_anomalies(_bot)
    if problems:
        logger.warning("Аномалии: %s", "; ".join(problems))


async def _ops_sweep_guarded() -> None:
    """Обёртка _ops_sweep с алертом при падении."""
    await _alert_guarded("ops-sweep", _ops_sweep)


async def _ton_maintenance_guarded() -> None:
    """Обёртка _ton_maintenance с алертом при падении."""
    await _alert_guarded("ton-settle", _ton_maintenance)


async def boot_maintenance() -> None:
    """Разовые задачи при старте: свежий бэкап БД до всего остального."""
    from app.backups import backup_job

    await _alert_guarded("db-backup@boot", backup_job)


def _register_job(job_id: str, func, trigger: str, **kwargs) -> None:
    """Регистрация джобы с изоляцией сбоев и запретом на молчаливый пропуск.

    Инцидент: обязательный аргумент bot в одной джобе ронял всю
    start_scheduler() — игра оставалась без тиков, watcher'а и выплат,
    а вебхук продолжал отвечать, маскируя мёртвое расписание. Теперь
    кривая регистрация глушит только саму себя.

    misfire_grace_time=None — запуск НИКОГДА не отбрасывается из-за опоздания.
    Дефолт APScheduler равен одной секунде, и это значит, что любой запуск с
    задержкой больше секунды просто не происходит, сопровождаясь одним WARNING
    в логе планировщика. Для этого расписания это недопустимо:

    * db-backup (cron, раз в сутки) молча пропускал бы ежедневный бэкап, если
      цикл был занят в 04:17;
    * vote-reminder (cron, 10:00 UTC) попадает ровно на границу 15-секундной
      сетки way-tick и конкурирует с ним каждый день;
    * постоянного jobstore нет — состояние в RAM, догоняющего запуска тоже
      нет, поэтому пропущенный cron не повторится никогда.

    Все джобы здесь либо самовосстанавливающиеся (tick закрывает догоняющие дни,
    watcher догоняет хвост по курсору, ton-settle разбирает очередь), либо
    обслуживающие, где пропуск молчалив. coalesce=True не даёт при этом
    накопиться очереди догоняющих запусков: сколько бы циклов ни пропало,
    выполнится ровно один.
    """
    kwargs.setdefault("misfire_grace_time", None)
    try:
        scheduler.add_job(
            func,
            trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **kwargs,
        )
    except Exception:
        logger.exception("Джоба %s не зарегистрирована", job_id)


async def _alert_guarded(job_id: str, func) -> None:
    """Фоновая авто-задача с алертом админу при падении.

    П.13 аудита: бэкап, шлифовка картинок и воскресные отчёты подолгу
    живут без присмотра, а падают молча — APScheduler глушит исключение, и
    сломанный отчёт недели выглядит как «отчёта просто не было». Оборачиваем
    обслужку: исключение логируется и немедленно уходит админу, при этом
    наружу НЕ пробрасывается (одна сломанная джоба не роняет расписание).

    Здесь же единственная точка учёта всех джоб в метриках: запуск, результат
    и длительность. Оборачивать каждую джобу отдельно — значит забыть одну;
    при max_instances=1 забытая джоба тихо съедает свои циклы.
    """
    from app.metrics import timed

    try:
        async with timed(job_id):
            await func()
    except Exception as exc:
        logger.exception("Фоновая задача «%s» упала: %s", job_id, exc)
        try:
            if _bot is not None and settings.admin_id_set:
                from app.ops import notify_admins

                await notify_admins(
                    _bot,
                    f"⚠️ Фоновая задача «{job_id}» упала: {exc} "
                    f"(детали в логах планировщика)",
                )
        except Exception:
            logger.exception("Алерт о падении «%s» не доставлен", job_id)


def shutdown_scheduler() -> None:
    """Остановка без AttributeError, если планировщик так и не стартовал."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def _cleanup_watcher_state_job() -> None:
    """Вычищает одноразовые/устаревшие ключи watcher_state.

    В списке только то, что действительно пишется. Одноразовые маркеры дедупа
    refund:* (app/ton_watch/refunds.py) и ledger:* (app/ton_watch/ledger.py):
    после того как возврат или доход уже созданы, метка — мёртвый груз, а
    таблица на больших сезонах растёт бесконечно. Раз в неделю держим её в узде,
    оставляя живые настройки и потоковые якоря.

    Отсюда убраны micro_event:*, teaser:*, pecho:*, sniff:*, memquiz:*,
    img_stubs:*, day_projection:*, art_bible:* — писателей у них нет ни одного,
    уборка чистила пустоту. Это наследие снятого слоя (соцмеханика, арт,
    проекции дня); возвращать префиксы стоит вместе с тем, что их пишет.

    ВНИМАНИЕ: pot:* в список НЕ входит и не должен. Это метки «копилка за этот
    день уже зачислена», и снос метки означал бы, что следующая финализация
    этого дня начислит копилку второй раз. Метка должна жить не меньше жизни
    соответствующего дня, то есть бессрочно: строк на день копилки мало, а
    записи дают ровно четыре на день.
    """
    try:
        async with SessionLocal() as session:
            stmt = select(WatcherState).where(
                (WatcherState.key.like("refund:%"))
                | (WatcherState.key.like("ledger:%"))
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return
            for row in rows:
                await session.delete(row)
            await session.commit()
            logger.info("watcher_state: вычищено %d устаревших ключей", len(rows))
    except Exception as exc:
        logger.warning("Очистка watcher_state не удалась: %s", exc, exc_info=True)


def start_scheduler() -> None:
    from functools import partial

    from app.backups import backup_job

    _register_job("way-tick", tick, "interval", seconds=15)
    # Тревоги идут всегда, независимо от TON: при выключенных деньгах они
    # всё равно нужны (очередь, dead-letter, зависший планировщик, бэкап).
    _register_job("ops-sweep", _ops_sweep_guarded, "interval", seconds=120)
    # Суточный бэкап в «мёртвый» час: 04:17 MSK.
    _register_job(
        "db-backup",
        partial(_alert_guarded, "db-backup", backup_job),
        "cron",
        hour=4,
        minute=17,
    )
    if settings.ton_enabled:
        _register_job("ton-watch", _watch_job_guarded, "interval", seconds=settings.ton_watch_interval_seconds)
        _register_job("ton-settle", _ton_maintenance_guarded, "interval", seconds=120)
        _register_job(
            "treasury-mirror",
            _treasury_mirror_guarded,
            "interval",
            seconds=settings.treasury_mirror_interval_seconds,
        )
    # Сброс разросшегося watcher_state: еженедельно в ночь после нагрузок.
    _register_job(
        "ws-cleanup",
        _cleanup_watcher_state_job,
        "cron",
        day_of_week="sun",
        hour=3,
        minute=30,
    )
    # Напоминание о голосовании: 10:00 UTC (за час до закрытия в 11:00 UTC)
    _register_job(
        "vote-reminder", _vote_reminder_job, "cron",
        hour=10, minute=0, timezone="UTC",
    )
    scheduler.start()


def _reminder_text(stake_mode: bool, rule_phrase: str, stakes: tuple[int, int]) -> str:
    """Напоминание за час до конца с персональной строкой о ставке игрока.

    stakes = (confirmed_nanotons, pending_nanotons) по СЕГОДНЯШНему дню.
    Игрок в рассылку попадает только не выбравшим путь, поэтому «путь не
    выбран» подразумевается; здесь главное — донести, сделана ли ставка и на
    сколько, чтобы человек знал, чего ждать после закрытия.
    """
    body = f"🐺 Голосование закрывается через час.\n🎬 Сцена дня: {rule_phrase}."
    if not stake_mode:
        return f"{body}\nПуть ещё не выбран — жми «Сцена I/II/III» под постом дня."
    confirmed, pending = stakes
    if confirmed > 0:
        words = (
            f"Твоя ставка {confirmed / 1e9:.2f} Gram уже принята, но путь ещё "
            "не выбран — жми «Сцена I/II/III» под постом дня."
        )
    elif pending > 0:
        words = (
            f"Твоя ставка {pending / 1e9:.2f} Gram подтверждается (обычно до минуты), "
            "а путь ещё не выбран — жми «Сцена I/II/III» под постом дня."
        )
    else:
        words = (
            "Ставка не сделана и путь не выбран: переведи Gram казначею и жми "
            "«Сцена I/II/III» под постом дня."
        )
    return f"{body}\n{words}"


async def _vote_reminder_job() -> None:
    """Напоминание за час до конца: DM ТОЛЬКО тем, кто ещё не выбрал путь.

    Одна рассылка на дату (маркер в operations). Личная строка о ставке:
    без неё игрок, поставивший Gram, но не выбравший кадр, лежит в неведении —
    а игрок, сделавший всё, напоминание не получает вовсе (приоритет — тем,
    кому ещё есть что сделать).
    """
    bot = _bot
    if bot is None:
        return
    try:
        async with SessionLocal() as session:
            current = await get_active_round(session)
            if current is None or current.status != RoundStatus.OPEN:
                return
            # Одна рассылка на дату: маркер выбирает победивший процесс.
            from app.ops import claim_once

            if not await claim_once(session, f"job:vote-reminder:{_now().strftime('%Y-%m-%d')}"):
                return
            # Закрепляем маркер даты отдельным COMMIT: рассылка — не то, что
            # нужно откатывать вместе с транзакцией чтения. Без COMMIT сессия
            # закроется откатом, маркер исчезнет, и «раз в день» превратится
            # в «каждый тик в 10:00» после каждого рестарта.
            await session.commit()
            # Получаем всех игроков, которые ещё не голосовали
            from sqlalchemy import select as _select

            from app.models import Vote

            voted_result = await session.execute(
                _select(Vote.player_id).where(Vote.round_id == current.id)
            )
            voted_ids = {row[0] for row in voted_result.all()}

            from app.models import Player

            all_players = await session.execute(
                _select(Player).where(Player.dm_subscribed == True)
            )
            unbotted = [p for p in all_players.scalars().all() if p.id not in voted_ids]

            if not unbotted:
                return
            unbotted_ids = {p.id for p in unbotted}

            stake_mode = (
                settings.ton_enabled
                and getattr(current, "money_mode", True) is not False
            )
            from app.models import RULE_PHRASES, VOTE_RULE_PHRASES

            rule_phrase = (RULE_PHRASES if stake_mode else VOTE_RULE_PHRASES)[
                current.win_rule
            ]
            # Суммы ставок по игрокам дня — одно чтение на всю рассылку:
            # правим язык напоминания под фактическое состояние игрока.
            stake_totals: dict[int, tuple[int, int]] = {}
            if stake_mode:
                from app.models import Stake

                stake_rows = await session.execute(
                    _select(Stake.player_id, Stake.amount_nanotons, Stake.status).where(
                        Stake.round_id == current.id
                    )
                )
                for pid, amount, status in stake_rows.all():
                    confirmed, pending = stake_totals.get(pid, (0, 0))
                    if status == "confirmed":
                        stake_totals[pid] = (confirmed + int(amount), pending)
                    else:
                        stake_totals[pid] = (confirmed, pending + int(amount))

            from app.broadcast import _dm_send_all

            async def _deliver(pid: int) -> None:
                # Аудитория сужена до не проголосовавших (only=), поэтому и
                # фильтровать внутри не нужно.
                #
                # СБОЙ ОТПРАВКИ НЕ ГЛОТАЕТСЯ. Раньше здесь стоял
                # except Exception -> logger.debug, из-за чего _dm_send_all
                # (он считает успехом любой вызов без исключения) записывал
                # неп��ставленное сообщение как доставленное, и в лог уходило
                # «Напоминание о голосовании отправлено: N сообщений» с
                # завышенным N. Теперь промах считается промахом, ретрай
                # Telegram отрабатывает, а причина видна на уровне warning.
                await bot.send_message(
                    pid,
                    _reminder_text(
                        stake_mode,
                        rule_phrase,
                        stake_totals.get(pid, (0, 0)),
                    ),
                )

            sent = await _dm_send_all(
                bot, _deliver, "vote-reminder", only=unbotted_ids
            )
            logger.info(
                "Напоминание о голосовании: доставлено %d из %d не проголосовавших",
                sent,
                len(unbotted_ids),
            )
    except Exception as exc:
        logger.warning("Ошибка напоминания о голосовании: %s", exc)
