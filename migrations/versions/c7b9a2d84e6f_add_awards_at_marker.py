"""add awards_at marker to rounds (idempotent score claim)

Маркер начисления очков дня: единый claim для award_points, чтобы краш между
finish_tally и award_points не терял очки молча, а повторный вызов (ретрай
тика, админский /advance, heal) не удваивал score. Ставится значением,
NULL — очки ещё не начислены либо дня-победителя нет.

Revision ID: c7b9a2d84e6f
Revises: 6f4a2c8b1d9e
Create Date: 2026-09-30 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7b9a2d84e6f'
down_revision: str | Sequence[str] | None = '6f4a2c8b1d9e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: create_all-база уже приходит с колонкой из модели,
    # и хвост не должен на ней падать (см. 2f5a1c9d4e6b, 6f4a2c8b1d9e).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "awards_at" not in cols:
        op.add_column(
            'rounds',
            sa.Column('awards_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "awards_at" in cols:
        op.drop_column('rounds', 'awards_at')