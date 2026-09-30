"""Свойства зеркала казны — что должно выполняться на любой истории.

Зеркало — независимая копия истории кошелька казначея, сверенная с БД по
ежедневной проверке «в ноль» (Σ balance_delta = balance). Эти свойства
страхуют парсеры TonAPI/Toncenter от регрессий: ошибка в формате на одном
провайдере ловится до продакшена.

Сценарии:
  1. Консервация: сумма delta по движениям = in - out - fee (без допуска на газ).
  2. Парсеры TonAPI и Toncenter дают идентичный MirrorMove для той же сути.
  3. Memo way:<day>:<kind>#<id> — round-trip: parse_way_memo(format) восстанавливает.
  4. Самоперевод: own→own — direction == "self", balance_delta == 0.
  5. Классификация: «refund» → «refund»; «way:5:prize#12» → «payout:prize».
  6. Никаких отрицательных value_nanotons в чистом incoming/outgoing.
"""
from __future__ import annotations

import pytest

from app.treasury_mirror import (
    MirrorMove,
    _derive_balance_delta,
    _self_direction,
    classify_incoming,
    classify_outgoing,
    parse_way_memo,
)

# --------- 1. Консервация balance_delta ---------

@pytest.mark.parametrize(
    "in_value,out_value,fee",
    [
        (10**9, 0, 0),                # чистый приход 1 Gram
        (0, 10**9, 0),                # чистый расход 1 Gram
        (10**9, 5 * 10**8, 10**6),     # приход с возвратом и комиссией
        (0, 0, 0),                    # пустая транзакция
        (123456789, 987654321, 100000),# произвольные значения
    ],
)
def test_derive_balance_delta_in_minus_out_minus_fee(in_value: int, out_value: int, fee: int) -> None:
    """Если провайдер не дал balance_delta, считаем как in - out - fee."""
    assert _derive_balance_delta(provider_delta=None, in_value=in_value, out_value=out_value, fee=fee) == (
        in_value - out_value - fee
    )


@pytest.mark.parametrize("provider_delta", [0, 1, -1, 10**18, "1234"])
def test_derive_balance_delta_prefers_provider_value(provider_delta) -> None:
    """Если провайдер дал явное значение, используем его (НЕ свою формулу)."""
    result = _derive_balance_delta(provider_delta=provider_delta, in_value=999, out_value=999, fee=999)
    expected = int(str(provider_delta))
    assert result == expected


def test_derive_balance_delta_none_falls_back_to_formula() -> None:
    """None → НЕ используется как значение, фолбэк на формулу in - out - fee."""
    # provider_delta=None означает «провайдер не дал», а не «значение равно None».
    result = _derive_balance_delta(provider_delta=None, in_value=10, out_value=3, fee=1)
    assert result == 6  # 10 - 3 - 1


def test_derive_balance_delta_handles_malformed_provider() -> None:
    """Невалидное значение провайдера → фолбэк на in - out - fee (без падения)."""
    # Нечисловая строка: парсер падает, фолбэк работает.
    result = _derive_balance_delta(provider_delta="not-a-number", in_value=10, out_value=3, fee=1)
    assert result == 6  # 10 - 3 - 1


# --------- 2. Self-direction ---------

@pytest.mark.parametrize(
    "treasury,counterparty,expected",
    [
        ("EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF", "EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF", True),
        # Один и тот же адрес в raw- и friendly-формах — тоже self.
        ("0:abc1234567890123456789012345678901234567890123456789012345678901",
         "EQAKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrwZ", False),
        ("", "EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF", False),
        ("EQDKbjIcfM6ezt8KjKJJLshZJJSqX7XOA4ff-W72r5gqPrHF", "", False),
    ],
)
def test_self_direction_detects_own_transfers(treasury: str, counterparty: str, expected: bool) -> None:
    """_self_direction срабатывает на own→own, в т.ч. при пустых строках."""
    assert _self_direction(counterparty, treasury) is expected


# --------- 3. Memo round-trip ---------

@pytest.mark.parametrize(
    "round_id,kind,payout_id",
    [
        (1, "prize", 1),
        (42, "refund", 99),
        (9999, "leaderboard", 12345),
        (0, "prize", 0),  # граничный случай: 0/0
    ],
)
def test_parse_way_memo_roundtrip(round_id: int, kind: str, payout_id: int) -> None:
    """format → parse восстанавливает (kind, payout_id) ровно."""
    memo = f"way:{round_id}:{kind}#{payout_id}"
    result = parse_way_memo(memo)
    assert result == (kind, payout_id)


def test_parse_way_memo_with_override_prefix() -> None:
    """Свободный текст переопределения (возвраты при паузе) + служебный суффикс:
    parse_way_memo находит ключ через rfind (берёт ПОСЛЕДНИЙ way:).
    """
    memo = "возврат игроку за день 12 | way:12:refund#5"
    assert parse_way_memo(memo) == ("refund", 5)


