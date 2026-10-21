"""add processed_transfers (журнал обработанных входящих переводов)

Один хеш цепочки — одна обработка. Строка журнала создаётся в той же
транзакции, что и денежный эффект перевода (ставка, возврат), поэтому
повторный проход watcher'а (overlap-окно, сброс курсора, ре-скан после
деплоя) не размножает эффект, а падение процесса откатывает и метку, и
эффект.

До этой таблицы «перевод уже разобран» проверялся тремя разными способами
(Stake.tx_hash, Income.unit_ref, метка watcher_state refund:<tx>), и у
каждого была своя дыра: перезапись tx_hash при вытеснении пылевой ставки
стирала след перевода, а отсутствие строки Income оставляло его невидимым
для сверки казны.

Revision ID: d7e3a9c41b58
Revises: a1d4f8c27b93
Create Date: 2026-10-21 20:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd7e3a9c41b58'
down_revision: str | Sequence[str] | None = 'a1d4f8c27b93'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: базы create_all-эпохи приводятся к якорю и досыгрывают
    # хвост (app.db._migrate), а create_all уже собрал им эту таблицу. Раньше
    # такой путь падал на «table ... already exists» — то есть реконсиляция
    # ломалась ровно на базах, ради которых написана.
    if sa.inspect(op.get_bind()).has_table("processed_transfers"):
        return
    op.create_table(
        'processed_transfers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tx_hash', sa.String(length=80), nullable=False),
        sa.Column('network', sa.String(length=16), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('amount_nanotons', sa.BigInteger(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tx_hash', 'network', name='uq_processed_transfer_tx_network'),
    )
    op.create_index(
        'ix_processed_transfer_network_created',
        'processed_transfers',
        ['network', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_processed_transfer_network_created', table_name='processed_transfers'
    )
    op.drop_table('processed_transfers')
