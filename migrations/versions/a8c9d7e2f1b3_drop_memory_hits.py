"""drop memory_hits

Снятие счётчика внимательности: таблица больше не используется (нет ни
кнопки памяти, ни счётчика «самый памятливый пёс» в лидерборде, ни
записи из handler'а). Эхо в кассетах и рендер `prev` в пост не задеты —
это независимый story-слой.

Revision ID: a8c9d7e2f1b3
Revises: f2d8b6c4a193
Create Date: 2026-10-09 18:50:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8c9d7e2f1b3'
down_revision: str | Sequence[str] | None = 'f2d8b6c4a193'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Падение должно быть идемпотентным: параллельный `create_all`-сценарий
    # (тесты, init_db) уже не создаст таблицу после удаления класса из
    # моделей, но старая БД из прода может её ещё хранить.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "memory_hits" in insp.get_table_names():
        op.drop_table('memory_hits')


def downgrade() -> None:
    """Downgrade schema."""
    # Возврат к старой схеме — на случай отката: первичный ключ составной
    # (player_id, round_id), время создания — на сервере.
    op.create_table(
        'memory_hits',
        sa.Column('player_id', sa.BigInteger(), nullable=False),
        sa.Column('round_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('player_id', 'round_id'),
    )
