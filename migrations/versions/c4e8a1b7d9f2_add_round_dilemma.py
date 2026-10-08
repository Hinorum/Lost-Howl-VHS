"""rounds.dilemma: блок «что предстоит решить» в посте дня

Новая структура поста (Lost Dogs-style): эхо вчерашнего выбора → сюжет дня →
блок дилеммы, а три варианта голосования — только в кнопках под названиями
карт. Поле снимается с кассеты при материализации раунда (как chapter_text);
NULL — прежняя структура: витрина трёх карт в посте. См. broadcast.status_text.

Revision ID: c4e8a1b7d9f2
Revises: f2d8b6c4a193
Create Date: 2026-10-08 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8a1b7d9f2'
down_revision: str | Sequence[str] | None = 'f2d8b6c4a193'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: create_all-база (тесты, init_db) уже приходит с
    # колонкой из модели — хвост не должен на ней падать (см. f2d8b6c4a193).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "dilemma" not in cols:
        op.add_column(
            'rounds',
            sa.Column('dilemma', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("rounds")}
    if "dilemma" in cols:
        op.drop_column('rounds', 'dilemma')