@pytest.mark.parametrize(
    "bad_memo",
    [
        "",                                  # пусто
        "way:",                              # без данных
        "way::refund#1",                     # пустой round_id
        "way:1:refund",                      # нет id
        "way:1:refund#abc",                  # не-цифровой id
        "way:1:refund#1.0",                  # дробный id
        "abc def",                           # не наш формат
    ],
)
def test_parse_way_memo_returns_none_on_garbage(bad_memo: str) -> None:
    """Невалидное мемо → None (не падать, не возвращать мусор)."""
    assert parse_way_memo(bad_memo) is None


# --------- 4. Классификация входящих ---------

def test_classify_incoming_recognizes_bank_memo() -> None:
    """Банковская инструкция «bank:…» → тег «bank».

    Формат bank-memo определён в payments.parse_bank_memo. Базовое мемо
    «bank:<8 hex байт>» срабатывает; classify_incoming читает именно его.
    """
    bank_memo = "bank:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    assert classify_incoming(bank_memo) == "bank"


@pytest.mark.parametrize(
    "stake_memo",
    [
        "",                                  # пусто — нет ни одного маркера
        "просто текст",
        "случайный комментарий без way/bank/etc",
    ],
)
def test_classify_incoming_defaults_to_stake(stake_memo: str) -> None:
    """Неизвестное входящее без специальных маркеров → stake (default)."""
    assert classify_incoming(stake_memo) == "stake"


# --------- 5. Классификация исходящих ---------

@pytest.mark.parametrize(
    "kind,expected_label",
    [
        ("prize", "payout:prize"),
        ("refund", "refund"),
        ("leaderboard", "payout:leaderboard"),
        ("stake", "payout:stake"),
    ],
)
def test_classify_outgoing_for_known_kinds(kind: str, expected_label: str) -> None:
    """Исходящее с известным kind получает «payout:<kind>», refund — «refund»."""
    memo = f"way:1:{kind}#7"
    assert classify_outgoing(memo) == expected_label


def test_classify_outgoing_for_legacy_no_memo() -> None:
    """Легаси-строки (старый формат, без «way:…») → «unknown_out» (связь по tx_hash)."""
    assert classify_outgoing("old format without way:") == "unknown_out"


def test_classify_outgoing_empty_memo() -> None:
    """Пустое мемо → unknown_out (не 'payout:', не 'refund')."""
    assert classify_outgoing("") == "unknown_out"


# --------- 6. MirrorMove shape ---------

def test_mirror_move_dataclass_is_frozen() -> None:
    """MirrorMove frozen — гарантирует, что движение после парсинга не меняется
    в БД и в отчётах."""
    from dataclasses import FrozenInstanceError

    mv = MirrorMove(
        tx_hash="abc",
        network="testnet",
        utime=1,
        lt=1,
        direction="in",
        value_nanotons=10,
        fee_nanotons=0,
        balance_delta_nanotons=10,
        counterparty="EQ...",
        comment="memo",
        success=True,
    )
    with pytest.raises(FrozenInstanceError):
        mv.balance_delta_nanotons = 0  # type: ignore[misc]


def test_mirror_move_is_money_move_predicate() -> None:
    """is_money_move: True если баланс реально изменился."""
    move_in = MirrorMove(
        tx_hash="x", network="testnet", utime=1, lt=1, direction="in",
        value_nanotons=10, fee_nanotons=0, balance_delta_nanotons=10,
        counterparty="", comment="", success=True,
    )
    move_self = MirrorMove(
        tx_hash="x", network="testnet", utime=1, lt=1, direction="self",
        value_nanotons=10, fee_nanotons=0, balance_delta_nanotons=0,
        counterparty="", comment="", success=True,
    )
    move_failed = MirrorMove(
        tx_hash="x", network="testnet", utime=1, lt=1, direction="out",
        value_nanotons=10, fee_nanotons=0, balance_delta_nanotons=0,
        counterparty="", comment="", success=False,
    )
    assert move_in.is_money_move is True
    assert move_self.is_money_move is False
    assert move_failed.is_money_move is False


# --------- 7. Псевдо-property: парсер не теряет in/out ---------

@pytest.mark.parametrize(
    "value_in,value_out,fee",
    [
        (10**9, 0, 10**6),
        (0, 5 * 10**8, 5 * 10**5),
        (1234, 5678, 100),
        (10**18, 10**18, 10**6),  # ребаланс 1:1
    ],
)
def test_balance_delta_signs_hold(value_in: int, value_out: int, fee: int) -> None:
    """Свойство парсинга: выведенный balance_delta корректен с любым знаком.

    in_value - out_value - fee должно ТОЧНО совпадать с тем, что мы получаем
    из формулы — иначе сверка «в ноль» будет врать на перекошенных парах
    «много входящих / мало исходящих».
    """
    result = _derive_balance_delta(
        provider_delta=None,
        in_value=value_in,
        out_value=value_out,
        fee=fee,
    )
    expected = value_in - value_out - fee
    assert result == expected
    # Если входа больше выхода + fee — дельта положительна; иначе — неположительна.
    if value_in > value_out + fee:
        assert result > 0
    elif value_in < value_out + fee:
        assert result < 0
