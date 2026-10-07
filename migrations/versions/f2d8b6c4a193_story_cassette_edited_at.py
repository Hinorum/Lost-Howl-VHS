"""story_cassettes.edited_at: гонка «деплой против правки» решается по отметке

Правка кассеты из /panel обязана переживать деплой (это весь смысл базы),
но контент-фиксы из репозитория обязаны доезжать до неправленных строк.
`edited_at` — провенанс: NULL значит «файл ни разу не правили, принимаю
каталог», значение значит «побеждает база» (см. app/story/store.py).

Revision ID: f2d8b6c4a193
Revises: a3f7d19c5b42
Create Date: 2026-10-07 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2d8b6c4a193'
down_revision: str | Sequence[str] | None = 'a3f7d19c5b42'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: create_all-база (тесты, init_db) уже приходит с
    # колонкой из модели, и хвост не должен на ней падать (см. e5d6a7c8b901).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "story_cassettes" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("story_cassettes")}
    if "edited_at" not in cols:
        op.add_column(
            'story_cassettes',
            sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "story_cassettes" not in insp.get_table_names():
        return
    cols = {column["name"] for column in insp.get_columns("story_cassettes")}
    if "edited_at" in cols:
        op.drop_column('story_cassettes', 'edited_at')
