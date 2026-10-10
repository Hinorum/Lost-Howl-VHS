"""Центральный реестр ключей watcher_state.

Все ключи-константы собраны в одном месте. Модули-«владельцы»
(ops/ton_watch/season/...) импортируют канонические имена отсюда и
пробрасывают их дальше — старые точки импорта не ломаются.

Правило: новый ключ добавляется здесь, а не в теле модуля. Удаление ключа —
осознанное решение, проходящее через git blame этого файла.
"""

from __future__ import annotations

# --- Операционная наблюдаемость / алерты (app/ops.py) ---

# Биение главного тика. Ставится ТОЛЬКО успешным тиком (app/ops.mark_tick):
# иначе зависший цикл каждые 15 секунд подтверждал бы собственную живость,
# и /health отвечал «ok» на замершем расписании (дни не открываются, анонсы
# молчат, а тревога «планировщик не тикает» не срабатывает никогда).
TICK_KEY = "last_tick_iso"
# Счётчик ТИКОВ ПОДРЯД, упавших с исключением, и текст последней ошибки.
# Отвечают на вопрос, которого не решает битие: «цикл падает прямо сейчас,
# но реже, чем раз в 5 минут» — тогда битие успевает обновиться, а авария
# всё равно видна. Счётчик обнуляется успешным тиком.
TICK_FAIL_KEY = "tick_fail_count"
TICK_FAIL_LAST_KEY = "tick_fail_last"
ALERT_TICK_FAIL_KEY = "alert_tick_fail_ts"
ALERT_WATCHER_KEY = "alert_watcher_ts"
ALERT_SCAN_GAP_KEY = "alert_scan_gap_ts"
ALERT_QUEUE_KEY = "alert_queue_ts"
ALERT_DEAD_KEY = "alert_dead_ts"
ALERT_TICK_KEY = "alert_tick_ts"
ALERT_BALANCE_KEY = "alert_balance_ts"
ALERT_MIRROR_KEY = "alert_mirror_ts"
ALERT_REFUND_KEY = "alert_refund_ts"
ALERT_STAKE_KEY = "alert_stake_ts"
ALERT_STUCK_KEY = "alert_stuck_ts"
ALERT_ENTROPY_KEY = "alert_entropy_ts"
ALERT_BACKUP_KEY = "alert_backup_ts"
# Тревога «рассылка не дошла». До появления меток delivery:* успешный проход
# рассылки и доставка всем были одним и тем же утверждением, а на деле нет:
# день мог уйти пустым, и тик при этом оставался живым.
ALERT_DELIVERY_KEY = "alert_delivery_ts"
# Тревога «рассылка прошла, а метки доставки нет». Отличие от ALERT_DELIVERY_KEY:
# там доставка известна и равна нулю, здесь — неизвестна вовсе (проход умер до
# записи метки). Молчание опаснее нуля: ноль значит «дошло немного».
ALERT_DELIVERY_MISSING_KEY = "alert_delivery_missing_ts"
# Тревога «анонс нового дня ушёл в пустоту»: нет ни активного чата, ни игрока
# с личной рассылкой. Отличие от ALERT_DELIVERY_KEY: там рассылка ШЛА и все
# получатели отказали (метка delivery:* = «0/N»), здесь получателей нет вовсе,
# а «0 из 0» меткой доставки не считается — без этой тревоги пустая аудитория
# была бы неотличима от «новость дня увидели все».
ALERT_ANNOUNCE_EMPTY_KEY = "alert_announce_empty_ts"
# Сам факт и день: анонс которого дня ушёл в пустоту. Пишет announce_new_day,
# снимает либо следующий анонс с получателем, либо check_anomalies, как только
# получатель появился (/bind, добавленный бот, вернувшийся игрок).
ANNOUNCE_EMPTY_DAY_KEY = "announce_empty_day"
# Метка времени последнего УСПЕШНОГО бэкапа. Без неё в тревогах не было
# ни одной строки про бэкапы: если pg_dump ломается, крон промахивается или
# контейнер пересоздаётся не вовремя, узнать об этом нельзя было ниоткуда —
# ни одна проверка check_anomalies про бэкапы не знала.
BACKUP_LAST_OK_KEY = "backup_last_ok_iso"

# Снимок последнего вердикта check_anomalies: JSON-список строк-проблем и
# время, когда он снят. Считает джоба ops-sweep раз в 120с; /health и /ops
# читают кэш и не пересчитывают: опрос мониторинга не должен ходить в сеть,
# а «что сейчас сломано» должно быть одним и тем же ответом везде.
# Возраст снимка виден наружу — им видно, что сам sweeper перестал ходить.
OPS_PROBLEMS_KEY = "ops_problems"
OPS_PROBLEMS_AT_KEY = "ops_problems_at"
# Кто из тревог сколько держится: ключ проблемы -> {text, since, seen, alert}.
OPS_ALERT_TRACK_KEY = "ops_alert_track"
# Час, в который ушла сводка по затянувшимся тревогам.
OPS_ALERT_DIGEST_KEY = "alert_digest_ts"

