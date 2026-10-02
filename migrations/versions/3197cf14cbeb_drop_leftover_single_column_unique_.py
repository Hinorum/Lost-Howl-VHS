"""drop leftover single-column unique stakes.tx_hash

Наложенный историей дубль уникальности: базовая ревизия 917223d866c0 создала
безымянный UNIQUE на колонке tx_hash, а 11f41c6ba244 добавила составной
uq_stake_tx_network (tx_hash, network) без снятия старого. Модель декларирует
только составной, поэтому единичный unique на tx_hash — мёртвая связка.

Диалекты реализуют её по-разному:
- Postgres — настоящий constraint 'stakes_tx_hash_key' (batch не пересоздаёт
  таблицу, узел дрейфа в alembic check);
- SQLite — анонимный unique (автоиндекс), который не снимается ни drop_index
  («index used by constraint»), ни drop_constraint по несуществующему имени.
  Имя анонимному constraint присваивает naming_convention batch-пересборки.

Ревизия рефлексивно находит любой unique ровно по tx_hash и снимает его
соответствующим способом; если его уже нет — честный no-op.

Revision ID: 3197cf14cbeb
Revises: c9d0e1f2a3b4
Create Date: 2026-09-21 09:30:38.806559

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3197cf14cbeb'
down_revision: str | Sequence[str] | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Соглашение имён для безымянного unique: то же, что SQLAlchemy использовал бы
# для обычного авто-именования UniqueConstraint (здесь имя детерминировано).
_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _leftover_single_column_unique() -> dict | None:
    """Unique-ограничение ровно по tx_hash (dict рефлексии) либо None.

    Различать надо не по имени, а по наличию: SQLite отдаёт анонимный
    автоиндекс с name=None — раньше он неотличим был от «снимать нечего»,
    и ревизия падала ValueError на базах, где дубликата нет вовсе (в том
    числе на базах, идущих через реконсиляцию легаси).
    """
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("stakes"):
        if constraint.get("column_names") == ["tx_hash"]:
            return constraint
    return None


def upgrade() -> None:
    """Upgrade schema."""
    constraint = _leftover_single_column_unique()
    if constraint is None:
        # Уже снято (или база шла create_all-минуя эту ревизию) — no-op.
        return
    name = constraint.get("name")
    if name:
        with op.batch_alter_table("stakes", schema=None) as batch_op:
            batch_op.drop_constraint(name, type_="unique")
        return
    # SQLite: имя анонимного (None) присваивает naming_convention — то же
    # соглашение, что у стандартного авто-именования, поэтому имя детерминировано.
    with op.batch_alter_table("stakes", schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint("uq_stakes_tx_hash", type_="unique")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("stakes", schema=None) as batch_op:
        batch_op.create_unique_constraint("stakes_tx_hash_key", ["tx_hash"])