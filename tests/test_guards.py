"""Guard-тесты командного контура: только админ рушит игру, вебхук — с секретом."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main as main_module
from app.config import settings
from app.db import SessionLocal
from app.handlers import cmd_advance, cmd_resetgame
from app.models import Payout
from app.rounds import reset_game
from app.ton_utils import to_nano


def _message(user_id: int | None, text: str = "/resetgame") -> SimpleNamespace:
    from_user = None if user_id is None else SimpleNamespace(id=user_id)
    # bot нужен: /advance и /resetgame теперь сами снимают паузу (broadcast).
    return SimpleNamespace(
        from_user=from_user,
        answer=AsyncMock(),
        text=text,
        bot=AsyncMock(),
        chat=SimpleNamespace(type="private"),
    )


@pytest.mark.parametrize("command", [cmd_resetgame, cmd_advance])
async def test_nonadmin_cannot_reset_or_advance(
    command, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_ids", "")
    message = _message(777_777)
    await command(message)
    message.answer.assert_awaited_once()
    assert "хранителя" in message.answer.await_args.args[0]


@pytest.mark.parametrize("command", [cmd_resetgame, cmd_advance])
async def test_anonymous_channel_post_cannot_reset(
    command, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Канальные посты/анонимы приходят без from_user — доступ закрыт."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    message = _message(None)
    await command(message)
    message.answer.assert_awaited_once()


def test_webhook_without_secret_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "https://example.onrender.com")
    monkeypatch.setattr(settings, "webhook_secret", "")
    with pytest.raises(RuntimeError) as excinfo:
        main_module.ensure_webhook_secret()
    assert "WEBHOOK_SECRET" in str(excinfo.value)


def test_webhook_with_secret_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "https://example.onrender.com")
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    main_module.ensure_webhook_secret()


def test_polling_mode_needs_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "")
    monkeypatch.setattr(settings, "render_external_url", "")
    monkeypatch.setattr(settings, "render_external_hostname", "")
    monkeypatch.setattr(settings, "webhook_secret", "")
    main_module.ensure_webhook_secret()


async def test_reset_refuses_while_payouts_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сброс запрещён, пока в очереди есть неотправленные выплаты."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=None,
            player_id=None,
            kind="prize",
            amount_nanotons=to_nano(0.1),
            dest_address="0:" + os.urandom(32).hex(),
            status="pending",
            network="mainnet",
        )
        session.add(payout)
        await session.commit()
        payout_id = payout.id
        try:
            # Хендлер: вежливый отказ, сброс не выполняется.
            message = _message(42, text="/resetgame confirm")
            await cmd_resetgame(message)
            message.answer.assert_awaited_once()
            assert "неотправленных" in message.answer.await_args.args[0]

            # Второй рубеж: reset_game напрямую тоже отказывается.
            with pytest.raises(RuntimeError) as excinfo:
                await reset_game(session)
            assert "выплат" in str(excinfo.value)
        finally:
            fresh = await session.get(Payout, payout_id)
            if fresh is not None:
                await session.delete(fresh)
            await session.commit()


def test_validate_config_ok_when_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Тесты ходят по SQLite законно, поэтому явное разрешение обязано быть:
    # при ADMIN_IDS без PostgreSQL старт запрещён (advisory-лок на SQLite
    # не работает). Проверяется это отдельно в test_main_boot.py.
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "ton_enabled", False)
    monkeypatch.setattr(settings, "health_token", "s3cret")
    monkeypatch.setattr(settings, "allow_sqlite", True)
    assert main_module.validate_config() == []


def test_validate_config_flags_missing_token_and_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(settings, "admin_ids", "")
    problems = main_module.validate_config()
    assert any("BOT_TOKEN" in p for p in problems)
    assert any("ADMIN_IDS" in p for p in problems)


def test_validate_config_flags_overfull_rake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "owner_rake_pct", 50.0)
    monkeypatch.setattr(settings, "leaderboard_rake_pct", 50.0)
    monkeypatch.setattr(settings, "weekly_pot_pct", 2.0)
    monkeypatch.setattr(settings, "pack_fund_pct", 1.0)
    problems = main_module.validate_config()
    assert any("рейк" in p or "rake" in p for p in problems)


def test_validate_config_flags_treasury_when_ton_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", "")
    monkeypatch.setattr(settings, "treasury_mnemonic", "")
    problems = main_module.validate_config()
    assert any("адреса казначея" in p for p in problems)
    assert any("мнемоники казначея" in p for p in problems)


def test_validate_config_flags_owner_same_as_treasury(monkeypatch: pytest.MonkeyPatch) -> None:
    """OWNER_WALLET_ADDRESS, совпадающий с казначеем, — конфиг-ошибка:
    рейк уходил бы «сам себе» и rwatcher гонял бы бесконечный refund-цикл."""
    import os

    address = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_address", address)
    monkeypatch.setattr(settings, "treasury_mnemonic", "муза мёд")
    monkeypatch.setattr(settings, "owner_wallet_address", address)
    problems = main_module.validate_config()
    assert any("OWNER_WALLET_ADDRESS" in p and "казначе" in p for p in problems)


def test_validate_config_flags_health_require_token_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEALTH_REQUIRE_TOKEN=true при пустом HEALTH_TOKEN — конфиг-ошибка, бот не стартует."""
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "health_require_token", True)
    monkeypatch.setattr(settings, "health_token", "")
    problems = main_module.validate_config()
    assert any("HEALTH_REQUIRE_TOKEN" in p for p in problems)


def test_validate_config_ok_with_health_token_and_require(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEALTH_REQUIRE_TOKEN=true + заданный HEALTH_TOKEN — конфиг корректен."""
    monkeypatch.setattr(settings, "bot_token", "123:token")
    monkeypatch.setattr(settings, "admin_ids", "42")
    monkeypatch.setattr(settings, "health_require_token", True)
    monkeypatch.setattr(settings, "health_token", "secret123")
    problems = main_module.validate_config()
    assert not any("HEALTH_REQUIRE_TOKEN" in p for p in problems)
