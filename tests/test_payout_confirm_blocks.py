"""Подтверждение блоков до пометки confirmed=True.

Режим payout_confirm_blocks > 0 закрывает риск reorg на mainnet: после bcast
сразу ставится sent + confirmed=False, и строки поднимаются до confirmed=True
только когда masterchain head прошёл достаточно блоков от mc_block_seqno
транзакции. При дефолте payout_confirm_blocks=0 поведение совпадает со
старым: real_hash из истории казначея = финальный статус.

В этих тестах сеть замокана через monkeypatch на ton_pay.fetch_*:
- fetch_broadcast_tx_map отдаёт memo → tx_hash;
- fetch_masterchain_head_seqno и fetch_tx_mc_seqno задают «глубину» транзакции.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


async def _seed_sent_payout(*, confirmed: bool = False, tx_hash: str = "bcast:1789470019") -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=64,
            player_id=42,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="sent",
            tx_hash=tx_hash,
            sent_at=datetime.now(UTC) - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
            confirmed=confirmed,
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()
        return payout_id


async def _cleanup(payout_id: int) -> None:
    async with SessionLocal() as session:
        await session.delete(await session.get(Payout, payout_id))
        await session.commit()


def _seed_tx_map(payout_id: int) -> dict[str, str]:
    return {f"way:64:prize#{payout_id}": "a" * 64}


async def test_legacy_mode_sets_confirmed_immediately(monkeypatch) -> None:
    """payout_confirm_blocks=0: real_hash из истории = финал, без depth.

    После успешного bcast строка sent + confirmed=False (по умолчанию новой
    колонки). Первый же цикл confirm_broadcast_payouts находит memo в
    истории казначея и должен сразу поднять confirmed=True, не дёргая
    TonAPI/Toncenter за masterchain head и tx mc_seqno (лишний расход
    квоты провайдера на каждой строке).
    """
    payout_id = await _seed_sent_payout(confirmed=False)

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return _seed_tx_map(payout_id)

    async def fail_head_seqno():
        raise AssertionError(
            "fetch_masterchain_head_seqno не должен зваться при payout_confirm_blocks=0"
        )

    monkeypatch.setattr(settings, "payout_confirm_blocks", 0)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)
    monkeypatch.setattr(ton_pay, "fetch_masterchain_head_seqno", fail_head_seqno)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "a" * 64
        assert row.confirmed is True
    finally:
        await _cleanup(payout_id)


async def test_strict_mode_waits_until_blocks_reached(monkeypatch) -> None:
    """payout_confirm_blocks=5, depth=2: confirmed остаётся False до след. цикла.

    Подтверждение в 5 блоков означает, что транзакция должна набрать
    глубину 5 (head - mc_block_seqno). При depth=2 строка ещё не созрела:
    confirm_broadcast_payouts пишет real_hash, оставляет confirmed=False и
    ждёт следующего цикла. Это ключевое отличие от legacy: теперь строка
    проходит через несколько циклов перед финалом.
    """
    payout_id = await _seed_sent_payout(confirmed=False)

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return _seed_tx_map(payout_id)

    real_hash = "a" * 64

    async def fake_head_seqno() -> int:
        return 1000

    async def fake_tx_mc_seqno(hash_arg: str) -> int:
        assert hash_arg == real_hash
        return 998  # depth = 1000 - 998 = 2, < payout_confirm_blocks=5

    monkeypatch.setattr(settings, "payout_confirm_blocks", 5)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)
    monkeypatch.setattr(ton_pay, "fetch_masterchain_head_seqno", fake_head_seqno)
    monkeypatch.setattr(ton_pay, "fetch_tx_mc_seqno", fake_tx_mc_seqno)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        # Метрика «подтверждено» в этом цикле — ноль: строка сменила
        # tx_hash, но НЕ поднята до confirmed=True. Счётчик учитывает
        # только confirmed=True (legacy-семантика), чтобы сравнения с
        # метриками из старых тестов не сдвигались.
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == real_hash
        assert row.confirmed is False
    finally:
        await _cleanup(payout_id)


async def test_strict_mode_promotes_when_depth_reached(monkeypatch) -> None:
    """payout_confirm_blocks=5, depth=5: строка поднимается до confirmed=True.

    Транзакция в блоке 995, current head = 1000 → depth = 5 ≥
    payout_confirm_blocks. Строка поднимается до confirmed=True. Это
    конечное состояние: следующий цикл выборки не возьмёт её (confirmed=True).
    """
    payout_id = await _seed_sent_payout(confirmed=False)

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return _seed_tx_map(payout_id)

    real_hash = "a" * 64

    async def fake_head_seqno() -> int:
        return 1000

    async def fake_tx_mc_seqno(hash_arg: str) -> int:
        assert hash_arg == real_hash
        return 995  # depth = 5

    monkeypatch.setattr(settings, "payout_confirm_blocks", 5)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)
    monkeypatch.setattr(ton_pay, "fetch_masterchain_head_seqno", fake_head_seqno)
    monkeypatch.setattr(ton_pay, "fetch_tx_mc_seqno", fake_tx_mc_seqno)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == real_hash
        assert row.confirmed is True
    finally:
        await _cleanup(payout_id)


async def test_strict_mode_keeps_unconfirmed_when_depth_unavailable(monkeypatch) -> None:
    """fetch_masterchain_head_seqno None → «не знаю» — НЕ поднимаем confirmed.

    Цена ошибки здесь асимметричная: подтвердить «наугад» — риск пропустить
    reorg (на mainnet теоретический, но платёж-то реальный). Пропустить
    подтверждение — задержка в один 120-секундный цикл, ничего больше.
    Пустая попытка: confirmed остаётся False, requeued=0, тело цикла
    завершается без записи.
    """
    payout_id = await _seed_sent_payout(confirmed=False)

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return _seed_tx_map(payout_id)

    async def fake_head_seqno() -> int:
        return None

    async def fail_tx_mc_seqno(hash_arg: str) -> int:
        raise AssertionError(
            "fetch_tx_mc_seqno не должен зваться, если head_seqno=None"
        )

    monkeypatch.setattr(settings, "payout_confirm_blocks", 5)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)
    monkeypatch.setattr(ton_pay, "fetch_masterchain_head_seqno", fake_head_seqno)
    monkeypatch.setattr(ton_pay, "fetch_tx_mc_seqno", fail_tx_mc_seqno)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        # tx_hash обновлён (memo нашли), confirmed=False — строка живёт
        # до следующего цикла.
        assert row.tx_hash == "a" * 64
        assert row.confirmed is False
    finally:
        await _cleanup(payout_id)


async def test_dispatch_marks_confirmed_only_in_legacy_mode(monkeypatch) -> None:
    """После успешного bcast confirmed=True только при payout_confirm_blocks=0.

    Тест идёт по двум сценариям в двух отдельных прогонах, чтобы не
    зависеть от глобального состояния между циклами (locking, alerts).
    Каждый прогон заводит свою строку, прогоняет один цикл
    dispatch_pending_payouts с замоканной сетью и проверяет флаг confirmed.
    """
    from app.ton_pay.dispatch import dispatch_pending_payouts

    async def _seed_pending():
        async with SessionLocal() as session:
            payout = Payout(
                round_id=64,
                player_id=42,
                kind="prize",
                amount_nanotons=500_000_000,
                dest_address="0:" + os.urandom(32).hex(),
                status="pending",
                network="testnet" if settings.is_testnet else "mainnet",
            )
            session.add(payout)
            await session.flush()
            pid = payout.id
            await session.commit()
            return pid

    async def fake_balance() -> tuple[int | None, str | None, str | None]:
        return 10_000_000_000, "active", "tonapi"

    async def fake_send_ton_transfer(dest, amount, comment) -> str:
        return "bcast:1789470019"

    async def fake_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_balance)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send_ton_transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "_get_wallet", lambda: None)
    # TON выключен, чтобы liteclient не поднимался в тесте; сами моки
    # fetch_account_state и send_ton_transfer достаточны для прогона цикла.
    monkeypatch.setattr(settings, "ton_enabled", False)

    # Сценарий 1: payout_confirm_blocks=0 (legacy) — confirmed=True сразу.
    pid_legacy = await _seed_pending()
    monkeypatch.setattr(settings, "payout_confirm_blocks", 0)
    try:
        await dispatch_pending_payouts(limit=10, bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, pid_legacy)
        assert row.status == "sent"
        assert row.confirmed is True
    finally:
        await _cleanup(pid_legacy)

    # Сценарий 2: payout_confirm_blocks=5 (strict) — confirmed=False до
    # подтверждения N блоков через confirm_broadcast_payouts.
    pid_strict = await _seed_pending()
    monkeypatch.setattr(settings, "payout_confirm_blocks", 5)
    try:
        await dispatch_pending_payouts(limit=10, bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, pid_strict)
        assert row.status == "sent"
        assert row.confirmed is False
    finally:
        await _cleanup(pid_strict)