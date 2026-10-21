"""Сеть и доля призового пула — в одном месте (денежные границы).

Два литерала, размазанные по коду, оба про деньги.

1. Сеть бота («mainnet»/«testnet») писалась в 19 местах инлайном. Колонка
   network у ставок и выплат — денежная граница: строки разных сетей не
   должны смешиваться, а опечатка в литерале уводила бы выплату в чужую
   сеть молча. Теперь это settings.active_network.

2. Доля призового пула (100 % минус рейк) вычислялась в четырёх копиях с
   РАЗНЫМ округлением: игрок видел разные проценты в /start, /wallet и
   /win_calc, а по ним понимает, куда уйдёт его ставка. Теперь это
   settings.prize_pool_pct (и settings.rake_total_pct для проверки старта) —
   одно место, одно округление.
"""

from __future__ import annotations

import pytest

from app.config import Settings, settings
from app.stakes import current_network


def test_active_network_follows_ton_network() -> None:
    """Сеть из настройки — одна на весь код."""
    assert settings.active_network == (
        "testnet" if settings.is_testnet else "mainnet"
    )
    assert current_network() == settings.active_network


def test_prize_pool_pct_is_complement_of_rake() -> None:
    """Пул и рейк — две стороны одного числа."""
    assert settings.prize_pool_pct == round(100.0 - settings.rake_total_pct, 2)
    assert settings.prize_pool_pct + settings.rake_total_pct == pytest.approx(100.0)


def test_prize_pool_pct_is_the_same_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Экраны показывают одну и ту же долю пула."""
    from app.handlers import wallet as wallet_mod

    monkeypatch.setattr(settings, "owner_rake_pct", 0.5)
    monkeypatch.setattr(settings, "leaderboard_rake_pct", 0.5)
    monkeypatch.setattr(settings, "weekly_pot_pct", 2.0)
    monkeypatch.setattr(settings, "pack_fund_pct", 1.0)
    monkeypatch.setattr(settings, "referral_pct", 1.0)

    expected = 95.0
    assert settings.prize_pool_pct == expected
    # /win_calc и /wallet берут одно и то же число.
    assert f"{expected:g}".replace(".", ",") in wallet_mod._win_calc_text()
    assert f"{expected:g}".replace(".", ",") in wallet_mod._economy_text()


def test_rake_total_covers_all_deductions() -> None:
    """Рейк — это все вычеты, и ни одного лишнего."""
    fields = (
        "owner_rake_pct",
        "leaderboard_rake_pct",
        "weekly_pot_pct",
        "pack_fund_pct",
        "referral_pct",
    )
    total = sum(float(getattr(settings, name)) for name in fields)
    assert settings.rake_total_pct == pytest.approx(total)


def test_settings_network_property_is_pure() -> None:
    """Свойство не мутирует настройки и не зависит от порядка вызовов."""
    fresh = Settings(ton_network="testnet")
    assert fresh.active_network == "testnet"
    assert fresh.active_network == "testnet"
    main = Settings(ton_network="mainnet")
    assert main.active_network == "mainnet"
