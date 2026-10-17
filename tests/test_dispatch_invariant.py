"""Стохастический инвариантный тест очереди выплат: «ни разу дважды, ни разу мимо».

Не сценарный, а модельный тест. Отдельные пути (таймаут, пачка, фолбэк)
закрыты сценарными тестами; здесь по НАСТОЯЩЕМУ циклу диспетчера
(dispatch_pending_payouts + confirm_broadcast_payouts) на фиксированном seed
прогоняется детерминированная «сеть» с отказами, и после каждого тика
проверяются инварианты — те, что обязаны жить в любой комбинации отказов:

1. ни одно memo не принято цепочкой дважды — анти-дубль не имеет права
   отправить один перевод ещё раз, даже когда ответ на вещание потерян
   (таймаут после успешного вещания, упавший канал, недоступная история);
2. строка, помеченная sent, обязана иметь ПРИНЯТОЕ вещание — sent значит
   «деньги ушли», а не «попробовали»;
3. сумма принятого никогда не превышает сумму очереди, лимит попыток
   не переполнен, failed возможен только на исчерпанном лимите;
4. в конце (аварийность выключена, история жива) все строки sent, у всех
   реальные хеши, принято ровно по одному переводу на строку.

Модель:
- _Chain — «блокчейн»: принятые вещания видны в истории сразу или через
  тик (задержка индексации), каждая tx имеет хеш;
- _Net — отказность вещания: ok / потеряно (таймаут без вещания) / принято,
  но ответ потерян (таймаут) / принято, но канал упал (ошибка) / явный
  отказ пачки (BatchRejected — доказуемо «не ушло»);
- история казначея периодически недоступна: маркеры пусты + флаг истории
  False — повторы обязаны ЗАМОРАЖИВАТЬСЯ, а не переотправляться;
- часы поддельные: тик = 700 с, окно подтверждения в тесте 600 с — так
  sent_at и задержки индексации пересекаются реалистично, а не «всё
  мгновенно» (иначе requeue-ветка confirm никогда бы не сработала).
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ensure_player, ensure_round

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout
from app.ton_pay.dispatch.send import BatchRejected, BatchUncertain

_ROUND_ID = 77
_PLAYER_BASE = 900
_AMOUNT = 500_000_000
_OUTAGE_TICKS = 10  # полная авария: успешных вещаний нет
_DEGRADED_TICKS = 28  # конец «затухающего» канала
_TICKS_TOTAL = 40
_TICK_SECONDS = 700


async def _seed(count: int) -> list[int]:
    """Очередь на N призов одного дня — все pending, получатели есть."""
    await ensure_round(_ROUND_ID)
    ids: list[int] = []
    for index in range(count):
        await ensure_player(_PLAYER_BASE + index)
        async with SessionLocal() as session:
            payout = Payout(
                round_id=_ROUND_ID,
                player_id=_PLAYER_BASE + index,
                kind="prize",
                amount_nanotons=_AMOUNT,
                dest_address="0:" + os.urandom(32).hex(),
                network="testnet" if settings.is_testnet else "mainnet",
            )
            session.add(payout)
            await session.flush()
            payout.status = "pending"
            await session.commit()
            ids.append(payout.id)
    return ids


async def _drop(ids: list[int]) -> None:
    async with SessionLocal() as session:
        for payout_id in ids:
            row = await session.get(Payout, payout_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


async def _rows(ids: list[int]) -> list[Payout]:
    async with SessionLocal() as session:
        return [await session.get(Payout, payout_id) for payout_id in ids]


def _memo(payout_id: int) -> str:
    return f"way:{_ROUND_ID}:prize#{payout_id}"


class _Chain:
    """Блокчейн: принятые вещания и задержанная индексация memo."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.current_tick = -1
        # Журнал принятых вещаний: (dest, nano, memo). Инвариант №1: memo
        # в этом списке не повторяется НИКОГДА — его проверяет _assert.
        self.accepted: list[tuple[str, int, str]] = []
        # memo → тик принятия (для свидетеля анти-дубля по маркерам).
        self.accept_tick: dict[str, int] = {}
        self.visible: dict[str, str] = {}
        self._pending: list[tuple[str, str, int]] = []
        self._tx_no = 0

    def accept(self, transfers: list[tuple[str, int, str]]) -> None:
        """Принять вещание: транзакция в цепочке, memo — с задержкой 0|1 тик."""
        for dest, amount, memo in transfers:
            self.accepted.append((dest, amount, memo))
            self.accept_tick[memo] = self.current_tick
            self._tx_no += 1
            tx = f"tx:{self._tx_no}"
            if self.rng.choice([0, 0, 1]) == 0:
                self.visible[memo] = tx
            else:
                self._pending.append((memo, tx, 1))

    def tick(self) -> None:
        """Один тик индексации: отложенные memo становятся видимыми."""
        hold: list[tuple[str, str, int]] = []
        for memo, tx, delay in self._pending:
            if delay <= 1:
                self.visible[memo] = tx
            else:
                hold.append((memo, tx, delay - 1))
        self._pending = hold

    def accepted_amount(self) -> int:
        return sum(amount for _dest, amount, _memo in self.accepted)


