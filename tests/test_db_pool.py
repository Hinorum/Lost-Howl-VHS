"""Пул соединений и advisory-лок: размер настраивается, лок не занимает слот.

На СЕССИОННОМ пулере (Supabase, порт 5432) каждое клиентское соединение —
отдельный серверный backend. Пул в 10+20 — это до 30 backend'ов на пике
закрытия дня, и база может ответить «too many clients». Поэтому размер пула
вынесен в настройки, а про слишком широкий пул бот предупреждает на старте.

Второе: advisory-лок живёт на физическом соединении и намеренно не
возвращается в пул (иначе лок уехал бы с чужой сессией). Но из основного
пула он занимал СЛОТ на всё время жизни процесса и старел мимо
pool_recycle. Теперь у лока свой движок с NullPool: слот основного пула не
тронут, соединение одно.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.db import engine


def test_pool_size_comes_from_settings() -> None:
    """Размер пула — из настроек, а если он нулевой, подбирается сам."""
    from app.db import pool_limits

    size, overflow = pool_limits()
    assert size >= 1
    assert overflow >= 0
    assert settings.db_pool_timeout >= 1
    if engine.dialect.name != "postgresql":
        # SQLite (StaticPool) не принимает pool-параметры — там размер пула
        # неприменим, проверка уходит в джобу test-postgres.
        pytest.skip("размер пула применяется только к Postgres")
    assert engine.pool.size() == size
    assert engine.pool._max_overflow == overflow


def test_pool_auto_sizing_follows_pooler_type(monkeypatch) -> None:
    """Пул подбирается по типу пулера: сессионный — узкий, остальные — широкий.

    Сессионный пулер Supabase (5432): каждое клиентское соединение —
    отдельный серверный backend. Пул 10+20 — это до 30 backend'ов на пике
    закрытия дня, и база отдаёт «too many clients» ровно тогда, когда игра
    busiest. Транзакционный пулер (6543) мультиплексирует — там широкий пул
    безопасен.
    """
    from app.db import pool_limits

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 0)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    assert pool_limits() == (5, 2)

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
    )
    assert pool_limits() == (10, 20)


def test_pool_auto_sizing_yields_to_explicit_settings(monkeypatch) -> None:
    """Явные настройки выигрывают: оператор может знать про тариф лучше нас."""
    from app.db import pool_limits

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 8)
    monkeypatch.setattr(settings, "db_max_overflow", 4)
    assert pool_limits() == (8, 4)


def test_pool_explicit_overflow_survives_auto_size(monkeypatch) -> None:
    """Явный DB_MAX_OVERFLOW не выбрасывается, когда DB_POOL_SIZE = 0.

    Регресс: условие было `if size > 0: return size, overflow`, поэтому
    оператор, задавший ТОЛЬКО переполнение (size оставил нулевым —
    «подбери сам»), получал автоподбор целиком. Своего числа он в пуле не
    видел, и конфиг молча расходился с поведением. Теперь каждая ось
    решается отдельно: нулевая — подбор, ненулевая — уважение.
    """
    from app.db import pool_limits

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
    )
    # Транзакционный пулер: автоподбор дал бы 10+20. Оператор оставил size
    # нулевым и попросил переполнение 5 — должно выйти 10+5, а не 10+20.
    monkeypatch.setattr(settings, "db_pool_size", 0)
    monkeypatch.setattr(settings, "db_max_overflow", 5)
    assert pool_limits() == (10, 5)

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )
    # Сессионный: автоподбор дал бы 5+2. Явное переполнение 5 → 5+5.
    monkeypatch.setattr(settings, "db_pool_size", 0)
    monkeypatch.setattr(settings, "db_max_overflow", 5)
    assert pool_limits() == (5, 5)


def test_pool_explicit_size_survives_auto_overflow(monkeypatch) -> None:
    """Обратная сторона: явный DB_POOL_SIZE при нулевом переполнении.

    Симметрия предыдущего теста: подставлять автоподбор в пустое место нужно
    на обеих осях, а не только на первой.
    """
    from app.db import pool_limits

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 3)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    assert pool_limits() == (3, 20)


def test_wide_pool_on_session_pooler_warns(monkeypatch) -> None:
    """Сессионный пулер + широкий пул = предупреждение на старте."""
    from app.main import _pool_size_warnings

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 10)
    monkeypatch.setattr(settings, "db_max_overflow", 20)
    warnings = _pool_size_warnings()
    assert warnings, "широкий пул на сессионном пулере должен быть замечен"
    assert "too many clients" in warnings[0]


def test_narrow_pool_on_session_pooler_is_silent(monkeypatch) -> None:
    """Урезанный пул — не повод для предупреждения."""
    from app.main import _pool_size_warnings

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 5)
    monkeypatch.setattr(settings, "db_max_overflow", 5)
    assert _pool_size_warnings() == []


def test_transaction_pooler_is_never_warned(monkeypatch) -> None:
    """Транзакционный пулер (6543) держит сколько угодно клиентских соединений."""
    from app.main import _pool_size_warnings

    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
    )
    monkeypatch.setattr(settings, "db_pool_size", 10)
    monkeypatch.setattr(settings, "db_max_overflow", 20)
    assert _pool_size_warnings() == []


async def test_lock_uses_its_own_engine(monkeypatch) -> None:
    """Лок не занимает слот основного пула: у него свой движок.

    Иначе соединение лока висело бы в пуле на всё время жизни процесса
    (и старело мимо pool_recycle, который основной пул пересоздаёт).
    """
    from app import scheduler_lock as slock

    class _FakeEngine:
        dialect = type("d", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.connected = 0

        async def connect(self):
            self.connected += 1
            return _FakeConn()

    class _FakeConn:
        async def execute(self, stmt, params=None):
            return _FakeResult()

        async def close(self) -> None:
            return None

    class _FakeResult:
        def scalar(self):
            return True

    fake = _FakeEngine()
    monkeypatch.setattr(slock, "_lock_conn", None)
    monkeypatch.setattr(slock, "_lock_engine_instance", lambda: fake)
    monkeypatch.setattr("app.db.engine", fake)

    assert await slock.acquire_scheduler_lock() is True
    assert fake.connected == 1
    assert slock.scheduler_lock_held() is True

    await slock.release_scheduler_lock()
