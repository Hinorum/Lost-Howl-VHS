"""add results_claimed_at marker to rounds (claim before broadcast)

Право на рассылку итогов фиксируется ДО бродкаста отдельной меткой.
`results_at` остаётся фактом доставки: его читают восстановитель и тревоги
ops, и он по-прежнему ставится только после успешного бродкаста. Новая
`results_claimed_at` — at-most-once claim: коммитится сразу, поэтому строка
дня и соединение пула больше не удерживаются всю рассылку. Ошибка снимает
только свой штамп; брошенный claim (жёсткий краш) перехватывается догоном
после `RESULTS_CLAIM_TIMEOUT_SECONDS`.

Revision ID: d4b8e6f2a9c1
Revises: a7d2e9c4b1f6
Create Date: 2026-11-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4b8e6f2a9c1'
down_revision: str | Sequence[str] | None = 'a7d2e9c4b1f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: create_all-база уже приходит с колонкой из модели,
    # и хвост не должен на ней падать (см. e5d6a7c8b901).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "results_claimed_at" not in cols:
        op.add_column(
            'rounds',
            sa.Column('results_claimed_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "results_claimed_at" in cols:
        op.drop_column('rounds', 'results_claimed_at')
