import asyncio
import contextlib
import difflib
import hmac
import logging
import os
import signal
from pathlib import Path

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.config import settings
from app.db import init_db
from app.handlers import build_dispatcher, create_bot
from app.http_utils import close_http_client, get_http_client
from app.log_scrub import install as install_log_scrub
from app.profile import apply_profile
from app.scheduler import set_bot, start_scheduler, tick
from app.scheduler_lock import acquire_scheduler_lock, release_scheduler_lock
from app.ton_utils import normalize_address


def _same_address(left: str, right: str) -> bool:
    """Являются ли два адреса одним кошельком (raw/UQ/EQ приводятся к канону)."""
    try:
        return normalize_address(left) == normalize_address(right)
    except Exception:
        return False


logging.basicConfig(level=logging.INFO)
# Мнемоника казначея не должна дойти до логов ни от кого: краска ставится
# сразу после настройки логирования и до старта любых задач, которые могут
# её упомянуть (см. app/log_scrub.py).
install_log_scrub()
# pytoniq именует логгеры по имени класса (client.py: self.__class__.__name__),
# поэтому лайтсервер пишет в «LiteClient» по строке на каждый shard-блок:
# getAllShardsInfo/getMasterchainInfo на каждом опросе. На живом прогоне это
# 49% объёма лога и ~155 МБ в сутки при непрерывной работе. Диагностической
# ценности в INFO-шуме нет, а WARNING/ERROR остаются видны — глушим только его.
logging.getLogger("LiteClient").setLevel(logging.WARNING)
log = logging.getLogger("way")


def _authorized(request: web.Request) -> bool:
    """Общая проверка доступа к диагностике (/health, /metrics).

    Если задан HEALTH_TOKEN, снимок (очередь выплат, возраст тика, watcher,
    метрики) доступен только с авторизацией: мониторинг Render/UptimeRobot
    передаёт токен в заголовке Authorization: Bearer <token>. Требование токена
    без самого токена — отказ (fail closed).

    Query-формы (?token=) здесь нет намеренно. Токен в строке запроса попадает
    в access-логи прокси и CDN, в историю браузера и в заголовок Referer при
    любом переходе со страницы, открытой в браузере. Легитимного потребителя у
    query-формы тоже нет: self-ping ходит с заголовком, а проба живости Render
    идёт на /alive, где токен не нужен вовсе.
    """
    if settings.health_require_token and not settings.health_token.strip():
        return False
    if not settings.health_token:
        return True
    expected = settings.health_token.strip()
    supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    # Сравнение за постоянное время: секрет длинный, но заголовок приходит из
    # сети, а не из локального кода — утечка по времени сравнения формально
    # возможна. В проекте hmac.compare_digest уже используется (referrals.py).
    return bool(expected) and hmac.compare_digest(supplied, expected)


async def alive(request: web.Request) -> web.Response:
    """Живость процесса — без токена и без снимка, для health check Render.

    Раньше проверка Render ходила на /health?token=<HEALTH_TOKEN>, и токен
    приходилось держать в render.yaml открытым текстом (репозиторий публичный,
    значение утекало всем желающим). Здесь нет ни секрета, ни данных: 200
    значит только «процесс отвечает». Операционный снимок — на /health и
    /metrics под токеном; вердикт «игра в порядке» для внешнего watchdog,
    который не умеет слать заголовки, — на /ready.
    """
    return web.json_response({"status": "alive"})


async def health(request: web.Request) -> web.Response:
    """Живость + операционный снимок: тик, очередь выплат, watcher, день.

    Сбой снимка (переходное окно миграции, деградация БД) не роняет
    эндпоинт: тело честно сообщает degraded, а HTTP-код повторяет вердикт —
    200 только при status=ok, иначе 503. Раньше код всегда был 200, и
    внешний watchdog физически не мог отличить «всё хорошо» от «тиков нет
    второй день»: инцидент 2026-10-01 прошёл мимо всех мониторингов, потому
    что проверять было нечем. Проба живости Render смотрит на /alive и от
    этой честности не зависит.
    """
    if not _authorized(request):
        return web.Response(status=401, text="unauthorized")
    try:
        from app.ops import snapshot

        payload = await snapshot()
    except Exception as exc:
        log.warning("snapshot упал — отвечаем degraded: %s", exc)
        payload = {"status": "degraded", "detail": "snapshot unavailable"}
    return web.json_response(payload, status=_verdict_code(payload))


def _verdict_code(payload: dict) -> int:
    """HTTP-код по вердикту из тела: ok — 200, всё остальное — 503."""
    return 200 if payload.get("status") == "ok" else 503


