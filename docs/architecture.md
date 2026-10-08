# Архитектура The Ways / LOST HOWL

> Цель документа — за 15 минут дать новому контрибьютору понимание, **где что
> живёт** и **как данные текут от нажатия кнопки в Telegram до перевода Gram
> в блокчейне**. После прочтения должно быть ясно, в каком модуле искать
> баг и куда добавлять новую механику.

Документ высокого уровня: детали API функций и инвариантов модулей — в
docstring'ах и в `README.md`.

---

## 1. Карта пакетов

```
app/
├── main.py              # Точка входа: aiohttp + aiogram dispatcher + scheduler + self-ping
├── config.py            # Pydantic-settings: все ключи .env + validate_config() на старте
├── models.py            # SQLAlchemy 2.x: 13 таблиц (Player, Round, Card, Stake, Payout, ...)
├── db.py                # Async-движок, init_db(), _migrate(), легаси-конвергенция
├── scheduler.py         # APScheduler: cron + interval джобы
├── http_utils.py        # Общий httpx.AsyncClient + http_post_with_retry
├── async_utils.py       # Маленькие хелперы над asyncio
│
├── core/                # Контракты домена
│   └── registry.py      # Каталог плагинов (банки, копилки, переводы)
│
├── handlers/            # Telegram-слой (aiogram Router-ы)
│   ├── bootstrap.py     # /start, /help, deep-link, регистрация
│   ├── common.py        # Базовые фильтры, локализация, форматтеры
│   ├── player.py        # Голосование: callback «Путь I/II/III» → votes
│   ├── topup.py         # Пополнение кошелька через бот
│   ├── wallet.py        # Привязка TON-кошелька, балансы, история ставок
│   ├── payout.py        # Очередь выплат (UI: «запросить выплату», «история»)
│   ├── panel.py         # Пульт игрока (навигация по чату)
│   ├── admin.py         # Админ-команды: /advance, /refund, /heal, /cassette, ...
│   └── fallback.py      # Любой неподходящий апдейт → fallback handler
│
├── rounds/              # Движок игрового дня
│   ├── time.py          # UTC-окна (open/close), battle-clock
│   ├── lifecycle.py     # open_day → tally → close, основной state-machine
│   ├── materialization.py # Материализация дня из шаблона кассеты
│   ├── rendering.py     # Генерация постов дня (текст + клавиатура)
│   ├── voting.py        # Подсчёт голосов/банков, применение закона дня
│   ├── queries.py       # Чтение раундов/карточек/голосов (read-only)
│   ├── pot.py           # Банки раунда (sum stake на путь, rake)
│   ├── anchor.py        # Привязка дня к блоку TON (rule_entropy)
│   └── narrative.py     # Канон дня: какой текст уцелевшей карты печатать
│
├── story/               # Сюжетный слой — изолирован от экономики и блокчейна
│   ├── schema.py        # Контракт кассеты: Pydantic + валидаторы
│   ├── bay.py           # Проигрыватель: install_bay() патчит rendering._plan_and_render
│   ├── editor.py        # Dry-run редактирование кассеты (preview)
│   └── cassettes/       # Библиотека кассет (4 шт. на сегодня)
│
├── ton_pay/             # Диспетчер выплат (пакет: листы + оркестрация в __init__)
├── ton_watch/           # Watcher входящих: мемпул → Stake/WatcherState
│   │                    #   __init__ — оркестрация, листы: sources, state,
│   │                    #   ledger, refunds, notify, revote
├── ton_codec.py         # Чистые кодеки: api_headers, extract_comment, norm_tx_hash
├── ton_utils.py         # TON-арифметика: nano/gram конверсии, форматы адресов
├── treasury_mirror.py   # Цепочечно-подтверждённое зеркало казны + ежедневная сверка
│
├── stakes.py            # Короткие операции со стейками (insert/refund)
├── payments.py          # Локальная таблица исходящих (pending/sending/sent/failed)
├── tally.py             # Подсчёт голосов с учётом закона дня и тай-брейка
├── voting.py            # Запись голоса (insert с уникальностью round×player)
├── streaks.py           # Счётчики «верных путей» (Следы)
├── weeks.py             # Копилки недели/месяца (dust roll-up)
├── leaderboard.py       # Топ игроков (бесплатные голоса + Следы)
├── referrals.py         # Реферальная копилка: пассивный доход пригласившего
├── disputes.py          # Арбитраж: ручное разрешение спорных дней
├── profile.py           # Карточка игрока (имя, витрина, badge)
├── backups.py           # Ротация локальных бэкапов SQLite
├── broadcast.py         # Рассылка сообщений по списку чатов
├── ops.py               # notify_admins, alert_guarded, health-эндпоинт
└── style.py             # Эмодзи/маркдаун-форматирование выводов
```

