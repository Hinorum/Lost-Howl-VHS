import ssl
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Сеть строго перечислением, а не строкой. Со строкой любая опечатка
# (testnt, mainnet-test, dev) молча разрешалась как mainnet — с настоящей
# 24-словной мнемоникой, real-адресом казначея и выплатами игрокам. Проверки
# при старте сеть не ловили: is_testnet это `== "testnet"`, то есть всё
# остальное — mainnet. С Literal опечатка валит импорт конфига сразу.
TonNetwork = Literal["mainnet", "testnet"]

LIBPQ_QUERY_KEYS = {
    "sslmode",
    "channel_binding",
    "gssencmode",
    "target_session_attrs",
}


def sqlalchemy_url(url: str) -> str:
    raw = url.strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://") and "+asyncpg" not in raw:
        raw = "postgresql+asyncpg://" + raw[len("postgresql://") :]
    parsed = urlparse(raw)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() not in LIBPQ_QUERY_KEYS]
    # Не пересобираем URL без изменений: urlunparse теряет «//» у схем без netloc (например, sqlite).
    if len(kept) == len(pairs):
        return raw
    return urlunparse(parsed._replace(query=urlencode(kept)))


def postgres_connect_args(url: str) -> dict:
    converted = sqlalchemy_url(url)
    if not converted.startswith("postgresql"):
        return {}
    # Всегда создаём таблицы в public: при подмене/сбросе Postgres-ресурса
    # Render search_path новой базы может не содержать схемы, и любой DDL
    # падал бы «no schema has been selected to create in».
    args: dict = {"server_settings": {"search_path": "public"}}
    host = urlparse(converted).hostname or ""
    if host not in {"localhost", "127.0.0.1"}:
        # Кастомный корневой CA (например, Supabase: сертификаты пулера
        # подписаны внутренним Root CA, не входящим в системное хранилище).
        # Если DATABASE_CA не задан, но мы подключаемся к pooler.supabase.com —
        # подхватываем сертификат из репозитория автоматически.
        ca_path = settings.database_ca
        if not ca_path and host.endswith(".pooler.supabase.com"):
            default_ca = Path(__file__).resolve().parents[1] / "certs" / "supabase-root.pem"
            if default_ca.exists():
                ca_path = str(default_ca)
        if ca_path:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=str(ca_path))
            args["ssl"] = ctx
        else:
            args["ssl"] = True
    return args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    admin_ids: str = ""
    # Час закрытия голосования (UTC): в этот же час — мгновенный подсчёт и
    # итоги сразу; следующий день дофинализируется фоном чуть позже (эпилог,
    # инлайн-генерация нового дня и пост).
    day_close_hour_utc: int = 11
    database_url: str = "sqlite+aiosqlite:///./data/the_way.db"
    # Явное разрешение жить на SQLite при непустых ADMIN_IDS. Нужно локально и в
    # тестах. На деплое с настоящими админами SQLite означает, что
    # pg_try_advisory_lock не работает (scheduler_lock.py отдаёт True «а как же
    # один процесс?»), то есть гарантия единственного инстанта исчезает ровно
    # тогда, когда нужна: два инстанса делят кассу и рассылают историю дважды.
    # Плюс база лежит на эфемерном диске и пропадает вместе с контейнером.
    allow_sqlite: bool = False
    # Путь к PEM-файлу корневого CA для Postgres (Supabase пулер). Пусто — стандартные CA.
    database_ca: str = ""
    timezone: str = "Europe/Moscow"
    media_dir: str = "./media/generated"

    ton_enabled: bool = False
    ton_network: TonNetwork = "mainnet"
    treasury_address: str = ""
    treasury_mnemonic: str = ""
    treasury_testnet_address: str = ""
    treasury_testnet_mnemonic: str = ""
    ton_api_base: str = "https://tonapi.io"
    ton_api_base_testnet: str = "https://testnet.tonapi.io"
    ton_api_key: str = ""
    # Версия контракта казначея: auto (детект по адресу), v4r2 или v5r1.
    treasury_wallet_version: str = "auto"
    # Свежий JSON-конфиг лайтсерверов для pytoniq (ADNL/UDP). Встроенный
    # конфиг тестнета периодически мёртв («have no alive peers»): сюда
    # подставляется рабочий URL, например официальный
    # https://ton.org/testnet-global.config.json
    liteserver_config_url: str = ""
    # Резервный источник истории переводов (Toncenter API v3): включается
    # автоматически, когда TonAPI лжёт (404 истории при живом кошельке) или лежит.
    toncenter_api_base: str = "https://toncenter.com"
    toncenter_api_base_testnet: str = "https://testnet.toncenter.com"
    toncenter_api_key: str = ""
    # Нижняя граница ставки; верхней нет — «кит» ограничен только своим кошельком.
    stake_min_ton: float = 0.5
    # Распределение фонда дня (в сумме со ставками победителей — 100%):
    # BASE: 96% делят поставившие на верный путь пропорционально, 1% копится
    # в Фонд Стаи (накопительный, раздача вручную хранителем), 2% капают в
    # копилку недели (в понедельник её делят топ-3 по верным ответам),
    # 0,5% — хранителю, 0,5% — в копилку месяца (/top). Проценты в долях.
    # REFERRAL: с каждой ПОДТВЕРЖДЁННОЙ ставки игрока, пришедшего по личной
    # ссылке, доля referral_pct% не попадает в пул победителей, а копится в
    # копилку пригласившего (при всех приведённых ставках пул = 96% − 1% = 95%;
    # день без приведённых ставок делится как раньше — 96% победителям).
    owner_rake_pct: float = 0.5
    leaderboard_rake_pct: float = 0.5
    weekly_pot_pct: float = 2.0
    pack_fund_pct: float = 1.0
    referral_pct: float = 1.0
    # Недельный лидерборд: минимальное число дней голосования за неделю,
    # чтобы претендовать на призовое место (анти-мультиаккаунт), и доли
    # мест «1-е,2-е,3-е» в процентах: сильнейший забирает больше (50/30/20).
    # Дополнительно для выплаты нужна хотя бы одна ставка в этой неделе
    # (и в каждом новом периоде — заново). Ничья: выше тот, кто поставил
    # больше Gram за период, при равенстве ставок — кто раньше нажал Claim
    # (кнопка в /start), далее — меньший player_id.
    weekly_min_days: int = 4
    weekly_prize_pcts: str = "50,30,20"
    # Анти-гринд лидербордов: сколько верных сцен в периоде засчитывается
    # одному игроку. 0 = без потолка (прежнее поведение). Способ, отличный от
    # «минимум дней», чтобы платные переголосования не давали сколь угодно
    # большой перевес по «верности»: дольше всего (за и порог потолка) все
    # сравниваются как равные и решают реальная игровая частота и ставки.
    leaderboard_correct_cap: int = 0
    # Сглаживание дисперсии месячной копилки: вместо «забрал всё сильнейший»
    # платим топ-K игроков месячного лидерборда, доли заданы весами (в сотых
    # долях, см. weekly). 1 = прежнее «забрал всё» (ровно один получатель).
    # Пример: "60,30,10" → три получателя с весами 60%/30%/10%. По умолчанию
    # месяц живёт той же механикой, что и неделя: топ-3 с весами 50/30/20.
    monthly_prize_top_k: int = 3
    monthly_prize_weights: str = "50,30,20"
    # Претензии на призовые места лидербордов: кнопка Claim в /start.
    # Решает только ничьи по (верность, вклад Gram) — победитель ничьей —
    # кто раньше нажал Claim в течение периода. 0/False выключает кнопку
    # (такие ничьи решаются меньшим player_id).
    leaderboard_claim_enabled: bool = True
    # Сколько часов даётся на подачу Claim после ничьей в лидерборде.
    # По истечении дедлайма заявки закрываются и выплата идёт по тайбрейку
    # (ранний Claim > тихий > меньший player_id). 0 = без дедлайма (ждём
    # вечно, пока все tied заявятся).
    claim_window_hours: int = 48
    # «Ставки решают исход дня»: победитель определяется суммами подтверждённых
    # ставок (Gram) на каждом пути — закон дня (majority/minority/median)
    # применяется к банкам путей, а не к бесплатным голосам. Путь без грамма
    # участвует в подсчёте как 0. Бесплатные голоса при этом НЕ влияют на исход:
    # они питают только лидерборд (score, correct_picks, стрики, копилки).
    # День, на который не поставлено ни одного грамма, решается голосами
    # (fallback: мир выбрал «сердцем» — иначе победителя не вывести).
    #
    # false = исход дня всегда по бесплатным голосам (прежний режим). Рычаг
    # аварийный: держать его в true — иначе README обещает флаг, которого нет.
    winner_by_stakes: bool = True
    disputes_enabled: bool = True
    owner_wallet_address: str = ""
    stake_confirm_seconds: int = 40
    # Столько раз зависшая выплата ретраится, прежде чем окончательно встать в failed.
    payout_max_attempts: int = 5
    # Оценка комиссии сети на один исходящий перевод (Gram). Вычитается из
    # призового пула ЗАРАНЕЕ и пропорционально доле каждого победителя:
    # приз приходит «чистыми», казначей не финансирует газ из своего остатка,
    # и очередь выплат не встаёт на середине дня с «недостаточно средств».
    #
    # Значение обязано совпадать с PAYOUT_FEE_GRAM в render.yaml: прод видит
    # рычаг только оттуда, а расхождение означало бы, что один и тот же газ
    # считается в коде и на деплое по-разному. Равенство проверяет
    # tests/test_money_config_parity.py. Само число берётся замером на testnet
    # (5–10 переводов A→B, среднее × 1.15, вверх) — см. MAINNET_READINESS.md.
    payout_fee_gram: float = 0.002
    # Перевод меньше этой суммы (Gram) не создаётся вовсе: комиссия съела бы
    # большую его часть. Пыльные доли капают в копилку недели — видно в итогах.
    min_payout_gram: float = 0.02
    # Порог (Gram), с которого накопленная реферальная награда превращается в
    # реальный перевод; ниже — копится дальше. Газ сети стоит ~payout_fee_gram
    # на перевод, поэтому микроперевод «поощрения» на 0.01 Gram терял бы
    # пятую часть на комиссии — платим крупой из накопленного.
    referral_min_payout_gram: float = 0.5
    # Нормализация газа на ВОЗВРАТАХ ставок. Обычно с каждого возврата держится
    # плоская комиссия payout_fee_gram (одна и та же и для мелкой, и для крупной
    # ставки — регрессивно: мелкий возврат теряет большую долю). Если задать
    # refund_fee_ratio > 0 (доля, напр. 0.01 = 1%), возврат = ставка × (1 − доля):
    # комиссия становится пропорциональной сумме и не съедает микро-возвраты
    # целиком. 0 = прежняя плоская комиссия payout_fee_gram.
    refund_fee_ratio: float = 0.0
    # Потолок одной попытки вещания перевода (сек): зависший лайтсервер не
    # имеет права замораживать весь цикл выплат — таймаут = обычный ретрай.
    payout_send_timeout_seconds: int = 90
    # Потолок пробы баланса напрямую с liteserver (сек) — третий источник,
    # когда TonAPI и Toncenter молчат. Это не отправка, а РЕШЕНИЕ «слать или
    # ждать», поэтому проба обязана быть короткой: в холодном старте в неё же
    # уходит поднятие пула лайтсерверов, и зависшая проба не должна
    # замораживать цикл дольше, чем стоит сама отправка.
    payout_balance_probe_timeout_seconds: int = 20
    # Через сколько секунд после вещания выплата считается «потерянной», если
    # memo так и не появилось в блокчейне: метка bcast: значит лишь «лайтсервер
    # принял запрос», а не «транзакция в блоке». Дольше этого окна строка
    # возвращается в очередь (dispatch отправит заново — анти-дубль по memo
    # не сработает, потому что перевода в цепочке нет).
    payout_confirm_timeout_seconds: int = 1200
    # Пауза между переводами пачки в HTTP-канале (сек): следующий seqno можно
    # подписывать, только когда предыдущий перевод подтверждён в блоке. Иначе
    # два внешних месседжа вплотную (приз+рейк, приз+возвраты) уходят в сеть
    # одновременно, и второй молча отбрасывается («ok» провайдера == «мемпул
    # принял», а не «транзакция в блоке») — потерю потом поймает лишь 2-часовая
    # сверка. Ждём memo предыдущего перевода в истории казначея до этого окна.
    payout_batch_confirm_seconds: int = 15
    # Сверка с историей казначея НЕ ограничена одной страницей (128 tx): memo
    # «уже отправленного» в длинной очереди легко выпадает из последних 128
    # исходящих, и сверка ошибочно решает «перевода нет» → повторная отправка
    # задвоила бы платёж. Ходим вглубь истории на payout_reconcile_history_seconds
    # (сек от текущего момента), но не дальше payout_reconcile_max_pages страниц.
    payout_reconcile_history_seconds: int = 604800  # 7 суток
    payout_reconcile_max_pages: int = 12
    # Сколько суток держать запись сбойной транзакции в stuck-списке
    # watcher_state (ton_watch_stuck_tx). Врачующийся вход помечается
    # reported и больше не тревожит — без ротации такие записи висят вечно.
    stuck_retention_days: int = 7
    # Как часто (сек) авто-лечение повторяется для брошенных сбойных переводов:
    # re-process идемпотентен (маркеры refund:/ledger:), повтор дёшев, а баг
    # версии мог быть починен деплоем — пока запись держится в stuck, у неё есть
    # шанс исцелиться без ручного разбора.
    stuck_heal_recheck_seconds: int = 600
    # Частота цикла наблюдателя входящих (сек): каждый цикл — один запрос
    # в индексатор (TonAPI, фолбэк Toncenter). 30 = подтверждение ставок и
    # кошельков в течение ~полуминуты; 60/120 = вдвое-вчетверо меньше запросов
    # к квоте провайдера (в простое цикл ≈ 1 запрос).
    ton_watch_interval_seconds: int = 30
    # Глубина скана входящих переводов казначея за один цикл наблюдателя:
    # до watch_max_pages страниц по watch_page_limit транзакций. Курсор делает
    # покрытие кумулятивным — после простоя хвост догоняется за пару циклов.
    # Если окно глубже бюджета (backlog), watcher сам удваивает бюджет до
    # x8 и пишет тревогу: молчащий сдвиг курсора здесь уже невозможен.
    watch_page_limit: int = 200
    watch_max_pages: int = 50
    # Окно перекрытия при чтении курсора watcher'а (сек): курсор читается на N
    # секунд раньше последнего обработанного utime, потому что у индексатора
    # перевод «той же секунды» может появиться с задержкой. Больше значение —
    # надёжнее против reorg (глубже окно перечитки истории), но дороже каждый
    # цикл (пачка повторно перечитывает окно; идемпотентность держится на
    # tx_hash). Увеличь, если сеть ловит частые реорганизации блоков.
    watch_cursor_overlap_seconds: int = 90
    # Авто-возврат только свежим переводам: после сброса базы курсор watcher'а
    # обнуляется и вся история казны перечитывается заново — без этого лимита
    # старый спам-хлам вечно превращается в новые dead-letter возвраты.
    watch_refund_max_age_days: int = 14
    # Переводы дешевле этой суммы (Gram) полностью НЕ создают авто-возврат:
    # газ возврата (payout_fee_gram) стоит в разы дороже самого перевода, и
    # микро-спам превращался бы в убыточные dead-letter выплаты. Пыль остаётся
    # в казне, игроку ничего не сообщается (шум для ботов).
    refund_min_gram: float = 0.05
    # Потолок авто-возвратов: без него любой перевод в казну, который не стал
    # ставкой, порождает ИСХОДЯЩУЮ транзакцию. Спам мелких переводов (напр. по
    # 0.06 Gram) заставлял казну платить газ за каждый: 10 000 переводов — это
    # 10 000 исходящих tx по ~payout_fee_gram, то есть сжигание газа в
    # десятки раз дороже присланного. Адрес казны публичен, стоимость атаки
    # определяется только суммой переводов.
    #
    # Перешагнувший лимит перевод НЕ возвращается автоматически: он уходит в
    # ledger как «in:refund:capped» и ждёт ручного возврата хранителем через
    # /incoming + /adjust. Деньги не пропадают — они видны в сверке.
    # 0 = лимита нет (осознанный отказ от защиты).
    refund_max_per_sender_day: int = 5
    refund_max_total_day: int = 50
    # Зеркало казны: независимая копия истории активного кошелька, на которой
    # сверка «баланс = Σ движений» сходится в ноль без допуска на газ.
    # Ограничение страниц за цикл (по 100 tx): бутстрап десятков тысяч
    # транзакций раскатывается фоново, не выжигая квоту индексатора.
    treasury_mirror_max_pages_per_sync: int = 40
    # Частота цикла зеркала (сек): после бутстрапа цикл ≈ 1 запрос истории +
    # 1 запрос баланса — дёшево, а «тютелька в тютельку» контролируется часто.
    treasury_mirror_interval_seconds: int = 180

    # Личные дубликаты рассылок: подписанные игроки (dm_subscribed в /start)
    # получают итоги дня, новый день, вечерний пост и прочие
    # анонсы в личку бота параллельно чатам, где бот админ. Отписка — кнопкой
    # в /start. Выключение флага возвращает прежнее поведение (только группы).
    player_dm: bool = True

    # Мир игры. Название попадает в тексты бота. Можно поменять из Environment,
    # не трогая код: получится другой мир с той же механикой.
    world_name: str = "LOST HOWL"

    # Каталог библиотеки сюжетных кассет (*.json, стандарт
    # docs/lost_howl_prompts.md). Пусто — библиотека в app/story/cassettes/.
    # Это КЭШ-зеркало: источник правды — таблица story_cassettes (app/story/store.py),
    # потому что на Render диск эфемерен и правки /panel не переживали рестарт.
    story_cassettes_dir: str = ""
    # Выключатель базового хранения кассет: false — только файлы (локальная
    # разработка без БД). При недоступной базе вызовы сами деградируют в диск.
    story_cassettes_db: bool = True

    revote_enabled: bool = True
    revote_stars: int = 25
    revote_ton: float = 0.1
    # Период само-пинга /health: держит free plan Render от засыпания, чтобы
    # день открывался по UTC-сетке. 0 — выключить.
    self_ping_seconds: int = 600
    # Окно догона для восстановителей по маркерам (awards_at / results_at /
    # announced_at / payouts_finalized): ловят только дни, закрытые (или
    # открытые) НЕДАВНО — за последние N часов. Без этого маркер «NULL =
    # не обработано» после миграции без бэкфилла трактует ВСЮ историю игры
    # как «недоставленную»: первый тик после деплоя рассылал заново все
    # закрытые дни (27 постов флудом), переначислял очки и пересоздавал
    # выплаты за старые дни. Догон нужен для крашей и рестартов (час-дни),
    # а не для археологии. 72 ч перекрывает деплой+рестарт с запасом.
    catchup_window_hours: int = 72
    port: int = 10000
    webhook_base_url: str = ""
    webhook_secret: str = ""
    # Токен доступа к /health (мониторинг). Пусто — снимок (очередь выплат,
    # возраст тика, watcher) отдаётся, только если health_require_token=False.
    # Аудит застал /health открытым, потому что токен «забыли» поставить:
    # молчаливо публичный снимок с данными недопустим.
    health_token: str = ""
    # Закрывать /health при пустом токене (fail closed). True по умолчанию:
    # инстанс без HEALTH_TOKEN отвечает 401, и Render помечает его нездоровым —
    # тревога конфигурации вместо открытого операционного снимка. Чек жизни
    # Render на free-плане токен не передаёт: healthCheckPath в render.yaml уже
    # ходит с ним в query (см. P0-1).
    health_require_token: bool = True
    render_external_url: str = ""
    render_external_hostname: str = ""
    # Личные приглашения (?start=ref_<id>_<токен>): секрет подписывает токен,
    # чтобы нельзя было подставить чужой id в ссылку. Пусто — рефералки выключены.
    referral_secret: str = ""
    # Username бота без «@» (t.me/<username>?start=...). Пусто — бот запросит
    # get_me при первом /invite и закэширует; если и это не выйдет, ссылки
    # не строятся до появления значения.
    bot_username: str = ""

    # === Схема базы ===
    # Осиротевшие NOT NULL-колонки без DEFAULT (удалённые механики) ломали
    # INSERT новых дней. Раньше init_db дропал их на каждом старте автоматически:
    # при откате или канареечном деплое старая версия могла снести колонку,
    # нужную новой. Теперь дроп — разовая осознанная операция: после деплоя
    # версии, удалившей колонку из моделей, подними флаг на один запуск и
    # верни в false. false (по умолчанию) — колонки не трогаем, найденные
    # сироты только логируются в предупреждениях.
    drop_orphan_columns: bool = False

    @property
    def admin_id_set(self) -> set[int]:
        ids: set[int] = set()
        for part in self.admin_ids.replace(" ", "").split(","):
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def async_database_url(self) -> str:
        return sqlalchemy_url(self.database_url)

    @property
    def public_base_url(self) -> str:
        if self.webhook_base_url:
            return self.webhook_base_url.rstrip("/")
        if self.render_external_url:
            return self.render_external_url.rstrip("/")
        if self.render_external_hostname:
            return f"https://{self.render_external_hostname.rstrip('/')}"
        return ""

    @property
    def use_webhook(self) -> bool:
        return bool(self.public_base_url)

    @property
    def is_testnet(self) -> bool:
        return self.ton_network.strip().lower() == "testnet"

    def liteserver_network_mismatch(self) -> str | None:
        """Конфиг лайтсерверов не про ту сеть, что выбрана — вернуть причину.

        LITESERVER_CONFIG_URL подставляется в pytoniq с приоритетом над выбором
        сети, поэтому конфиг testnet при TON_NETWORK=mainnet означает, что
        get_seqno() читает seqno mainnet-адреса на testnet-нодах. Выплата
        подписывается неверным seqno, result == 1, строка помечается sent,
        пять раз ретраится и уходит в dead-letter: игрокам не платят вообще,
        а /health зелёный. Пересечение сети с сетью ловим на старте.
        """
        url = (self.liteserver_config_url or "").strip().lower()
        if not url:
            return None
        url_looks_testnet = "testnet" in url or "sandbox" in url
        if self.is_testnet and not url_looks_testnet:
            return (
                "TON_NETWORK=testnet, а LITESERVER_CONFIG_URL не похож на тестнетовый "
                f"({self.liteserver_config_url})"
            )
        if not self.is_testnet and url_looks_testnet:
            return (
                "TON_NETWORK=mainnet, а LITESERVER_CONFIG_URL указывает на тестнет "
                f"({self.liteserver_config_url})"
            )
        return None

    @property
    def active_treasury_address(self) -> str:
        return self.treasury_testnet_address if self.is_testnet else self.treasury_address

    @property
    def active_treasury_mnemonic(self) -> str:
        return self.treasury_testnet_mnemonic if self.is_testnet else self.treasury_mnemonic

    @property
    def active_ton_api_base(self) -> str:
        return self.ton_api_base_testnet if self.is_testnet else self.ton_api_base

    @property
    def active_toncenter_api_base(self) -> str:
        return self.toncenter_api_base_testnet if self.is_testnet else self.toncenter_api_base


settings = Settings()
