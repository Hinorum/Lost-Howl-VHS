"""Запуск процесса: бут-последовательность, вебхук, self-ping, точка входа.

app/main.py — это код, который выполняется ровно один раз в жизни процесса, и
поэтому ничем не был покрыт: сбой в любом из шагов означает «игра не стартовала»,
а найти его можно только в логах прод-контейнера. Здесь каждый шаг проверяется
отдельно, включая те, что обязаны НЕ ронять запуск.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import runpy
import signal
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import main as main_module
from app.config import settings

RAW = "0:" + "ab" * 32


def test_same_address_compares_canonical_forms() -> None:
    """Один и тот же кошелёк в разной записи (raw/UQ/EQ) — один кошелёк."""
    assert main_module._same_address(RAW, RAW) is True
    assert main_module._same_address(RAW, "") is False


def test_same_address_handles_normalizer_error(monkeypatch) -> None:
    """Ровно тот случай, ради которого у _same_address есть try/except:
    нормализатор может бросить на странном вводе (например, на None)."""
    monkeypatch.setattr(
        main_module,
        "normalize_address",
        Mock(side_effect=ValueError("not an address")),
    )
    assert main_module._same_address(RAW, "мусор") is False


async def test_self_ping_disabled_without_public_url(monkeypatch) -> None:
    """Без публичного URL пинговать нечего — и локальный запуск не должен
    стучаться в интернет по кругу."""
    monkeypatch.setattr(settings, "webhook_base_url", "")
    client = SimpleNamespace(get=AsyncMock())
    monkeypatch.setattr(main_module, "get_http_client", Mock(return_value=client))
    await asyncio.wait_for(main_module._self_ping_loop(asyncio.Event()), timeout=5)
    client.get.assert_not_awaited()


async def test_self_ping_pings_with_token_and_stops_on_event(monkeypatch) -> None:
    """Free plan Render засыпает без входящего трафика, а день открывается по
    UTC-сетке: пинг самому себе держит расписание живым."""
    monkeypatch.setattr(settings, "webhook_base_url", "https://way.example/")
    monkeypatch.setattr(settings, "health_token", " s3cret ")
    monkeypatch.setattr(settings, "self_ping_seconds", 3600)
    stop = asyncio.Event()

    async def fake_get(url, headers=None):
        assert url == "https://way.example/health"
        assert headers == {"Authorization": "Bearer s3cret"}
        stop.set()
        return SimpleNamespace(status_code=200)

    client = SimpleNamespace(get=fake_get)
    monkeypatch.setattr(main_module, "get_http_client", Mock(return_value=client))
    await asyncio.wait_for(main_module._self_ping_loop(stop), timeout=5)


async def test_self_ping_survives_network_failure(monkeypatch) -> None:
    """Падение пинга не должно убивать фоновую задачу: иначе после одного
    разрыва Render перестанет будить бота навсегда."""
    monkeypatch.setattr(settings, "webhook_base_url", "https://way.example")
    monkeypatch.setattr(settings, "health_token", "")
    stop = asyncio.Event()

    async def fake_get(url, headers=None):
        stop.set()
        raise RuntimeError("connection reset")

    client = SimpleNamespace(get=fake_get)
    monkeypatch.setattr(main_module, "get_http_client", Mock(return_value=client))
    await asyncio.wait_for(main_module._self_ping_loop(stop), timeout=5)


async def test_install_stop_handlers_tolerates_missing_signals(monkeypatch) -> None:
    """Платформа может не знать сигнал (и add_signal_handler недоступен на
    Windows) — тогда остаёмся без своих обработчиков, но не падаем."""
    monkeypatch.setattr(main_module.signal, "SIGTERM", None)
    stop = asyncio.Event()
    # На Windows add_signal_handler бросает NotImplementedError: ветка «pass»
    # обязана быть рабочей, а не мёртвой.
    main_module._install_stop_handlers(stop)
    assert not stop.is_set()


async def test_boot_game_runs_every_step(monkeypatch) -> None:
    """Порядок старта зафиксирован: планировщик первый (иначе сбой сети
    оставит игру без тиков), затем тик, прогрев кэшей, бэкап и профиль."""
    scheduler = importlib.import_module("app.scheduler")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())
    monkeypatch.setattr(main_module, "tick", AsyncMock())
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock())
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock())
    monkeypatch.setattr(story_bay, "install_bay", Mock())

    bot = SimpleNamespace()
    await main_module.boot_game(bot)
    main_module.set_bot.assert_called_once_with(bot)
    main_module.tick.assert_awaited_once_with(bot)
    main_module.start_scheduler.assert_called_once()
    main_module.apply_profile.assert_awaited_once_with(bot)
    scheduler.boot_maintenance.assert_awaited_once()


async def test_boot_game_keeps_going_after_broken_steps(monkeypatch) -> None:
    """Каждый шаг старта опционален: падение проигрывателя кассет, первого
    тика, бэкапа или профиля не имеет права оставить игру без расписания."""
    scheduler = importlib.import_module("app.scheduler")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())
    monkeypatch.setattr(main_module, "tick", AsyncMock(side_effect=RuntimeError("сеть легла")))
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock(side_effect=RuntimeError("нет фото")))
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock(side_effect=OSError("диск")))
    monkeypatch.setattr(story_bay, "install_bay", Mock(side_effect=ValueError("нет каталога")))

    await main_module.boot_game(SimpleNamespace())
    # Планировщик и профиль по-прежнему запущены/проверены — падения не каскад.
    main_module.start_scheduler.assert_called_once()
    main_module.apply_profile.assert_awaited_once()
    scheduler.boot_maintenance.assert_awaited_once()


async def test_boot_game_survives_cache_warmup_failure(monkeypatch) -> None:
    """Прогрев кэшей дня — тоже необязательный шаг."""
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())
    monkeypatch.setattr(main_module, "tick", AsyncMock())
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock())
    scheduler = importlib.import_module("app.scheduler")
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock())
    # Настоящий install_bay() тут запускать нельзя: он глобально подменяет
    # рендеринг/жизненный цикл и обязан быть демонтирован в том же тесте.
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(story_bay, "install_bay", Mock())
    rounds = importlib.import_module("app.rounds")

    async def broken_anchor(_session):
        raise RuntimeError("нет таблицы")

    monkeypatch.setattr(rounds, "get_run_anchor", broken_anchor)
    await main_module.boot_game(SimpleNamespace())
    main_module.start_scheduler.assert_called_once()


async def test_boot_game_yields_to_holder_of_scheduler_lock(monkeypatch) -> None:
    """Второй инстанс на общей базе не должен отработать НИ ОДНОГО шага.

    Инцидент: проснувшийся сервис Render поднял старый билд с тем же
    DATABASE_URL и раскатал историю дней. Лок берётся ДО первого тика, потому
    что стартовый tick тоже пишет в игру (анонсы, финализация, выплаты).
    Отказ не падает: /health обязан продолжать работать, иначе Render убьёт
    процесс и отметит сервис как неполадку.

    Дополнительно проверяется, что boot_game НЕ остаётся в вечной слепой зоне:
    при неудачном acquire запускается фоновая retry-таска, которая перехватит
    лок после его освобождения без ручного рестарта.
    """
    scheduler = importlib.import_module("app.scheduler")
    scheduler_lock = importlib.import_module("app.scheduler_lock")
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())
    monkeypatch.setattr(main_module, "tick", AsyncMock())
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock())
    monkeypatch.setattr(
        main_module, "acquire_scheduler_lock", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(scheduler_lock, "scheduler_lock_held", Mock(return_value=False))
    # Делаем retry-цикл «мгновенным», чтобы он не висел в asyncio.sleep
    # и не предупреждал «Task was destroyed but it is pending!» после теста.
    monkeypatch.setattr(main_module, "_SCHEDULER_LOCK_RETRY_SECONDS", 0)
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock())

    # Сбрасываем глобальную ссылку на случай, если другой тест её оставил.
    main_module._lock_retry_task = None
    try:
        await main_module.boot_game(SimpleNamespace())

        main_module.start_scheduler.assert_not_called()
        main_module.tick.assert_not_awaited()
        scheduler.boot_maintenance.assert_not_awaited()
        main_module.apply_profile.assert_not_awaited()
        # Фоновая retry-таска должна быть создана — это и есть средство
        # восстановления после гонки при деплое.
        assert main_module._lock_retry_task is not None
        assert not main_module._lock_retry_task.done()
    finally:
        task = main_module._lock_retry_task
        main_module._lock_retry_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_boot_game_retry_captures_lock_after_release(monkeypatch) -> None:
    """Фоновая retry-таска должна перехватить лок, как только он освободится,
    и выполнить bootstrap один раз — без повторного create_task и без двойного
    start_scheduler. Это и есть починка инцидента, когда после Render rolling
    deploy бот оставался без tick'ов до ручного рестарта.
    """
    scheduler = importlib.import_module("app.scheduler")
    scheduler_lock = importlib.import_module("app.scheduler_lock")
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())
    monkeypatch.setattr(main_module, "tick", AsyncMock())
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock())
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock())
    monkeypatch.setattr(scheduler_lock, "scheduler_lock_held", Mock(return_value=False))
    monkeypatch.setattr(main_module, "_SCHEDULER_LOCK_RETRY_SECONDS", 0)
    # install_bay() подменяет _plan_and_render в rendering и lifecycle глобально и
    # снимается только через uninstall_bay(). Этот тест доходит до реального
    # шага установки, поэтому без мока monkeypatch он оставлял бы обёртку на
    # модулях и ломал последующие тесты кассет (тест на порядок запускал
    # install_bay по-настоящему).
    monkeypatch.setattr(story_bay, "install_bay", Mock(return_value=True))

    # Сначала лок занят (первый acquire возвращает False), потом свободен.
    acquire_results = iter([False, True])
    acquire_mock = AsyncMock(side_effect=lambda: next(acquire_results))
    monkeypatch.setattr(main_module, "acquire_scheduler_lock", acquire_mock)

    main_module._lock_retry_task = None
    try:
        await main_module.boot_game(SimpleNamespace())

        # Даём retry-циклу дойти до второго acquire и завершить bootstrap.
        task = main_module._lock_retry_task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)

        # acquire был вызван дважды: первый — синхронный в boot_game (False),
        # второй — фоновый в retry-цикле (True).
        assert acquire_mock.await_count == 2
        # Bootstrap-шаги выполнены ровно по одному разу (без задвоения).
        main_module.start_scheduler.assert_called_once()
        main_module.tick.assert_awaited_once()
        main_module.apply_profile.assert_awaited_once()
        scheduler.boot_maintenance.assert_awaited_once()
    finally:
        main_module._lock_retry_task = None


async def test_boot_game_takes_lock_before_first_tick(monkeypatch) -> None:
    """Порядок обязателен: лок берётся раньше любого тика."""
    order: list[str] = []
    scheduler = importlib.import_module("app.scheduler")
    monkeypatch.setattr(main_module, "set_bot", Mock())
    monkeypatch.setattr(main_module, "start_scheduler", Mock())

    async def _lock():
        order.append("lock")
        return True

    async def _tick(_bot):
        order.append("tick")

    monkeypatch.setattr(main_module, "acquire_scheduler_lock", _lock)
    monkeypatch.setattr(main_module, "tick", _tick)
    monkeypatch.setattr(main_module, "apply_profile", AsyncMock())
    monkeypatch.setattr(scheduler, "boot_maintenance", AsyncMock())
    story_bay = importlib.import_module("app.story.bay")
    monkeypatch.setattr(story_bay, "install_bay", Mock())

    await main_module.boot_game(SimpleNamespace())

    assert order == ["lock", "tick"]


async def test_run_webhook_lifecycle(monkeypatch) -> None:
    """Полный жизненный цикл вебхука: роуты, секрет, set_webhook, старт бота,
    затем по сигналу — глушение планировщика, отмена задач и закрытие сессий.
    Накопленные за сон апдейты (голоса, оплаты Stars) не должны выбрасываться."""
    scheduler = importlib.import_module("app.scheduler")
    monkeypatch.setattr(settings, "webhook_base_url", "https://way.example")
    monkeypatch.setattr(settings, "webhook_secret", "hook-secret")
    monkeypatch.setattr(main_module, "boot_game", AsyncMock())
    monkeypatch.setattr(main_module, "close_http_client", AsyncMock())
    monkeypatch.setattr(scheduler, "shutdown_scheduler", Mock())

    class _Handler:
        def __init__(self, dispatcher=None, bot=None, secret_token=None) -> None:
            self.secret_token = secret_token

        def register(self, app, path: str) -> None:
            self.app = app
            self.path = path

    handlers: list[_Handler] = []

    def _handler_factory(**kwargs):
        handler = _Handler(**kwargs)
        handlers.append(handler)
        return handler

    monkeypatch.setattr(main_module, "SimpleRequestHandler", _handler_factory)
    monkeypatch.setattr(main_module, "setup_application", Mock())

    started: list[str] = []

    class _Site:
        def __init__(self, runner, host, port) -> None:
            started.append(f"{host}:{port}")

        async def start(self) -> None:
            return None

    monkeypatch.setattr(main_module.web, "TCPSite", _Site)

    def _install(stop: asyncio.Event) -> None:
        # Сигнала в тесте нет, поэтому имитируем остановку сразу после старта.
        stop.set()

    monkeypatch.setattr(main_module, "_install_stop_handlers", _install)

    client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr(main_module, "get_http_client", Mock(return_value=client))

    bot = SimpleNamespace(
        set_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    dispatcher = SimpleNamespace()
    await asyncio.wait_for(main_module.run_webhook(bot, dispatcher), timeout=15)

    assert handlers[0].secret_token == "hook-secret"
    assert handlers[0].path == "/webhook"
    assert started, "сайт вебхука не поднялся"
    # drop_pending_updates=False: оплаты и голоса, накопленные за сон, обязаны дойти.
    assert bot.set_webhook.await_args.kwargs["drop_pending_updates"] is False
    assert bot.set_webhook.await_args.args[0] == "https://way.example/webhook"
    scheduler.shutdown_scheduler.assert_called_once()
    bot.session.close.assert_awaited_once()
    main_module.close_http_client.assert_awaited_once()
    # Старт игры планируется отдельной задачей, а не блокирует подъём сервера.
    main_module.boot_game.assert_called_once_with(bot)


async def test_run_webhook_without_public_url_skips_set_webhook(monkeypatch) -> None:
    """Локальный запуск на polling: публичного URL нет — TelegramWebhook не
    выставляем, но сайт с /health всё равно поднимаем."""
    scheduler = importlib.import_module("app.scheduler")
    monkeypatch.setattr(settings, "webhook_base_url", "")
    monkeypatch.setattr(settings, "webhook_secret", "")
    monkeypatch.setattr(main_module, "boot_game", AsyncMock())
    monkeypatch.setattr(main_module, "close_http_client", AsyncMock())
    monkeypatch.setattr(scheduler, "shutdown_scheduler", Mock())
    monkeypatch.setattr(main_module, "SimpleRequestHandler", lambda **kwargs: SimpleNamespace(register=lambda app, path: None))
    monkeypatch.setattr(main_module, "setup_application", Mock())
    monkeypatch.setattr(
        main_module.web,
        "TCPSite",
        lambda runner, host, port: SimpleNamespace(start=AsyncMock()),
    )
    monkeypatch.setattr(main_module, "_install_stop_handlers", lambda stop: stop.set())
    client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr(main_module, "get_http_client", Mock(return_value=client))

    bot = SimpleNamespace(set_webhook=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    await asyncio.wait_for(main_module.run_webhook(bot, SimpleNamespace()), timeout=15)
    bot.set_webhook.assert_not_awaited()
    bot.session.close.assert_awaited_once()


async def test_main_refuses_to_start_on_broken_config(monkeypatch) -> None:
    """Fail-fast: очевидные ошибки конфигурации видны сразу, а не через часы
    бездействия — и БД/бот при этом даже не создаются."""
    made: list[str] = []

    class _Path:
        def __init__(self, value: str) -> None:
            self.value = value

        def mkdir(self, **_kwargs) -> None:
            made.append(self.value)

    monkeypatch.setattr(main_module, "Path", _Path)
    monkeypatch.setattr(main_module, "validate_config", Mock(return_value=["BOT_TOKEN пуст"]))
    init_db = AsyncMock()
    monkeypatch.setattr(main_module, "init_db", init_db)
    with pytest.raises(RuntimeError, match="Критичные проблемы конфигурации"):
        await main_module.main()
    init_db.assert_not_awaited()
    # Каталоги создаются ДО проверки конфига: диск должен быть готов всегда.
    assert "data" in made


async def test_main_warns_but_starts_on_typo_in_env_name(monkeypatch, caplog) -> None:
    """Опечатка в имени переменной — предупреждение, а не повод не стартовать.

    validate_config роняет бота, и это правильно для пустого токена. Но
    подозрение на опечатку (PAYOUT_FEE_GARM вместо _GRAM) — не поломка
    конфигурации: процесс может подняться и работать, просто с дефолтом.
    Раньше такой случай был не виден вовсе — pydantic молча игнорирует лишние
    переменные, и прод уезжал с комиссией, которую никто не выбирал.
    """
    monkeypatch.setenv("PAYOUT_FEE_GARM", "0.002")
    monkeypatch.setattr(main_module, "Path", lambda value: SimpleNamespace(mkdir=Mock()))
    monkeypatch.setattr(main_module, "validate_config", Mock(return_value=[]))
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    bot = SimpleNamespace(delete_webhook=AsyncMock())
    monkeypatch.setattr(main_module, "create_bot", AsyncMock(return_value=bot))
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    monkeypatch.setattr(main_module, "build_dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(main_module, "boot_game", AsyncMock())
    monkeypatch.setattr(settings, "webhook_base_url", "")
    run_webhook = AsyncMock()
    monkeypatch.setattr(main_module, "run_webhook", run_webhook)

    with caplog.at_level("WARNING", logger="app.main"):
        await main_module.main()

    # Старт состоялся — предупреждение не превратилось в отказ.
    dispatcher.start_polling.assert_awaited_once_with(bot)
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("PAYOUT_FEE_GARM" in message for message in warned)


def test_env_typo_detection_ignores_unrelated_variables(monkeypatch) -> None:
    """Проверка целится в опечатки, а не перечисляет всё лишнее в окружении.

    В окружении Render/CI/локальной машины законно лежит сотня чужих
    переменных (PATH, PROCESSOR_*, CI_*). Если бы проверка ругалась на них
    всех, её перестали бы читать — и тогда она перестала бы ловить настоящее.
    """
    monkeypatch.delenv("PAYOUT_FEE_GARM", raising=False)
    monkeypatch.setenv("SOME_COMPLETELY_UNRELATED_VAR", "1")
    monkeypatch.setenv("PATH_TO_SOMEWHERE", "1")
    assert main_module.warn_config() == []

    monkeypatch.setenv("PAYOUT_FEE_GARM", "0.002")
    warnings_seen = main_module.warn_config()
    assert len(warnings_seen) == 1
    assert "PAYOUT_FEE_GRAM" in warnings_seen[0]


def test_env_typo_detection_is_not_a_startup_problem(monkeypatch) -> None:
    """Подозрение на опечатку не попадает в validate_config (он блокирует)."""
    monkeypatch.setenv("PAYOUT_FEE_GARM", "0.002")
    problems = main_module.validate_config()
    assert not any("PAYOUT_FEE_GARM" in problem for problem in problems)


async def test_main_runs_webhook_mode(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "validate_config", Mock(return_value=[]))
    monkeypatch.setattr(main_module, "Path", lambda value: SimpleNamespace(mkdir=Mock()))
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    bot = SimpleNamespace()
    monkeypatch.setattr(main_module, "create_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(main_module, "build_dispatcher", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(settings, "webhook_base_url", "https://way.example")
    monkeypatch.setattr(settings, "webhook_secret", "hook-secret")
    run_webhook = AsyncMock()
    monkeypatch.setattr(main_module, "run_webhook", run_webhook)
    await main_module.main()
    run_webhook.assert_awaited_once()


async def test_main_runs_polling_mode(monkeypatch) -> None:
    """Локальный режим: старые вебхуки сносятся с сохранением очереди апдейтов,
    игра бутится, дальше — long polling."""
    monkeypatch.setattr(main_module, "validate_config", Mock(return_value=[]))
    monkeypatch.setattr(main_module, "Path", lambda value: SimpleNamespace(mkdir=Mock()))
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    bot = SimpleNamespace(delete_webhook=AsyncMock())
    monkeypatch.setattr(main_module, "create_bot", AsyncMock(return_value=bot))
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    monkeypatch.setattr(main_module, "build_dispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(main_module, "boot_game", AsyncMock())
    monkeypatch.setattr(settings, "webhook_base_url", "")
    run_webhook = AsyncMock()
    monkeypatch.setattr(main_module, "run_webhook", run_webhook)
    await main_module.main()
    run_webhook.assert_not_awaited()
    # drop_pending_updates=False: накопленные за сон голоса должны дойти.
    assert bot.delete_webhook.await_args.kwargs["drop_pending_updates"] is False
    main_module.boot_game.assert_awaited_once_with(bot)
    dispatcher.start_polling.assert_awaited_once_with(bot)


def test_entrypoint_swallows_keyboard_interrupt(monkeypatch) -> None:
    """Ctrl+C в контейнере — штатная остановка, а не падение с трейсбеком."""

    def _fake_run(coro, *_args, **_kwargs):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", _fake_run)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        runpy.run_module("app.main", run_name="__main__")


async def test_signal_handlers_are_installed_or_skipped() -> None:
    """Договорённость с ОС: либо оба сигнала подхвачены, либо обработчики не
    регистрируются вовсе — третий путь (исключение наружу) недопустим."""
    stop = asyncio.Event()
    main_module._install_stop_handlers(stop)
    assert signal.SIGINT is not None
