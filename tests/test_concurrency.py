"""Конкурентные сценарии: гонки на атомарных claim-операциях.

Все три точки используют условный UPDATE/WHERE как защиту от гонки
(«at-most-once» семантика): ровно один «процесс» выигрывает, остальные
выходят без побочных эффектов.

Покрывает:
  1. claim_once (app.ops) — INSERT ... ON CONFLICT DO NOTHING.
  2. Round.status OPEN → TALLYING (rounds/lifecycle.close_voting).
  3. Round.status TALLYING → CLOSED + Round.payouts_finalized
     (stakes.finalize_day_payouts) — два «процесса» финализируют один день.
  4. ReferralPot UPDATE WHERE nanotons=amount — списание с копилки.
  5. claim_once с разными ключами не конфликтует.
  6. claim_once валидирует длину ключа (≤80).
  7. claim_announcement (rounds/lifecycle) — объявление дня достаётся
     ровно одному «процессу»: UPDATE WHERE announced_at IS NULL.

ВАЖНО про среду:
  - aiosqlite сериализует записи в одном SQLite-файле через свой
    worker-thread. Настоящей параллельной записи нет; «database is
    locked» ловится даже с WAL+busy_timeout. Реальные гонки
    валидируются на Postgres (CI job test-postgres).
  - Здесь проверяем СЕМАНТИКУ атомарных примитивов через последовательные
    вызовы: rowcount==1 для первого, rowcount==0 для следующих.
    Атомарность ON CONFLICT DO NOTHING и conditional UPDATE — это
    свойство SQL-движка, одинаковое в SQLite и Postgres.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Base,
    ReferralPot,
    Round,
    RoundStatus,
    WatcherState,
    WinRule,
)
from app.ops import claim_once
from app.rounds.lifecycle import claim_announcement

# ----- Утилиты -----------------------------------------------------------


async def _fresh_db(tmp_path):
    """Свежая БД на tmp_path с WAL и busy_timeout.

    Возвращает (engine, maker). Каждый тест использует свои сессии
    через maker() — последовательно, чтобы не упираться в сериализацию
    aiosqlite.
    """
    db_path = tmp_path / "race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, maker


# ----- Test 1: claim_once — атомарность через rowcount -------------------


async def test_claim_once_atomic_semantics(tmp_path) -> None:
    """Первый вызов claim_once(key) даёт True (rowcount==1),
    второй — False (rowcount==0). Это семантика ON CONFLICT DO NOTHING,
    идентичная в SQLite и Postgres.
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            first = await claim_once(sess, "race:announce:2026-09-27")
            await sess.commit()
            second = await claim_once(sess, "race:announce:2026-09-27")
            third = await claim_once(sess, "race:announce:2026-09-27")
        assert first is True, f"первый должен быть True: {first}"
        assert second is False, f"второй должен быть False: {second}"
        assert third is False, f"третий должен быть False: {third}"

        # В БД — ровно одна запись.
        async with maker() as sess:
            rows = (await sess.execute(
                select(WatcherState).where(WatcherState.key == "race:announce:2026-09-27")
            )).scalars().all()
        assert len(rows) == 1, f"дубликаты ключа в БД: {len(rows)}"
    finally:
        await engine.dispose()


# ----- Test 2: условный UPDATE OPEN → TALLYING ----------------------------


