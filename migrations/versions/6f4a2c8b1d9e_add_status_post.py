"""add status_post (where the day's status post was delivered)

Точки доставки поста-статуса текущего дня (чат + message_id), чтобы при росте
подтверждённого банка дня можно было ОТРЕДАКТИРОВАТЬ уже отправленный пост, а
не просить игрока звать /today. last_pot_nanotons дедуплицирует правки: пост
правим только когда сумма подтверждённых ставок реально изменилась.

Revision ID: 6f4a2c8b1d9e
Revises: 5f70c7b44dbc
Create Date: 2026-09-28 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6f4a2c8b1d9e'
down_revision: str | Sequence[str] | None = '5f70c7b44dbc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: см. 2f5a1c9d4e6b — create_all-база приходит с готовой
    # таблицей, и хвост не должен на ней падать.
    if sa.inspect(op.get_bind()).has_table("status_post"):
        return
    op.create_table(
        'status_post',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('round_id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('is_dm', sa.Boolean(), nullable=False),
        sa.Column('last_pot_nanotons', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['round_id'], ['rounds.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('round_id', 'chat_id', name='uq_status_post_round_chat'),
    )
    op.create_index(op.f('ix_status_post_round_id'), 'status_post', ['round_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_status_post_round_id'), table_name='status_post')
    op.drop_table('status_post')