async def ready(request: web.Request) -> web.Response:
    """Вердикт здоровья для внешнего watchdog — без токена и без данных.

    Мониторинг, который не умеет передавать заголовок Authorization
    (UptimeRobot и большинство бесплатных HTTP-чекеров), /health читать не
    может: там 401 без Bearer-токена, и такой чекер врал бы «плохо»
    постоянно. Единственный открытый при этом /alive показывает живость
    процесса — а про инцидент, когда процесс жил, а тики умерли, он
    промолчал бы ровно так же, как и раньше.

    Поэтому здесь только вердикт: 200 при status=ok, 503 при degraded
    (включая недоступность снимка). Ни очереди выплат, ни списка тревог, ни
    возраста тика — по телу не видно ничего, кроме одного бита; подробности
    остаются под токеном в /health и /ops.
    """
    try:
        from app.ops import snapshot

        verdict = (await snapshot()).get("status")
    except Exception as exc:
        log.warning("snapshot для /ready недоступен: %s", exc)
        verdict = "degraded"
    payload = {"status": "ok" if verdict == "ok" else "degraded"}
    return web.json_response(payload, status=_verdict_code(payload))


async def metrics(request: web.Request) -> web.Response:
    """Метрики процесса в текстовом формате Prometheus.

    Основа для графиков и алертов: раньше длительность и успешность фоновых
    задач не измерялись ничем, а при max_instances=1 долгий цикл тихо съедал
    следующие. Снимок БД не обязателен: при его недоступности отдаём 200 с
    way_snapshot_up 0 и счётчиками из памяти — частичные данные полезнее 500.
    """
    if not _authorized(request):
        return web.Response(status=401, text="unauthorized")
    from app.metrics import render
    from app.ops import snapshot

    try:
        payload = await snapshot()
    except Exception as exc:
        log.warning("снимок для /metrics недоступен: %s", exc)
        payload = None
    # Content-Type с version=0.0.4 ставим заголовком: aiohttp не даёт указать
    # charset в content_type иначе, а именно эта версия формата нужна сборщику.
    response = web.Response(text=render(payload), content_type="text/plain")
    response.headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
    return response