---

## 2. Карта данных: от апдейта до блокчейна

```
        Telegram                          TON                              SQLite
          │                                │                                 │
          │  /start или кнопка «Путь II»   │                                 │
          ▼                                │                                 │
   ┌─────────────┐                         │                                 │
   │  bot.polling│                         │                                 │
   │  / webhook  │                         │                                 │
   └──────┬──────┘                         │                                 │
          │ aiogram Update                 │                                 │
          ▼                                │                                 │
   ┌─────────────┐                         │                                 │
   │ handlers/   │                         │                                 │
   │ player.py   │                         │                                 │
   │ topup.py    │────► votes ─────────────┼───────────────────────────►    │
   │ wallet.py   │────► stakes ────────────┼───────────────────────────►    │
   │ admin.py    │                         │                                 │
   └─────────────┘                         │                                 │
                                           │                                 │
          ┌────────────────────────────────┤                                 │
          │  scheduler.py (APScheduler)    │                                 │
          │   - open_daily_job   @ 11:00 UTC                                 │
          │   - close_daily_job  @ 11:00 UTC                                 │
          │   - payout_loop      @ 60s                                       │
          │   - watch_loop       @ 30s                                       │
          │   - heal_stale_rounds @ 5min                                     │
          │   - check_anomalies  @ 1d                                        │
          │   - self_ping         @ 600s                                     │
          ▼                                │                                 │
   ┌─────────────┐                         │                                 │
   │ rounds/     │                         │                                 │
   │ lifecycle.py│──► Round (status=OPEN/TALLYING/CLOSED) ──────────────►    │
   │ voting.py   │──► Card, Stake, Vote, Winner ───────────────────────►    │
   │ tally.py    │                         │                                 │
   └─────────────┘                         │                                 │
          │ winner_card_id                 │                                 │
          ▼                                │                                 │
   ┌─────────────┐                         │                                 │
   │ story/bay.py│                         │                                 │
   │ (подменяет  │                         │                                 │
   │  render() в │                         │                                 │
   │  lifecycle) │                         │                                 │
   └─────────────┘                         │                                 │
          │ canon text                     │                                 │
          ▼                                │                                 │
   ┌─────────────┐                         │                                 │
   │ handlers/   │                         │                                 │
   │ payout.py   │──► Payout (status=pending/sending/sent/failed) ─────►    │
   │ broadcast.py│                         │                                 │
   └─────────────┘                         │                                 │
          │                                ▼                                 │
          │                       ┌─────────────────┐                        │
          │                       │ ton_pay/        │                        │
          │                       │  send_via_liteserver() ─► liteserver ► TON
          │                       │  send_via_toncenter() ─► https API   ─► TON
          │                       └─────────────────┘                        │
          │                                │                                 │
          │                       ┌─────────────────┐                        │
          │                       │ ton_watch/      │◄── liteserver ◄────────│
          │                       │  watcher_state  │                        │
          │                       │  → Stake(WATCHED)                        │
          │                       └─────────────────┘                        │
          │                                │                                 │
          │                                ▼                                 │
          │                       ┌─────────────────┐                        │
          │                       │ treasury_mirror │                        │
          │                       │  daily_check()  │   pg_dump + archive    │
          │                       └─────────────────┘                        │
          ▼                                                                ▼
      Сообщения в чат                                            Состояние раундов
```

---

## 3. Главные инварианты

Эти свойства НЕ ДОЛЖНЫ ломаться ни при каких правках. Каждое покрыто
property-тестом (`tests/test_invariants.py`).

1. **Консервация банка дня**: `Σ stake на путях − rake − gas − dust_rollup = Σ payout`.
   Если разошлось — рассыпается экономика.
2. **Консервация казны**: `Σ treasury_moves.delta = balance_treasury`. Без допуска на газ.
3. **Идемпотентность выплат**: один и тот же `(round_id, player_id, seqno)` не отправляется дважды.
   Защита: `UNIQUE(round_id, seqno)` на `payouts` + `UNIQUE(tx_hash, network)` на `stakes`.
