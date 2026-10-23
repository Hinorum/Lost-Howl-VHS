"""drop disputes

Команды /dispute и /disputes удалены: у игры нет дежурного хранителя,
который разбирал бы очередь, а тикетница без уведомлений только создавала
невыполненное обещание. Таблицу сносим безусловно вместе с её записями:
код к ней больше не обращается, а хранить то, что ни разу не было
разобрано, — только мёртвый вес в схеме.

Revision ID: a7d2e9c4b1f6
Revises: f4c8a2d91e37
Create Date: 2026-10-23 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7d2e9c4b1f6'
down_revision: str | Sequence[str] | None = 'f4c8a2d91e37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Upgrade schema."""
    if "disputes" not in _table_names():
        return
    op.drop_table("disputes")


def downgrade() -> None:
    """Downgrade schema."""
    if "disputes" in _table_names():
        return
    op.create_table(
        "disputes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=True),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("keeper_note", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_disputes_player_id", "disputes", ["player_id"], unique=False)
    op.create_index("ix_disputes_round_id", "disputes", ["round_id"], unique=False)