# Пауза игры (стоп-кран) и режим «со ставками» / «без ставок».
PAUSE_KEY = "game_paused_iso"
PAUSE_REASON_KEY = "game_paused_reason"
MONEY_MODE_KEY = "money_mode_on"

# Kill switch исходящих выплат (/halt-payouts). Сознательно ОТДЕЛЬНЫЙ от
# PAUSE_KEY ключ: пауза останавливает игру, но очередь выплат продолжает
# разгребаться (см. текст /pause), а здесь останавливается сама отправка
# денег. Состояние живёт в watcher_state — переживает рестарт и видно
# каждой копии диспетчера, а не только той, что приняла команду.
PAYOUT_HALT_KEY = "payouts_halted_iso"
PAYOUT_HALT_REASON_KEY = "payouts_halted_reason"

# --- Лидерборды (app/leaderboard.py) ---

MARKER_KEY = "leaderboard_settled_through"
WEEKLY_MARKER_KEY = "weekly_settled_through"
MONTH_READY_KEY = "month_leaderboard_ready"
WEEK_READY_KEY = "week_leaderboard_ready"
WEEK_CLAIM_WINDOW_KEY = "claim_window:week"
MONTH_CLAIM_WINDOW_KEY = "claim_window:month"

# --- Сезон / сюжет (якорь забега — app/rounds/anchor.py, кассеты — app/story/bay.py,
# назначение — app/handlers/panel.py) ---

RUN_START_KEY = "run_season_anchor"
# «Следующая» кассета из библиотеки app/story/cassettes/, назначенная в /panel:
# имя файла *.json. Проигрыватель зачитывает её при планировании дня; значение
# лишь разрешает конфликт нескольких кассет одного месяца, активация всегда
# по календарному месяцу кассеты.
STORY_CASSETTE_NEXT_KEY = "story_cassette_next"
# Намерение правки кассеты из /cassette: <имя_файла.json>|<месяц|день>. Ставится
# кнопкой «Редактор плёнки», снимается после приёма документа (обработка файла),
# команды /cassette или отдельной кнопкой отмены — хранитель не застревает.
STORY_CASSETTE_EDIT_KEY = "story_cassette_edit"

# --- TON-watcher (пакет app/ton_watch/) ---

CURSOR_KEY = "ton_watch_cursor_utime"
BEAT_KEY = "ton_watch_beat_iso"
SOURCE_KEY = "ton_watch_last_source"
WALLET_NORM_KEY = "wallet_norm_v1"
# Хронически падающие транзакции (после нескольких попыток их обработки):
# JSON-объект {tx_hash: {"utime": int, "fails": int}}. Держимся за них, пока
# fails < минимума, а исчерпавшие лимит — пропускаем, НЕ двигая курсор за них
# с потерей: админ видит их в watcher_state и может разобрать вручную.
STUCK_TX_KEY = "ton_watch_stuck_tx"
# Проход watcher'а не вычитал окно входящих целиком: бюджет страниц кончился
# раньше, чем пагинация дошла до курсора. JSON: {since, floor, pages, boost,
# at}. Пока запись жива, курсор стоит на границе покрытия (floor) и бюджет
# страниц поднят вдвое, а ops.py держит тревогу: непрочитанное окно не должно
# исчезать молча.
SCAN_GAP_KEY = "ton_watch_scan_gap"
# Во сколько раз поднят бюджет страниц поверх watch_max_pages (1 = базовый).
# Сбрасывается в 1 полным проходом.
SCAN_BOOST_KEY = "ton_watch_scan_boost"

# --- Зеркало казны (app/treasury_mirror.py) ---

# Голова зеркала: lt самой свежей учтённой транзакции. Пока бутстрап не
# завершён (TREASURY_MIRROR_BOOTSTRAP_KEY пуст) — голова это верх истории,
# на неё опирается инкрементальный проход после покрытия генезиса.
TREASURY_MIRROR_CURSOR_KEY = "treasury_mirror_cursor_lt"
# Дно бутстрапа: lt самой ДРЕВНЕЙ транзакции, до которой зеркало уже дошло.
# Действует только в небутстрапленном состоянии; ровно с него продолжается
# следующий цикл (страницы строго старше, по before_lt).
TREASURY_MIRROR_BOTTOM_KEY = "treasury_mirror_bottom_lt"
# "1" — зеркало покрыло генезис→голову, тождество «Σ balance_delta = баланс»
# измеримо и строго; пусто — бутстрап ещё идёт, сверка невозможна.
TREASURY_MIRROR_BOOTSTRAP_KEY = "treasury_mirror_bootstrapped"
TREASURY_MIRROR_BEAT_KEY = "treasury_mirror_beat_iso"
TREASURY_MIRROR_SOURCE_KEY = "treasury_mirror_last_source"
# Результат последней проверки тождества «зеркало == баланс цепочки» (JSON:
# exact, diff_nanotons, checked_at). Читается ежедневной автосверкой без
# лишнего запроса к индексатору.
TREASURY_MIRROR_CHECK_KEY = "treasury_mirror_check"