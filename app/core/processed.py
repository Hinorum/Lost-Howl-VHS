"""Журнал обработанных входящих переводов: один хеш — одна обработка.

Хелперы живут в app/core, а не в app.ton_watch, потому что их зовут и
денежный контур (app.stakes), и watcher (app.ton_watch) — а модуль не тянет
ничего из app, иначе появился бы цикл импортов.

Зачем эта таблица: «перевод уже разобран» проверялось тремя разными
способами (Stake.tx_hash, Income.unit_ref, метка watcher_state refund:<tx>),
и у каждого была своя дыра:
- tx_hash пылевой ставки перезаписывался, когда её вытесняла валидная
  ставка, — след перевода исчезал, и повторный проход watcher'а создавал
  для него второй авто-возврат (казна платила дважды);
- строка Income жила в другой транзакции, чем сама ставка: падение между
  ними оставляло перевод «разобранным, но невидимым» для сверки казны —
  расхождение висело молча, потому что тревога срабатывает только на
  нехватке, а тут излишек;
- возврат, созданный при вытеснении пылевой ставки, не имел ни метки, ни
  tx_hash — и ни одна из трёх проверок его не ловила.

Здесь одна строка на перевод, и создаётся она в ТОЙ ЖЕ транзакции, что и
денежный эффект: повторный проход (overlap-окно, сброс курсора, ре-скан
после деплоя) не размножает эффект, а падение процесса откатывает и метку,
и эффект — перевод будет разобран заново.
"""

from __future__ import annotations

from sqlalchemy import text

# Виды обработки. Значение кладётся в колонку kind и читается обратно, чтобы
# отличать «перевод уже возвращён» от «перевод уже учтён как ставка» или как
# плата за смену пути.
KIND_STAKE = "stake"
KIND_REFUND = "refund"
KIND_REVOTE = "revote"

# Длина колонки processed_transfers.tx_hash. Хеши TON — 64 hex, но источник
# может отдать мусорный или склеенный идентификатор; Payout.tx_hash уже
# обрезается, и журнал должен вести себя так же, а не падать на Postgres.
_TX_HASH_MAX = 80


def _tx_key(tx_hash: str) -> str:
    return (tx_hash or "")[:_TX_HASH_MAX]


async def claim_transfer(
    session,
    tx_hash: str,
    network: str,
    kind: str,
    amount_nanotons: int = 0,
) -> bool:
    """Помечает перевод обработанным. True — мы первые, False — уже был.

    Метка не коммитится сама: она остаётся в транзакции вызывающего кода, и
    коммит вместе с денежным эффектом. Это и есть главное свойство журнала:
    метка без эффекта (или эффект без метки) невозможны — только всё или
    ничего.
    """
    result = await session.execute(
        text(
            "INSERT INTO processed_transfers (tx_hash, network, kind, amount_nanotons) "
            "VALUES (:tx, :network, :kind, :amount) "
            "ON CONFLICT (tx_hash, network) DO NOTHING"
        ),
        {
            "tx": _tx_key(tx_hash),
            "network": network,
            "kind": kind,
            "amount": amount_nanotons,
        },
    )
    return result.rowcount == 1


async def transfer_claim_kind(session, tx_hash: str, network: str) -> str | None:
    """Чем помечен перевод (kind) или None — перевод не обработан."""
    result = await session.execute(
        text(
            "SELECT kind FROM processed_transfers "
            "WHERE tx_hash = :tx AND network = :network"
        ),
        {"tx": _tx_key(tx_hash), "network": network},
    )
    return result.scalar_one_or_none()
