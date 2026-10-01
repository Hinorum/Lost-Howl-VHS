import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.config import settings
from app.db import init_db
from app.handlers import build_dispatcher, create_bot
from app.http_utils import close_http_client, get_http_client
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
log = logging.getLogger("way")


def _authorized(request: web.Request) -> bool:
    """Общая проверка доступа к диагностике (/health, /metrics).

    Если задан HEALTH_TOKEN, снимок (очередь выплат, возраст тика, watcher,
    метрики) доступен только с авторизацией: мониторинг Render/UptimeRobot
    передаёт токен в заголовке Authorization: Bearer <token> либо в
    ?token=. Требование токена без самого токена — отказ (fail closed).
    """
    if settings.health_require_token and not settings.health_token.strip():
        return False
    if not settings.health_token:
        return True
    expected = settings.health_token.strip()
    supplied = (
        (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        or request.query.get("token", "").strip()
    )
    return bool(expected) and supplied == expected


async def health(request: web.Request) -> web.Response:
    """Живость + операционный снимок: тик, очередь выплат, watcher, день.

    Сбой снимка (переходное окно миграции, деградация БД) не роняет
    эндпоинт — Render должен видеть процесс живым; но и «ok» без данных мы
    не притворяемся: честный статус degraded.
    """
    if not _authorized(request):
        return web.Response(status=401, text="unauthorized")
    try:
        from app.ops import snapshot

        payload = await snapshot()
    except Exception as exc:
        log.warning("snapshot упал — отвечаем degraded: %s", exc)
        payload = {"status": "degraded", "detail": "snapshot unavailable"}
    return web.json_response(payload)


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


async def boot_game(bot) -> None:
    """Стартовые шаги. Планировщик запускается ПЕРВЫМ делом: сетевой сбой
    бэкапа или профиля не смеет оставлять игру без тиков навсегда (раньше
    исключение до start_scheduler означало молчаливо мёртвое расписание)."""
    set_bot(bot)
    # Лок ПЕРЕД первым тиком, а не перед start_scheduler: стартовый tick тоже
    # пишет в игру (анонсы, финализация, выплаты), поэтому второй инстанс не
    # должен отработать даже один раз.
    if not await acquire_scheduler_lock():
        log.critical(
            "База уже занята другим процессом — этот экземпляр уходит в фон: "
            "/health работает, игру он не трогает. Если так не задумано, на "
            "Render живут два сервиса с одним DATABASE_URL."
        )
        return
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


async def run_webhook(bot, dispatcher) -> None:
    path = "/webhook"
    secret = settings.webhook_secret or None
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
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
    for task in (boot_task, ping_task):
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
