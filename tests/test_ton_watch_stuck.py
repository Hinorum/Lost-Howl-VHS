"""Регрессия инцидента 2026-09-17: удержанные переводы в казне.

Три границы, которые молча прорвались:
1. claim-маркер «refund:<64-hex>» (71 символ) упал в колонку key VARCHAR(64) —
   SQLite длину VARCHAR не проверяет и тесты были зелёными, а Postgres кидал
   StringDataRightTruncationError. Теперь claim_once сам не пускает ключи
   длиннее колонки — двигатель в тестах не влияет на результат.
2. Курсор прошёл мимо сбойной транзакции после лимита попыток и больше её
   никогда не перечитывал — деньги висели в казне до ручного разбора.
   Авто-лечение (_heal_stuck_transfers) переобрабатывает брошенные записи
   по снимку и возвращает деньги, если обработка стабильно падает.
3. Сбойные переводы не тревожили админа. Сверка аномалий теперь считает
   stuck-список и шлёт уведомление (проверяем счётчик, не сам канал).
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.config import settings
from app.models import Payout, WatcherState


def _hex_hash(length: int = 64) -> str:
    return "a" * length


async def test_claim_markers_fit_watcher_state_key_column() -> None:
    """Все claim-маркеры, строящиеся из tx_hash, обязаны влезать в колонку key.

    Корень инцидента: 'refund:' + 64-hex хеш = 71 символ, колонка была 64 —
    INSERT падал на проде (SQLite в тестах молчит). length берётся из схемы
    модели, поэтому тест следует за реальным ограничением БД.
    """
    from app.models import WatcherState

    limit = WatcherState.key.type.length
    assert limit >= 80, f"колонка key уже не должна сужаться: {limit}"
    tx = _hex_hash()
    markers = {
        f"ledger:{tx}",
        f"refund:{tx}",
        f"manual_refund:{10 ** 9}:{10 ** 9}",
        f"job:vote-reminder:{datetime.now().strftime('%Y-%m-%d')}",
    }
    for marker in markers:
        assert len(marker) <= limit, f"маркер {marker[:32]}… длиной {len(marker)} > {limit}"


async def test_claim_once_rejects_key_longer_than_column(session) -> None:
    """Fail-fast: ключ длиннее колонки — ValueError, а не тихое INSERT-падение.

    Раньше SQLite пропускал, Postgres ронял весь цикл watcher'а. Теперь обе
    БД ведут себя одинаково: ошибка в тесте/деве, а не инцидент на проде.
    """
    from app.ops import claim_once

    limit = WatcherState.key.type.length
    with pytest.raises(ValueError, match="длиннее колонки"):
        await claim_once(session, "x" * (limit + 1))


async def test_heal_reprocesses_reported_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Брошенная запись со снимком лечится повторной обработкой: если баг версии
    починен деплоем, перевод классифицируется и уходит из stuck-списка."""
    from app.db import SessionLocal
    from app.ton_watch import _heal_stuck_transfers, _read_stuck, _write_stuck

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stuck_heal_recheck_seconds", 0)
    source = "0:" + os.urandom(32).hex()
    tx_hash = "heal-" + os.urandom(8).hex()
    utime = int(datetime.now(UTC).timestamp())
    stuck = {
        tx_hash: {
            "utime": utime,
            "fails": 99,
            "reported": True,
            "heal_fails": 0,
            "source": source,
            "value_nanotons": 300_000_000,
            "comment": "",
        }
    }
    async with SessionLocal() as db:
        await _write_stuck(db, stuck)
        try:
            healed = await _heal_stuck_transfers()
            assert healed == 1, "повторная обработка снимка не исцелила запись"
            assert not await _read_stuck(db), "исцелённая запись осталась в stuck"
            payout = (
                await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))
            ).first()
            assert payout is not None, "heal не переклассифицировал перевод"
            assert payout._mapping["kind"] == "refund"
            assert payout._mapping["dest_address"] == source
        finally:
            await db.execute(delete(Payout).where(Payout.tx_hash == tx_hash))
            await db.execute(delete(WatcherState).where(WatcherState.key == "ton_watch_stuck_tx"))
            await db.commit()


async def test_heal_auto_refunds_after_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если повторная обработка стабильно падает (_STUCK_HEAL_MAX_REFUND_FAILS раз),
    лекарство возвращает деньги отправителю авто-возвратом: брошенная сумма не
    должна зависать в казне до ручного разбора (инцидент Kote, 0.5+0.5 G)."""
    import app.ton_watch as tw
    from app.db import SessionLocal

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stuck_heal_recheck_seconds", 0)
    monkeypatch.setattr(settings, "refund_min_gram", 0.05)

    # process_transfer теперь всегда падает — как баг версии, который не починили.
    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("вечный баг версии")

    monkeypatch.setattr(tw, "process_transfer", _boom)

    source = "0:" + os.urandom(32).hex()
    tx_hash = "healb-" + os.urandom(8).hex()
    utime = int(datetime.now(UTC).timestamp())
    stuck = {
        tx_hash: {
            "utime": utime,
            "fails": 99,
            "reported": True,
            "heal_fails": 0,
            "source": source,
            "value_nanotons": 500_000_000,
            "comment": "",
        }
    }
    async with SessionLocal() as db:
        await tw._write_stuck(db, stuck)
        try:
            # Провалы лечения копят heal_fails; авто-возврат срабатывает с лимита.
            for _ in range(tw._STUCK_HEAL_MAX_REFUND_FAILS):
                await tw._heal_stuck_transfers()
            row = (await db.execute(Payout.__table__.select().where(Payout.tx_hash == tx_hash))).first()
            assert row is not None, "после лимита провалов авто-возврат не создан"
            assert row._mapping["kind"] == "refund" or row._mapping["kind"] == "prize"
            assert row._mapping["dest_address"] == source
        finally:
            await db.execute(delete(Payout).where(Payout.tx_hash == tx_hash))
            await db.execute(delete(WatcherState).where(WatcherState.key == "ton_watch_stuck_tx"))
            await db.commit()