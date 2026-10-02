"""merge: сливает f8e7d6c5b4a3 и f8e7d6c5b4a4

Параллельные ветки f8e7d6c5b4a3 (от a7b8c9d0e1f2) и f8e7d6c5b4a4 (от
c4a9e2b7d158) добавляют одну и ту же колонку payouts.confirmed с
идемпотентным try/except. Чтобы alembic видел ровно один head (требование
test_single_head_and_reachable_revisions), сливаем их через no-op merge.

Upgrade и downgrade ничего не делают: обе ветки уже применили свои
изменения на момент, когда alembic дойдёт до merge; колонка существует
ровно в одном экземпляре.

Revision ID: f8e7d6c5b4a5
Revises: f8e7d6c5b4a3, f8e7d6c5b4a4
Create Date: 2026-10-01 12:31:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = 'f8e7d6c5b4a5'
down_revision: str | Sequence[str] | None = ('f8e7d6c5b4a3', 'f8e7d6c5b4a4')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op merge: обе ветки уже применили одинаковые изменения."""
    pass


def downgrade() -> None:
    """No-op merge: rollback не раздваивает обратно на обе ветки."""
    pass