"""backfill round delivery markers for historical days

Колонки-маркеры доставки (announced_at / results_at / awards_at) добавлялись
миграциями БЕЗ бэкфилла, а восстановители планировщика трактуют «NULL =
не обработано». Поэтому у КАЖДОГО исторического закрытого дня маркер был
NULL, и первый же тик новой версии считал всю историю игры недоставленной:
_ retry_results_job рассылал заново все закрытые дни (27 постов флудом за
пару минут), award_pending_points переначислял очки, _retry_new_day_job
объявлял старые дни заново. На проде это выглядело как «дни сами
прокрутились, применились ставки, которых не отправляли».

recency-гард в догонах (catchup_cutoff) — основная защита. Эта миграция
дополнительно делает маркеры честными: помечает ОБРАБОТАННЫМИ дни, чья
граница (opens_at) старше окна догона. Свежие дни (в пределах окна) НЕ
трогаем — их NULL-маркер означает настоящий свежий краш, и живой догон
должен их доставить. Старые дни в окно не попадают, поэтому подавление
свежего догона невозможно.

Идемпотентна (WHERE маркер IS NULL) и безопасна для SQLite/Postgres
(используется Core-update с типами, а не raw SQL). Деньги не трогает:
payouts_finalized намеренно НЕ бэкфиллится — у выплат своя логика
(идемпотентность по флагу внутри finalize_day_payouts).

Revision ID: b8c2d1e0f9a3
Revises: e5d6a7c8b901
Create Date: 2026-10-01 12:00:00.000000

"""
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c2d1e0f9a3'
down_revision: str | Sequence[str] | None = 'e5d6a7c8b901'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Окно догона (совпадает с настройкой catchup_window_hours). Только дни,
# открытые ДО этой границы, считаем историей. Миграция разовая, значение
# зафиксировано здесь намеренно: не тянем app.config (миграции должны быть
# самодостаточны и не «плавать» от настроек будущих версий).
_CATCHUP_WINDOW_HOURS = 72


def _rounds_table():
    return sa.table(
        "rounds",
        sa.column("status", sa.String(16)),
        sa.column("opens_at", sa.DateTime(timezone=True)),
        sa.column("voting_ends_at", sa.DateTime(timezone=True)),
        sa.column("tally_ends_at", sa.DateTime(timezone=True)),
        sa.column("announced_at", sa.DateTime(timezone=True)),
        sa.column("results_at", sa.DateTime(timezone=True)),
        sa.column("awards_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    """Пометить исторические дни обработанными."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    required = {"opens_at", "voting_ends_at", "tally_ends_at", "status"}
    if not required.issubset(cols):
        return

    rounds = _rounds_table()
    cutoff = datetime.now(UTC) - timedelta(hours=_CATCHUP_WINDOW_HOURS)
    old = (rounds.c.opens_at < cutoff)
    closed = (rounds.c.status == 'closed')
    # Момент закрытия/открытия — для маркеров доставки.
    boundary = sa.func.coalesce(
        rounds.c.tally_ends_at, rounds.c.voting_ends_at, rounds.c.opens_at
    )

    if "announced_at" in cols:
        bind.execute(
            rounds.update()
            .where(rounds.c.announced_at.is_(None), old)
            .values(announced_at=rounds.c.opens_at)
        )
    if "results_at" in cols:
        bind.execute(
            rounds.update()
            .where(rounds.c.results_at.is_(None), closed, old)
            .values(results_at=boundary)
        )
    if "awards_at" in cols:
        # Помечаем ВСЕ закрытые дни, включая дни без победителя: очки там
        # начислять нечего (award_pending_points требует winner_card), то есть
        # день уже полностью обработан. Оставленный NULL мигал бы в диагностике
        # как незакрытый «долг по очкам», которого не существует.
        bind.execute(
            rounds.update()
            .where(rounds.c.awards_at.is_(None), closed, old)
            .values(awards_at=boundary)
        )


def downgrade() -> None:
    """Данные: откатывать нечего — какие маркеры были проставлены, история
    не помнит. Восстановители защищены recency-гардом, поэтому downgrade
    ничего не сломает."""
    return
