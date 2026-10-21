"""Сверка «sent»-выплат с блокчейном: confirm_broadcast_payouts.

Метка bcast:<unix> значит лишь «лайтсервер принял запрос», а не «транзакция
в блоке». При гонке двух быстрых переводов (приз + рейк одного дня) один
может не попасть в цепочку: база остаётся с несуществующим переводом.
Джоба сверяет memo с историей казначея: найденное → реальный хеш вместо
bcast, отсутствующее дольше окна → обратно в очередь на повторную отправку.
"""

import os
from datetime import UTC, datetime, timedelta

from conftest import ensure_player, ensure_round

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


async def _seed_sent_payout(kind: str = "prize", round_id: int | None = 64) -> int:
    # Родители до ребёнка: строка payouts ссылается на раунд и игрока,
    # поэтому сначала гарантируем их существование в глобальной БД.
    if round_id is not None:
        await ensure_round(round_id)
    await ensure_player(42)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=round_id,
            player_id=42,
            kind=kind,
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="sent",
            tx_hash="bcast:1789470019",
            sent_at=datetime.now(UTC) - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()
        return payout_id


async def _cleanup(payout_id: int) -> None:
    async with SessionLocal() as session:
        await session.delete(await session.get(Payout, payout_id))
        await session.commit()


async def test_confirm_replaces_bcast_marker_with_real_hash(monkeypatch) -> None:
    """memo найдено в истории — bcast-метка заменяется реальным хешем."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {f"way:64:prize#{payout_id}": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "0" * 64
        assert row.last_error is None
    finally:
        await _cleanup(payout_id)


async def test_confirm_requeues_lost_transfer(monkeypatch) -> None:
    """memo нет в истории, окно верификации истекло — перевод не ушёл: retry."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"way:63:prize#1": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"
        assert row.attempts >= 1
        assert "не найдено в блокчейне" in (row.last_error or "")
    finally:
        await _cleanup(payout_id)


