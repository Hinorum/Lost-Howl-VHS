"""Тесты каркаса TON: адреса, конвертация, делёж фонда, ставки и выплаты."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stakes as stakes_mod
from app.config import settings
from app.models import Payout, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.ton_utils import friendly_address, from_nano, is_valid_ton_address, normalize_address, to_nano
from app.weeks import iso_week_key

USER_FRIENDLY = "EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF"
RAW = "0:ca6e321c7cce9ecedf0a8ca2492ec8592494aa5fb5ce0387dff96ef6af982a3e"


def test_address_validation() -> None:
    assert is_valid_ton_address(USER_FRIENDLY)
    assert is_valid_ton_address(RAW)
    assert not is_valid_ton_address("not an address")
    assert not is_valid_ton_address("EQshort")
    assert not is_valid_ton_address("0:zzzz")


def test_normalize_address_matches_friendly_and_raw() -> None:
    assert normalize_address(USER_FRIENDLY) == normalize_address(RAW)


def test_normalize_address_never_raises_on_garbage() -> None:
    """Неразбираемые строки не роняют watcher/миграции: канонизация только
    того, что разобралось. Реальный инцидент-класс: легаси-запись в БД или
    источник блокчейна с битым base64 валит весь цикл исключением."""
    # Правильная длина и префикс, но битый base64url.
    assert normalize_address("UQ" + "!" * 46) == ("uq" + "!" * 46)
    # Правильный base64, но неверная длина декода (35 байт вместо 36).
    import base64 as _b64

    short = _b64.b64encode(b"\x51\x00" + b"\x01" * 33).decode()
    assert normalize_address(short) == short.lower()
    # Мусор других форм — насквозь.
    assert normalize_address("мусор") == "мусор"
    assert normalize_address("") == ""


def test_friendly_address_roundtrip() -> None:
    """friendly_address обращает normalize_address: CRC и теги сходятся."""
    for source in (
        USER_FRIENDLY,  # mainnet bounceable EQ…
        "UQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPuwA",  # mainnet non-bounce
        "0QDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPleK",  # testnet non-bounce 0Q…
        "kQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPgpP",  # testnet bounceable kQ…
        RAW,
    ):
        raw = normalize_address(source)
        assert is_valid_ton_address(source)
        assert len(source) == 48 or source == RAW
        for testnet in (False, True):
            for bounceable in (False, True):
                shown = friendly_address(raw, testnet=testnet, bounceable=bounceable)
                assert is_valid_ton_address(shown)
                assert len(shown) == 48
                # Префикс отвечает сети и bounce-биту: mainnet EQ/UQ, testnet kQ/0Q.
                expected_prefix = {(False, False): "UQ", (False, True): "EQ", (True, False): "0Q", (True, True): "kQ"}[
                    (testnet, bounceable)
                ]
                assert shown[:2] == expected_prefix
                assert normalize_address(shown) == raw
    # Мусор на входе проходит насквозь, а не падает.
    assert friendly_address("мусор") == "мусор"
    assert friendly_address("ff:00") == "ff:00"


def test_nano_conversion_roundtrip() -> None:
    assert to_nano(1.5) == 1_500_000_000
    assert from_nano(1_500_000_000) == 1.5
    assert to_nano(0.1) == 100_000_000


async def test_wallet_migration_survives_duplicate_raw(session) -> None:
    """Регресс-ловушка: миграция легаси-привязок одного кошелька (дружественная
    форма у одного, raw — у другого) раньше падала на unique=wallet_address при
    commit и канселила каждый раунд watcher'а. Теперь дубль решается без падения."""
    from sqlalchemy import select as sa_select

    from app.models import WatcherState
    from app.ton_watch import WALLET_NORM_KEY, _migrate_wallet_formats

    # Два игрока держат один кошелёк в разных формах, обе -> 0:83df...
    session.add_all(
        [
            Player(id=900_001, username="friendly", wallet_address=USER_FRIENDLY),
            Player(id=900_000, username="raw", wallet_address=RAW),
        ]
    )
    await session.commit()

    await _migrate_wallet_formats(session)  # не должно бросить IntegrityError

    rows = (
        (await session.execute(sa_select(Player.id, Player.wallet_address))).all()
    )
    holders = {pid: addr for pid, addr in rows if addr}
    assert len(holders) == 2
    # Уникальность = барьер unique=wallet_address не нарушен: у всех хранящиеся
    # строки попарно различны (иначе commit бы бросил и тест упал выше).
    literals = list(holders.values())
    assert len(literals) == len(set(literals))
    # Пострадавший от дубля не «сломал» миграцию: канонический raw закреплён
    # за своим законным держателем (raw-форма уже канонична и не тронута).
    assert holders[900_000] == RAW
    # Флаг проставлен — миграция идемпотентна, повтор не нужен.
    assert await session.get(WatcherState, WALLET_NORM_KEY) is not None