async def _self_ping_loop(stop: asyncio.Event) -> None:
    """Пингует собственный /health: free plan Render засыпает без входящего
    трафика, а каждый пинг считается входящим запросом. Расписание дней
    якорится к UTC-сетке, поэтому без пинга день открывался бы при первом
    пробудившем запросе, а не в 11:00 UTC."""
    if not settings.public_base_url:
        return
    url = f"{settings.public_base_url}/health"
    headers = {}
    if settings.health_token:
        headers["Authorization"] = f"Bearer {settings.health_token.strip()}"
    while not stop.is_set():
        try:
            client = get_http_client()
            response = await client.get(url, headers=headers)
            log.info("self-ping %s -> %s", url, response.status_code)
        except Exception as exc:
            log.warning("self-ping не удался: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.self_ping_seconds)


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows и часть песочниц: обработчик не поддерживается, но
            # остановку всё равно ловит внешний supervisor. Ожидаемо.
            pass


def validate_config() -> list[str]:
    """Проверки критичной конфигурации при старте.

    Вернёт список проблем; пустой список → всё в порядке. Вызывается до
    init_db/create_bot, чтобы очевидные ошибки (пустой токен, невалидный rake
   %) не диагностировались молча через часы бездействия.
    """
    problems: list[str] = []
    if not settings.bot_token.strip():
        problems.append(
            "BOT_TOKEN пуст — бот не сможет отправлять сообщения в Telegram. "
            "Получи токен у @BotFather и укажи его в переменной окружения."
        )
    if not settings.admin_ids.strip():
        problems.append(
            "ADMIN_IDS не заполнены — административные команды (/advance, "
            "/resetgame, /panel, /disputes) будут недоступны никому."
        )
    rake = (
        settings.owner_rake_pct
        + settings.leaderboard_rake_pct
        + settings.weekly_pot_pct
        + settings.pack_fund_pct
        + settings.referral_pct
    )
    if rake > 100:
        problems.append(
            f"Суммарный рейк {rake:.2f}% превышает 100% (owner {settings.owner_rake_pct}%"
            f" + leaderboard {settings.leaderboard_rake_pct}%"
            f" + weekly {settings.weekly_pot_pct}%"
            f" + fund {settings.pack_fund_pct}%"
            f" + referral {settings.referral_pct}%) — "
            "prize_pool станет отрицательным, и все ставки уйдут в копилку недели."
        )
    if settings.health_require_token and not settings.health_token.strip():
        problems.append(
            "HEALTH_REQUIRE_TOKEN=true, но HEALTH_TOKEN пуст: "
            "/health закрыт для всех (включая self-ping и чек живости Render). "
            "Задай HEALTH_TOKEN либо выключи HEALTH_REQUIRE_TOKEN."
        )
    if settings.admin_ids.strip() and settings.database_url.startswith("sqlite") and not settings.allow_sqlite:
        problems.append(
            "DATABASE_URL указывает на SQLite, а ADMIN_IDS заданы — это продакшен-режим, "
            "и он ломает две вещи сразу. advisory-лок (scheduler_lock) на SQLite "
            "не работает, то есть ничто не мешает подняться второму инстансу: два "
            "процесса делят очередь выплат и могут разослать историю дважды. И база "
            "лежит на эфемерном диске контейнера, то есть пропадает при каждом "
            "деплое вместе с журналом выплат. Укажи PostgreSQL в DATABASE_URL; если "
            "SQLite нужен намеренно (локально, в тестах) — поставь ALLOW_SQLITE=true."
        )
    liteserver_mismatch = settings.liteserver_network_mismatch()
    if liteserver_mismatch:
        problems.append(
            f"{liteserver_mismatch}. URL лайтсерверов подставляется с приоритетом над "
            f"TON_NETWORK: seqno казначея будет читаться на нодах чужой сети, выплаты "
            "подпишутся неверным seqno и уйдут в dead-letter. Для mainnet нужен "
            "https://ton.org/global.config.json, для testnet — "
            "https://ton.org/testnet-global.config.json."
        )
    if getattr(settings, "ton_enabled", False):
        if not settings.active_treasury_address:
            problems.append(
                "TON_ENABLED=true, но нет адреса казначея "
                "(TREASURY_ADDRESS для mainnet или TREASURY_TESTNET_ADDRESS для testnet). "
                "Ставки не будут приниматься."
            )
        if not settings.active_treasury_mnemonic:
            problems.append(
                "TON_ENABLED=true, но нет мнемоники казначея "
                "(TREASURY_MNEMONIC / TREASURY_TESTNET_MNEMONIC). "
                "Выплаты не будут отправляться."
            )
        if (
            settings.owner_wallet_address
            and settings.active_treasury_address
            and _same_address(settings.owner_wallet_address, settings.active_treasury_address)
        ):
            problems.append(
                "OWNER_WALLET_ADDRESS совпадает с адресом казначея — рейк хранителя "
                "и доли копилки уйдут «сами себе». Укажи отдельный кошелёк владельца."
            )
    return problems


def warn_config() -> list[str]:
    """Неблокирующие замечания к конфигурации (warn, не мешают старту).

    Держится отдельно от validate_config, потому что тот роняет старт, а
    подозрение на опечатку в имени переменной — не повод не подниматься.
    """
    return _unknown_env_keys()


def _unknown_env_keys() -> list[str]:
    """Переменные окружения, похожие на настройки, но не совпадающие с ними.

    Причина проверки: pydantic по умолчанию молча игнорирует лишние
    переменные окружения. Опечатка в имени денежного рычага (например
    PAYOUT_FEE_GARM вместо _GRAM) выглядит как «настроил», а работает как
    «применилось значение по умолчанию» — и прод уезжает с комиссией,
    которую никто не выбирал.

    Отбираются только БЛИЗКИЕ имена (difflib), а не все лишние переменные:
    в окружении Render/CI/локальной машины законно лежит сотня чужих
    переменных (PATH, PROCESSOR_*, CI_*), и общий список на старте только
    приучил бы читать предупреждение мимо. Близость — признак опечатки.
    """
    known = [name.upper() for name in type(settings).model_fields]
    suspects: list[str] = []
    for key in os.environ:
        if not key.isupper() or "_" not in key or key.upper() in known:
            continue
        close = difflib.get_close_matches(key.upper(), known, n=1, cutoff=0.9)
        if close:
            suspects.append(f"{key} → {close[0]}")
    if not suspects:
        return []
    return [
        "Похоже на опечатку в имени переменной окружения (будет применено "
        "значение по умолчанию, а не твоё): " + ", ".join(sorted(suspects))
    ]


# Интервал фоновой попытки перехватить лок планировщика. Согласован с ритмом
# way-tick (15 с): как только старый инстанс отпустит advisory lock, новый
# подхватит его в течение одного-двух циклов, без ручного рестарта на Render.
_SCHEDULER_LOCK_RETRY_SECONDS = 15

# Ссылка на фоновую задачу retry-а: нужна, чтобы корректно дождаться/отменить
# её при shutdown. None до неудачного acquire_scheduler_lock().
_lock_retry_task: asyncio.Task | None = None


async def _boot_after_lock(bot) -> None:
    """Шаги, которые выполняются только после успешного захвата лока планировщика.

    Вынесено из boot_game(), чтобы и синхронный путь (первый acquire удался),
    и фоновая retry-попытка (defer после освобождения лока старым инстансом
    при Render rolling deploy) выполняли одну и ту же последовательность:
    install_bay → tick → прогрев кэшей → start_scheduler → boot_maintenance →
    apply_profile. Иначе пришлось бы дублировать шаги и рисковать расхождением
    (например, один путь забудет вызвать boot_maintenance и пропустит свежий
    бэкап после рестарта).
    """
    # Сюжетный слой (необязателен): проигрыватель кассет включает себя, только
    # если есть каталог библиотеки; сбой установки не смеет ронять игру.
    try:
        from app.story.bay import install_bay

        install_bay()
    except Exception:
        log.exception("Проигрыватель кассет не поднялся — движок играет шаблон")
    try:
        await tick(bot)
    except Exception:
        log.exception("Первый тик не удался — повторится по расписанию")
    # Холодный старт: кэш якоря сезона греется сразу, не дожидаясь первого тика —
    # иначе /panel или анонс в чат увидят пустой якорь, а под вебхуком первый
    # апдейт способен прийти до тика вовсе. Банк дня читается из БД на лету.
    try:
        from app.db import SessionLocal
        from app.rounds import get_active_round, get_run_anchor

        async with SessionLocal() as session:
            await get_run_anchor(session)
            await get_active_round(session)
    except Exception:
        log.exception("Прогрев кэшей дня не удался — первый тик догонит")
    start_scheduler()
    from app.scheduler import boot_maintenance

    for name, step in (("backup", boot_maintenance), ("profile", lambda: apply_profile(bot))):
        try:
            await step()
        except Exception:
            log.exception("Шаг старта «%s» не удался — игра продолжается без него", name)


async def _retry_scheduler_lock_loop(bot) -> None:
    """Фоновая попытка перехватить лок планировщика после его отпускания.

    Инцидент: при Render rolling deploy новый инстанс поднимался, пока старый
    ещё держал Postgres advisory lock. acquire_scheduler_lock() возвращал False,
    boot_game() уходил в фон (без start_scheduler), и бот стоял без tick'ов
    до ручного рестарта — дни не закрывались, лидерборд месяца не выплачивался.

    Цикл пробует лок каждые _SCHEDULER_LOCK_RETRY_SECONDS: как только старый
    инстанс закрыл соединение и pg_advisory_lock освободился, retry захватывает
    его, прогоняет _boot_after_lock() и завершается. /health и webhook'и всё
    это время работают — основной процесс не блокируется. Исключение внутри
    цикла логируется, но не убивает задачу: один сбой БД не должен оставлять
    бот без игрового движка.
    """
    from app.scheduler_lock import scheduler_lock_held

    while True:
        if scheduler_lock_held():
            # Лок каким-то образом уже у процесса (гонка с основным boot_game
            # невозможна: create_task срабатывает только после неудачного acquire,
            # где _lock_conn остаётся None; защищаемся на всякий случай).
            log.info("Фоновый retry лока: лок уже у процесса — выходим")
            return
        try:
            if await acquire_scheduler_lock():
                log.info(
                    "Лок планировщика перехвачен фоновым retry — продолжаем запуск"
                )
                try:
                    await _boot_after_lock(bot)
                except Exception:
                    log.exception(
                        "Фоновый bootstrap после получения лока упал — игра стоит",
                        exc_info=True,
                    )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Фоновая попытка взять лок планировщика упала")
        await asyncio.sleep(_SCHEDULER_LOCK_RETRY_SECONDS)


async def boot_game(bot) -> None:
    """Стартовые шаги. Планировщик запускается ПЕРВЫМ делом: сетевой сбой
    бэкапа или профиля не смеет оставлять игру без тиков навсегда (раньше
    исключение до start_scheduler означало молчаливо мёртвое расписание).

    Лок берётся синхронно: если БД занята другим процессом (Render rolling
    deploy оставил старый инстанс, ещё не отдавший advisory lock), возвращаемся
    немедленно, чтобы /health и webhook'и работали, и параллельно запускаем
    фоновый retry — как только лок освободится, инициализация продолжится
    без ручного рестарта. Один процесс — один путь загрузки: либо синхронный
    (когда лок свободен сразу), либо фоновая retry-попытка (когда занят)."""
    global _lock_retry_task
    set_bot(bot)
    # Лок ПЕРЕД первым тиком, а не перед start_scheduler: стартовый tick тоже
    # пишет в игру (анонсы, финализация, выплаты), поэтому второй инстанс не
    # должен отработать даже один раз.
    if not await acquire_scheduler_lock():
        log.critical(
            "База уже занята другим процессом — этот экземпляр уходит в фон: "
            "/health работает, игру он не трогает. Если так не задумано, на "
            "Render живут два сервиса с одним DATABASE_URL. "
            "Фоновый retry через %d с попробует перехватить лок после освобождения.",
            _SCHEDULER_LOCK_RETRY_SECONDS,
        )
        # Один процесс — одна retry-таска: повторный create_task защищён от
        # двойного фонового bootstrap (двух _boot_after_lock и двух start_scheduler).
        if _lock_retry_task is None or _lock_retry_task.done():
            _lock_retry_task = asyncio.create_task(
                _retry_scheduler_lock_loop(bot),
                name="scheduler-lock-retry",
            )
        return
    await _boot_after_lock(bot)


async def run_webhook(bot, dispatcher) -> None:
    path = "/webhook"
    secret = settings.webhook_secret or None
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/alive", alive)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/metrics", metrics)
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dispatcher, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    if settings.public_base_url:
        # drop_pending_updates=False: накопленные за сон апдейты (голоса
        # кнопками, оплаты Stars!) должны обработаться, а не выброситься.
        await bot.set_webhook(
            f"{settings.public_base_url}{path}",
            secret_token=secret,
            drop_pending_updates=False,
        )
    boot_task = asyncio.create_task(boot_game(bot))
    stop = asyncio.Event()
    ping_task = asyncio.create_task(_self_ping_loop(stop))
    _install_stop_handlers(stop)
    await stop.wait()
    log.info("Остановка: глушим планировщик и веб-сервер")
    from app.scheduler import shutdown_scheduler

    shutdown_scheduler()
    # Лок отдаём явно: пока держим соединение, держим и лок, а Render может
    # переиспользовать контейнер. Само закрытие соединения тоже освободило бы
    # лок, но на shutdown полагаться на это не стоит.
    try:
        await release_scheduler_lock()
    except Exception:
        log.warning("Лок планировщика не отдан явно — освободится с соединением", exc_info=True)
    stop.set()  # будим self-ping для корректного завершения
    boot_task.cancel()
    ping_task.cancel()
    # Фоновая retry-захвата лока: если она жива (новый инстанс не успел
    # перехватить лок до shutdown), отменяем — пусть живёт ровно столько,
    # сколько живёт процесс. Если уже успела завершиться (захватила лок и
    # прогнала _boot_after_lock), cancel() будет no-op, а планировщик уже
    # остановлен через shutdown_scheduler() выше.
    if _lock_retry_task is not None and not _lock_retry_task.done():
        _lock_retry_task.cancel()
    for task in (boot_task, ping_task, _lock_retry_task):
        if task is None:
            continue
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await runner.cleanup()
    await bot.session.close()
    await close_http_client()