class _Net:
    """Отказность вещания: фаза аварий → фаза здоровья."""

    def __init__(self, chain: _Chain, rng: random.Random) -> None:
        self.chain = chain
        self.rng = rng
        self.degraded = 0.0  # 0 = сеть здорова
        self.history_down = False
        # Свидетели аварийных веток: тест обязан ИХ испытать, иначе зелёный
        # прогон доказывает лишь «пустая сеть не ломает очередь».
        self.counts: dict[str, int] = {}
        self.history_down_ticks = 0

    def _note(self, outcome: str) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1

    def _outcome(self) -> str:
        if self.degraded <= 0:
            return "ok"
        r = self.rng.random()
        if r < self.degraded * 0.4:
            return "lost"  # таймаут, вещания не было
        if r < self.degraded * 0.6:
            return "accepted_timeout"  # вещание было, ответ потерян
        if r < self.degraded * 0.8:
            return "accepted_error"  # вещание было, канал упал
        if r < self.degraded:
            return "rejected"  # явный отказ: доказуемо «не ушло»
        return "ok"

    async def send_row(self, dest: str, amount: int, comment: str) -> str:
        outcome = self._outcome()
        self._note(f"row:{outcome}")
        if outcome == "lost":
            raise TimeoutError()
        if outcome == "rejected":
            raise RuntimeError("Лайтсерверы не приняли перевод (результат 0)")
        self.chain.accept([(dest, amount, comment)])
        if outcome == "accepted_timeout":
            raise TimeoutError()
        if outcome == "accepted_error":
            raise RuntimeError("канал оборвался после вещания")
        return "bcast:row"

    async def send_batch(self, transfers: list[tuple[str, int, str]]) -> str:
        outcome = self._outcome()
        self._note(f"batch:{outcome}")
        self._note(f"batch_legs:{len(transfers)}")
        if outcome == "lost":
            raise TimeoutError()
        if outcome == "rejected":
            raise BatchRejected("Лайтсерверы не приняли пачку (результат 0)")
        self.chain.accept(list(transfers))
        if outcome == "accepted_timeout":
            raise TimeoutError()
        if outcome == "accepted_error":
            raise BatchUncertain("судьба пачки неизвестна")
        return "bcast:batch"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _Witness:
    """Свидетели аварийных веток: тест обязан их ИСПЫТАТЬ, иначе зелёный
    прогон доказывает лишь «здоровая сеть не ломает очередь»."""

    def __init__(self) -> None:
        # memo → тик, на котором вещание принято цепочкой.
        self.accepted_tick: dict[str, int] = {}
        # Строка ушла НЕ на тике принятия своего memo: значит, повтор был
        # погашен маркерами истории (ветка анти-дубля), а не новой отправкой.
        self.markers_dedupe = 0
        # Строка sent → pending в confirm: переочередь неподтверждённого memo.
        self.requeues = 0
        # Тики, когда хотя бы одна строка была заморожена недоступной историей.
        self.freeze_ticks = 0
        self._prev_status: dict[int, str] = {}

    def observe(self, tick: int, chain: _Chain, rows: list[Payout]) -> None:
        frozen = False
        for row in rows:
            status = str(row.status)
            if self._prev_status.get(row.id) == "sent" and status == "pending":
                self.requeues += 1
            if status == "sent" and self._prev_status.get(row.id) != "sent":
                accepted_tick = chain.accept_tick.get(_memo(row.id))
                if accepted_tick is not None and accepted_tick < tick:
                    self.markers_dedupe += 1
            if status == "pending" and (row.last_error or "").startswith("история казначея"):
                frozen = True
            self._prev_status[row.id] = status
        if frozen:
            self.freeze_ticks += 1


