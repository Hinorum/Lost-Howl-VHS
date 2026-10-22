"""Оценка комиссии сети: настроенная, фактическая и рабочая.

PAYOUT_FEE_GRAM — константа, записанная при настройке. Сеть вправе
подорожать (перегрузка), и тогда казна платит разницу: из пула вычиталась
оценка, а сеть брала больше. Разница с одного перевода мала, но она
накапливается, а допуск сверки растёт вместе с числом выплат — то есть
расхождение подольше не видно.

Зеркало казны знает ФАКТИЧЕСКУЮ стоимость каждой транзакции
(treasury_moves.balance_delta_nanotons — изменение баланса казны).
Значит, рабочая оценка может сверяться с реальностью: берём максимум из
настроенной и средней фактической, а при заметном превышении поднимаем
тревогу — оператору пора обновить PAYOUT_FEE_GRAM.

ПОЧЕМУ НЕ fee_nanotons. Сеть берёт с отправителя ДВЕ части: комиссию
получателя (это total_fees в цепочке, его и хранит fee_nanotons) и
forward fee самого сообщения (его отправитель платит сверху, а в
total_fees оно не входит — в этом и смысл зеркала: дельта баланса
считается как in − out − (fee + fwd_fee)). Замерено по живым переводам
казны 2026-10-21: forward fee — около 9% от total_fees, то есть по
fee_nanotons стоимость перевода занижена примерно на 11%.

Полная стоимость выводится из зеркала напрямую: за исходящий перевод баланс
казны уменьшился на (сумма перевода + стоимость сети) → стоимость =
−balance_delta − value. Сходится с цепочкой до нанотона.

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


def network_cost_nano(
    balance_delta_nanotons: int | None, value_nanotons: int | None
) -> int | None:
    """Полная стоимость сети за исходящий перевод — из дельты баланса.

    За исходящий перевод баланс казны уменьшился на сумму перевода плюс
    стоимость сети: value + (total_fees + fwd_fee). Значит, стоимость =
    −balance_delta − value.

    None — из строки не выводится (нет дельты или нет значения): такую строку
    в выборку не берём, лучше меньше данных, чем заниженная стоимость.
    """
    if balance_delta_nanotons is None or value_nanotons is None:
        return None
    cost = -int(balance_delta_nanotons) - int(value_nanotons)
    return cost if cost > 0 else None


async def realized_fee_nano(session, limit: int = REALIZED_FEE_SAMPLE) -> int | None:
    """Средняя ФАКТИЧЕСКАЯ стоимость исходящего перевода (для отправителя).

    None — данных нет (зеркало ещё не набрало истории, или это первый
    перевод). Тогда рабочая оценка остаётся настроенной.

    Считаем по ТРАНЗАКЦИЯМ, а не по сообщениям: пачка (несколько выплат в
    одном внешнем сообщении) стоит дешевле на перевод — при включённой
    пачке оценка получается завышенной, а это безопасная сторона (казна не
    платит разницу из своего остатка).
    """
    network = settings.active_network
    result = await session.execute(
        select(TreasuryMove.balance_delta_nanotons, TreasuryMove.value_nanotons)
        .where(
            TreasuryMove.network == network,
            TreasuryMove.direction == "out",
        )
        .order_by(TreasuryMove.id.desc())
        .limit(limit)
    )
    costs = [
        cost
        for cost in (
            network_cost_nano(delta, value) for delta, value in result.all()
        )
        if cost is not None
    ]
    if not costs:
        return None
    return sum(costs) // len(costs)


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
