"""Личные приглашения «на будущее»: токен ссылки, честный приход, /invite.

Каждый игрок получает персональную ссылку ?start=ref_<id>_<токен>. Токен —
HMAC от id на секрете: вписать чужой id нельзя. Первый валидный переход
фиксируется один раз; наград копятся в копилке (баланс показывает /invite,
объединённый с бывшим /referral)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.handlers import cmd_invite, cmd_start
from app.handlers import player as player_mod
from app.models import Referral
from app.referrals import (
    invited_count,
    parse_referral_arg,
    record_referral,
    referral_link,
)
from app.voting import upsert_player

_uid = 750_000
REFERRAL_SECRET = "test-secret-приглашений"


def make_user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"u{uid}", first_name="Тест")


def next_uid() -> int:
    global _uid
    _uid += 1
    return _uid


def make_message(chat_type: str, uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        from_user=make_user(uid),
        text=text,
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )


@pytest.fixture(scope="module", autouse=True)
async def _referrals_db():
    await init_db()
    yield
    async with SessionLocal() as session:
        await session.execute(Referral.__table__.delete())
        await session.commit()


@pytest.fixture(autouse=True)
def _referral_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "referral_secret", REFERRAL_SECRET)
    monkeypatch.setattr(settings, "bot_username", "the_ways_bot")
    monkeypatch.setattr(settings, "media_dir", str(tmp_path / "generated"))


async def _existing_player(uid: int):
    async with SessionLocal() as session:
        await upsert_player(session, make_user(uid))


def test_referral_link_builds_and_roundtrips() -> None:
    link = referral_link(42, "the_ways_bot")
    assert link.startswith("https://t.me/the_ways_bot?start=ref_42_")
    arg = link.split("?start=", 1)[1]
    assert parse_referral_arg(arg) == 42


def test_referral_link_strips_at_from_username() -> None:
    link = referral_link(42, "@the_ways_bot  ")
    arg = link.split("?start=", 1)[1]
    assert link.startswith("https://t.me/the_ways_bot?start=ref_42_")
    # Важно: подпись та же, что для username без «@» — переход читается.
    assert parse_referral_arg(arg) == 42


def test_referral_link_needs_secret_and_username() -> None:
    assert referral_link(42, None) is None
    assert referral_link(42, "") is None


def test_parse_referral_arg_rejects_tampering_and_garbage() -> None:
    good = referral_link(101, "the_ways_bot").split("?start=", 1)[1]
    parts = good.split("_")
    assert parse_referral_arg(f"ref_{parts[1]}_{'x' * len(parts[2])}") is None  # подделка
    assert parse_referral_arg("ref_101_zzz") is None
    assert parse_referral_arg("banana") is None
    assert parse_referral_arg("ref_") is None
    assert parse_referral_arg("ref_101") is None
    assert parse_referral_arg("ref_abc_whatever") is None
    assert parse_referral_arg("") is None
    assert parse_referral_arg(None) is None


def test_parse_referral_arg_disabled_when_secret_empty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_secret", "")
    assert parse_referral_arg("ref_42_deadbeef") is None
    assert referral_link(42, "the_ways_bot") is None


async def test_record_referral_saves_only_first_valid_pair() -> None:
    referrer = next_uid()
    newcomer = next_uid()
    other = next_uid()
    await _existing_player(referrer)

    async with SessionLocal() as session:
        assert await record_referral(session, referrer, newcomer) is True

    # Повторный приход того же новичка по чужой ссылке — первый факт остаётся.
    await _existing_player(other)
    async with SessionLocal() as session:
        assert await record_referral(session, other, newcomer) is False

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Referral).where(Referral.referred_id == newcomer)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].referrer_id == referrer


async def test_record_referral_rejects_self_and_missing_referrer() -> None:
    uid = next_uid()
    ghost = next_uid()
    await _existing_player(uid)
    async with SessionLocal() as session:
        assert await record_referral(session, uid, uid) is False  # самоссылка
        assert await record_referral(session, ghost, next_uid()) is False  # нет приглашающего


async def test_record_referral_rejects_two_link_cycle() -> None:
    """A привёл B; B пытается привести A обратно — замыкание цепочки отклоняется."""
    a, b = next_uid(), next_uid()
    await _existing_player(a)
    await _existing_player(b)
    async with SessionLocal() as session:
        assert await record_referral(session, a, b) is True
        assert await record_referral(session, b, a) is False  # A уже «предок» B

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Referral).where(Referral.referred_id.in_([a, b]))
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].referrer_id == a and rows[0].referred_id == b


async def test_record_referral_rejects_long_chain_cycle() -> None:
    """A→B→C: C не может привести A (глубокий цикл), но C может привести D."""
    a, b, c, d = next_uid(), next_uid(), next_uid(), next_uid()
    for uid in (a, b, c, d):
        await _existing_player(uid)
    async with SessionLocal() as session:
        assert await record_referral(session, a, b) is True
        assert await record_referral(session, b, c) is True
        assert await record_referral(session, c, a) is False  # A — «дедушка» C
        assert await record_referral(session, c, d) is True  # вне цикла — легально

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Referral).where(Referral.referred_id.in_([b, c, a, d]))
            )
        ).scalars().all()
        assert {row.referrer_id for row in rows} == {a, b, c}
        assert {row.referred_id for row in rows} == {b, c, d}


async def test_record_referral_middle_of_chain_cannot_pull_referrer() -> None:
    """A→B→C: B не может привести своего пригласившего A (мини-цикл)."""
    a, b, c = next_uid(), next_uid(), next_uid()
    for uid in (a, b, c):
        await _existing_player(uid)
    async with SessionLocal() as session:
        assert await record_referral(session, a, b) is True
        assert await record_referral(session, b, c) is True
        assert await record_referral(session, b, a) is False

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Referral).where(Referral.referred_id.in_([b, c, a]))
            )
        ).scalars().all()
        assert len(rows) == 2
        assert {row.referred_id for row in rows} == {b, c}


async def test_record_referral_disabled_without_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_secret", "")
    a, b = next_uid(), next_uid()
    await _existing_player(a)
    async with SessionLocal() as session:
        assert await record_referral(session, a, b) is False


async def test_invite_shows_link_count_and_pot(monkeypatch) -> None:
    referrer = next_uid()
    await _existing_player(referrer)
    for _ in range(2):
        invited = next_uid()
        async with SessionLocal() as session:
            await record_referral(session, referrer, invited)

    message = make_message("private", referrer, "/invite")
    await cmd_invite(message)
    text = message.answer.call_args.args[0]
    assert "https://t.me/the_ways_bot?start=ref" in text
    assert str(referrer) in text
    assert "Приведено: 2" in text
    # Объединение с /referral: копилка наград показывается здесь же.
    assert "В копилке:" in text
    assert await invited_count(referrer) == 2


async def test_invite_off_when_secret_or_username_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_username", "")
    uid = next_uid()
    await _existing_player(uid)
    message = make_message("private", uid, "/invite")
    await cmd_invite(message)
    assert "не открыты" in message.answer.call_args.args[0]


async def test_invite_silent_in_groups() -> None:
    uid = next_uid()
    message = make_message("group", uid, "/invite")
    await cmd_invite(message)
    assert message.answer.call_count == 0


async def test_start_via_valid_link_records_referral(monkeypatch) -> None:
    monkeypatch.setattr(player_mod, "cmd_today", AsyncMock())
    referrer, newcomer = next_uid(), next_uid()
    await _existing_player(referrer)
    payload = referral_link(referrer, "the_ways_bot").split("?start=", 1)[1]

    message = make_message("private", newcomer, f"/start {payload}")
    await cmd_start(message)

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(Referral).where(Referral.referred_id == newcomer)
            )
        ).scalars().first()
        assert row is not None
        assert row.referrer_id == referrer


async def test_start_with_garbage_args_does_not_break(monkeypatch) -> None:
    monkeypatch.setattr(player_mod, "cmd_today", AsyncMock())
    uid = next_uid()
    message = make_message("private", uid, "/start реклама-мусор")
    await cmd_start(message)
    assert message.answer.called
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Referral).where(Referral.referred_id == uid)
            )
        ).scalars().all()
        assert rows == []


async def test_menu_invite_button_shows_card_in_private() -> None:
    """Кнопка «Позвать в стаю» в пульте отдаёт ту же карточку, что /invite."""
    uid = next_uid()
    await _existing_player(uid)
    callback = SimpleNamespace(
        data="menu:invite",
        from_user=make_user(uid),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    await player_mod._menu_invite(callback)
    text = callback.message.answer.call_args.args[0]
    assert "https://t.me/the_ways_bot?start=ref" in text
    assert "Приведено: 0" in text
    callback.answer.assert_awaited_once()


async def test_menu_invite_button_directs_groups_to_private() -> None:
    """В группе кнопка не выдаёт личное — только направляет в личку."""
    uid = next_uid()
    callback = SimpleNamespace(
        data="menu:invite",
        from_user=make_user(uid),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="supergroup"),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    await player_mod._menu_invite(callback)
    callback.message.answer.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True