def test_split_pot_proportional_with_dust_to_largest() -> None:
    # 10 TON на троих с долями 5/3/2 → 5.0/3.0/2.0 ровно.
    shares = dict(stakes_mod.split_pot(to_nano(10), [(1, to_nano(5)), (2, to_nano(3)), (3, to_nano(2))]))
    assert shares == {1: to_nano(5), 2: to_nano(3), 3: to_nano(2)}
    # Пыль: 101 нанотон между двумя равными ставками — пыль старшему по ставке,
    # при равенстве — меньшему id.
    shares = dict(stakes_mod.split_pot(101, [(7, 50), (3, 50)]))
    assert sum(shares.values()) == 101
    assert shares[3] == 51 and shares[7] == 50
    # Крупная ставка получает пыль.
    shares = dict(stakes_mod.split_pot(101, [(4, 60), (9, 40)]))
    assert shares[4] == 61 and shares[9] == 40


def test_split_pot_edge_cases() -> None:
    assert stakes_mod.split_pot(0, [(1, 100)]) == []
    assert stakes_mod.split_pot(100, []) == []


async def make_closed_round(
    session: AsyncSession, winner_card: int, day_index: int = 1
) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        winner_card=winner_card,
        vote_counts_json="{}",
    )
    session.add(round_row)
    await session.flush()
    return round_row


