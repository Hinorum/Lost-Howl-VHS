"""drop story_beats.hook_text

(легаси) Крючок главы: больше не пишется ни одним кодом — кассеты
самостоятельны, а эхо живёт в слое story/bay.py. Колонка осталась от
старой механики глав. Дроп идемпотентен: create_all-сценарий (тесты,
init_db) после удаления поля из моделей её уже не создаёт, а старая
БД прода может ещё хранить.

Revision ID: b7e1d4c9a2f6
Revises: 6c62256c2134
Create Date: 2026-10-10 11:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e1d4c9a2f6'
down_revision: str | Sequence[str] | None = '6c62256c2134'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "story_beats" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("story_beats")}
    if "hook_text" in cols:
        op.drop_column('story_beats', 'hook_text')


def downgrade() -> None:
    """Downgrade schema."""
    # Возврат к старой схеме — на случай отката версии: колонка
    # nullable, значения не восстанавливаются (старый код и не писал их).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "story_beats" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("story_beats")}
    if "hook_text" not in cols:
        op.add_column(
            'story_beats',
            sa.Column('hook_text', sa.String(700), nullable=True),
        )