def _install_core(
    monkeypatch: pytest.MonkeyPatch, use_batch: bool
) -> None:
    """Казна testnet, батч-режим, короткие окна — иначе сценарий не ускорить."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "word " * 24)
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + "33" * 32)
    monkeypatch.setattr(settings, "payout_v5_batch", use_batch)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 0)
    monkeypatch.setattr(settings, "payout_confirm_timeout_seconds", 600)
    monkeypatch.setattr(settings, "payout_max_attempts", 25)

    async def account_state():
        return 50_000_000_000, "active", "tonapi"

    class _Wallet:
        async def get_seqno(self):
            return 4242

    async def fake_wallet():
        return _Wallet()

    monkeypatch.setattr(ton_pay, "fetch_account_state", account_state)
    monkeypatch.setattr(ton_pay, "_get_wallet", fake_wallet)
    monkeypatch.setattr(ton_pay, "wallet_supports_batch", lambda wallet: True)


def _install_history(monkeypatch: pytest.MonkeyPatch, net: _Net) -> None:
    """Маркеры/карта истории читают «блокчейн»; в тик недоступности — молчат."""

    async def markers() -> set[str]:
        if net.history_down:
            ton_pay.state._RECONCILE_HISTORY_OK = False
            return set()
        return set(net.chain.visible)

    async def tx_map(targets: set[str] | None = None) -> dict[str, str]:
        if net.history_down:
            return {}
        if targets is None:
            return dict(net.chain.visible)
        return {memo: net.chain.visible[memo] for memo in targets if memo in net.chain.visible}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", tx_map)


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    import app.ton_pay.dispatch as dispatch_module
    import app.ton_pay.dispatch.confirm as confirm_module

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(dispatch_module, "datetime", _FakeDatetime)
    monkeypatch.setattr(confirm_module, "datetime", _FakeDatetime)


async def _assert_invariants(
    chain: _Chain,
    ids: list[int],
    total: int,
    witness: _Witness,
    tick: int,
) -> None:
    """Инварианты после каждого тика — независимо от того, что успело упасть."""
    memos = [memo for _dest, _amount, memo in chain.accepted]
    assert len(memos) == len(set(memos)), f"двойное вещание одного memo: {memos}"
    for _dest, _amount, memo in chain.accepted:
        assert memo in {_memo(payout_id) for payout_id in ids}, f"чужой перевод: {memo}"

    accepted_amount = chain.accepted_amount()
    assert accepted_amount <= total, "принято больше, чем вся очередь"

    rows = await _rows(ids)
    accepted_set = set(memos)
    sent_amount = 0
    for row in rows:
        assert row.attempts <= settings.payout_max_attempts, f"строка {row.id}: лимит попыток переполнен"
        if row.status == "sent":
            assert _memo(row.id) in accepted_set, (
                f"строка {row.id} помечена sent без принятого вещания"
            )
            sent_amount += row.amount_nanotons
        if row.status == "failed":
            assert row.attempts >= settings.payout_max_attempts, (
                f"строка {row.id} ушла в failed до исчерпания лимита"
            )
    assert sent_amount <= accepted_amount, "sent-строки весят больше, чем принято вещаний"
    witness.observe(tick, chain, rows)


@pytest.mark.parametrize(
    ("use_batch", "seed"),
    [(False, 20261017), (True, 20261018)],
)
async def test_dispatch_never_double_pays_and_never_loses(
    monkeypatch: pytest.MonkeyPatch, use_batch: bool, seed: int
) -> None:
    """Полный цикл в отказной сети: инварианты держатся на каждом тике.

    Сценарий: N призов; первых 10 тиков — полная авария (ни одного успешного
    вещания), следующие 18 — затухающий канал (успех ~1 из 7), последние 12 —
    здоровье; каждый третий аварийный тик недоступна история казначея.
    Баланс, seqno и сборка — исправные: проверяются ЛОГИКА РЕШЕНИЙ очереди,
    а не транспорт.
    """
    rng = random.Random(seed)
    chain = _Chain(rng)
    net = _Net(chain, rng)
    clock = _Clock()
    witness = _Witness()
    _install_core(monkeypatch, use_batch)
    _install_history(monkeypatch, net)
    _install_clock(monkeypatch, clock)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", net.send_row)
    monkeypatch.setattr(ton_pay, "send_ton_transfers_batch", net.send_batch)

    ids = await _seed(6)
    total = _AMOUNT * len(ids)
    try:
        for tick in range(_TICKS_TOTAL):
            if tick < _OUTAGE_TICKS:
                # Полное окно аварии: успешного исхода вещания не бывает —
                # как при реальном отказе канала. Строки обязаны копить
                # попытки, повторы пересекаться с недоступной историей.
                net.degraded = 1.0
            elif tick < _DEGRADED_TICKS:
                # Затухающий канал: успех редок (одна попытка из ~7).
                net.degraded = 0.85
            else:
                net.degraded = 0.0
            # Каждый третий тик — история казначея недоступна: повторы
            # обязаны замораживаться, а не переотправляться вслепую.
            net.history_down = tick % 3 == 2 and tick < _DEGRADED_TICKS
            if net.history_down:
                net.history_down_ticks += 1
            chain.current_tick = tick
            chain.tick()
            clock.advance(_TICK_SECONDS)
            await ton_pay.dispatch_pending_payouts(bot=None)
            await ton_pay.confirm_broadcast_payouts(bot=None)
            await _assert_invariants(chain, ids, total, witness, tick)
            if os.environ.get("INVARIANT_DEBUG"):
                rows = await _rows(ids)
                print(
                    tick,
                    "down" if net.history_down else "up",
                    net.counts,
                    [(r.id, r.status, r.attempts) for r in rows],
                )

        # Финальная сверка: все доставлены, хеши реальные, деньги сходятся ровно.
        chain.tick()
        await ton_pay.confirm_broadcast_payouts(bot=None)
        rows = await _rows(ids)
        assert all(row.status == "sent" for row in rows), (
            f"остались недоставленные: {[(r.id, r.status, r.last_error) for r in rows]}"
        )
        assert chain.accepted_amount() == total, "принято не ровно сумму очереди"
        assert sorted(memo for _d, _a, memo in chain.accepted) == sorted(
            _memo(payout_id) for payout_id in ids
        ), "каждая строка обязана быть вещана ровно один раз"
        assert all(row.tx_hash and row.tx_hash.startswith("tx:") for row in rows), (
            f"не у всех реальные хеши: {[(r.id, r.tx_hash) for r in rows]}"
        )

        # Свидетели: тест обязан был реально пройти аварийные ветки. Без этих
        # проверок «зелено» означало бы лишь «здоровая сеть не мешает», и тест
        # деградировал бы в пустышку при любой правке модели.
        assert net.history_down_ticks >= 1, "ни одного тика с недоступной историей"
        assert witness.freeze_ticks >= 1, (
            "заморозка повторов при недоступной истории не испытана"
        )
        assert witness.markers_dedupe >= 1, "анти-дубль по маркерам истории не испытан"
        if use_batch:
            batch_attempts = sum(
                count for name, count in net.counts.items() if name.startswith("batch:")
            )
            assert batch_attempts >= 2, "пачка пыталась уйти меньше двух раз — отказные ветки не пройдены"
            assert net.counts.get("batch:rejected", 0) >= 1, "явный отказ пачки не испытан"
            row_after_broadcast = net.counts.get("row:accepted_timeout", 0) + net.counts.get(
                "row:accepted_error", 0
            )
            assert row_after_broadcast >= 1, "поштучный фолбэк после отказа пачки не испытал потерь ответа"
        else:
            assert net.counts.get("row:lost", 0) >= 1, "потеря вещания по таймауту не испытана"
            row_after_broadcast = net.counts.get("row:accepted_timeout", 0) + net.counts.get(
                "row:accepted_error", 0
            )
            assert row_after_broadcast >= 1, "таймаут/сбой после принятого вещания не испытаны"
            assert net.counts.get("row:rejected", 0) >= 1, "явный отказ отправки не испытан"
        print(
            "WITNESS",
            net.counts,
            "hist_down",
            net.history_down_ticks,
            "freeze_ticks",
            witness.freeze_ticks,
            "markers_dedupe",
            witness.markers_dedupe,
            "requeues",
            witness.requeues,
        )
    finally:
        await _drop(ids)
