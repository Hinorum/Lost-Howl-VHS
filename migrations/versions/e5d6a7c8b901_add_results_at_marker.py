"""add results_at marker to rounds (at-least-once results delivery)

Маркер доставки итогов дня: ставится ПОСЛЕ успешного бродкаста (общий пост +
личные) в _announce_results_job и восстановителем _retry_results_job. NULL у
CLOSED-дня позади актуального = краш между коммитом закрытия и рассылкой —
восстановитель досылает. Откат транзакции снимает маркер: повтор после краха
разрешён, дублей не бывает (атомарный claim).

Revision ID: e5d6a7c8b901
Revises: c7b9a2d84e6f
Create Date: 2026-09-30 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5d6a7c8b901'
down_revision: str | Sequence[str] | None = 'c7b9a2d84e6f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: create_all-база уже приходит с колонкой из модели,
    # и хвост не должен на ней падать (см. c7b9a2d84e6f).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "results_at" not in cols:
        op.add_column(
            'rounds',
            sa.Column('results_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "results_at" in cols:
        op.drop_column('rounds', 'results_at')