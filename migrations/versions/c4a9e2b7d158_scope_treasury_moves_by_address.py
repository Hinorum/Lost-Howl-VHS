"""scope treasury_moves by wallet address

Сумма зеркала казны бралась по одному network, а адрес кошелька в строках не
хранился. Поэтому после ротации TREASURY_*_ADDRESS строки прежнего кошелька
суммировались в баланс нового навсегда: /mirror reset перестраивает историю,
но лишние строки не удаляет, и расхождение «зеркало ≠ цепочка» становится
неубираемым штатным инструментом.

Добавляем колонку address и проставляем её у имеющихся строк по сети: писались
они активным на тот момент кошельком, а адрес в них не остался. Строки сети без
настроенного адреса остаются пустыми — их убирает полный перескан, который
adopt-ит совпавшие и удаляет несовпавшие.

Revision ID: c4a9e2b7d158
Revises: b8c2d1e0f9a3
Create Date: 2026-10-01 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config import settings

# revision identifiers, used by Alembic.
revision: str = 'c4a9e2b7d158'
down_revision: str | Sequence[str] | None = 'b8c2d1e0f9a3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    insp = sa.inspect(op.get_bind())
    if not insp.has_table('treasury_moves'):
        return
    if 'address' in {col['name'] for col in insp.get_columns('treasury_moves')}:
        return
    op.add_column(
        'treasury_moves',
        sa.Column('address', sa.String(length=80), nullable=False, server_default=''),
    )
    bind = op.get_bind()
    for network, address in (
        ('testnet', settings.treasury_testnet_address),
        ('mainnet', settings.treasury_address),
    ):
        if not address:
            continue
        bind.execute(
            sa.text('UPDATE treasury_moves SET address = :addr WHERE network = :net'),
            {'addr': address, 'net': network},
        )
    # server_default был нужен только чтобы добавить NOT NULL-колонку в таблицу с
    # данными. Оставляем его — и схема миграций разойдётся с create_all-базой
    # (test_migrations_flow ловит modify_default), и любая вставка мимо ORM будет
    # молча получать '' вместо адреса. В модели default клиентский, как у соседей.
    # batch обязателен: SQLite не умеет ALTER COLUMN DROP DEFAULT.
    with op.batch_alter_table('treasury_moves', schema=None) as batch_op:
        batch_op.alter_column(
            'address',
            existing_type=sa.String(length=80),
            existing_nullable=False,
            server_default=None,
        )
    op.create_index('ix_treasury_moves_address', 'treasury_moves', ['address'])
    op.create_index('ix_treasury_moves_address_lt', 'treasury_moves', ['address', 'lt'])


def downgrade() -> None:
    """Downgrade schema."""
    insp = sa.inspect(op.get_bind())
    if not insp.has_table('treasury_moves'):
        return
    if 'address' not in {col['name'] for col in insp.get_columns('treasury_moves')}:
        return
    op.drop_index('ix_treasury_moves_address_lt', table_name='treasury_moves')
    op.drop_index('ix_treasury_moves_address', table_name='treasury_moves')
    op.drop_column('treasury_moves', 'address')