async def test_register_stake_flow(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    player = Player(id=11, username="u")
    session.add(player)
    now = datetime.now(UTC)
    round_row = Round(
        day_index=1,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )
    session.add(round_row)
    await session.commit()

    result = await stakes_mod.register_stake(session, round_row, player, to_nano(1), "tx1")
    assert result == "ok"
    # Повтор той же транзакции и вторая ставка тем же игроком отклоняются.
    assert await stakes_mod.register_stake(session, round_row, player, to_nano(1), "tx1") == "duplicate_tx"
    assert await stakes_mod.register_stake(session, round_row, player, to_nano(2), "tx2") == "already_staked"
    # Лимиты: отклонённая микровставка НЕ занимает слот игрока в дне — валидная
    # ставка записывается на её место, а пылевой остаток возвращается сразу
    # (тем же расчётом, что и поток финализации). Верхней границы нет —
    # крупная сумма принимается как обычная ставка.
    other = Player(id=12)
    session.add(other)
    third = Player(id=13)
    session.add(third)
    await session.commit()
    assert await stakes_mod.register_stake(session, round_row, other, to_nano(0.01), "tx3") == "too_small"
    assert await stakes_mod.register_stake(session, round_row, other, to_nano(50), "tx4") == "ok"
    stake_row = (
        await session.execute(select(Stake).where(Stake.tx_hash == "tx4"))
    ).scalar_one()
    assert stake_row.status == "pending" and stake_row.amount_nanotons == to_nano(50)
    # Пылевая 0.01 Gram не потерялась: возврат той же величины расcчитан.
    refund_row = (
        await session.execute(
            select(Payout).where(Payout.kind == "refund", Payout.player_id == 12)
        )
    ).scalar_one_or_none()
    assert refund_row is not None
    assert refund_row.amount_nanotons == stakes_mod.refund_net_amount(to_nano(0.01))
    # Игрок с уже обычной пылевой в добавку к валидной ставке — снова already_staked.
    assert await stakes_mod.register_stake(session, round_row, other, to_nano(0.01), "tx3b") == "already_staked"
    assert await stakes_mod.register_stake(session, round_row, third, to_nano(500), "tx5") == "ok"
    # Выключенная интеграция и закрытый день.
    monkeypatch.setattr(settings, "ton_enabled", False)
    fourth = Player(id=14)
    session.add(fourth)
    await session.commit()
    assert await stakes_mod.register_stake(session, round_row, fourth, to_nano(1), "tx6") == "disabled"
    round_row.status = RoundStatus.CLOSED
    await session.commit()
    monkeypatch.setattr(settings, "ton_enabled", True)
    assert await stakes_mod.register_stake(session, round_row, fourth, to_nano(1), "tx7") == "closed"


async def test_dust_refund_waits_for_unverified_wallet(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пылевой возврат не уходит на недоказанный bv: адрес.

    Привязка сама по себе не доказывает владение: после перепривязки на чужой
    адрес пыль ушла бы в bounce. Поэтому dest пустой — сумма не теряется, её
    подставит _hydrate_player_dests, когда игрок подтвердит кошелёк. Ставку
    при этом не блокируем: это возврат, а не приём денег.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    now = datetime.now(UTC)
    round_row = Round(
        day_index=555,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",
        opens_at=now - timedelta(hours=1),
        voting_ends_at=now + timedelta(hours=1),
        tally_ends_at=now + timedelta(hours=2),
    )
    session.add(round_row)
    player = Player(id=91, username="dust", wallet_address="EQforeign", wallet_verified=False)
    session.add(player)
    await session.commit()

    assert await stakes_mod.register_stake(session, round_row, player, to_nano(0.01), "dust-1") == "too_small"
    assert await stakes_mod.register_stake(session, round_row, player, to_nano(2), "dust-2") == "ok"

    refund = (
        await session.execute(
            select(Payout).where(Payout.kind == "refund", Payout.player_id == 91)
        )
    ).scalar_one()
    # Ждёт подтверждения, а не улетает на перепривязанный адрес.
    assert refund.dest_address == ""
    assert refund.amount_nanotons == stakes_mod.refund_net_amount(to_nano(0.01))
    # Валидная ставка на месте — возврат не должен был её сломать.
    stake_row = (
        await session.execute(select(Stake).where(Stake.tx_hash == "dust-2"))
    ).scalar_one()
    assert stake_row.status == "pending" and stake_row.amount_nanotons == to_nano(2)


def test_split_equal_dust_to_smallest_id() -> None:
    shares = stakes_mod.split_equal(to_nano(1), [5, 2, 9])
    assert shares == {2: 333_333_334, 5: 333_333_333, 9: 333_333_333}
    assert stakes_mod.split_equal(0, [1, 2]) == {}
    assert stakes_mod.split_equal(100, []) == {}


async def test_finalize_payouts_proportional_and_rake(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper-wallet")
    for pid in (1, 2, 3):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0)
    # Победил путь 0; голоса: игроки 1 и 2, ставки только у 1 и 3.
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Vote(round_id=round_row.id, player_id=3, card_position=1),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(6), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=3, amount_nanotons=to_nano(4), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    payouts = list((await session.execute(Payout.__table__.select())).mappings())
    by_kind_player = {(row["kind"], row["player_id"]): row for row in payouts}
    pot = to_nano(10)
    house = pot * 50 // 10_000          # 0,5%
    board_cut = pot * 50 // 10_000      # 0,5%
    weekly_cut = pot * 200 // 10_000    # 2% — копилка недели
    fund_cut = pot * round(settings.pack_fund_pct * 100) // 10_000  # 1% — Фонд Стаи
    prize_pool = pot - house - board_cut - weekly_cut - fund_cut
    fee = to_nano(settings.payout_fee_gram)  # газ сети: один перевод — один вычет
    assert round_row.pot_nanotons == pot
    assert round_row.rake_nanotons == house + board_cut
    # Игрок 1 — единственный ставивший на верный путь: весь призовой фонд минус газ.
    assert by_kind_player[("prize", 1)]["amount_nanotons"] == prize_pool - fee
    assert by_kind_player[("prize", 1)]["dest_address"] == "wallet-1"
    # Игрок 3 проиграл — строки выплаты нет, ставка сгорает.
    assert ("prize", 3) not in by_kind_player
    # Микровыплат угадавшим без ставки больше нет — их 2% капает в копилку недели.
    assert all(kind != "bonus" for kind, _pid in by_kind_player)
    # Консервация фонда: приз (чистыми) + рейк + копилка недели + копилка месяца + фонд + газ = весь пул.
    assert sum(row["amount_nanotons"] for row in payouts) + board_cut + weekly_cut + fund_cut + fee == pot
    # Доли казны уходят хранителю без игрока.
    assert by_kind_player[("rake", None)]["dest_address"] == "keeper-wallet"
    # Копилка месяца — отдельный учёт в LeaderboardPot, а не Payout.
    assert len(payouts) == created == 2
    # Копилка месяца записана отдельной строкой учёта, недели — своей.
    pots = list((await session.execute(stakes_mod.LeaderboardPot.__table__.select())).mappings())
    assert len(pots) == 1 and pots[0]["nanotons"] == board_cut
    weeks = list((await session.execute(stakes_mod.WeeklyPot.__table__.select())).mappings())
    assert len(weeks) == 1 and weeks[0]["nanotons"] == weekly_cut
    assert weeks[0]["week"] == iso_week_key(round_row.opens_at)
    assert round_row.weekly_nanotons == weekly_cut
    # Фонд Стаи накопил свой процент отдельной строкой.
    funds = list((await session.execute(stakes_mod.PackFund.__table__.select())).mappings())
    assert len(funds) == 1 and funds[0]["nanotons"] == fund_cut
    # Прозрачный журнал фонда зафиксировал поступление этого дня.
    ledger = list((await session.execute(stakes_mod.PackFundLedger.__table__.select())).mappings())
    assert len(ledger) == 1
    assert ledger[0]["entry_type"] == "in"
    assert ledger[0]["amount_nanotons"] == fund_cut
    assert ledger[0]["round_id"] == round_row.id
    # Повторный вызов ничего не меняет.
    assert await stakes_mod.finalize_day_payouts(session, round_row) == 0


async def test_record_fund_dispense_logs_out_and_decrements(session: AsyncSession) -> None:
    """Прозрачная раздача фонда: списывает баланс и пишет «out»-строку в журнал."""
    from sqlalchemy import func, select

    session.add(stakes_mod.PackFund(nanotons=to_nano(3)))
    await session.commit()

    res = await stakes_mod.record_fund_dispense(session, to_nano(1), "приз забега")
    assert "раздача" in res
    fund = (await session.execute(select(stakes_mod.PackFund))).scalars().one()
    assert fund.nanotons == to_nano(2)
    ledger = (await session.execute(select(stakes_mod.PackFundLedger))).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].entry_type == "out"
    assert ledger[0].amount_nanotons == to_nano(1)
    assert ledger[0].note == "приз забега"

    # Запись сверх баланса отменяется без изменений.
    res = await stakes_mod.record_fund_dispense(session, to_nano(99))
    assert "в фонде меньше" in res
    fund = (await session.execute(select(stakes_mod.PackFund))).scalars().one()
    assert fund.nanotons == to_nano(2)
    assert (
        await session.scalar(
            select(func.count()).select_from(stakes_mod.PackFundLedger)
        )
    ) == 1

    # Неположительная сумма отклоняется.
    assert "положительн" in await stakes_mod.record_fund_dispense(session, 0)


async def test_finalize_routes_free_pool_without_recipients(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Угадавшие без ставки (даже без кошелька) — их 2% целиком в копилку недели."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    session.add(Player(id=1, wallet_address="wallet-1", wallet_verified=True))
    session.add(Player(id=2))  # угадал, но кошелька нет
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(5), tx_hash="a", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    pot = to_nano(5)
    weekly_cut = pot * 200 // 10_000
    payouts = list((await session.execute(Payout.__table__.select())).mappings())
    by_kind = {row["kind"]: row for row in payouts}
    assert "bonus" not in by_kind
    assert by_kind["prize"]["amount_nanotons"] == (
        pot - pot * 50 // 10_000 - pot * 50 // 10_000 - weekly_cut - pot * round(settings.pack_fund_pct * 100) // 10_000 - to_nano(settings.payout_fee_gram)
    )
    assert created == 2  # приз + рейк (копилка месяца — в LeaderboardPot)
    weeks = list((await session.execute(stakes_mod.WeeklyPot.__table__.select())).mappings())
    assert len(weeks) == 1 and weeks[0]["nanotons"] == weekly_cut


async def test_leaderboard_pot_accumulates_across_days(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    for pid in (1,):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    first = await make_closed_round(session, winner_card=0, day_index=101)
    second = await make_closed_round(session, winner_card=0, day_index=102)
    second.tally_ends_at += timedelta(days=12_000)  # другой месяц
    session.add_all(
        [
            Vote(round_id=first.id, player_id=1, card_position=0),
            Stake(round_id=first.id, player_id=1, amount_nanotons=to_nano(2), tx_hash="a", status="confirmed"),
            Vote(round_id=second.id, player_id=1, card_position=0),
            Stake(round_id=second.id, player_id=1, amount_nanotons=to_nano(3), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    await stakes_mod.finalize_day_payouts(session, first)
    await stakes_mod.finalize_day_payouts(session, second)
    pots = {
        row["month"]: row["nanotons"]
        for row in (
            await session.execute(stakes_mod.LeaderboardPot.__table__.select())
        ).mappings()
    }
    assert len(pots) == 2
    assert set(pots.values()) == {to_nano(2) * 50 // 10_000, to_nano(3) * 50 // 10_000}


async def test_finalize_refunds_when_no_winning_stakes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    for pid in (1, 2):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=2)
    # Победивший путь не выбрал ни один игрок со ставкой — делить нечего,
    # все подтверждённые ставки возвращаются.
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(3), tx_hash="a", status="confirmed"),
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(7), tx_hash="b", status="confirmed"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    assert created == 2
    payouts = list((await session.execute(Payout.__table__.select())).mappings())
    assert all(row["kind"] == "refund" for row in payouts)
    assert all(row["amount_nanotons"] > 0 for row in payouts)
    assert round_row.rake_nanotons == 0


async def test_finalize_skips_unconfirmed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    session.add(Player(id=1, wallet_address="w", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0)
    session.add(
        Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(1), tx_hash="a", status="pending")
    )
    await session.commit()
    assert await stakes_mod.finalize_day_payouts(session, round_row) == 1
    assert round_row.payouts_finalized is True
    # Неподтверждённая ставка возвращается (минус газ на перевод), а не оседает в казначее.
    payout = (await session.execute(Payout.__table__.select())).first()._mapping
    assert payout["kind"] == "refund"
    assert payout["amount_nanotons"] == to_nano(1) - to_nano(settings.payout_fee_gram)


async def test_networks_are_isolated(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "keeper")
    for pid in (1, 2):
        session.add(Player(id=pid, wallet_address=f"wallet-{pid}", wallet_verified=True))
    round_row = await make_closed_round(session, winner_card=0)
    session.add_all(
        [
            Vote(round_id=round_row.id, player_id=1, card_position=0),
            Vote(round_id=round_row.id, player_id=2, card_position=0),
            # mainnet-ставка победителя.
            Stake(round_id=round_row.id, player_id=1, amount_nanotons=to_nano(5), tx_hash="m", status="confirmed", network="mainnet"),
            # testnet-ставка того же дня: другой контур.
            Stake(round_id=round_row.id, player_id=2, amount_nanotons=to_nano(7), tx_hash="t", status="confirmed", network="testnet"),
        ]
    )
    await session.commit()

    created = await stakes_mod.finalize_day_payouts(session, round_row)
    # mainnet: приз игроку 1 + рейк — 2 строки;
    # копилка месяца — в LeaderboardPot, копилка недели — в WeeklyPot.
    assert created == 2
    payouts = list((await session.execute(Payout.__table__.select())).mappings())
    assert all(row["network"] == "mainnet" for row in payouts)
    pot = to_nano(5)
    by_kind_player = {(row["kind"], row["player_id"]): row for row in payouts}
    assert by_kind_player[("prize", 1)]["amount_nanotons"] == (
        pot - pot * 50 // 10_000 * 2 - pot * 200 // 10_000 - pot * round(settings.pack_fund_pct * 100) // 10_000 - to_nano(settings.payout_fee_gram)
    )
    assert all(kind != "bonus" for kind, _pid in by_kind_player)
    # Фонд посчитан только по активной сети.
    assert round_row.pot_nanotons == to_nano(5)

    # Переключение контура: testnet финализируется независимо.
    monkeypatch.setattr(settings, "ton_network", "testnet")
    round_row.payouts_finalized = False
    await session.commit()
    created = await stakes_mod.finalize_day_payouts(session, round_row)
    assert created == 2
    payouts = list((await session.execute(Payout.__table__.select().order_by(Payout.id))).mappings())
    assert len(payouts) == 4
    assert all(row["network"] == "testnet" for row in payouts[2:])
    test_pot = to_nano(7)
    test_rows = {
        (row["kind"], row["player_id"]): row
        for row in (
            await session.execute(Payout.__table__.select().order_by(Payout.id))
        ).mappings()
    }
    assert test_rows[("prize", 2)]["amount_nanotons"] == (
        test_pot - test_pot * 50 // 10_000 * 2 - test_pot * 200 // 10_000 - test_pot * round(settings.pack_fund_pct * 100) // 10_000 - to_nano(settings.payout_fee_gram)
    )
    # Копилка недели накопила долю обеих сетей одного дня.
    weeks = list((await session.execute(stakes_mod.WeeklyPot.__table__.select())).mappings())
    assert len(weeks) == 1
    assert weeks[0]["nanotons"] == pot * 200 // 10_000 + test_pot * 200 // 10_000
    assert round_row.pot_nanotons == to_nano(7)


async def test_register_stamps_active_network(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    player = Player(id=21, wallet_address="w")
    session.add(player)
    now = datetime.now(UTC)
    round_row = Round(
        day_index=2,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )
    session.add(round_row)
    await session.commit()
    assert await stakes_mod.register_stake(session, round_row, player, to_nano(1), "tx-net") == "ok"
    stake = (await session.execute(Stake.__table__.select())).first()._mapping
    assert stake["network"] == "testnet"


async def test_confirm_stake_respects_network(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """confirm_stake не подтверждает ставку чужой сети при коллизии tx_hash."""
    monkeypatch.setattr(settings, "ton_network", "testnet")
    player = Player(id=31, wallet_address="w")
    session.add(player)
    round_row = _open_round(31)
    session.add(round_row)
    await session.commit()
    # Ставка из другой сети (mainnet) с тем же tx_hash.
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=player.id,
            amount_nanotons=to_nano(1),
            tx_hash="tx-cross",
            memo="m",
            network="mainnet",
            status="pending",
        )
    )
    await session.commit()
    assert await stakes_mod.confirm_stake(session, "tx-cross") is False
    wrong = (await session.execute(select(Stake))).scalars().one()
    assert wrong.status == "pending"
    # Ставка активной сети подтверждается тем же хешем.
    wrong.network = "testnet"
    await session.commit()
    assert await stakes_mod.confirm_stake(session, "tx-cross") is True


def _open_round(day_index: int, status: RoundStatus = RoundStatus.OPEN) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=status,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )


async def test_wallet_rebind_locked_with_active_stake(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пока ставка в незакрытом дне — перепривязка кошелька запрещена.

    Хендлеры работают с глобальной БД, поэтому данные сеем в неё и
    используем уникальные значения, чтобы прогоны не конфликтовали.
    """
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db import SessionLocal
    from app.handlers import cmd_wallet

    pid = 700_000 + int.from_bytes(os.urandom(2), "big")
    wallet_old = "0:" + os.urandom(32).hex()
    wallet_new = "0:" + os.urandom(32).hex()
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet_old))
        round_row = _open_round(day_index=900_000)
        db.add(round_row)
        await db.flush()
        stake = Stake(
            round_id=round_row.id,
            player_id=pid,
            amount_nanotons=to_nano(1),
            tx_hash=f"lock-{pid}",
            status="confirmed",
        )
        db.add(stake)
        await db.commit()

        try:
            message = SimpleNamespace(
                chat=SimpleNamespace(type="private"),
                from_user=SimpleNamespace(id=pid, username=f"u{pid}", first_name="Т"),
                text=f"/wallet {wallet_new}",
                answer=AsyncMock(),
            )
            await cmd_wallet(message)
            assert "закреплён" in message.answer.call_args.args[0]
            player = await db.get(Player, pid)
            assert player.wallet_address == wallet_old
        finally:
            await db.delete(stake)
            await db.delete(round_row)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_unknown_sender_transfer_is_auto_refunded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Перевод с непривязанного кошелька уходит в очередь возврата, а не в туман."""
    import os

    from app.db import SessionLocal
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    source = "0:" + os.urandom(32).hex()
    tx_hash = "stray-" + os.urandom(8).hex()
    transfer = Transfer(
        tx_hash=tx_hash, source=source, value_nanotons=to_nano(0.3), comment="", utime=int(datetime.now(UTC).timestamp())
    )
    async with SessionLocal() as db:
        try:
            assert await process_transfer(transfer) == "refund_queued"
            row = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))
            ).first()._mapping
            assert row["kind"] == "refund"
            assert row["dest_address"] == source
            assert row["amount_nanotons"] == to_nano(0.3)
            assert row["round_id"] is None
            assert row["player_id"] is None
            assert row["status"] == "pending"

            # Повторная обработка транзакции не плодит вторую выплату.
            assert await process_transfer(transfer) == "refund_duplicated"
            rows = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))
            ).all()
            assert len(rows) == 1
        finally:
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash == tx_hash))
            await db.commit()


