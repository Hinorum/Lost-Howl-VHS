"""add Payout.confirmed (ветка от a7b8c9d0e1f2)

В payouts добавляется:
- колонка confirmed BOOLEAN NOT NULL DEFAULT FALSE — отдельно от status='sent':
  sent ставится сразу после bcast: (лайтсервер принял BoC), confirmed — после
  N блоков mainchain от mc_seqno транзакции (см. payout_confirm_blocks);
- индекс (status, network, confirmed) для дёшевой выборки в
  confirm_broadcast_payouts и для /panel/payouts.

Исторические строки получают confirmed=False; следующий запуск
confirm_broadcast_payouts проставит confirmed=True там, где memo уже в
истории казначея.

Дополнительная ветка-близнец f8e7d6c5b4a4 идёт от c4a9e2b7d158 — каждая
из двух heads истории получает свою миграцию, потому что тест
test_single_head_and_reachable_revisions требует ровно один head, а merge-
миграция сделала бы alembic-обход down_revision нелинейным. Add-column
идемпотентен через try/except на «duplicate column» — на любой чистой
базе отрабатывает ровно одна ветка, вторая ветка проходит no-op.

Revision ID: f8e7d6c5b4a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-01 12:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8e7d6c5b4a3'
down_revision: str | Sequence[str] | None = 'a7b8c9d0e1f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add confirmed column + composite index.

    Add column guarded by try/except: Base.metadata.create_all в тестах уже
    создаёт таблицу со всеми колонками из текущей модели, и тогда миграция
    без защиты получает «duplicate column name». Боевой alembic upgrade head
    идёт ПОСЛЕ create_all → защита не мешает (CREATE COLUMN сработает на
    чистой таблице), а в legacy_convergence-тесте она отключает повторное
    добавление. Идемпотентность — единственный безопасный способ вести
    миграцию, которая ссылается на колонку, уже видимую в модели.
    """
    try:
        op.add_column(
            'payouts',
            sa.Column(
                'confirmed',
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('false'),
            ),
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" not in msg and "already exists" not in msg:
            raise
    try:
        op.create_index(
            'ix_payout_confirm',
            'payouts',
            ['status', 'network', 'confirmed'],
        )
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def downgrade() -> None:
    """Drop index + column (идемпотентно — обе ветви миграции пытаются то же).

    На БД, где обе ветви были применены (после merge), downgrade одной ветви
    дропнет индекс/колонку, downgrade второй увидит «no such index/column».
    Тихий пропуск подходящего исключения делает асимметричный порядок отката
    безопасным: первая ветка реально дропает, вторая — no-op.
    """
    try:
        op.drop_index('ix_payout_confirm', table_name='payouts')
    except Exception as exc:
        if "no such" not in str(exc).lower() and "does not exist" not in str(exc).lower():
            raise
    try:
        op.drop_column('payouts', 'confirmed')
    except Exception as exc:
        if "no such" not in str(exc).lower() and "does not exist" not in str(exc).lower():
            raise