"""add treasury_moves.out_msgs_count

Стоимость транзакции казны сеть берёт целиком, а одна транзакция может
содержать несколько исходящих сообщений (пачка v5r1 — до 50 выплат). Чтобы
оценка комиссии «за перевод» не превращалась в оценку «за пачку», зеркало
запоминает число исходящих сообщений, а app/core/fees.py делит на него
стоимость транзакции.

Исторические строки получают 1: пачка до этой миграции не включалась
(PAYOUT_V5_BATCH=false), то есть каждая транзакция несла один перевод.
Переигрываемость: колонка добавляется только если её ещё нет — create_all
на новых базах создаёт её сам.

Revision ID: f4c8a2d91e37
Revises: d7e3a9c41b58
Create Date: 2026-10-22 13:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4c8a2d91e37'
down_revision: str | Sequence[str] | None = 'd7e3a9c41b58'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"] for col in inspector.get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "treasury_moves" not in inspector.get_table_names():
        return
    if "out_msgs_count" in _columns("treasury_moves"):
        return
    op.add_column(
        'treasury_moves',
        sa.Column(
            'out_msgs_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1'),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "treasury_moves" not in inspector.get_table_names():
        return
    if "out_msgs_count" not in _columns("treasury_moves"):
        return
    with op.batch_alter_table("treasury_moves", schema=None) as batch_op:
        batch_op.drop_column('out_msgs_count')
