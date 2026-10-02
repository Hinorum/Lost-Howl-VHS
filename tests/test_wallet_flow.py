"""Диалог привязки кошелька и инструкция /stake.

/wallet без адреса открывает режим ожидания: следующее сообщение игрока —
адрес, привязка происходит без единой команды. /stake показывает адрес
казначея, шаги и статус ставки; порядок «голос/перевод» не важен.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.db import SessionLocal, init_db
from app.handlers import (
    cmd_stake,
    cmd_wallet,
    on_private_fallback,
    on_stake_view,
)
from app.handlers import wallet as wallet_mod
from app.models import Player, Round, RoundStatus, Stake, WalletDialog, WinRule
from app.ton_utils import normalize_address

# Адреса уникальны в рамках прогона: players.wallet_address имеет UNIQUE,
# а глобальная тестовая БД общая для всех модулей. Все ниже — валидные
# user-friendly (CRC16 верный), сгенерированы friendly_address().
DIALOG_ADDRESS = "UQCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqppe"
LOCKED_ADDRESS = "UQAiM0RVZneImaq7zN3u_wARIjNEVWZ3iJmqu8zd7v8AERDo"
RESTART_ADDRESS = "UQAzRFVmd4iZqrvM3e7_ABEiM0RVZneImaq7zN3u_wARIuR6"

DUP_ADDRESS = "UQBEVWZ3iJmqu8zd7v8AESIzRFVmd4iZqrvM3e7_ABEiMwrl"

_uid = 800_000


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
        answer_media_group=AsyncMock(),
    )


@pytest.fixture(scope="module", autouse=True)
async def _handlers_db():
    await init_db()
    yield


async def _dialog_active(uid: int) -> bool:
    async with SessionLocal() as session:
        return await session.get(WalletDialog, uid) is not None


@pytest.fixture(autouse=True)
def _clean_pending():
    yield


@pytest.fixture(autouse=True)
async def _wipe_dialogs_after():
    yield
    async with SessionLocal() as session:
        await session.execute(WalletDialog.__table__.delete())
        await session.commit()


async def start_dialog(uid: int) -> None:
    await cmd_wallet(make_message("private", uid, "/wallet"))


async def test_wallet_without_args_opens_dialog() -> None:
    uid = next_uid()
    message = make_message("private", uid, "/wallet")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    assert "следующим сообщением" in text
    assert await _dialog_active(uid)


async def test_next_message_binds_address() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, DIALOG_ADDRESS)
    await on_private_fallback(message)
    assert "привязан" in message.answer.call_args.args[0]
    assert not await _dialog_active(uid)


async def test_bad_address_keeps_dialog_open() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "не адрес")
    await on_private_fallback(message)
    assert "не похоже" in message.answer.call_args.args[0]
    assert await _dialog_active(uid)


async def test_cancel_word_closes_dialog() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "отмена")
    await on_private_fallback(message)
    assert "Отменено" in message.answer.call_args.args[0]
    assert not await _dialog_active(uid)


async def test_other_command_closes_dialog_silently() -> None:
    """Обычные команды перехватываются раньше fallback; если дошло сюда —
    молча выходим из режима ожидания."""
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "/чтоугодно")
    await on_private_fallback(message)
    assert message.answer.call_count == 0
    assert not await _dialog_active(uid)


async def test_dialog_survives_restart() -> None:
    """Диалог живёт в БД: после «рестарта» (новый процесс, та же БД) ожидание
    продолжает работать — перезапуск не теряет игрока посреди привязки."""
    uid = next_uid()
    await start_dialog(uid)
    # Рестарт ничего не стирает: helpers читают ту же таблицу заново.
    message = make_message("private", uid, RESTART_ADDRESS)
    await on_private_fallback(message)
    assert "привязан" in message.answer.call_args.args[0]


async def test_fallback_ignores_strangers() -> None:
    message = make_message("private", next_uid(), "какой-то текст")
    await on_private_fallback(message)
    assert message.answer.call_count == 0


async def test_rebind_locked_while_stake_in_game(monkeypatch) -> None:

    uid = next_uid()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        session.add(Player(id=uid, username="staker", wallet_address=LOCKED_ADDRESS))
        rnd = Round(
            day_index=99,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="x",

            opens_at=now,
            voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        session.add(rnd)
        await session.flush()
        session.add(
            Stake(
                round_id=rnd.id,
                player_id=uid,
                amount_nanotons=100_000_000,
                tx_hash="lock-tx",
                status="confirmed",
            )
        )
        await session.commit()

    message = make_message("private", uid, f"/wallet {DIALOG_ADDRESS}")
    await cmd_wallet(message)
    assert "закреплён до итогов дня" in message.answer.call_args.args[0]


async def test_stake_shows_treasury_and_order_note(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", "EQ" + "a" * 46)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    message = make_message("private", next_uid(), "/stake")
    await cmd_stake(message)
    text = message.answer.call_args.args[0]
    assert ("EQ" + "a" * 46) in text
    assert "Порядок не важен" in text
    assert "три шага" in text


async def test_stake_in_group_hides_details() -> None:
    message = make_message("supergroup", next_uid(), "/stake")
    await cmd_stake(message)
    kwargs = message.answer.call_args.kwargs
    assert kwargs.get("reply_markup") is not None


def _pay_keyboard_rows(markup):
    return [[(b.text, b.callback_data, b.url) for b in row] for row in markup.inline_keyboard]


async def test_pay_keyboard_multiwallet_and_copy(monkeypatch) -> None:
    """Кнопки: Keeper + Tonhub (ссылки) и «Скопировать адрес» (callback)."""
    from app.handlers import _stake_pay_keyboard

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", "UQ" + "a" * 46)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    markup = _stake_pay_keyboard()
    rows = _pay_keyboard_rows(markup)
    assert len(rows) == 2
    texts = [cell[0] for row in rows for cell in row]
    assert any("Keeper" in t for t in texts)
    assert any("Tonhub" in t for t in texts)
    assert any("Скопировать адрес" in t for t in texts)
    urls = [cell[2] for row in rows for cell in row if cell[2]]
    assert any("app.tonkeeper.com/transfer/UQ" in u for u in urls)
    assert any("tonhub.com/transfer/UQ" in u for u in urls)
    copy_cb = [cell[1] for row in rows for cell in row if cell[1] is not None]
    assert copy_cb == ["stake:copy"]


async def test_stake_copy_button_sends_address(monkeypatch) -> None:
    from app.handlers import on_stake_copy

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", "UQ" + "a" * 46)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    callback = SimpleNamespace(
        data="stake:copy",
        from_user=make_user(next_uid()),
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await on_stake_copy(callback)
    sent = callback.message.answer.call_args.args[0]
    assert "Адрес Фонда игры" in sent
    assert "UQ" + "a" * 46 in sent
    assert "<code>" in sent
    assert "привязанного кошелька" in sent

    # Выключенный TON — вежливый отказ.
    monkeypatch.setattr(settings, "ton_enabled", False)
    off_cb = SimpleNamespace(
        data="stake:copy",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await on_stake_copy(off_cb)
    args, kwargs = off_cb.answer.call_args
    assert "выключен" in args[0]
    assert kwargs.get("show_alert") is True


async def test_stake_button_alert_short() -> None:
    callback = SimpleNamespace(
        data="stake:view",
        from_user=make_user(next_uid()),
        message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
        answer=AsyncMock(),
    )
    await on_stake_view(callback)
    args, kwargs = callback.answer.call_args
    assert len(args[0]) <= 200
    assert kwargs.get("show_alert") is True


async def _link_wallet(uid: int, address: str) -> None:
    await start_dialog(uid)
    message = make_message("private", uid, address)
    await on_private_fallback(message)


async def _clear_cooldown(uid: int) -> None:
    """start_dialog зовёт /wallet и записывает кулдаун wallet_cd:<uid> — чистим."""
    from app.models import WatcherState

    async with SessionLocal() as session:
        row = await session.get(WatcherState, f"wallet_cd:{uid}")
        if row is not None:
            await session.delete(row)
            await session.commit()


async def test_wallet_answers_fallback_when_builder_breaks(monkeypatch) -> None:
    """/wallet обязан ответить даже при сбое сборки вида — статичной инструкцией."""
    uid = next_uid()
    fallback_address = "UQCrq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq5jh"
    await _link_wallet(uid, fallback_address)

    async def boom(user):
        raise RuntimeError("внезапный сбой сборки")

    monkeypatch.setattr(wallet_mod, "_wallet_view_text", boom)
    await _clear_cooldown(uid)  # привязка выше уже потратила окно троттлинга
    message = make_message("private", uid, "/wallet")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    assert "Раздел кошелька" in text
    assert "/wallet UQ…" in text  # путь привязки продиктован


async def test_stake_answers_fallback_when_builder_breaks(monkeypatch) -> None:
    async def boom(user):
        raise RuntimeError("внезапный сбой сборки")

    monkeypatch.setattr(wallet_mod, "_stake_view_text", boom)
    message = make_message("private", next_uid(), "/stake")
    await cmd_stake(message)
    text = message.answer.call_args.args[0]
    assert "три шага" in text


async def test_wallet_survives_total_inner_crash(monkeypatch) -> None:
    """Падение на любом шаге (БД, диалог) не оставляет игрока без ответа."""
    uid = next_uid()

    async def db_down(session, user):
        raise RuntimeError("db down")

    monkeypatch.setattr(wallet_mod, "upsert_player", db_down)
    message = make_message("private", uid, "/wallet")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    assert "Раздел кошелька" in text
    assert "/wallet UQ…" in text
    # Диалог привязки при упавшей команде не открывается.
    assert not await _dialog_active(uid)


import re as _re

_HTML_TAG = _re.compile(r"<(?!/?(?:code|b|i|u|s|pre|a|blockquote)\b)[^>]{1,30}>")


async def test_wallet_view_html_is_telegram_safe(monkeypatch) -> None:
    """Регрессия прод-сбоя: «/wallet <адрес>» парсился Telegram как битый тег.

    Вид кошелька уходит с parse_mode=HTML — кириллических псевдотегов и
    любых незнакомых угловых скобок в нём быть не должно.
    """
    from pathlib import Path

    from app.handlers import _wallet_view_text

    # 1. Сам источник больше не содержит литерал-виновника. Исходник разъехался
    # на пакет — скан распространяется на все модули app/handlers/.
    pkg_dir = Path(__file__).resolve().parents[1] / "app" / "handlers"
    sources = [pkg_dir / f if f != "__init__.py" else None
               for f in ("common.py", "player.py", "wallet.py", "topup.py",
                         "admin.py", "payout.py", "panel.py", "fallback.py",
                         "bootstrap.py", "__init__.py")]
    joined = "\n".join(
        p.read_text(encoding="utf-8") for p in sources if p is not None and p.exists()
    )
    assert "<адрес" not in joined

    # 2. Сгенерированный вид (обе ветки) проходит Telegram-парсер.
    monkeypatch.setattr(settings, "ton_enabled", True)
    uid = next_uid()
    linked_address = "UQBVZneImaq7zN3u_wARIjNEVWZ3iJmqu8zd7v8AESIzRJBb"
    await _seed_open_day_with_stake(uid, day_index=99_099, amount_nanotons=100_000_000, address=linked_address)
    try:
        user = make_user(uid)
        text = await _wallet_view_text(user)
        assert _HTML_TAG.search(text) is None
        assert "<code>" in text and "</code>" in text
    finally:
        await _cleanup_open_day(99_099)


async def _seed_open_day_with_stake(uid: int, day_index: int, amount_nanotons: int, address: str) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        session.add(Player(id=uid, username=f"viewer{uid}", wallet_address=address))
        rnd = Round(
            day_index=day_index,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            chapter_title="t",
            chapter_text="x",

            opens_at=now,
            voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        session.add(rnd)
        await session.flush()
        session.add(
            Stake(
                round_id=rnd.id,
                player_id=uid,
                amount_nanotons=amount_nanotons,
                tx_hash=f"view-tx-{day_index}",
                status="confirmed",
            )
        )
        await session.commit()


async def _cleanup_open_day(day_index: int) -> None:
    from sqlalchemy import delete as sa_delete

    async with SessionLocal() as session:
        await session.execute(sa_delete(Stake).where(Stake.tx_hash == f"view-tx-{day_index}"))
        await session.execute(sa_delete(Round).where(Round.day_index == day_index))
        await session.commit()


async def test_stake_button_alert_shows_amount(monkeypatch) -> None:
    """Кнопка в группе показывает личную сумму ставки: попап виден только нажавшему."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    uid = next_uid()
    address = "UQABAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAZAm"
    # День с большим индексом, чем у соседних тестов модуля: активным
    # становится именно он (get_active_round берёт последний).
    await _seed_open_day_with_stake(uid, day_index=99_098, amount_nanotons=250_000_000, address=address)
    try:
        callback = SimpleNamespace(
            data="stake:view",
            from_user=make_user(uid),
            message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
            answer=AsyncMock(),
        )
        await on_stake_view(callback)
        text = callback.answer.call_args.args[0]
        assert "0.25 Gram" in text
        assert "подтверждена" in text
        assert len(text) <= 200
    finally:
        await _cleanup_open_day(99_098)


