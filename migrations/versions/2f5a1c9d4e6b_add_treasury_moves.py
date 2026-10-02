"""add treasury_moves (chain-backed mirror of the treasury wallet)

Зеркало транзакций активного кошелька казначея: независимая копия истории
цепочки для сверки «тютелька в тютельку». Тождество по построению:
баланс = Σ balance_delta от генезиса до головы цепочки, поэтому сверка
с живым балансом не зависит от оценки газа (реальный fee из total_fees).

Revision ID: 2f5a1c9d4e6b
Revises: 3197cf14cbeb
Create Date: 2026-09-22 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2f5a1c9d4e6b'
down_revision: str | Sequence[str] | None = '3197cf14cbeb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: базы create_all-эпохи приводятся к якорю и досыгрывают
    # хвост (app.db._migrate), а create_all уже собрал им эту таблицу. Раньше
    # такой путь падал на «table treasury_moves already exists» — то есть
    # реконсиляция ломалась ровно на базах, ради которых написана.
    if sa.inspect(op.get_bind()).has_table("treasury_moves"):
        return
    op.create_table(
        'treasury_moves',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tx_hash', sa.String(length=80), nullable=False),
        sa.Column('network', sa.String(length=16), nullable=False),
        sa.Column('utime', sa.BigInteger(), nullable=False),
        sa.Column('lt', sa.BigInteger(), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('value_nanotons', sa.BigInteger(), nullable=False),
        sa.Column('fee_nanotons', sa.BigInteger(), nullable=False),
        sa.Column('balance_delta_nanotons', sa.BigInteger(), nullable=False),
        sa.Column('counterparty', sa.String(length=80), nullable=False),
        sa.Column('comment', sa.String(length=200), nullable=False),
        sa.Column('linked_id', sa.BigInteger(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_treasury_moves_tx_hash', 'treasury_moves', ['tx_hash'], unique=True)
    op.create_index('ix_treasury_moves_network', 'treasury_moves', ['network'])
    op.create_index('ix_treasury_moves_utime', 'treasury_moves', ['utime'])
    op.create_index('ix_treasury_moves_lt', 'treasury_moves', ['lt'])
    op.create_index('ix_treasury_moves_kind', 'treasury_moves', ['kind'])
    op.create_index('ix_treasury_moves_linked_id', 'treasury_moves', ['linked_id'])
    op.create_index(
        'ix_treasury_moves_lt_id', 'treasury_moves', ['network', 'lt', 'id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_treasury_moves_lt_id', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_linked_id', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_kind', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_lt', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_utime', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_network', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_tx_hash', table_name='treasury_moves')
    op.drop_table('treasury_moves')