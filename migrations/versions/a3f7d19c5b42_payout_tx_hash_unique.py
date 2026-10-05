"""payouts: один хеш цепочки — одна выплата (частичный unique)

БД-барьер от двойной оплаты. Раньше уникальность tx_hash у выплат держалась
только на коде: claim_once("refund:<tx>") в ton_watch.refunds и условный
UPDATE ... WHERE status='pending' в диспетчере. Ни один из этих гейтов не
проверяется базой, поэтому пропущенный путь превращался бы в повторный
перевод без всякого сопротивления.

Частичный индекс: NULL («ещё не отправлялось») и метка вещания bcast:<unix> —
общая для всех переводов, разосланных в одну секунду, — выпадают из условия.

Revision ID: a3f7d19c5b42
Revises: d8f1a2b3c4d5
Create Date: 2026-10-05 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f7d19c5b42'
down_revision: str | Sequence[str] | None = 'd8f1a2b3c4d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# То же, что app.models._PAYOUT_TX_UNIQUE_WHERE: дубликаты ищем ДО создания
# индекса, чтобы вместо непонятного IntegrityError на старте бота получить
# перечень строк, которые надо разобрать руками.
_WHERE = "tx_hash IS NOT NULL AND tx_hash NOT LIKE 'bcast:%'"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Переигрываемость: базы create_all-эпохи получают индекс прямо из моделей
    # (app.db._migrate делает create_all и штампует якорь), а промежуточная
    # геометрия миграций прогоняется поверх уже собранной таблицы. Ровно та же
    # история была у treasury_moves — см. ревизию 2f5a1c9d4e6b.
    if "uq_payout_tx_network" in {ix["name"] for ix in inspector.get_indexes("payouts")}:
        return

    # Сначала — честная диагностика дублей, и только потом индекс: иначе
    # вместо перечня строк оператор получил бы невнятный IntegrityError.
    # GROUP_CONCAT (SQLite) / STRING_AGG (Postgres) собирают id, чтобы
    # разбор не начинался с поиска строк руками.
    aggregate = "GROUP_CONCAT(id)" if bind.dialect.name == "sqlite" else "STRING_AGG(id::text, ',')"
    duplicates = bind.execute(
        sa.text(
            f"SELECT tx_hash, network, COUNT(*) AS n, {aggregate} AS ids "
            f"FROM payouts WHERE {_WHERE} "
            "GROUP BY tx_hash, network HAVING COUNT(*) > 1"
        )
    ).fetchall()

    if duplicates:
        listing = "; ".join(
            f"tx_hash={row[0]} network={row[1]} n={row[2]} ids=[{row[3]}]" for row in duplicates
        )
        raise RuntimeError(
            "В payouts уже есть одинаковые tx_hash — частичный unique-индекс "
            "создать нельзя, а молча обнулить хеш у второй строки нельзя тоже "
            "(это вернул бы её в сверку и могло привести к повторной отправке). "
            "Разберись с дублями вручную и повтори миграцию. Дубли: " + listing
        )

    op.create_index(
        'uq_payout_tx_network',
        'payouts',
        ['tx_hash', 'network'],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    """Downgrade schema."""
    if "uq_payout_tx_network" not in {
        ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("payouts")
    }:
        return
    op.drop_index('uq_payout_tx_network', table_name='payouts')