async def test_wallet_view_includes_today_stake(monkeypatch) -> None:
    """/wallet показывает вклад в фонд текущего дня рядом с адресом кошелька."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    uid = next_uid()
    address = "UQAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIMV5"
    await _seed_open_day_with_stake(uid, day_index=99_097, amount_nanotons=600_000_000, address=address)
    try:
        message = make_message("private", uid, "/wallet")
        await cmd_wallet(message)
        text = message.answer.call_args.args[0]
        assert "0.6 Gram" in text
        assert "Ставка сегодня" in text
        assert "привязанный кошелёк" in text.lower()
    finally:
        await _cleanup_open_day(99_097)


async def test_stake_button_without_wallet_hints_dialog(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    callback = SimpleNamespace(
        data="stake:view",
        from_user=make_user(next_uid()),
        message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
        answer=AsyncMock(),
    )
    await on_stake_view(callback)
    text = callback.answer.call_args.args[0]
    assert "не привязан" in text
    assert "/wallet" in text


async def test_wallet_rate_limited_on_spam(monkeypatch) -> None:
    """Второй /wallet тем же игроком подряд встречает троттлинг, диалог
    не переоткрывается; админ и «остывший» игрок проходят свободно."""
    uid = next_uid()
    first = make_message("private", uid, "/wallet")
    await cmd_wallet(first)
    assert "следующим сообщением" in first.answer.call_args.args[0]

    second = make_message("private", uid, "/wallet")
    await cmd_wallet(second)
    assert "Не так часто" in second.answer.call_args.args[0]
    assert await _dialog_active(uid)  # старый диалог не тронут

    # Остывание: обнуляем окно — снова пускает.
    monkeypatch.setattr(wallet_mod, "_WALLET_COOLDOWN", 0.0)
    third = make_message("private", uid, "/wallet")
    await cmd_wallet(third)
    assert "Не так часто" not in third.answer.call_args.args[0]


async def test_wallet_throttle_expires_with_time(monkeypatch) -> None:
    """Окно троттлинга конечное: после остывания команда снова работает.

    Кулдаун живёт в watcher_state (запись wallet_cd:<uid>) — «остывание»
    моделируется возрастом записи, как это видит второй инстанс бота.
    """
    from app.models import WatcherState

    uid = next_uid()
    cooldown_key = f"wallet_cd:{uid}"

    first = make_message("private", uid, "/wallet")
    await cmd_wallet(first)
    assert "Не так часто" not in first.answer.call_args.args[0]

    second = make_message("private", uid, "/wallet")
    await cmd_wallet(second)
    assert "Не так часто" in second.answer.call_args.args[0]

    # Старим запись за пределы окна кулдауна — как по прошествии времени.
    async with SessionLocal() as session:
        row = await session.get(WatcherState, cooldown_key)
        assert row is not None
        row.value = (datetime.now(UTC) - timedelta(seconds=40)).isoformat()
        await session.commit()

    third = make_message("private", uid, "/wallet")
    await cmd_wallet(third)
    assert "Не так часто" not in third.answer.call_args.args[0]


async def test_duplicate_wallet_bind_is_rejected_not_crash() -> None:
    """Регресс-ловушка: привязка адреса, уже закреплённого за другим игроком,
    раньше падала на необработанном IntegrityError (unique=wallet_address)."""
    owner_uid = next_uid()
    thief_uid = next_uid()
    async with SessionLocal() as session:
        # Владелец уже завязал адрес на себя (в той же канонической raw-форме,
        # что хранит _bind_wallet через normalize_address).
        session.add(
            Player(
                id=owner_uid,
                username=f"owner{owner_uid}",
                wallet_address=normalize_address(DUP_ADDRESS),
            )
        )
        await session.commit()

    msg = make_message("private", thief_uid, f"/wallet {DUP_ADDRESS}")
    await cmd_wallet(msg)
    text = msg.answer.call_args.args[0]
    assert "уже привязан" in text
    assert "другому игроку" in text
    # У второго игрока адрес так и не записан, у владельца цел.
    async with SessionLocal() as session:
        owner = await session.get(Player, owner_uid)
        thief = await session.get(Player, thief_uid)
        assert owner is not None and owner.wallet_address == normalize_address(DUP_ADDRESS)
        assert thief is not None and thief.wallet_address is None
