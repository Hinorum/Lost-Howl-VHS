"""drop cards.description

(легаси) Авторская суть развилки: в пост дня не шла (там блок дилеммы и
кнопки-названия карт), читалась только в превью редактора /cassette и в
public_round_view. Колонка была NOT NULL без дефолта — как image_path,
который сносили миграцией d5f8a1c39b02. Дроп идемпотентен: create_all
после удаления поля из моделей колонку уже не создаёт, старая БД прода
может ещё хранить.

Revision ID: e3a7c1d59f42
Revises: b7e1d4c9a2f6
Create Date: 2026-10-10 13:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3a7c1d59f42'
down_revision: str | Sequence[str] | None = 'b7e1d4c9a2f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cards" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("cards")}
    if "description" in cols:
        op.drop_column('cards', 'description')


def downgrade() -> None:
    """Downgrade schema."""
    # Возврат к старой схеме — на случай отката версии: колонка nullable,
    # значения не восстанавливаются (старый код и не показывал их никому).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cards" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("cards")}
    if "description" not in cols:
        op.add_column('cards', sa.Column('description', sa.Text(), nullable=True))
