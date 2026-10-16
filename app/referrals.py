"""Личные приглашения в стаю.

У каждого игрока есть личная ссылка ?start=ref_<id>_<токен>. Токен — HMAC
от id игрока на settings.referral_secret, поэтому вписать в ссылку чужой id
нельзя без секрета. Подпись нового формата — первые 128 бит (32 hex);
выданные ранее 40-битные ссылки (10 hex) ещё принимаются, чтобы не рвать
уже разосланные приглашения.
Первый валидный переход фиксируется в таблице Referral
строго один раз на игрока; повторные /start с чужими ссылками игнорируются.
Приведённым становится только НОВЫЙ игрок: у него ещё нет ни одной ставки
в статусе pending/confirmed. Иначе действующий кит сам кликнул бы чужую
ссылку и отдал 1% своего оборота — классическая рефералка приводит новых,
а не переписывает историю уже ставящих.
Самоподписка заблокирована (referrer_id == referred_id) — позвать самого себя
по своей же ссылке невозможно, как и цепочку-цикл (A привёл B, B пытается
привести A обратно): record_referral идёт вверх по цепочке приглашений и
рвёт замыкание.

Пригласивший получает награду referral_pct (по умолчанию 1%) с каждой
подтверждённой ставки приведённого игрока: она копится в ReferralPot и
уходит обычной очередью выплат при достижении referral_min_payout_gram
(см. app/stakes.py — финализация дня). Размен между СВОИМИ аккаунтами
(альт вносит TON, второй альт приглашает его) технически не блокируется:
Telegram-аккаунты и кошельки разные, и без независимой идентификации
такую схему не отличить от живого приглашения. Это осознанный риск,
присущий любой реферальной системе, а не эксплуатабельная дыра: обратно
можно вернуть максимум referral_pct (1%) собственного вложения, а сам
вклад полностью уходит в игру — ферма лишь компенсирует себе 1% при
неудачном дне, не создавая чистого выигрыша из воздуха.
"""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models import Player, Referral, ReferralPot, Stake

_BOT_USERNAME_CACHE: str | None = None


def _referral_digest(player_id: int) -> str | None:
    """Полный HMAC-SHA256 (hex) от id игрока. None — нет секрета (каркас выключен)."""
    if not settings.referral_secret:
        return None
    return hmac.new(
        settings.referral_secret.encode("utf-8"),
        str(player_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _referral_token(player_id: int) -> str | None:
    """Подпись нового формата: digest[:32] (128 бит). None — нет секрета."""
    digest = _referral_digest(player_id)
    if digest is None:
        return None
    return digest[:32]


def _legacy_referral_token(player_id: int) -> str | None:
    """Подпись старого формата digest[:10] (40 бит): принимаем уже выданные ссылки."""
    digest = _referral_digest(player_id)
    if digest is None:
        return None
    return digest[:10]


def referral_link(player_id: int, username: str | None) -> str | None:
    """Персональная ссылка приглашения. None — каркас выключен или нет username."""
    token = _referral_token(player_id)
    if not token or not username:
        return None
    clean = username.strip().lstrip("@")
    if not clean:
        return None
    return f"https://t.me/{clean}?start=ref_{player_id}_{token}"


def parse_referral_arg(arg: str | None) -> int | None:
    """Referrer_id из аргумента /start (ref_<id>_<токен>) при совпавшей
    подписи. Принимаются оба формата: новый digest[:32] и легаси digest[:10]
    (ссылки, выданные до усиления). Мусор, чужие id и выключенный каркас → None."""
    if not arg or not arg.startswith("ref_"):
        return None
    parts = arg.split("_", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    referrer_id = int(parts[1])
    for expected in (_referral_token(referrer_id), _legacy_referral_token(referrer_id)):
        if expected and hmac.compare_digest(parts[2], expected):
            return referrer_id
    return None


async def record_referral(session, referrer_id: int, referred_id: int) -> bool:
    """Фиксирует приведение, если оно первое и честное. False — самоссылка,
    цикл в цепочке приглашений, каркас выключен, приглашающего нет в базе,
    переход уже записан или приведённый уже ставил (рефералка — для новых)."""
    if referrer_id <= 0 or referred_id <= 0 or referrer_id == referred_id:
        return False
    if settings.referral_secret == "":
        return False
    if await session.get(Player, referrer_id) is None:
        return False
    # Циклы: цепочка «кто кого привёл» не должна замыкаться. Если среди
    # предков приглашающего уже есть тот, кого он пытается привести (A→B, B→A),
    # отказ — иначе награды шли бы по кругу, а не «сверху вниз». Идём вверх
    # по referrer-цепочке; каждый игрок приведён максимум один раз
    # (referred_id unique), поэтому обход всегда конечен.
    cursor = referrer_id
    seen: set[int] = set()
    while cursor > 0:
        if cursor == referred_id:
            return False
        if cursor in seen:
            # Некорректные данные (уже есть цикл) нас останавливают, не зацикливаемся.
            return False
        seen.add(cursor)
        parent = await session.scalar(
            select(Referral.referrer_id).where(Referral.referred_id == cursor)
        )
        if parent is None:
            break
        cursor = parent
    already = await session.scalar(
        select(Referral.referred_id).where(Referral.referred_id == referred_id)
    )
    if already is not None:
        return False
    # Действующего ставившего перепригласить нельзя: pending (деньги в пути)
    # и confirmed (деньги в игре) значат, что игрок не новый. Отклонённые и
    # возвращённые ставки не считаются — деньги игроку вернули.
    staked = await session.scalar(
        select(Stake.id)
        .where(Stake.player_id == referred_id, Stake.status.in_(("pending", "confirmed")))
        .limit(1)
    )
    if staked is not None:
        return False
    session.add(Referral(referrer_id=referrer_id, referred_id=referred_id))
    try:
        await session.commit()
    except IntegrityError:
        # Гонка двух параллельных /start: победил другой — это не ошибка.
        await session.rollback()
        return False
    return True


async def invited_count(player_id: int) -> int:
    """Сколько игроков пришло по личной ссылке."""
    async with SessionLocal() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referrer_id == player_id)
        )
        return int(total or 0)


async def referral_pot_balance(player_id: int) -> int:
    """Накопленная реферальная награда игрока в нанотонах (0 — ещё не копилось)."""
    async with SessionLocal() as session:
        value = await session.scalar(
            select(ReferralPot.nanotons).where(ReferralPot.referrer_id == player_id)
        )
        return int(value or 0)


async def resolve_bot_username(bot) -> str | None:
    """Username бота для ссылки: настройка приоритетнее, иначе get_me один
    раз (кэш на процесс). None — строить ссылку нечем."""
    global _BOT_USERNAME_CACHE
    if settings.bot_username.strip():
        return settings.bot_username.strip().lstrip("@")
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    if bot is None:
        return None
    try:
        me = await bot.get_me()
    except Exception:
        return None
    name = (getattr(me, "username", "") or "").strip().lstrip("@")
    if name:
        _BOT_USERNAME_CACHE = name
        return name
    return None