async def test_confirm_keeps_row_when_scan_truncated(monkeypatch) -> None:
    """Усечённый скан окна — «не знаю»: в очередь НЕ возвращаем.

    sent_at вне окна верификации, но внутри окна сверки; скан не покрыл окно
    целиком (упёрся в payout_reconcile_max_pages). «memo не найдено» здесь не
    доказывает, что перевода нет: возврат в очередь рискнул бы задвоить уже
    ушедший перевод.
    """
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        # Как настоящий fetch после усечённого скана.
        ton_pay.state._RECONCILE_WINDOW_COMPLETE = False
        return {"way:63:prize#1": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent", "«не знаю» не имеет права возвращать в очередь"
        assert row.tx_hash == "bcast:1789470019"
    finally:
        await _cleanup(payout_id)



async def test_confirm_leaves_fresh_broadcast_alone(monkeypatch) -> None:
    """Свежая вещация (окно верификации не истекло) не трогается: блокчейн
    мог просто не успеть проиндексировать перевод."""
    payout_id = await _seed_sent_payout()
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.sent_at = datetime.now(UTC) - timedelta(seconds=30)
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"another": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "bcast:1789470019"
    finally:
        await _cleanup(payout_id)


async def test_confirm_skips_when_history_empty(monkeypatch) -> None:
    """Оба провайдера молчат — «не знаем»: ни подтверждать, ни reqeue.

    Возврат в очередь при слепой сети мог бы задвоить ушедший перевод:
    memo могло успеть попасть в блок, а история ещё не отвечает."""
    payout_id = await _seed_sent_payout()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash == "bcast:1789470019"
    finally:
        await _cleanup(payout_id)


async def test_confirm_uses_comment_override(monkeypatch) -> None:
    """Выплаты со свободным комментарием сверяются по нему же.

    Свободный текст в цепочке несёт уникальный суффикс way:…#id: сверка
    обязана искать полную строку. Сырой текст кандидатом больше не является
    (досуффиксные переводы давно вне окна сверки) — иначе чужая транзакция
    с тем же текстом могла бы отметиться как «уже ушла»."""
    payout_id = await _seed_sent_payout()
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.comment_override = "техработы 12.09"
        await session.commit()
    expected = f"техработы 12.09 | way:64:prize#{payout_id}"
    assert ton_pay._payout_comment_candidates(row) == [expected]

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {expected: "1" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.tx_hash == "1" * 64
    finally:
        await _cleanup(payout_id)


async def test_confirm_batch_shares_transaction_hash(monkeypatch) -> None:
    """Пачка v5r1: несколько выплат в ОДНОЙ транзакции → один хеш на всех.

    Регресс: сверка писала реальный хеш каждой строке пачки, а индекс
    (tx_hash, network) на это не даёт — коммит падал IntegrityError, ВЕСЬ цикл
    сверки откатывался: ни одна выплата не получала настоящий хеш, потерянные
    переводы не возвращались в очередь. Теперь хеш транзакции достаётся ровно
    одной строке, остальные помечаются хешем с суффиксом «#<id>».
    """
    await ensure_round(65)
    await ensure_player(43)
    ids: list[int] = []
    async with SessionLocal() as session:
        for _ in range(2):
            payout = Payout(
                round_id=65,
                player_id=43,
                kind="prize",
                amount_nanotons=500_000_000,
                dest_address="0:" + os.urandom(32).hex(),
                network="mainnet",
                status="sent",
                tx_hash="bcast:1789470019",
                sent_at=datetime.now(UTC)
                - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
            )
            session.add(payout)
            await session.flush()
            ids.append(payout.id)
        await session.commit()

    real = "f" * 64

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        # Одна транзакция пачки: у обоих memo ОДИН хеш (как в reconcile.py).
        return {f"way:65:prize#{payout_id}": real for payout_id in ids}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 2
        async with SessionLocal() as session:
            rows = [await session.get(Payout, payout_id) for payout_id in ids]
        stored = [row.tx_hash for row in rows]
        # Ровно одна строка хранит «чистый» хеш транзакции, вторая — с суффиксом.
        assert stored.count(real) == 1, stored
        suffixed = [value for value in stored if value != real]
        assert len(suffixed) == 1, stored
        assert suffixed[0].startswith(real) and suffixed[0].endswith(f"#{ids[1]}"), suffixed
        # Индекс (tx_hash, network) цел: хеши разные.
        assert len(set(stored)) == 2
        # Обе строки подтверждены и выпадают из повторной сверки.
        assert all(row.status == "sent" for row in rows)
        assert all(row.tx_hash and not row.tx_hash.startswith("bcast:") for row in rows)
    finally:
        async with SessionLocal() as session:
            for payout_id in ids:
                row = await session.get(Payout, payout_id)
                if row is not None:
                    await session.delete(row)
            await session.commit()


async def test_confirm_keeps_existing_hash_owner(monkeypatch) -> None:
    """Хеш, занятый чужой строкой раньше, не переписывается: суффикс и у неё.

    Сверка не должна отбирать чужой хеш: индекс (tx_hash, network) — это
    барьер от двойной оплаты, и подмена хеша ломала бы его смысл.
    """
    await ensure_round(66)
    await ensure_player(44)
    owner_hash = "e" * 64
    async with SessionLocal() as session:
        owner = Payout(
            round_id=66,
            player_id=44,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            network="mainnet",
            status="sent",
            tx_hash=owner_hash,
            sent_at=datetime.now(UTC),
        )
        session.add(owner)
        await session.flush()
        other = Payout(
            round_id=66,
            player_id=44,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            network="mainnet",
            status="sent",
            tx_hash="bcast:1789470019",
            sent_at=datetime.now(UTC)
            - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
        )
        session.add(other)
        await session.flush()
        owner_id, other_id = owner.id, other.id
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {f"way:66:prize#{other_id}": owner_hash}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        await ton_pay.confirm_broadcast_payouts(bot=None)
        async with SessionLocal() as session:
            owner = await session.get(Payout, owner_id)
            other = await session.get(Payout, other_id)
        assert owner.tx_hash == owner_hash
        assert other.tx_hash == f"{owner_hash}#{other_id}"
    finally:
        async with SessionLocal() as session:
            for payout_id in (owner_id, other_id):
                row = await session.get(Payout, payout_id)
                if row is not None:
                    await session.delete(row)
            await session.commit()


async def test_confirm_ignores_other_networks(monkeypatch) -> None:
    """Сверка работает только по выплатам активной сети."""
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    await ensure_round(64)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=64,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            network="testnet",
            status="sent",
            tx_hash="bcast:1789470019",
            sent_at=datetime.now(UTC) - timedelta(seconds=settings.payout_confirm_timeout_seconds + 60),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {f"way:64:prize#{payout_id}": "2" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
    finally:
        await _cleanup(payout_id)


async def test_confirm_skips_rows_with_real_hash(monkeypatch) -> None:
    """Уже подтверждённые реальным хешем выплаты сверка не трогает."""
    await ensure_round(64)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=64,
            kind="refund",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="sent",
            tx_hash="3" * 64,
            sent_at=datetime.now(UTC) - timedelta(hours=5),
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.tx_hash == "3" * 64
    finally:
        await _cleanup(payout_id)


async def test_confirm_marks_window_elapsed_as_failed(monkeypatch) -> None:
    """sent_at старше окна сверки и memo не в истории — НЕ pending, а failed.

    Сценарий: бот лежал дольше окна сверки (по умолчанию 7 суток). При
    подъёме memo старой строки нет в tx_map (за окном), и предыдущая логика
    возвращала её в pending — диспетчер вещал тот же memo повторно, а в
    цепочке деньги уже были. Теперь строка идёт в failed с понятной
    причиной: ручной разбор через /payouts → retry только после сверки
    админом, что перевод НЕ ушёл.
    """
    payout_id = await _seed_sent_payout()
    # sent_at — за окном сверки: window_cutoff = now - reconcile_history_seconds.
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.sent_at = datetime.now(UTC) - timedelta(
            seconds=settings.payout_reconcile_history_seconds + 3600
        )
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        # Возвращаем НЕпустую карту: пустой словарь рано выйдет через
        # `if not tx_map: return 0` ещё до тела цикла. Возвращаем memo
        # чужой выплаты — наш таргет не совпадёт, и пойдём в ветку
        # «за окном сверки».
        return {"way:64:prize#999": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        # Не pending — иначе диспетчер отправит тот же memo повторно.
        assert row.status == "failed", (
            "за окном сверки строка должна идти в failed, не в pending"
        )
        assert "за окном сверки" in (row.last_error or "")
        # Лимит попыток сожжён: failed с attempts < max гигиена _reset_retriable
        # оживила бы в следующем цикле, и тот же memo ушёл бы в цепочку повторно.
        assert row.attempts == settings.payout_max_attempts
    finally:
        await _cleanup(payout_id)


async def test_window_elapsed_waits_for_manual_review(monkeypatch) -> None:
    """Регресс: гигиена очереди не оживляет failed «за окном сверки».

    _reset_retriable возвращает failed в pending, пока attempts < max. Ветка
    «за окном сверки» раньше не трогала attempts — и строка оживала сама,
    диспетчер вещал тот же memo повторно: двойной платёж за пределами окна
    сверки, где анти-дубль по истории казначея уже не видит перевод.
    Обратно в очередь строку поднимает только ручной разбор хранителя.
    """
    payout_id = await _seed_sent_payout()
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.sent_at = datetime.now(UTC) - timedelta(
            seconds=settings.payout_reconcile_history_seconds + 3600
        )
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        return {"way:64:prize#999": "0" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    network = "testnet" if settings.is_testnet else "mainnet"
    try:
        await ton_pay.confirm_broadcast_payouts(bot=None)

        # Цикл гигиены очереди: failed оживает только при attempts < max.
        async with SessionLocal() as session:
            await ton_pay._reset_retriable(session, network)
            await session.commit()
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed", (
            "автооживление строки за окном сверки — прямой путь к двойному платежу"
        )

        # Ручной разбор хранителя — единственный вход обратно в очередь.
        async with SessionLocal() as session:
            new_status = await ton_pay.resolve_dead_payout(session, payout_id, "retry")
        assert new_status == "pending"
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"
        # Счётчик не сброшен: повтор пройдёт анти-дубль по истории казначея.
        assert row.attempts == settings.payout_max_attempts
    finally:
        await _cleanup(payout_id)


async def test_confirm_still_requeues_within_window(monkeypatch) -> None:
    """Регресс: сразу после payout_confirm_timeout (но В окне сверки) — re-queue.

    Страховка от перетягивания ветки «за окном сверки»: случаи в окне
    (sent_at моложе reconcile_history_seconds) должны идти в pending, как
    раньше. Иначе мы заморозили бы все выплаты сразу после длительной
    паузы, а это поломало бы автоматический ретрай.
    """
    payout_id = await _seed_sent_payout()
    # sent_at — между confirm_timeout и reconcile_history.
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.sent_at = datetime.now(UTC) - timedelta(
            seconds=settings.payout_confirm_timeout_seconds + 600
        )
        await session.commit()

    async def fake_tx_map(**kwargs) -> dict[str, str]:
        # Непустой, чтобы не словить `if not tx_map: return 0` выше тела
        # цикла — там cancel проходит до ветки с проверкой окна.
        return {"way:64:prize#other": "1" * 64}

    monkeypatch.setattr(ton_pay, "fetch_broadcast_tx_map", fake_tx_map)

    try:
        changed = await ton_pay.confirm_broadcast_payouts(bot=None)
        assert changed == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        # В окне сверки: re-queue как раньше.
        assert row.status == "pending"
        assert row.attempts >= 1
        assert "не найдено в блокчейне" in (row.last_error or "")
    finally:
        await _cleanup(payout_id)