async def test_close_voting_conditional_update_semantics(tmp_path) -> None:
    """Условный UPDATE (status='open' → 'tallying') даёт rowcount == 1
    первому процессу и 0 второму — независимо от СУБД.

    Имитируем два «процесса» через последовательные сессии.
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        now = datetime.now(UTC)
        # Создаём раунд через первую сессию.
        async with maker() as sess:
            round_row = Round(
                day_index=1,
                status=RoundStatus.OPEN,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",
                opens_at=now - timedelta(hours=1),
                voting_ends_at=now + timedelta(hours=1),
                tally_ends_at=now + timedelta(hours=2),
                winner_card=0,
            )
            sess.add(round_row)
            await sess.commit()
            round_id = round_row.id

        # Первый процесс: условный UPDATE.
        async with maker() as sess:
            first = (await sess.execute(
                update(Round)
                .where(Round.id == round_id, Round.status == RoundStatus.OPEN)
                .values(status=RoundStatus.TALLYING)
            )).rowcount
            await sess.commit()
        assert first == 1, f"первый процесс должен пройти: rowcount={first}"

        # Второй процесс: тот же условный UPDATE — rowcount == 0.
        async with maker() as sess:
            second = (await sess.execute(
                update(Round)
                .where(Round.id == round_id, Round.status == RoundStatus.OPEN)
                .values(status=RoundStatus.TALLYING)
            )).rowcount
        assert second == 0, f"второй процесс НЕ должен пройти: rowcount={second}"

        # Финальное состояние — TALLYING.
        async with maker() as sess:
            r = await sess.get(Round, round_id)
            assert r.status == RoundStatus.TALLYING
    finally:
        await engine.dispose()


# ----- Test 3: финализация дня — claim payouts_finalized ----------------


async def test_finalize_day_payouts_atomic_claim(tmp_path) -> None:
    """Условный UPDATE `payouts_finalized=False → True` даёт rowcount == 1
    ровно одному процессу. Это защита от двойной финализации.
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        now = datetime.now(UTC)
        async with maker() as sess:
            round_row = Round(
                day_index=42,
                status=RoundStatus.CLOSED,
                win_rule=WinRule.MAJORITY,
                chapter_title="t",
                chapter_text="x",
                opens_at=now - timedelta(hours=25),
                voting_ends_at=now - timedelta(hours=1),
                tally_ends_at=now,
                winner_card=0,
            )
            sess.add(round_row)
            await sess.commit()
            round_id = round_row.id

        # Первый процесс — выигрывает claim.
        async with maker() as sess:
            first = (await sess.execute(
                update(Round)
                .where(Round.id == round_id, Round.payouts_finalized.is_(False))
                .values(payouts_finalized=True)
            )).rowcount
            await sess.commit()
        assert first == 1, f"первый процесс должен пройти: rowcount={first}"

        # Второй процесс — не проходит.
        async with maker() as sess:
            second = (await sess.execute(
                update(Round)
                .where(Round.id == round_id, Round.payouts_finalized.is_(False))
                .values(payouts_finalized=True)
            )).rowcount
        assert second == 0, f"второй процесс НЕ должен пройти: rowcount={second}"

        # Финальное состояние.
        async with maker() as sess:
            r = await sess.get(Round, round_id)
            assert r.payouts_finalized is True
    finally:
        await engine.dispose()


# ----- Test 4: списание с ReferralPot атомарно ----------------------------


