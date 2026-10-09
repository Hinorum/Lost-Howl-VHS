"""merge a8c9d7e2f1b3 and c4e8a1b7d9f2 heads

Revision ID: 6c62256c2134
Revises: a8c9d7e2f1b3, c4e8a1b7d9f2
Create Date: 2026-10-09 20:50:58.941882

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c62256c2134'
down_revision: Union[str, Sequence[str], None] = ('a8c9d7e2f1b3', 'c4e8a1b7d9f2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
