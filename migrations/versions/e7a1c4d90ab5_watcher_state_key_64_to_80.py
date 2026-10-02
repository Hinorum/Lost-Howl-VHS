"""watcher_state.key 64 -> 80: claim-маркеры refund:<tx_hash> не влезали

Прод-инцидент 2026-09-17: 'refund:' + 64 hex tx-hash = 71 символ ронял
INSERT INTO watcher_state (StringDataRightTruncationError на PK VARCHAR(64)).
Перевод навсегда зацикливался в stuck-списке: возврат с неверным MEMO не
создавался, проверочный bv:-перевод не подтверждал кошелёк, удержанный
приз не уходил. Тип колонки поднимается до VARCHAR(80).

Заодно закрывает drift моделей: payouts.claimed_at (10436e6) и
rounds.rule_entropy (3141168) добавлялись только runtime-DDL в db.py
и не имели Alembic-ревизий — из-за них alembic check падал в CI.

Revision ID: e7a1c4d90ab5
Revises: cf07bde2fcc2
Create Date: 2026-09-17 22:05:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7a1c4d90ab5'
down_revision: str | Sequence[str] | None = 'cf07bde2fcc2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('watcher_state', schema=None) as batch_op:
        batch_op.alter_column(
            'key',
            existing_type=sa.String(length=64),
            type_=sa.String(length=80),
        )
    inspector = sa.inspect(op.get_bind())
    if 'claimed_at' not in {column['name'] for column in inspector.get_columns('payouts')}:
        with op.batch_alter_table('payouts', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True)
            )
    if 'rule_entropy' not in {column['name'] for column in inspector.get_columns('rounds')}:
        with op.batch_alter_table('rounds', schema=None) as batch_op:
            batch_op.add_column(sa.Column('rule_entropy', sa.String(length=80), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('rounds', schema=None) as batch_op:
        batch_op.drop_column('rule_entropy')
    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.drop_column('claimed_at')
    # Возврат на 64 возможен только для ключей короче 65 символов: маркеры
    # refund:/ledger: с полным хешем пришлось бы удалить. На практике даунгрейд
    # схемы казны не выполняется — оставляем честный ALTER.
    with op.batch_alter_table('watcher_state', schema=None) as batch_op:
        batch_op.alter_column(
            'key',
            existing_type=sa.String(length=80),
            type_=sa.String(length=64),
        )