4. **Один голос на игрока в сутках**: `UNIQUE(round_id, player_id)` в `votes`.
5. **Закон дня зафиксирован до ставок**: `rule_entropy` хранит `seqno:root_hash`
   блока masterchain TON, записанный **до** открытия ставок. Подогнать задним числом нельзя.
6. **Состояние раунда монотонно**: `OPEN → TALLYING → CLOSED`. Переходы — атомарные
   UPDATE с `WHERE status = prev`. Двум процессам нельзя закрыть один день.
7. **Watched ↔ Sent**: каждый стейк либо WATCHED, либо имеет pending-payout, но не оба;
   каждый `payout.status='sent'` имеет `tx_hash` из зеркала, а не из памяти.

### Доставка итогов игроку (at-least-once)

Доставка — единственное действие бота, у которого нет получателя, способного
подтвердить приём. Поэтому «доставлено» здесь всегда значит «Telegram принял
запрос», и это лучшее из доступного.

**Гарантируется (at-least-once).** Маркер `results_at` ставится ДО бродкаста в
той же транзакции, что и сама рассылка (`_claim_results`). Откат транзакции
сносит маркер, и `_retry_results_job` довсылает день заново. Право на одну
рассылку ровно одно: `UPDATE ... WHERE results_at IS NULL`. Итоги дня не теряются
ни при каком сбое сборки, отправки или сети.

**Не гарантируется.** Окно между последним успешным `send_message` и
`commit`. Крах процесса в этом окне означает, что `results_at` не зафиксирован,
а сообщение уже у игрока: следующий проход пришлёт итоги повторно. Закрыть это
можно только состоянием «доставлено именно этому получателю» — записью на
каждый чат после каждого успешного `send_message`. Сделано не было сознательно:

- повтор итогов — неприятно; потеря итогов — игрок не узнает победителя и судьбу
  своей ставки;
- цена — отдельная запись в БД на каждое доставленное сообщение плюс сквозная
  передача «кому уже не надо» через `_broadcast_text` в восстановитель;
- выигрыш всё равно частичный: крах между отправкой чату и записью метки
  оставит дубликат ровно у одного чата.

Если размен «редкий дубль» против «частой потери» когда-нибудь поменяется на
обратный, менять нужно в `_announce_results_job` и `_retry_results_job`, а не в
`record_delivery`: метка доставки — это счётчик, а не журнал доставки.

**Счётчик доставки.** Метки `delivery:<вид>` пишет `record_delivery` там, где
известны оба числа. `attempted <= 0` метку не пишет: «0 из 0» — это не доставка.
Запись метки никогда не роняет рассылку (телеметрия, а не условие доставки) —
ошибка уходит в warning.

Ключ метки задаёт вид рассылки, а не обрезок текста: `delivery:results:31`,
а не `delivery:text:Итоги дня 31`. День обязан быть в ключе — иначе тревога
«у дня N нет отметки» не находит свою метку.

**Тревоги по доставке** в `check_anomalies`:

- `ALERT_DELIVERY_KEY` — доставлено ноль из попыток. Порога по доле нет и не
  будет: частичные потери норма (заблокировавшие бота, чаты без прав), порог
  кричал бы постоянно и приучил бы игнорировать тревоги.
- `ALERT_DELIVERY_MISSING_KEY` — день помечен рассылкой (`results_at`), а метки
  с его номером нет вовсе. Ноль значит «дошло немного», отсутствие метки —
  «не знаем», и молчание опаснее нуля. Оговорка на `results_at` обязательна:
  день, закрытый ручным переходом, ещё не рассылался, и тревога кричала бы на
  каждом таком дне.

**Заблокировавшие бота.** `deactivate_chat` гасит чат без прав,
`deactivate_player` — игрока, заблокировавшего бота. Без второго адрес
перебирался каждой рассылкой вечно, и метка доставки из-за него не могла
набрать «N из N». Отписка происходит только на `TelegramForbiddenError`: сеть и
таймауты подписку не трогают, иначе один сбой лишил бы игрока рассылки
навсегда. Обратного перехода нет — разблокировавший переключает рассылку сам.

---

## 4. Границы и стыки

