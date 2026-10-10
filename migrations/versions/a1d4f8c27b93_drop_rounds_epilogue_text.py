"""drop rounds.epilogue_text

Эпилог дня снесён указом владельца: канон (уцелевший consequence) хранится
в StoryBeat (журнал пути), поле epilogue_text больше не читается и не
пишется — из поста итогов эпилог убран ещё раньше (е7ae920). Флаги
готовности лидерборда (month/week_leaderboard_ready) ставятся теперь на
факте закрытия последнего дня периода, а не на записи текста.

Дроп идемпотентен: create_all после удаления поля из моделей колонку уже
не создаёт, старая БД прода может ещё хранить.

Revision ID: a1d4f8c27b93
Revises: e3a7c1d59f42
Create Date: 2026-10-10 16:25:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1d4f8c27b93'
down_revision: str | Sequence[str] | None = 'e3a7c1d59f42'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("rounds")}
    if "epilogue_text" in cols:
        op.drop_column('rounds', 'epilogue_text')


def downgrade() -> None:
    """Downgrade schema."""
    # Возврат к старой схеме: колонка возвращается пустой — значения не
    # восстанавливаются (старый код их и не читает).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "rounds" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("rounds")}
    if "epilogue_text" not in cols:
        op.add_column(
            'rounds',
            sa.Column(
                'epilogue_text', sa.String(700), nullable=False,
                server_default=sa.text("''"),
            ),
        )
