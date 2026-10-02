"""legacy_convergence: сведение баз create_all-эпохи в alembic-таймлайн

Базы, которые бутстрапились БЕЗ alembic (схему собирал create_all, недостающие
колонки дотягивал рантайм-DDL старого db.py), не имеют геометрии истории:
прогнать по ним цепочку ревизий нельзя — она упрётся в уже существующие
колонки/таблицы. Эта ревизия делает ровно то, что делал рантайм-хелпер, но в
единственном источнике правды (migrations/): колонка добавляется только если
её нет, типы расширяются только если провисают. На любой базе ревизия
повторяется безопасно; на свежих базах и базах, ведущихся alembic с нуля,
она проходит как no-op — все колонки уже на месте.

Revision ID: c9d0e1f2a3b4
Revises: a7b8c9d0e1f2
Create Date: 2026-09-18 13:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> dict[str, dict]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"]: col for col in inspector.get_columns(table)}
    except Exception:
        return {}


def _add_column(table: str, column: sa.Column) -> None:
    """ADD COLUMN, только если колонки ещё нет (SQLite — batch, PG — plain)."""
    if column.name in _columns(table):
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(column)


def _widen_pg_column(
    table: str,
    name: str,
    target: sa.types.TypeEngine,
    *,
    shorter_than: int | None = None,
) -> None:
    """Расширение типа только под Postgres. SQLite не знает длины VARCHAR и
    рефлективно «уже ок», а батч-ALTER там пересоздал бы таблицу без нужды."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    current = _columns(table).get(name, {}).get("type")
    if current is None or isinstance(current, sa.Text):
        return
    length = getattr(current, "length", None)
    if shorter_than is not None and (length is None or length >= shorter_than):
        return
    op.alter_column(table, name, existing_type=current, type_=target)


def _drop_not_null(table: str, name: str) -> None:
    col = _columns(table).get(name)
    if col is None or col.get("nullable") is not False:
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.alter_column(name, existing_type=col["type"], nullable=True)


def upgrade() -> None:
    """Upgrade schema."""
    # --- rounds ---
    _widen_pg_column("rounds", "chapter_title", sa.String(300), shorter_than=300)
    _add_column("rounds", sa.Column("pot_nanotons", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    _add_column("rounds", sa.Column("rake_nanotons", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    _add_column("rounds", sa.Column("payouts_finalized", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    _add_column("rounds", sa.Column("epilogue_text", sa.String(700), nullable=False, server_default=sa.text("''")))
    _add_column("rounds", sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True))
    _add_column("rounds", sa.Column("tie_note", sa.String(200), nullable=True))
    _add_column("rounds", sa.Column("tie_entropy", sa.String(80), nullable=True))
    _add_column("rounds", sa.Column("rule_entropy", sa.String(80), nullable=True))
    _add_column("rounds", sa.Column("stake_counts_json", sa.Text(), nullable=True))
    _add_column("rounds", sa.Column("weekly_nanotons", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    _add_column("rounds", sa.Column("referral_nanotons", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    _add_column("rounds", sa.Column("money_mode", sa.Boolean(), nullable=False, server_default=sa.text("1")))

    # --- cards ---
    _add_column("cards", sa.Column("tag", sa.String(16), nullable=False, server_default=sa.text("'care'")))

    # --- players ---
    _add_column("players", sa.Column("wallet_address", sa.String(80), nullable=True))
    _add_column("players", sa.Column("wallet_linked_at", sa.DateTime(timezone=True), nullable=True))
    _add_column("players", sa.Column("inspiration", sa.Integer(), nullable=False, server_default=sa.text("0")))
    _add_column("players", sa.Column("wallet_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    _add_column("players", sa.Column("wallet_verify_code", sa.String(16), nullable=True))
    _add_column("players", sa.Column("wallet_verify_created", sa.DateTime(timezone=True), nullable=True))
    _add_column("players", sa.Column("dm_subscribed", sa.Boolean(), nullable=False, server_default=sa.text("1")))
    _add_column("players", sa.Column("current_streak", sa.Integer(), nullable=False, server_default=sa.text("0")))
    _add_column("players", sa.Column("best_streak", sa.Integer(), nullable=False, server_default=sa.text("0")))

    # --- stakes ---
    _add_column("stakes", sa.Column("network", sa.String(16), nullable=False, server_default=sa.text("'mainnet'")))

    # --- payouts ---
    _add_column("payouts", sa.Column("network", sa.String(16), nullable=False, server_default=sa.text("'mainnet'")))
    _add_column("payouts", sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    _add_column("payouts", sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    _add_column("payouts", sa.Column("last_error", sa.String(200), nullable=True))
    _add_column("payouts", sa.Column("comment_override", sa.String(120), nullable=True))
    _add_column("payouts", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    _drop_not_null("payouts", "player_id")
    _drop_not_null("payouts", "round_id")

    # --- incomes ---
    _add_column("incomes", sa.Column("network", sa.String(16), nullable=False, server_default=sa.text("'mainnet'")))

    # --- story_beats ---
    _add_column("story_beats", sa.Column("hook_text", sa.String(700), nullable=True))

    # --- watcher_state: живёт под казной (PG); SQLite не при делах ---
    _widen_pg_column("watcher_state", "value", sa.Text())
    _widen_pg_column("watcher_state", "key", sa.String(80), shorter_than=80)

    # Старые базы держали ENUM-значения в верхнем регистре
    op.execute("UPDATE rounds SET status = lower(status) WHERE status = upper(status)")
    op.execute("UPDATE rounds SET win_rule = lower(win_rule) WHERE win_rule = upper(win_rule)")


def downgrade() -> None:
    """Односторонняя реконсиляция: база уже «где-то» в истории alembic,
    честного даунгрейда для неё не существует."""
    pass