async def test_owner_bank_credit_is_kept_not_refunded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пополнение казны владельцем (мемо bank: с OWNER_WALLET_ADDRESS) не
    возвращается и не становится ставкой: пишется строкой входящего дохода."""
    import os

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Income
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    owner = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "owner_wallet_address", owner)
    tx_hash = "bank-" + os.urandom(8).hex()
    transfer = Transfer(
        tx_hash=tx_hash,
        source=owner,
        value_nanotons=to_nano(1.0),
        comment="bank: депозит после тестов",
        utime=int(datetime.now(UTC).timestamp()),
    )
    async with SessionLocal() as db:
        try:
            assert await process_transfer(transfer) == "bank_credit"
            income = (
                await db.execute(select(Income).where(Income.unit_ref == tx_hash))
            ).scalar_one_or_none()
            assert income is not None
            assert income.player_id is None and income.round_id is None
            assert "in:bank" in income.note
            refund = (
                await db.execute(select(Payout).where(Payout.tx_hash == tx_hash))
            ).scalar_one_or_none()
            assert refund is None
            # Повторная обработка транзакции не плодит вторую строку дохода.
            assert await process_transfer(transfer) == "bank_credit"
            rows = (await db.execute(select(Income).where(Income.unit_ref == tx_hash))).scalars().all()
            assert len(rows) == 1
        finally:
            await db.execute(Income.__table__.delete().where(Income.unit_ref == tx_hash))
            await db.commit()


async def test_stranger_bank_memo_is_still_refunded(monkeypatch: pytest.MonkeyPatch) -> None:
    """bank: с чужого кошелька — не привилегия: обычный неизвестный возврат."""
    import os

    from app.db import SessionLocal
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "0:" + os.urandom(32).hex())
    source = "0:" + os.urandom(32).hex()
    tx_hash = "bk-" + os.urandom(8).hex()
    transfer = Transfer(
        tx_hash=tx_hash,
        source=source,
        value_nanotons=to_nano(1.0),
        comment="bank: капитан",
        utime=int(datetime.now(UTC).timestamp()),
    )
    async with SessionLocal() as db:
        try:
            assert await process_transfer(transfer) == "refund_queued"
        finally:
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash == tx_hash))
            await db.commit()


async def test_dust_and_expired_transfers_reach_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пыль и древние переводы НЕ возвращаются, но пишут строку Income:
    деньги остаются в казне и обязан быть учтены в сверке с балансом."""
    import os

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Income
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)
    monkeypatch.setattr(settings, "watch_refund_max_age_days", 14)
    source = "0:" + os.urandom(32).hex()
    old_utime = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
    dust = Transfer("dust-1", source, to_nano(0.01), "", int(datetime.now(UTC).timestamp()))
    expired = Transfer("exp-1", source, to_nano(0.2), "", old_utime)
    async with SessionLocal() as db:
        try:
            assert await process_transfer(dust) == "refund_dust"
            assert await process_transfer(expired) == "refund_expired"
            notes = set(
                (
                    await db.execute(
                        select(Income.note).where(
                            Income.unit_ref.in_(["dust-1", "exp-1"]),
                            Income.kind == "ton",
                        )
                    )
                ).scalars()
            )
            assert notes == {"in:refund:dust;src:…" + source[-10:], "in:refund:expired;src:…" + source[-10:]}
        finally:
            await db.execute(
                Income.__table__.delete().where(Income.unit_ref.in_(["dust-1", "exp-1"]))
            )
            await db.commit()


