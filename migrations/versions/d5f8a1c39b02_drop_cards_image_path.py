"""drop cards.image_path

Revision ID: d5f8a1c39b02
Revises: c4a9e2b7d158
Create Date: 2026-10-04

Арт в проекте не делается: build_day_post() возвращает пустой список, поэтому
колонка не заполнялась ни разу за все месяцы игры и ни на что не влияла. Поле
убрано из схемы кассеты (CardModel.image_path), из публичного вида дня и из
экспорта редактора, ключи вычищены из всех кассет.

Трубу доставки медиа (send_photo / send_media_group) НЕ трогаем: она работает и
покрыта тестами, вернётся, когда появится реальная генерация картинок.

Правка колонки обязательна, а не косметика: image_path была NOT NULL без
дефолта, и удаление поля из модели роняло INSERT карточек на уровне БД.

Снос защищён проверкой существования — по образцу legacy_convergence. Без неё
ревизия падает KeyError на базе, где колонки не было никогда: цепочка
create_all-эпохи собирает схему из текущих моделей, а она уже без artа. Тест
test_legacy_create_all_db_converges ловит именно этот случай.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f8a1c39b02"
down_revision: str | Sequence[str] | None = "c4a9e2b7d158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> dict[str, dict]:
    try:
        return {col["name"]: col for col in sa.inspect(op.get_bind()).get_columns(table)}
    except Exception:
        return {}


def upgrade() -> None:
    if "image_path" not in _columns("cards"):
        return
    with op.batch_alter_table("cards", schema=None) as batch_op:
        batch_op.drop_column("image_path")


def downgrade() -> None:
    if "image_path" in _columns("cards"):
        return
    with op.batch_alter_table("cards", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("image_path", sa.String(length=400), nullable=False, server_default="")
        )