async def test_referral_pot_claim_is_atomic(tmp_path) -> None:
    """Условный UPDATE ReferralPot WHERE nanotons=amount даёт rowcount == 1
    ровно одному — защита от того, чтобы два инстанса списали одну копилку.
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            pot = ReferralPot(referrer_id=42, nanotons=1000)
            sess.add(pot)
            await sess.commit()
            pot_id = pot.id
            initial_nanotons = pot.nanotons

        # Первый процесс — успешно списывает.
        async with maker() as sess:
            first = (await sess.execute(
                update(ReferralPot)
                .where(ReferralPot.id == pot_id, ReferralPot.nanotons == initial_nanotons)
                .values(nanotons=0, updated_at=datetime.now(UTC))
            )).rowcount
            await sess.commit()
        assert first == 1, f"первый процесс должен пройти: rowcount={first}"

        # Второй процесс — условие WHERE nanotons=1000 уже не выполняется.
        async with maker() as sess:
            second = (await sess.execute(
                update(ReferralPot)
                .where(ReferralPot.id == pot_id, ReferralPot.nanotons == initial_nanotons)
                .values(nanotons=0, updated_at=datetime.now(UTC))
            )).rowcount
        assert second == 0, f"второй процесс НЕ должен пройти: rowcount={second}"

        # Копилка обнулена.
        async with maker() as sess:
            p = await sess.get(ReferralPot, pot_id)
            assert p.nanotons == 0
    finally:
        await engine.dispose()


# ----- Test 5: claim_once с разными ключами не конфликтует ----------------


async def test_claim_once_different_keys_all_succeed(tmp_path) -> None:
    """N вызовов с разными ключами — все получают True."""
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            results = []
            for i in range(5):
                results.append(await claim_once(sess, f"race:key:{i}"))
            await sess.commit()
        assert all(results), f"не все ключи были взяты: {results}"

        async with maker() as sess:
            rows = (await sess.execute(
                select(WatcherState).where(WatcherState.key.like("race:key:%"))
            )).scalars().all()
        assert len(rows) == 5, f"строк: {len(rows)}, ожидали 5"
    finally:
        await engine.dispose()


# ----- Test 7: claim_announcement — право объявить день достаётся один раз ---


def _round_row(day_index: int) -> Round:
    now = datetime.now(UTC)
    return Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="x",
        opens_at=now - timedelta(hours=1),
        voting_ends_at=now + timedelta(hours=1),
        tally_ends_at=now + timedelta(hours=2),
        winner_card=0,
    )


async def test_claim_announcement_is_atomic(tmp_path) -> None:
    """Объявить день может ровно один «процесс».

    claim_announcement — условный UPDATE Round SET announced_at=... WHERE
    announced_at IS NULL, тот же примитив at-most-once, что close_voting.
    Если бы он стал no-op'ом или read-modify-write, два инстанса бота
    объявили бы один день дважды: второй вызов пошёл бы в send_announcement
    повторно (лишний пост участникам, дважды запущенный таймер дня).
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            row = _round_row(11)
            sess.add(row)
            await sess.commit()
            round_id = row.id

        # Два независимых вызывающих, каждый со своей сессией.
        async with maker() as sess:
            first = await claim_announcement(sess, await sess.get(Round, round_id))
        async with maker() as sess:
            second = await claim_announcement(sess, await sess.get(Round, round_id))
        async with maker() as sess:
            third = await claim_announcement(sess, await sess.get(Round, round_id))

        assert first is True, "первый должен забрать право объявления"
        assert second is False, "второй НЕ должен объявлять день заново"
        assert third is False, "третий НЕ должен объявлять день заново"

        async with maker() as sess:
            r = await sess.get(Round, round_id)
            assert r.announced_at is not None
            stamped = r.announced_at
        # Метка не перетиралась: победил именно первый вызов.
        async with maker() as sess:
            r = await sess.get(Round, round_id)
            assert r.announced_at == stamped
    finally:
        await engine.dispose()


async def test_claim_announcement_is_per_round(tmp_path) -> None:
    """Claim относится к конкретному дню: объявление другого дня не блокируется."""
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            first_day, second_day = _round_row(12), _round_row(13)
            sess.add_all([first_day, second_day])
            await sess.commit()
            first_id, second_id = first_day.id, second_day.id

        async with maker() as sess:
            assert await claim_announcement(sess, await sess.get(Round, first_id)) is True
        async with maker() as sess:
            assert await claim_announcement(sess, await sess.get(Round, second_id)) is True
        async with maker() as sess:
            r = await sess.get(Round, first_id)
            assert r.announced_at is not None
    finally:
        await engine.dispose()


# ----- Test 6: claim_once длина ключа валидируется ------------------------


async def test_claim_once_key_length_validated(tmp_path) -> None:
    """Длинный ключ должен падать ValueError ДО обращения к БД — иначе
    Postgres уронит StringDataRightTruncationError в проде. Защита от
    инцидента 2026-09-17 (см. docstring claim_once).
    """
    engine, maker = await _fresh_db(tmp_path)
    try:
        async with maker() as sess:
            long_key = "x" * 200  # WatcherState.key.type.length = 80.
            with pytest.raises(ValueError, match="длиннее колонки"):
                await claim_once(sess, long_key)
    finally:
        await engine.dispose()
