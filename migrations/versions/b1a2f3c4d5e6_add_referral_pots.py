"""add referral_pots (rewards for referrers)

Накопленные реферальные награды (доля referral_pct от подтверждённых ставок
приведённых игроков). Отдельная таблица-накопитель под каждого реферера:
выплаты идут обычной очередью Payout kind="referral" при достижении
referral_min_payout_gram — микропереводы не плодятся.

Revision ID: b1a2f3c4d5e6
Revises: e7a1c4d90ab5
Create Date: 2026-09-18 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1a2f3c4d5e6'
down_revision: str | Sequence[str] | None = 'e7a1c4d90ab5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'referral_pots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('referrer_id', sa.BigInteger(), nullable=False),
        sa.Column('nanotons', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # unique=True + index=True в модели: уникальный индекс, а НЕ отдельный
    # UniqueConstraint (SQLite-отражение alembic check видит их по-разному).
    op.create_index('ix_referral_pots_referrer_id', 'referral_pots', ['referrer_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_referral_pots_referrer_id', table_name='referral_pots')
    op.drop_table('referral_pots')