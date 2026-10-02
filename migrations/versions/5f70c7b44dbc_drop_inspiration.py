"""drop inspiration (жетон «Второго нюха» выпилен: личной микросцены нет)

Жетон-задел под личную микросцену дня так и не получил механики траты и
никогда не выдавался (всегда 0) — убираем поле, чтобы не плодить мёртвый
непотратный ресурс в схеме.

Revision ID: 5f70c7b44dbc
Revises: 2f5a1c9d4e6b
Create Date: 2026-09-23 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5f70c7b44dbc'
down_revision: str | Sequence[str] | None = '2f5a1c9d4e6b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: на базе, собранной create_all по текущим моделям,
    # колонки уже нет (см. 2f5a1c9d4e6b) — снимать нечего, no-op.
    inspector = sa.inspect(op.get_bind())
    if "inspiration" not in {column["name"] for column in inspector.get_columns("players")}:
        return
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.drop_column('inspiration')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('inspiration', sa.Integer(), nullable=False, server_default=sa.text('0'))
        )