async def test_self_transfer_does_not_refund_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Рейк, уходящий «казначею самому себе» (OWNER == TREASURY), не плодит
    бесконечный refund-цикл: самоперевод распознаётся и молча пропускается."""
    import os

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Income
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    treasury = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "treasury_address", treasury)
    tx_hash = "self-" + os.urandom(8).hex()
    transfer = Transfer(
        tx_hash=tx_hash,
        source=treasury,
        value_nanotons=to_nano(0.5),
        comment="",
        utime=int(datetime.now(UTC).timestamp()),
    )
    async with SessionLocal() as db:
        try:
            assert await process_transfer(transfer) == "self_transfer"
            refunds = (
                await db.execute(select(Payout).where(Payout.kind == "refund", Payout.tx_hash == tx_hash))
            ).scalars().all()
            incomes = (
                await db.execute(select(Income).where(Income.unit_ref == tx_hash))
            ).scalars().all()
            assert refunds == []
            assert incomes == []
        finally:
            await db.commit()


async def test_repeat_stake_and_closed_day_transfers_are_refunded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Деньги, не ставшие ставкой (повтор за поставившего, закрытый день), возвращаются."""
    import os

    from app.db import SessionLocal
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    pid = 800_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(32).hex()
    hash_dup = "dup-" + os.urandom(8).hex()
    hash_late = "late-" + os.urandom(8).hex()
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet))
        round_row = _open_round(day_index=901_000)
        db.add(round_row)
        await db.flush()
        db.add(
            Stake(
                round_id=round_row.id,
                player_id=pid,
                amount_nanotons=to_nano(1),
                tx_hash=f"base-{pid}",
                status="confirmed",
            )
        )
        await db.commit()
        try:
            dup = Transfer(hash_dup, wallet, to_nano(2), "", int(datetime.now(UTC).timestamp()))
            assert await process_transfer(dup) == "already_staked"
            row = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == hash_dup))
            ).first()._mapping
            assert row["kind"] == "refund" and row["amount_nanotons"] == to_nano(2)
            assert row["round_id"] == round_row.id
            stakes_left = (
                await db.execute(
                    Stake.__table__.select().where(Stake.player_id == pid)
                )
            ).all()
            assert len(stakes_left) == 1  # повтор не увеличил фонд

            await db.execute(
                Round.__table__.update()
                .where(Round.id == round_row.id)
                .values(status=RoundStatus.CLOSED)
            )
            await db.commit()
            late = Transfer(hash_late, wallet, to_nano(0.4), "", int(datetime.now(UTC).timestamp()))
            assert await process_transfer(late) == "refund_queued"
            row = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == hash_late))
            ).first()._mapping
            assert row["kind"] == "refund" and row["round_id"] is None
        finally:
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash.in_([hash_dup, hash_late])))
            await db.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
            await db.delete(round_row)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_submin_already_staked_is_refunded_with_revote_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сумма ниже минимума ставки за уже поставившего = «недоехавший» revote.

    Кошелёк не приложил rv:-мемо → перевод уходит в ветку ставки и упирается в
    «уже есть». Сумма (0.1 = revote_ton) ниже минимума ставки (0.5), поэтому
    игроку объясняют про смену пути, а не путанное «ставка уже есть»."""
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db import SessionLocal
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    pid = 820_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(32).hex()
    tx_hash = "submin-" + os.urandom(8).hex()
    bot = SimpleNamespace(send_message=AsyncMock())
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet))
        round_row = _open_round(day_index=903_000)
        db.add(round_row)
        await db.flush()
        db.add(
            Stake(
                round_id=round_row.id,
                player_id=pid,
                amount_nanotons=to_nano(1),
                tx_hash=f"base-{pid}",
                status="confirmed",
            )
        )
        await db.commit()
        try:
            # 0.05 — ниже и минимума ставки, и платы за смену пути (0.1): ветка
            # авто-гранта по сумме не срабатывает, перевод уходит в ставку и
            # упирается в «уже есть» — игроку дают revote-подсказку.
            t = Transfer(
                tx_hash,
                wallet,
                to_nano(0.05),
                "",  # мемо не приложилось
                int(datetime.now(UTC).timestamp()),
            )
            assert await process_transfer(t, bot=bot) == "already_staked"
            row = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))
            ).first()._mapping
            assert row["kind"] == "refund" and row["round_id"] == round_row.id
            sent = bot.send_message.await_args.args[1]
            assert "не распознал комментарий rv" in sent
        finally:
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash == tx_hash))
            await db.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
            await db.delete(round_row)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_auto_grant_by_amount_when_memo_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Известный игрок, уже выбравший путь, шлёт 0.1 (=revote_ton) без мемо —
    грант выдаётся по сумме, деньги не застревают и не возвращаются."""
    import os

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RevoteGrant
    from app.ton_watch import Transfer, process_transfer
    from app.voting import cast_vote

    monkeypatch.setattr(settings, "ton_enabled", True)
    pid = 830_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(32).hex()
    tx_hash = "autog-" + os.urandom(8).hex()
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet))
        round_row = _open_round(day_index=904_000)
        db.add(round_row)
        await db.flush()
        await cast_vote(db, round_row, pid, 0)
        await db.commit()
        try:
            t = Transfer(
                tx_hash, wallet, to_nano(0.1), "", int(datetime.now(UTC).timestamp())
            )
            assert await process_transfer(t) == "revote_ok"
            grants = (
                (await db.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == tx_hash)))
                .scalars()
                .all()
            )
            assert len(grants) == 1 and grants[0].status == "granted"
            assert grants[0].round_id == round_row.id
            refunds = (
                (await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash)))
                .all()
            )
            assert [] == refunds
        finally:
            await db.execute(RevoteGrant.__table__.delete().where(RevoteGrant.unit_ref == tx_hash))
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash == tx_hash))
            await db.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
            await db.delete(round_row)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_auto_grant_returns_refund_if_no_vote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сумма из зоны revote, но пути ещё нет — гранта нет, деньги возвращаются
    (иначе каждый дарённый 0.1 раздувал бы казну)."""
    import os

    from app.db import SessionLocal
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    pid = 840_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(32).hex()
    tx_hash = "autonv-" + os.urandom(8).hex()
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet))
        round_row = _open_round(day_index=905_000)
        db.add(round_row)
        await db.commit()
        try:
            t = Transfer(
                tx_hash, wallet, to_nano(0.1), "", int(datetime.now(UTC).timestamp())
            )
            assert await process_transfer(t) == "revote_auto_no_vote"
            row = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))
            ).first()._mapping
            assert row["kind"] == "refund"
        finally:
            await db.execute(Payout.__table__.delete().where(Payout.tx_hash == tx_hash))
            await db.delete(round_row)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


async def test_failed_revote_payments_are_refunded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оплата смены пути мала или день закрыт — деньги автоматически возвращаются."""
    import os

    from app.db import SessionLocal
    from app.models import RevoteGrant
    from app.ton_watch import Transfer, process_transfer

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "revote_ton", 1.0)
    pid = 810_000 + int.from_bytes(os.urandom(2), "big")
    wallet = "0:" + os.urandom(32).hex()
    hash_small = "rvs-" + os.urandom(8).hex()
    hash_late = "rvl-" + os.urandom(8).hex()
    async with SessionLocal() as db:
        db.add(Player(id=pid, username=f"u{pid}", wallet_address=wallet))
        open_round = _open_round(day_index=902_000)
        closed_round = _open_round(day_index=902_001, status=RoundStatus.CLOSED)
        db.add_all([open_round, closed_round])
        await db.flush()
        await db.commit()
        try:
            small = Transfer(hash_small, wallet, to_nano(0.2), f"rv:{open_round.id}", int(datetime.now(UTC).timestamp()))
            assert await process_transfer(small) == "revote_too_small"
            late = Transfer(hash_late, wallet, to_nano(2), f"rv:{closed_round.id}", int(datetime.now(UTC).timestamp()))
            assert await process_transfer(late) == "revote_closed"

            rows = (
                await db.execute(
                    Payout.__table__.select().where(Payout.tx_hash.in_([hash_small, hash_late]))
                )
            ).all()
            kinds = {r._mapping["tx_hash"]: r._mapping["kind"] for r in rows}
            assert kinds == {hash_small: "refund", hash_late: "refund"}
            grants = (
                await db.execute(RevoteGrant.__table__.select().where(RevoteGrant.player_id == pid))
            ).all()
            assert grants == []  # гранты на проваленные оплаты не выдаются
        finally:
            await db.execute(
                Payout.__table__.delete().where(Payout.tx_hash.in_([hash_small, hash_late]))
            )
            for r in (open_round, closed_round):
                await db.delete(r)
            player = await db.get(Player, pid)
            if player is not None:
                await db.delete(player)
            await db.commit()


