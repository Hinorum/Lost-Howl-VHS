"""Оценка комиссии сети: настроенная, фактическая и рабочая.

PAYOUT_FEE_GRAM — константа, записанная при настройке. Сеть вправе
подорожать (перегрузка), и тогда казна платит разницу: из пула вычиталась
оценка, а сеть брала больше. Разница с одного перевода мала, но она
накапливается, а допуск сверки растёт вместе с числом выплат — то есть
расхождение подольше не видно.

Зеркало казны знает ФАКТИЧЕСКУЮ комиссию каждой транзакции
(treasury_moves.fee_nanotons — это total_fees из цепочки, а не оценка).
Значит, рабочая оценка может сверяться с реальностью: берём максимум из
настроенной и средней фактической, а при заметном превышении поднимаем
тревогу — оператору пора обновить PAYOUT_FEE_GRAM.

Модуль — в app/core, потому что его зовут и денежный контур
(app.stakes), и диспетчер выплат (app.ton_pay.dispatch), а из app.core не
тянется ничего, что тянуло бы их обратно: иначе появился бы цикл импортов.
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.models import TreasuryMove
from app.ton_utils import to_nano

# Во сколько раз фактическая комиссия должна превысить настроенную оценку,
# чтобы об этом стоило тревожить. Ниже — нормальный разброс сети.
FEE_ALERT_RATIO = 1.3

# Сколько последних исходящих переводов смотрим: сеть могла подорожать
# недавно, и старые переводы не должны мешать увидеть новое.
REALIZED_FEE_SAMPLE = 20


def configured_fee_nano() -> int:
    """Настроенная оценка комиссии (PAYOUT_FEE_GRAM) в нанотонах."""
    return to_nano(settings.payout_fee_gram)


async def realized_fee_nano(session, limit: int = REALIZED_FEE_SAMPLE) -> int | None:
    """Средняя ФАКТИЧЕСКАЯ комиссия по последним исходящим переводам.

    None — данных нет (зеркало ещё не набрало истории, или это первый
    перевод). Тогда рабочая оценка остаётся настроенной.
    """
    network = settings.active_network
    result = await session.execute(
        select(TreasuryMove.fee_nanotons)
        .where(
            TreasuryMove.network == network,
            TreasuryMove.direction == "out",
            TreasuryMove.fee_nanotons > 0,
        )
        .order_by(TreasuryMove.id.desc())
        .limit(limit)
    )
    fees = [int(fee) for fee in result.scalars().all() if fee]
    if not fees:
        return None
    return sum(fees) // len(fees)


async def fee_estimate_nano(session) -> int:
    """Рабочая оценка комиссии за один исходящий перевод.

    Максимум из настроенной и средней фактической: пока сеть стоит столько
    же, сколько записано при настройке, поведение прежнее; если комиссия
    выросла, дыру в пуле закрывает фактическая, а не настроенная — иначе
    казна тихо платит разницу.
    """
    configured = configured_fee_nano()
    realized = await realized_fee_nano(session)
    if realized is None:
        return configured
    return max(configured, realized)
