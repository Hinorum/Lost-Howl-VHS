"""add Payout.confirmed (ветка от c4a9e2b7d158)

Близнец f8e7d6c5b4a3, идущий от второй head истории миграций. Содержимое
upgrade/downgrade идентичное, потому что обе ветки должны привести payouts к
одной схеме: колонка confirmed + индекс ix_payout_confirm. Боевой
alembic upgrade head прогонит только одну из них (ту, что достижима от
текущей alembic_version); вторая останется «висячей» веткой, пока не
будет применена на БД, пришедшей с другой стороны.

Add column идемпотентен через try/except на «duplicate column» — на любой
чистой базе отрабатывает ровно одна ветка, вторая проходит no-op.

Revision ID: f8e7d6c5b4a4
Revises: c4a9e2b7d158
Create Date: 2026-10-01 12:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8e7d6c5b4a4'
down_revision: str | Sequence[str] | None = 'c4a9e2b7d158'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add confirmed column + composite index (см. f8e7d6c5b4a3)."""
    try:
        op.add_column(
            'payouts',
            sa.Column(
                'confirmed',
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('false'),
            ),
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" not in msg and "already exists" not in msg:
            raise
    try:
        op.create_index(
            'ix_payout_confirm',
            'payouts',
            ['status', 'network', 'confirmed'],
        )
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def downgrade() -> None:
    """Drop index + column (идемпотентно — см. f8e7d6c5b4a3)."""
    try:
        op.drop_index('ix_payout_confirm', table_name='payouts')
    except Exception as exc:
        if "no such" not in str(exc).lower() and "does not exist" not in str(exc).lower():
            raise
    try:
        op.drop_column('payouts', 'confirmed')
    except Exception as exc:
        if "no such" not in str(exc).lower() and "does not exist" not in str(exc).lower():
            raise