def test_split_pot_conserves_money_property() -> None:
    """Свойство: фонд не теряется и не создаётся. На 300 случайных
    раздачах сумма долей <= фонда, недобор — только пыль от целочисленного
    деления (не больше одного нанотона на получателя), доли положительны,
    а делёж детерминирован."""
    import random

    from app.stakes import split_pot

    rng = random.Random(20260823)
    for _ in range(300):
        pool = rng.randint(1, 10**10)
        entries = [
            (rng.randint(1, 50_000), rng.randint(1, 5 * 10**9))
            for _ in range(rng.randint(1, 12))
        ]
        shares = split_pot(pool, entries)
        assert {pid for pid, _amount in shares} == {pid for pid, _amount in entries}
        distributed = sum(amount for _pid, amount in shares)
        assert 0 < distributed <= pool
        assert pool - distributed <= len(entries)  # пыль, а не потеря
        assert all(amount > 0 for _pid, amount in shares)
        assert shares == split_pot(pool, entries)  # детерминизм


def test_split_equal_conserves_money_property() -> None:
    """Равный делёж: сумма ровно равна фонду (пыль уходит первому id),
    разброс долей не больше одного нанотона."""
    import random

    from app.stakes import split_equal

    rng = random.Random(20260824)
    for _ in range(300):
        total = rng.randint(1, 10**9)
        ids = rng.sample(range(1, 100_000), rng.randint(1, 15))
        shares = split_equal(total, ids)
        assert set(shares) == set(ids)
        assert sum(shares.values()) == total
        values = list(shares.values())
        # Пыль целиком уходит меньшему id: разброс не больше n-1 нанотонов.
        assert max(values) - min(values) <= len(set(ids)) - 1
        assert shares[min(ids)] == max(values)


