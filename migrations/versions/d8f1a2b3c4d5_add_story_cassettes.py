"""story_cassettes: кассета сюжета переезжает с эфемерного диска в базу

Рендер-диск живёт только до перезапуска деплоя: все правки кассет, сделанные
хранителем в /panel, исчезали молча, а сюжет месяца откатывался к тому, что
лежало в репозитории. Теперь источник правды — таблица (payload + слепок для
Undo), а каталог на диске остаётся кэшем-зеркалом для синхронного движка.

Revision ID: d8f1a2b3c4d5
Revises: c4a9e2b7d158
Create Date: 2026-10-02 11:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8f1a2b3c4d5'
down_revision: str | Sequence[str] | None = 'c4a9e2b7d158'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Переигрываемость: базы create_all-эпохи приводятся к якорю и досыгрывают
    # хвост (app.db._migrate), а create_all уже собрал им эту таблицу из
    # текущих моделей. Без guard такой путь падал бы на «table story_cassettes
    # already exists» — то есть реконсиляция ломалась ровно на базах, ради
    # которых написана (см. 2f5a1c9d4e6b, 6f4a2c8b1d9e).
    if sa.inspect(op.get_bind()).has_table("story_cassettes"):
        return
    op.create_table(
        'story_cassettes',
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('backup', sa.Text(), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('name'),
    )


def downgrade() -> None:
    """Downgrade schema.

    Данные кассет при откате теряются: на диске остаётся последнее зеркало,
    а полноценного возврата к файловой библиотеке downgrade не делает — он
    существует только для отката схемы, не для отката контента.
    """
    op.drop_table('story_cassettes')