| Граница | Что знает о чём | Что НЕ знает |
|---|---|---|
| `handlers/` | aiogram, форматирование, клавиатуры | SQL, TON, схема раундов |
| `rounds/` | SQL, состояние раундов, закон дня | Telegram API, конкретные кассеты |
| `story/` | Кассеты, формат `YYYY-MM`, лор | БД, TON, состояние раундов (получает через патч `rendering._plan_and_render`) |
| `ton_pay/` | Активный/фоллбэк канал, seqno, memo | Что выплачивается и зачем |
| `ton_watch/` | Inbox, мемпул, идемпотентность по `tx_hash` | Payouts, UI |
| `treasury_mirror.py` | Цепочка, комиссии, эталонный баланс | Кто получает выплаты |

**Правило добавления нового:** если фича меняет экономику — идёт в `rounds/` или
`payments.py`/`payouts.py`, Telegram-обёртка — в `handlers/`. Сюжет — только в
`story/cassettes/`. Никогда не импортировать `story/` из `rounds/` напрямую:
единственный стык — `app/rounds/rendering.py::_plan_and_render` (см. `bay.install_bay`).

---

## 5. Конфигурация и запуск

```
.env  (см. .env.example, 12 КБ)
  ├─ BOT_TOKEN             — Telegram
  ├─ ADMIN_IDS             — список id (CSV)
  ├─ DATABASE_URL          — sqlite|postgresql+asyncpg
  ├─ TON_NETWORK           — mainnet|testnet
  ├─ TONCENTER_API_KEY     — опц., повышает лимит
  ├─ TREASURY_MNEMONIC     — sync:false, ТОЛЬКО на Render
  ├─ OWNER_WALLET_ADDRESS  — адрес казначея (для подписи сообщений в зеркале)
  └─ ~40 прочих ключей    — RENDER_EXTERNAL_URL, WEBHOOK_SECRET, HEALTH_TOKEN, ...
```

Запуск:
- **polling (локально):** `python -m app.main`
- **webhook (Render):** старт скрипта ставит webhook на `${RENDER_EXTERNAL_URL}/webhook/${WEBHOOK_SECRET}`
- **миграции:** `python -m scripts.check_db` (или вручную: `alembic upgrade head && alembic check`)
- **тесты:** `python -m pytest -q`

`config.validate_config()` ловит критичные ошибки конфигурации на старте
(пустой токен, rake > 100%, казначей = owner, и т.д.).

---

## 6. Тестовая стратегия

| Уровень | Где | Что покрывает |
|---|---|---|
| Unit | `tests/test_*.py` | Чистые функции: кодеки, форматы, схема кассеты, энтропия |
| Integration | `tests/conftest.py` (SQLite в tmp) | handlers × DB × time-mock |
| Cross-DB | CI: SQLite + Postgres 16 | Дрейф диалектов, VARCHAR-truncation, пулинг |
| E2E | `scripts/e2e_testnet.py` | Реальный testnet TON: пополнение, ставка, выплата |
| Property | `tests/test_invariants.py` | Экономика: банки консервативны, нет отрицательных балансов |

CI enforced: `--cov-fail-under=85` (при последнем замере 87 %).

---

## 7. Что НЕ здесь

- **Нет web-интерфейса.** Только Telegram-бот и HTTP `/health` для мониторинга.
- **Нет фронта для админа.** Все админ-операции — команды в Telegram.
- **Нет внешних платных API.** Только бесплатные публичные индексаторы TON
  (TonAPI v2, Toncenter v3) с опциональным API-ключом.
- **Нет мульти-ботов.** Один процесс — одна кассета в лотке — одна стая.

---

## 8. Как делать изменения

1. Правка схемы → `app/models.py` → `alembic revision --autogenerate` → миграция
   обязана быть идемпотентно-применимой (см. `legacy_convergence` как образец).
2. Новый Telegram-команды → `app/handlers/...` + регистрация роутера в `app/main.py`.
3. Новая механика дня → `app/rounds/` (state-machine) + UI в `handlers/panel.py`.
4. Новая кассета → JSON в `app/story/cassettes/` по контракту `docs/lost_howl_prompts.md`,
   валидация через `python -m scripts.cassette_tool.py lint path.json`.
5. Любое изменение в пакетах `ton_pay/` или `ton_watch/` — обязательно
   `scripts/e2e_testnet.py` на testnet перед мержем.

---



Новые пункты сюда не копятся: план на месяц живёт в трекере задач
репозитория, а не в архитектурном документе -- здесь он успевает
протухать и обманывать читателя.