def ensure_webhook_secret() -> None:
    """Fail-fast: вебхук без секрета принимает поддельные апдейты.

    Кто угодно, знающий URL сервиса, мог бы отправить фальшивое «сообщение
    от админа» и выполнить /resetgame или /advance. Лучше упасть на старте,
    чем держать открытый командный контур.
    """
    if settings.use_webhook and not settings.webhook_secret:
        raise RuntimeError(
            "WEBHOOK_SECRET обязателен в режиме вебхука: без него кто угодно, "
            "знающий URL сервиса, может подсунуть фальшивый апдейт Telegram "
            "(вплоть до сообщений от имени админа)."
        )


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    problems = validate_config()
    for problem in problems:
        log.error("CONFIG: %s", problem)
    if problems:
        raise RuntimeError(
            "Критичные проблемы конфигурации (см. лог выше). "
            "Укажи недостающие переменные в .env и перезапусти."
        )
    for warning in warn_config():
        log.warning("CONFIG: %s", warning)
    await init_db()
    bot = await create_bot()
    dispatcher = build_dispatcher()
    if settings.use_webhook:
        ensure_webhook_secret()
        await run_webhook(bot, dispatcher)
        return
    await bot.delete_webhook(drop_pending_updates=False)
    await boot_game(bot)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C — штатная остановка, а не сбой: тишина здесь уместна.
        pass
