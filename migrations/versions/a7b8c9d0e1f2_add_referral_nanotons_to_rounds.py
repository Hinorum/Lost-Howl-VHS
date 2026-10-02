"""add referral_nanotons to rounds

Доля дня, ушедшая в реферальные копилки пригласивших (1% фонда), — чтобы
пост итогов дня показывал, сколько ушло реферальной награде (прозрачность).
День без приведённых ставок или день возврата — 0.

Revision ID: a7b8c9d0e1f2
Revises: b1a2f3c4d5e6
Create Date: 2026-09-18 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: str | Sequence[str] | None = 'b1a2f3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'rounds',
        sa.Column('referral_nanotons', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rounds', 'referral_nanotons')