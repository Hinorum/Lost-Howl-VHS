from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RoundStatus(StrEnum):
    OPEN = "open"
    TALLYING = "tallying"
    CLOSED = "closed"


class WinRule(StrEnum):
    MAJORITY = "majority"
    MINORITY = "minority"
    MEDIAN = "median"


RULE_PHRASES = {
    WinRule.MAJORITY: "уцелеет кадр с большей суммой Gram (день Большинства)",
    WinRule.MINORITY: "уцелеет кадр с меньшей суммой Gram (день Меньшинства)",
    WinRule.MEDIAN: "уцелеет кадр со средней суммой Gram (день Середины)",
}

# Формулировки для дней, исход которых решают бесплатные голоса:
# fallback (на день не поставлено ни одного Gram) и легаси-режим
# (winner_by_stakes=False / ставки не включены).
VOTE_RULE_PHRASES = {
    WinRule.MAJORITY: "уцелеет кадр, собравший больше всех голосов (день Большинства)",
    WinRule.MINORITY: "уцелеет кадр, собравший меньше всех голосов (день Меньшинства)",
    WinRule.MEDIAN: "уцелеет кадр со средним числом голосов (день Середины)",
}


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    correct_picks: Mapped[int] = mapped_column(Integer, default=0)
    wallet_address: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    wallet_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Кошелёк подтверждён микро-переводом с него (мемо bv:<код>): привязка без
    # доказательства владения давала бы присваивать любой публичный адрес и
    # ловить на него чужие призы. Пока деньги включены, ставки с неподтверждённого
    # кошелька не считаются — сначала владелец доказывает контроль.
    wallet_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Код подтверждения привязки: известен только владельцу телеграм-аккаунта,
    # отправить перевод с нужного адреса может только владелец кошелька — так
    # совпадение «адрес + код» доказывает контроль. Null — ждать нечего.
    wallet_verify_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    wallet_verify_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Подписка на личные дубликаты рассылок (итоги дня, новый день с обложкой,
    # вечерний пост и прочие анонсы) в личку бота. По умолчанию — да; игрок
    # может снять или вернуть её кнопкой в /start.
    dm_subscribed: Mapped[bool] = mapped_column(Boolean, default=True)
    # Стрик: текущая серия правильных голосований и лучшая за всё время.
    current_streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Referral(Base):
    """Кто привёл кого по личной ссылке ?start=ref_<id>_<токен>.

    Строго одна запись на приведённого: первый валидный переход фиксируется
    навсегда, повторные /start с чужой ссылкой игнорируются. Самоподписка и
    циклы цепочки (A привёл B, B приводит A обратно) блокируются в
    record_referral. Размен между собственными альтами не различим на уровне
    кошельков — осознанный риск реферальных схем, ограниченный долей
    referral_pct (см. app/referrals.py). Награды копятся в ReferralPot
    (см. там) и выплачиваются через обычную очередь Payout.
    """

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReferralPot(Base):
    """Накопленная реферальная награда пригласившего (доля referral_pct% от
    подтверждённых ставок приведённых игроков).

    Обязательство перед реферером: деньги остаются на кошельке казначея и
    ждут, НЕ создавая микропереводов. Перевод создаётся обычной очередью
    выплат (kind="referral") только когда накопление >= referral_min_payout_gram
    (пыль копится дальше — газ сети стоит дороже). К рефералу привязывается
    кошелёк игрока; без подтверждённого кошелька накопление просто ждёт.
    """

    __tablename__ = "referral_pots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chat(Base):
    """Чаты (группы/каналы), где бот состоит и куда рассылаются анонсы дней."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _enum_values(enum_cls):
    return [member.value for member in enum_cls]


class Round(Base):
    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day_index: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[RoundStatus] = mapped_column(
        Enum(RoundStatus, native_enum=False, values_callable=_enum_values),
        default=RoundStatus.OPEN,
    )
    win_rule: Mapped[WinRule] = mapped_column(
        Enum(WinRule, native_enum=False, values_callable=_enum_values)
    )
    chapter_title: Mapped[str] = mapped_column(String(300))
    chapter_text: Mapped[str] = mapped_column(Text)
    # Блок «что предстоит решить» (проблема/дилемма/вопрос дня) — снимок поля
    # dilemma кассеты на момент создания раунда, как и глава: правка кассеты
    # не двигает уже открытый пост. Структура поста одна — эхо → сюжет →
    # дилемма, варианты выбора только в кнопках (см. broadcast.status_text).
    # NULL — только переходные строки до миграции c4e8a1b7d9f2.
    dilemma: Mapped[str | None] = mapped_column(Text, nullable=True)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    voting_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tally_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    winner_card: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vote_counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Суммы подтверждённых ставок (нанотоны Gram) по путям дня — решающий счёт,
    # когда winner_by_stakes включён: исход определяется банками путей. Null —
    # день закрыт по голосам (fallback/легаси). Не трогает vote_counts_json:
    # тот хранит бесплатные голоса для лидерборда и отображения.
    stake_counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Человеческое объяснение ничьей: «жребий закона выбрал путь II из II и III».
    # Null — победа без равенства.
    tie_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Честная жеребьёвка на цепи: «seqno:hash» мастерхчейн-блока TON, взятого
    # в момент завязки при ничьей. Блок публичен и проверяем в эксплорере,
    # поэтому исход не манипулируем и не предсказуем заранее. Снимается ОДИН раз
    # (close_voting) и фиксируется в дне; heal/пересчёт используют ту же энтропию,
    # иначе исход мог бы измениться. Null — ничьей не было либо TON недоступен.
    tie_entropy: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Энтропия закона дня: «seqno:root_hash» мастерхчейн-блока TON, из root_hash
    # которого детерминированно выводится правило дня (root_hash % 3). Блок уже
    # лежит в цепочке в момент открытия дня: его нельзя подогнать задним числом,
    # каждый игрок может проверить seqno в эксплорере и пересчитать закон.
    # Снимается один раз при открытии дня. Null — TON выключен или оба узла
    # молчали (правило падает на локальный честный жребий).
    rule_entropy: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Момент первой успешной рассылки дня: повторный анонс того же дня
    # невозможен даже при гонке двух процессов после деплоя.
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Момент доставки итогов дня (общий пост + личные) — маркер at-least-once:
    # ставится ПОСЛЕ успешного бродкаста; отсутствие у CLOSED-дня позади
    # актуального включает восстановитель (_retry_results_job). Откат снимает.
    results_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pot_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    rake_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    # Доля дня, ушедшая в копилку недели (2% фонда) — для поста итогов.
    weekly_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    # Доля дня, ушедшая в реферальные копилки пригласивших (1% фонда) —
    # для поста итогов. День без приведённых ставок или день возврата — 0.
    referral_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    payouts_finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    # Момент начисления очков дня — единый маркер-claim: проставляет только
    # тот, кто победил в гонке (scheduler, heal, админский /advance), и только
    # в одной транзакции с самим начислением. Краш между finish_tally и
    # award_points оставляет день CLOSED без очков, но целевой запрос по
    # awards_at IS NULL добирает его; повторный вызов не удваивает score.
    # Null — очки ещё не начислены (или день без победителя).
    awards_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Денежная версия дня (ставки TON + платная смена выбора): снимок режима
    # на момент открытия дня. Хранитель переключает «версию со ставками/без»
    # из /panel — новая версия вступает со СЛЕДУЮЩЕГО дня, а этот флаг
    # замораживает решение текущего дня (банк, /stake, /change).
    money_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    # Эпилог дня от нейросети: чем отозвался победивший путь (пусто — не написан).
    epilogue_text: Mapped[str] = mapped_column(String(700), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_round_status_finalized", "status", "payouts_finalized"),
    )

    cards: Mapped[list[Card]] = relationship(back_populates="round", cascade="all, delete-orphan")


class StatusPost(Base):
    """Где живёт пост-статус текущего дня (тот, что с кнопками выбора).

    Пост дня уходит один раз при анонсе — с суммой подтверждённых ставок на тот
    момент. Когда watcher подтверждает новые ставки, банк в отправленном посте
    устаревает, и игроку приходится звать /today. Здесь храним точку доставки
    (чат + message_id), чтобы отредактировать тот же пост; last_pot_nanotons
    дедуплицирует правки — редактируем только когда сумма реально выросла.
    Строки открытого раунда чистит проход refresh; строки прошлых дней убирает
    тот же дедуп (правим лишь текущий OPEN раунд).
    """

    __tablename__ = "status_post"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    # Отдельные чаты (группы) и личные дубликаты подписчиков редактируются
    # одинаково, но метка нужна для диагностики и будущей разной риторики.
    is_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    # Последний показанный в посте банк (нанотоны Gram). Null — пост ещё ни разу
    # не правился/банк не подсматривался; изменение числа — триггер правки.
    last_pot_nanotons: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("round_id", "chat_id", name="uq_status_post_round_chat"),
    )


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    # image_path удалён миграцией d5f8a1c39b02: арт не делается (build_day_post
    # пуст), поле было NOT NULL без дефолта и роняло INSERT карточек.
    consequence: Mapped[str] = mapped_column(Text)
    tag: Mapped[str] = mapped_column(String(16), default="care")

    round: Mapped[Round] = relationship(back_populates="cards")

    __table_args__ = (UniqueConstraint("round_id", "position", name="uq_card_round_pos"),)


class Vote(Base):
    """Append-only. Counts are never stored here during an open round."""

    __tablename__ = "votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    card_position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("round_id", "player_id", name="uq_vote_round_player"),)


class StoryBeat(Base):
    __tablename__ = "story_beats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day_index: Mapped[int] = mapped_column(Integer, unique=True)
    winning_title: Mapped[str] = mapped_column(String(120))
    winning_text: Mapped[str] = mapped_column(Text)
    hook_text: Mapped[str | None] = mapped_column(String(700), default=None)  # (легаси) крючок главы: больше не пишется, кассеты самостоятельны
    win_rule: Mapped[str] = mapped_column(String(32))
    vote_counts: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Stake(Base):
    """Ставка TON на выбор дня. Одна на игрока в раунде, идемпотентна по tx_hash.

    Жизненный цикл: pending (увидена в блокчейне) → confirmed (набрало
    «возраст» в блоках) → после итогов дня либо учтена в фонде победителям,
    либо refunded. rejected — нарушены лимиты/мемо, деньги отправляются назад.
    """

    __tablename__ = "stakes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    tx_hash: Mapped[str] = mapped_column(String(80))
    memo: Mapped[str] = mapped_column(String(64), default="")
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tx_hash", "network", name="uq_stake_tx_network"),
        UniqueConstraint("round_id", "player_id", name="uq_stake_round_player"),
        CheckConstraint("amount_nanotons > 0", name="ck_stake_positive_amount"),
        CheckConstraint("status IN ('pending', 'confirmed', 'rejected', 'refunded')", name="ck_stake_valid_status"),
        Index("ix_stake_network_round_status", "network", "round_id", "status"),
    )


# Один хеш цепочки — ровно одна выплата, и только для «настоящих» хешей:
# NULL означает «ещё не отправлялось», а метка вещания bcast:<unix> общая для
# всех переводов, разосланных в одну секунду (см. dispatch.send), — она
# уникальностью обладать не может и выпадает из индекса.
_PAYOUT_TX_UNIQUE_WHERE = text("tx_hash IS NOT NULL AND tx_hash NOT LIKE 'bcast:%'")


class Payout(Base):
    """Исходящий перевод: приз, бонус угадавшему без ставки, возврат ставки,
    авто-возврат «ничейного» перевода или доля казны (рейк и копилка месяца).

    player_id и round_id nullable: у долей казны игрока нет, а переводы от
    неопознанных отправителей или в закрытый день не привязаны к раунду.
    """

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="prize")
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    dest_address: Mapped[str] = mapped_column(String(80))
    tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Момент клейма строки в работу (sending). _reset_retriable возвращает
    # sending → pending только когда клейм заведомо «мёртв» (старше окна
    # вещания): живой вещающий процесс строку не перехватывает — иначе вторая
    # копия диспетчера перевела бы деньги дважды. См. _reset_retriable.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Причина последней неудачи отправки: видно в /payouts и алертах админу,
    # диагноз не требует раскопок логов сервиса.
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Свободный комментарий перевода вместо служебного memo «way:…»:
    # возвраты при паузе игры несут игроку текст о техработах, а не
    # технический идентификатор. Null — используется стандартное memo.
    comment_override: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Админ уже предупреждён об этой выплате: дедуп алертов живёт в БД,
    # а не в памяти процесса — переживает рестарт, безопасен при репликах.
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("amount_nanotons > 0", name="ck_payout_positive_amount"),
        CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed', 'dismissed')", name="ck_payout_valid_status"),
        Index("ix_payout_dispatch", "status", "network", "dest_address"),
        Index("ix_payout_alert", "status", "network", "alerted"),
        # БД-барьер от двойной оплаты: раньше единственной защитой был код
        # (claim_once в ton_watch.refunds, лок + условный UPDATE в диспетчере).
        # Ловит два класса — повторный авто-возврат на один входящий перевод и
        # подтверждение одной транзакции сразу за две выплаты.
        Index(
            "uq_payout_tx_network",
            "tx_hash",
            "network",
            unique=True,
            sqlite_where=_PAYOUT_TX_UNIQUE_WHERE,
            postgresql_where=_PAYOUT_TX_UNIQUE_WHERE,
        ),
    )


class WatcherState(Base):
    """Служебные метки фоновых задач: ключ → значение.

    Здесь живут курсор наблюдателя TON, сердцебиения, план Хозяина Ошибки
    (список канонических событий сезона — длинные строки) и якорь арта.
    Value — Text без лимита: план злодея на 2-й ступени сезона переставал
    влезать в VARCHAR(255), роняя тик до создания следующего дня.
    """

    __tablename__ = "watcher_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaderboardPot(Base):
    """Копилка месяца: 0,5% фонда каждого дня капает сюда.

    В конце месяца сумма уходит игроку (игрокам) с максимумом верных
    ответов за месяц; месяц — ключ «YYYY-MM» по UTC-времени итогов дня.
    """

    __tablename__ = "leaderboard_pots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WeeklyPot(Base):
    """Копилка недели: 2% фонда каждого дня капает сюда.

    В понедельник сумма уходит топ-3 недели по числу верных ответов
    (места делят приз по WEEKLY_PRIZE_PCTS, по умолчанию 50/30/20%:
    сильнейший забирает больше); неделя — ISO-ключ «YYYY-Www» по UTC-времени
    открытия дня. Доле места без подходящего игрока (нет кошелька, мало дней
    голосования или нет ставки за неделю) ждать нечего — она переносится
    в копилку новой недели.
    """

    __tablename__ = "weekly_pots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week: Mapped[str] = mapped_column(String(16), unique=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeaderboardClaim(Base):
    """Претензия на место лидерборда: решает ничьи по кнопке Claim в /start.

    Ничья в периоде решается по верности путей, затем по сумме ставок Gram,
    затем — кто раньше нажал Claim (кнопка видна в течение периода игрокам
    с кошельком и ставкой). Запись фиксирует момент претензии; unique-тройка
    (player_id, kind, period) делает Claim идемпотентным.
    """

    __tablename__ = "leaderboard_claims"
    __table_args__ = (
        UniqueConstraint("player_id", "kind", "period", name="uq_leaderboard_claims_player_kind_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # "week" | "month"
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # "YYYY-Www" | "YYYY-MM"
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PackFund(Base):
    """Фонд Стаи: 1% банка каждого дня капает сюда и не раздаётся сам.

    В отличие от копилок недели (WeeklyPot) и месяца (LeaderboardPot) у фонда нет
    периода авто-розыгрыша: это неубывающее накопление-обязательство. Хранитель
    видит баланс в пульте и распоряжается им вручную (например, разыгрывает
    среди любой группы или тратит на приз), выводя деньги обычным ручным путём.
    """

    __tablename__ = "pack_fund"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PackFundLedger(Base):
    """Прозрачный журнал Фонда Стаи: каждое поступление и ручная раздача.

    PackFund — это накопление-обязательство без периода авто-розыгрыша: 1% банка
    дня капает сюда, а Хранитель распоряжается деньгами вручную с казначейского
    кошелька. Сами физические переводы делает обычная очередь выплат, поэтому
    журнал здесь — аудит-след: «in» пишется при каждом начислении дня, «out» —
    ручной операцией хранителя (см. /fundout), чтобы стая видела, куда уходят
    накопленные граммы, а не одна непрозрачная цифра баланса.
    """

    __tablename__ = "pack_fund_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_type: Mapped[str] = mapped_column(String(8), default="in")  # in | out
    amount_nanotons: Mapped[int] = mapped_column(BigInteger)
    # Намеренно БЕЗ ForeignKey на rounds: журнал — append-only аудит, который
    # живёт дольше истории дней и не должен подпадать под сброс reset_game.
    # round_id — просто номер дня-источника (вместе с note хранит и день).
    round_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dispute(Base):
    """Формальная жалоба игрока на итог дня, рассматриваемая хранителем.

    Каркас разрешения споров без авто-эскроу: сам исход дня НЕ отменяется
    и не переворачивается задним числом (выплаты победителям не трогаются),
    а рассматривается как претензия — результат фиксируется резолюцией
    (resolved/rejected). Компенсация при подтверждённой претензии — это
    обычная выплата kind="dispute" в общей очереди выплат, сделанная вручную
    хранителем после ревью. Так реальные деньги двигаются только по
    выстраданному пути, а спор остаётся подконтрольным аудитом.
    """

    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved|rejected
    keeper_note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RevoteGrant(Base):
    """Оплаченное право изменить свой выбор в дне (Stars или TON).

    unit_ref — идемпотентность платежа: telegram_payment_charge_id для Stars,
    хеш транзакции для TON. granted → used в момент фактической смены пути.
    round_id nullable — так записываются «осиротевшие» оплаты за закрытый день
    (возврат таких — ручная операция хранителя).
    """

    __tablename__ = "revote_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="stars")
    unit_ref: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="granted")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Income(Base):
    """Поступление в казну: ledger для сверки с балансом бота и кошелька.

    kind="stars" — Telegram Stars за смену пути (amount_stars), выводятся
    владельцем через Fragment; kind="ton" — прямые переводы в казну
    (amount_nanotons); network помечает TON-строки (mainnet/testnet), чтобы
    сверка казны не смешивала контуры. unit_ref — тот же идемпотент, что у гранта.
    """

    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_stars: Mapped[int] = mapped_column(Integer, default=0)
    amount_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True, index=True)
    network: Mapped[str] = mapped_column(String(16), default="mainnet")
    unit_ref: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TreasuryMove(Base):
    """Зеркало транзакций казначея: точная копия истории активного кошелька.

    Каждая цепочечная транзакция (сторона казначея) — строка здесь.
    Тождество «по построению»: баланс казны = сумма balance_delta от генезиса
    до головы цепочки, поэтому сверка зеркала с живым балансом не знает
    допуска на газ — реальный fee приходит из самой цепочки (total_fees), и
    сходимость «тютелька в тютельку» не зависит от оценки payout_fee_gram.
    Строки зеркала НЕ заменяют Income/Payout/Stake: это независимый дубликат
    блокчейна для аудита, диагноза «куда делось» и ежедневной автосверки.
    """

    __tablename__ = "treasury_moves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    network: Mapped[str] = mapped_column(String(16), default="mainnet", index=True)
    # Адрес кошелька, чей это транзакция. Сумма зеркала считается по паре
    # (network, address), а не по одному network: после ротации адреса
    # казначея строки прежнего кошелька суммировались бы в баланс нового
    # навсегда, и ни один /mirror reset этого не отменил бы.
    address: Mapped[str] = mapped_column(String(80), default="", index=True)
    utime: Mapped[int] = mapped_column(BigInteger, index=True)
    # Логическое время транзакции (lt): стабильный ключ пагинации индексаторов.
    lt: Mapped[int] = mapped_column(BigInteger, index=True)
    # Куда двигался баланс казны: in — пришло, out — ушло, self — перевод
    # казначея самому себе (рейк/доли копилки), other — без перевода сумм.
    direction: Mapped[str] = mapped_column(String(8), default="in")
    # Классификация после связки с БД: stake / revote / refund / payout:prize /
    # unknown_in / unknown_out ... Импровизированная строка для отчёта.
    kind: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    # Абсолютная сумма перевода (для чтения); знак несёт balance_delta.
    value_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    # Реальная комиссия транзакции из цепочки (total_fees) — не оценка.
    fee_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    # Сальдо аккаунта от этой транзакции (со знаком): Σ по генезису = баланс.
    balance_delta_nanotons: Mapped[int] = mapped_column(BigInteger, default=0)
    counterparty: Mapped[str] = mapped_column(String(80), default="")
    comment: Mapped[str] = mapped_column(String(200), default="")
    # id связанной строки БД: Payout (out) / Income или Stake (in). None — нет.
    linked_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_treasury_moves_lt_id", "network", "lt", "id"),
        Index("ix_treasury_moves_address_lt", "address", "lt"),
    )


class WalletDialog(Base):
    """Диалог привязки кошелька в личке: игрок → ожидаем адрес следующим сообщением.

    В таблице, а не в памяти процесса: переживает рестарт и работает при
    нескольких инстансах за одним вебхуком.
    """

    __tablename__ = "wallet_dialogs"

    player_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreparedDay(Base):
    """Заготовка следующего дня, сгенерированная заранее в час подсчёта.

    payload — JSON: глава, правило с солью, пути к готовым картинкам.
    Материализация в раунд в момент открытия занимает миллисекунды, поэтому
    сетка 11:00 не плывёт на время нейрогенераций. Если заготовки нет
    (рестарт посреди генерации) — создание дня падает обратно в старый
    синхронный путь без потерь.
    """

    __tablename__ = "prepared_days"

    day_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoryCassette(Base):
    """Кассета сюжета в базе — единственный долговечный источник плёнки.

    На Render файловая система эфемерна: перезапуск деплоя стирал все правки
    кассет, и монтаж нового месяца приходилось заливать заново. Диск остался
    кэшем зеркала (его читает синхронный движок), а источник правды живёт
    здесь: payload — нормализованный JSON кассеты, backup — слепок перед
    последней перезаписью (Undo хранителя), edited_at — отметка правки из
    /panel: такая строка при деплое побеждает контент репозитория (см.
    app/story/store.py :: _adopt_disk), а строка без отметки принимает файл
    с диска — так в базу приезжают новые месяцы и контент-фиксы из git.
    """

    __tablename__ = "story_cassettes"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    backup: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


