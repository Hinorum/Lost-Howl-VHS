"""Анти-дубль выплат: сверка memo с историей казначея перед повторной отправкой.

Краш между вещанием перевода и коммитом «sent» оставляет строку в
sending → ретрай. Без сверки это второй реальный перевод. Сверка по memo
(уникальному way:<день>:<тип>#<id>) находит уже ушедший платёж и помечает
выплату sent без новой отправки.
"""

import asyncio as _asyncio
import base64
import os
import time as _t
from unittest.mock import AsyncMock

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


def _comment(payout_id: int) -> str:
    return f"way:7:prize#{payout_id}"


async def _seed_payout(attempts: int) -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=42,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
        )
        session.add(payout)
        await session.flush()
        payout.attempts = attempts
        payout.status = "pending"
        await session.commit()
        return payout.id


async def _seed_payouts(count: int, attempts: int = 0) -> list[int]:
    """Несколько выплат подряд — чтобы проверить поведение ПАЧКИ."""
    ids: list[int] = []
    for index in range(count):
        async with SessionLocal() as session:
            payout = Payout(
                round_id=7,
                player_id=100 + index,
                kind="prize",
                amount_nanotons=500_000_000,
                dest_address="0:" + os.urandom(32).hex(),
            )
            session.add(payout)
            await session.flush()
            payout.attempts = attempts
            payout.status = "pending"
            await session.commit()
            ids.append(payout.id)
    return ids


async def _drop_payouts(ids: list[int]) -> None:
    async with SessionLocal() as session:
        for payout_id in ids:
            row = await session.get(Payout, payout_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


# ---------- Окно подтверждения seqno в пачке выплат ----------


async def test_batch_waits_for_previous_confirm_on_liteserver_path(monkeypatch) -> None:
    """Второй перевод пачки ждёт блока по первому и на ЛАЙТСЕРВЕРНОМ пути.

    Окно подтверждения нужно любому каналу: батч-seqno переиспользуется и на
    лайтсерверах, а узел держит подписанный seqno+1 в мемпуле. Без ожидания
    второй внешний месседж (seqno+2) сражается с первым за место и молча
    отбрасывается — потерю ловит только 2-часовая сверка. Раньше ожидание
    стояло под условием «работает HTTP-канал», то есть на живом лайтсерверном
    пути пропускалось вовсе.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 5)
    ids = await _seed_payouts(2)
    waits: list[tuple] = []

    async def empty_markers() -> set[str]:
        return set()

    async def fake_wait(memo, seconds):
        waits.append((set(memo), seconds))
        return True  # первый перевод подтвердился — ждать дальше нечего

    transfer = AsyncMock(return_value="bcast:1")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    # Именно в dispatch: модуль импортирует символ себе при загрузке, и подмена
    # одноимённого атрибута в ton_pay его бы не увидела.
    monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", fake_wait)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 2
        # Ровно одно ожидание — перед вторым переводом, по его предшественнику.
        assert len(waits) == 1
        waited_memo, waited_seconds = waits[0]
        assert waited_seconds == 5
        assert waited_memo == {_comment(ids[0])}
    finally:
        await _drop_payouts(ids)


async def test_batch_defers_remaining_payouts_when_prev_unconfirmed(monkeypatch) -> None:
    """Предыдущий перевод не подтвердился за окно — пачка встаёт на паузу.

    Раньше в этой ветке только сбрасывался счётчик seqno, и следующий перевод
    шёл с seqno, прочитанным из сети. Но в сети там всё ещё НЕПОДТВЕРЖДЁННЫЙ
    номер предыдущего перевода: второй подписался бы тем же seqno и молча
    потерялся бы. Читать живой seqno можно только после блока, поэтому
    остаток пачки возвращается в очередь, а не вещается вслепую.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 1)
    ids = await _seed_payouts(3)

    async def empty_markers() -> set[str]:
        return set()

    async def never_confirmed(memo, seconds):
        return False  # предыдущий перевод в блок не встал

    transfer = AsyncMock(return_value="bcast:1")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", never_confirmed)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        # Вещан только первый: второй и третий не подписывались вслепую.
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            rows = [await session.get(Payout, payout_id) for payout_id in ids]
        assert rows[0].status == "sent"
        assert [row.status for row in rows[1:]] == ["pending", "pending"]
        # Причина видна оператору — не «списали молча».
        assert "не подтверждён" in rows[1].last_error
        assert "не подтверждён" in rows[2].last_error
        # Попытка клейма возвращена: эти строки до отправки не дошли, и их
        # attempts не должны расти на чужих сбоях. Иначе после
        # payout_max_attempts таких пауз строка уйдёт в dead-letter, не будучи
        # отправлена ни разу, и игрок просто не получит приз.
        assert [row.attempts for row in rows[1:]] == [0, 0], (
            "отложенные строки не должны терять попытки клейма"
        )
    finally:
        await _drop_payouts(ids)


async def test_frozen_repeat_does_not_burn_attempts(monkeypatch) -> None:
    """Заморозка повтора при недоступной истории не тратит попытку.

    Когда оба провайдера истории молчат, повтор (attempts > 1) нельзя
    переотправлять: не известно, ушёл ли такой-то перевод уже. Строка
    возвращается в очередь с видимой причиной и ждёт восстановления истории.

    Отправки при этом не было ни разу, поэтому попытка клейма обязана
    возвращаться. Иначе недоступность истории — внешнее и временное обстоятельство —
    расходовала бы retries игрока: через payout_max_attempts циклов выплата
    умерла бы как failed, не будучи отправленной ни разу.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_max_attempts", 5)
    ids = await _seed_payouts(1, attempts=1)

    async def markers_history_down() -> set[str]:
        # Так ведёт себя настоящий fetch_broadcast_markers, когда оба
        # провайдера не ответили: пусто + флаг истории недоступны.
        ton_pay.state._RECONCILE_HISTORY_OK = False
        return set()

    transfer = AsyncMock(return_value="bcast:1")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers_history_down)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    try:
        for _ in range(settings.payout_max_attempts + 2):
            await ton_pay.dispatch_pending_payouts(bot=None)

        assert transfer.await_count == 0, "замороженный повтор не должен вещаться"
        async with SessionLocal() as session:
            row = await session.get(Payout, ids[0])
        assert row.status == "pending", (
            f"выплата умерла как {row.status} за {settings.payout_max_attempts + 2} "
            "циклов, не будучи отправленной ни разу"
        )
        assert row.attempts == 1, "попытка клейма не возвращена при заморозке"

        # История ожила — повтор уходит сам, без ручного retry.
        async def markers_ok() -> set[str]:
            return set()

        monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers_ok)
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, ids[0])
        assert row.status == "sent"
    finally:
        await _drop_payouts(ids)


async def test_batch_resumes_normally_after_pause(monkeypatch) -> None:
    """Пауза не «съедает» очередь: следующий цикл разосылает остаток целиком."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_batch_confirm_seconds", 1)
    ids = await _seed_payouts(2)

    async def empty_markers() -> set[str]:
        return set()

    async def never_confirmed(memo, seconds):
        return False

    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", never_confirmed)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", AsyncMock(return_value="bcast:1"))
    try:
        await ton_pay.dispatch_pending_payouts(bot=None)

        # Второй цикл: сеть отвечает, подтверждение пришло — пачка идёт дальше.
        transfer = AsyncMock(return_value="bcast:2")
        monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

        async def confirmed(memo, seconds):
            return True

        monkeypatch.setattr("app.ton_pay.dispatch._wait_for_broadcast_memo", confirmed)
        await ton_pay.dispatch_pending_payouts(bot=None)

        async with SessionLocal() as session:
            rows = [await session.get(Payout, payout_id) for payout_id in ids]
        assert [row.status for row in rows] == ["sent", "sent"]
        assert rows[1].tx_hash == "bcast:2"
    finally:
        await _drop_payouts(ids)


def test_out_comments_extractors_cover_both_providers() -> None:
    tonapi_item = {
        "out_msgs": [
            {"raw_message": "way:1:rake#3"},
            {"msg_data": {"decoded_comment": "way:7:prize#9"}},
            {"msg_data": {"text": base64.b64encode("привет".encode()).decode()}},
            {"msg_data": {}},
            "мусор",
        ]
    }
    comments = ton_pay._out_comments_tonapi(tonapi_item)
    assert comments == ["way:1:rake#3", "way:7:prize#9", "привет"]

    toncenter_item = {
        "out_msgs": [
            {"message_content": {"decoded": {"@type": "comment", "comment": "way:8:refund#11"}}},
            {"message_content": {}},
        ]
    }
    assert ton_pay._out_comments_toncenter(toncenter_item) == ["way:8:refund#11"]

    # Toncenter v3 в проде отдаёт @type=text_comment — покрываем оба формата.
    toncenter_item_v3 = {
        "out_msgs": [
            {"message_content": {"decoded": {"@type": "text_comment", "comment": "way:64:prize#80"}}},
        ]
    }
    assert ton_pay._out_comments_toncenter(toncenter_item_v3) == ["way:64:prize#80"]


async def test_dispatch_marks_sent_when_memo_already_broadcast(monkeypatch) -> None:
    """Перевод уже ушёл в цепочку в прошлый раз — ретрай НЕ задваивает платёж."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=1)

    async def fake_markers() -> set[str]:
        return {_comment(payout_id)}

    transfer = AsyncMock(return_value=None)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 0  # вещания не было
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.sent_at is not None
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_dispatch_still_sends_when_marker_absent(monkeypatch) -> None:
    """Сверка не нашла memo (сеть молчала в прошлый раз) — обычная отправка."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=1)

    async def empty_markers() -> set[str]:
        return set()

    transfer = AsyncMock(return_value="bcast:123")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.tx_hash == "bcast:123"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_first_attempt_still_checks_history(monkeypatch) -> None:
    """Даже при attempts==0 сверка с историей обязательна: краш после вещания
    на первом проходе (статус не сохранился) не должен повторить перевод."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=0)

    history_checked = {"calls": 0}

    async def fake_markers() -> set[str]:
        history_checked["calls"] += 1
        return set()

    transfer = AsyncMock(return_value="bcast:77")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert history_checked["calls"] == 1  # один проход истории на цикл
        assert sent == 1
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_admin_retry_keeps_history_check_on_redispatch(monkeypatch) -> None:
    """Ручной retry НЕ сбрасывает attempts: попытка могла уже уйти в цепочку
    (краш между вещанием и коммитом «sent»), и повтор без сверки с memo
    задвоил бы реальный перевод. Регрессия на resolve_dead_payout(action=retry)."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_max_attempts", 5)
    payout_id = await _seed_payout(attempts=5)
    async with SessionLocal() as session:
        row = await session.get(Payout, payout_id)
        row.status = "failed"
        row.alerted = True
        await session.commit()
        assert await ton_pay.resolve_dead_payout(session, payout_id, "retry") == "pending"
        row = await session.get(Payout, payout_id)
        assert row.attempts == 5  # счётчик сохранён, анти-дубль остаётся активным

    async def fake_markers() -> set[str]:
        return {_comment(payout_id)}

    transfer = AsyncMock(return_value=None)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 0  # вещания не было — денег дважды нет
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_failed_transfer_records_reason(monkeypatch) -> None:
    """Причина неудачи пишется в last_error и переживает исчерпание ретраев."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    # Guard пропускает дальше только при непустой мнемонике активной сети.
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    payout_id = await _seed_payout(attempts=settings.payout_max_attempts - 1)

    # Сломанная пара мнемоника/адрес — типичная тестнет-причина.
    async def broken_wallet():
        raise ValueError("Адрес казначея не совпадает с производным от мнемоники")

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", broken_wallet)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed"
        assert row.last_error is not None
        assert "не совпадает" in row.last_error

        # Ручной retry снова поднимет строку с той же видимой причиной.
        row.status = "pending"
        row.attempts = 0
        row.alerted = False
        await session.commit()
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_no_wallet_prize_self_heals_when_player_binds(monkeypatch) -> None:
    """Приз без кошелька не тонет в failed: ждёт привязки и уходит сам.

    Регрессия: раньше пустой dest-адрес помечал выплату failed, и игрок,
    привязавший кошелёк на следующий день, терял приз до ручного retry.
    Теперь строка остаётся в очереди, а цикл после /wallet вставляет адрес
    и платит в тот же момент."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    player_id = 930_001
    async with SessionLocal() as session:
        from app.models import Player

        session.add(Player(id=player_id, username="late", wallet_address=""))
        payout = Payout(round_id=8, player_id=player_id, kind="prize",
                        amount_nanotons=400_000_000, dest_address="", status="pending")
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    transfer = AsyncMock(return_value="bcast:91")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", AsyncMock(return_value=set()))
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        # Без кошелька: не отправляется, но и не failed — ждёт привязки.
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 0
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"
        assert "кошелёк игрока ещё не привязан" in (row.last_error or "")

        # Игрок привязал /wallet и подтвердил bv — следующий цикл сам платит приз.
        async with SessionLocal() as session:
            player = await session.get(Player, player_id)
            player.wallet_address = "0:" + os.urandom(32).hex()
            player.wallet_verified = True
            await session.commit()
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.dest_address != ""
    finally:
        async with SessionLocal() as session:
            from app.models import Player as _P

            await session.delete(await session.get(Payout, payout_id))
            player = await session.get(_P, player_id)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_empty_treasury_dest_revives_from_owner_env(monkeypatch) -> None:
    """Рейк без адреса (OWNER_WALLET_ADDRESS задан позже) уходит сам, без retry."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    owner = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "owner_wallet_address", owner)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=None,
            kind="rake",
            amount_nanotons=50_000_000,
            dest_address="",  # создано до того, как адрес появился в окружении
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    transfer = AsyncMock(return_value="bcast:88")
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        assert transfer.await_args.args[0] == owner
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.last_error is None
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_empty_dest_without_owner_fails_with_clear_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=None,
            kind="leaderboard",
            amount_nanotons=30_000_000,
            dest_address="",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed"
        assert "OWNER_WALLET_ADDRESS" in row.last_error
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_payouts_listing_shows_reason(monkeypatch) -> None:
    """/payouts показывает причину у каждой строки — разбор без логов."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import cmd_payouts

    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=9,
            kind="prize",
            amount_nanotons=2_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="pending",
            attempts=2,
            last_error="Лайтсерверы не приняли перевод (результат 0)",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=4242),
        text="/payouts",
        answer=AsyncMock(),
    )
    try:
        await cmd_payouts(message)
        text = message.answer.call_args.args[0]
        assert f"#{payout_id}" in text
        assert "Лайтсерверы не приняли" in text
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_peer_failure_engages_http_channel(monkeypatch) -> None:
    """«have no alive peers» больше не топит строку: диспетчер уходит в HTTP-канал.

    Раньше мёртвые лайтсерверы означали вердикт «задай LITESERVER_CONFIG_URL»
    и выплата копила попытки. Теперь сбой лайтсерверного канала вызывает
    оффлайн-подпись и HTTPS-вещание (_send_ton_transfer_http): если канал жив,
    строка уходит в sent тем же циклом."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    payout_id = await _seed_payout(attempts=0)

    async def no_peers():
        raise RuntimeError("LiteServerError: have no alive peers")

    async def http_success(dest, amount, comment):
        return f"bcast:{int(_time_now() + 1)}"

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", no_peers)
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_success)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.tx_hash and row.tx_hash.startswith("bcast:")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_http_channel_failure_records_reason(monkeypatch) -> None:
    """HTTP-канал тоже может упасть: ошибка провайдера обязана стать last_error."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    payout_id = await _seed_payout(attempts=0)

    async def no_peers():
        raise RuntimeError("LiteServerError: have no alive peers")

    async def http_broken(dest, amount, comment):
        raise RuntimeError("Toncenter не принял сообщение (500): внутренняя ошибка")

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", no_peers)
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_broken)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"  # attempts=1 < max → вернётся в очередь
        assert "Toncenter" in (row.last_error or "")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_send_timeout_is_retry_not_freeze(monkeypatch) -> None:
    """Зависший лайтсервер не морозит цикл: таймаут = ретрай с причиной."""

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    monkeypatch.setattr(settings, "payout_send_timeout_seconds", 1)
    payout_id = await _seed_payout(attempts=0)

    async def hang_forever(dest, amount, comment):
        await _asyncio.sleep(30)
        return None

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", AsyncMock(return_value=object()))
    monkeypatch.setattr(ton_pay, "send_ton_transfer", hang_forever)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        started = _time_now()
        await _asyncio.wait_for(
            ton_pay.dispatch_pending_payouts(bot=None), timeout=10
        )
        elapsed = _time_now() - started
        assert elapsed < 5, "цикл не должен ждать полный hang"
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"  # attempts=1 < max → вернётся в очередь
        assert "таймаут вещания" in (row.last_error or "")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


def _time_now() -> float:

    return _t.monotonic()


def test_watcher_state_value_is_unlimited_text() -> None:
    """Регрессия: план Хозяина Ошибки рвал тик о VARCHAR(255)."""
    from sqlalchemy import Text as SAText

    from app.models import WatcherState

    column_type = WatcherState.__table__.c.value.type
    assert isinstance(column_type, SAText)
    assert column_type.length is None


async def test_wallet_uses_remote_liteserver_config(monkeypatch) -> None:
    """LITESERVER_CONFIG_URL доходит до конструктора провайдера."""
    import pytest
    from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

    monkeypatch.setattr(ton_pay, "_provider", None)
    monkeypatch.setattr(ton_pay, "_wallet", None)
    monkeypatch.setattr(ton_pay, "_wallet_network", None)
    words = mnemonic_new(24)
    _, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)
    address = ton_pay._wallet_address("v4r2", public_key, -239)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_address", address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "auto")

    fetched: dict = {}

    async def fake_fetch(url: str) -> dict:
        fetched["url"] = url
        return {"liteservers": []}

    class Boom(Exception):
        pass

    def fake_from_config(config):
        fake_from_config.config = config
        raise Boom("доходим до построения провайдера")

    monkeypatch.setattr(ton_pay.wallet, "fetch_remote_json", fake_fetch)
    monkeypatch.setattr("pytoniq.LiteBalancer.from_config", staticmethod(fake_from_config))
    monkeypatch.setattr(settings, "liteserver_config_url", "https://example.test/config.json")

    with pytest.raises(Boom):
        await ton_pay._get_wallet()
    assert fetched["url"] == "https://example.test/config.json"
    assert fake_from_config.config == {"liteservers": []}


async def test_dead_letter_alert_carries_reason(monkeypatch) -> None:
    """Алерт админу называет причину, а не только id."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    sent: list[tuple[int, str]] = []

    class Bot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    async with SessionLocal() as session:
        payout = Payout(
            round_id=8,
            kind="prize",
            amount_nanotons=1_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="failed",
            alerted=False,
            attempts=5,
            last_error="Лайтсерверы не приняли перевод",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    try:
        await ton_pay._alert_admin(Bot(), settings.ton_network)
        texts = [text for _chat, text in sent]
        assert any(str(payout_id) in t and "Лайтсерверы не приняли" in t for t in texts)
        # Дедуп: повторный вызов молчит.
        await ton_pay._alert_admin(Bot(), settings.ton_network)
        assert len(sent) == len(settings.admin_id_set)
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


# ---------- Свободный комментарий возврата не ломает анти-дубль ----------


def test_payout_comment_candidates_keep_unique_suffix() -> None:
    """Переопределённый текст дополняется уникальным суффиксом way:…:#id.

    Два возврата с одним текстом (пауза) неразличимы для сверки, если слать
    текст как есть: потерявшийся возврат может быть помечен sent «по чужой»
    транзакции, и игрок не получит деньги. Суффикс возвращает уникальность."""
    override = "Игра приостановлена: идут технические работы"
    p1 = Payout(round_id=7, kind="refund", id=11, comment_override=override)
    p2 = Payout(round_id=7, kind="refund", id=12, comment_override=override)
    c1 = ton_pay._payout_comment_candidates(p1)
    c2 = ton_pay._payout_comment_candidates(p2)
    assert c1[0] != c2[0]
    assert c1[0].endswith("| way:7:refund#11")
    assert c2[0].endswith("| way:7:refund#12")
    assert override in c1[0]
    # Легаси-кандидат — сырой текст: старые переводы (до суффикса) всё ещё
    # находятся в истории, иначе сверка вернёт в очередь уже разосланное.
    assert c1[1] == override
    # Без переопределения — привычное служебное memo.
    assert ton_pay._payout_comment_candidates(Payout(round_id=7, kind="prize", id=5)) == ["way:7:prize#5"]
    # Длинный текст не съедает суффикс (лимит ячейки 120): режется сам текст.
    long_row = Payout(round_id=7, kind="refund", id=13, comment_override="д" * 120)
    c3 = ton_pay._payout_comment_candidates(long_row)[0]
    assert len(c3) <= 120 and c3.endswith("| way:7:refund#13")


async def _seed_override_payout() -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=None,
            kind="refund",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            comment_override="Игра приостановлена: идут технические работы",
        )
        session.add(payout)
        await session.flush()
        payout.attempts = 1
        payout.status = "pending"
        await session.commit()
        return payout.id


async def test_same_override_comments_stay_distinct(monkeypatch) -> None:
    """Возврат, потерянный в таймауте, не списывается на чужую транзакцию
    с тем же текстом: сверка видит свой уникальный суффикс и отправляет заново."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    x_id = await _seed_override_payout()
    y_id = await _seed_override_payout()
    async with SessionLocal() as session:
        x_row = await session.get(Payout, x_id)
        y_row = await session.get(Payout, y_id)
    x_comment = ton_pay._payout_comment_candidates(x_row)[0]
    y_comment = ton_pay._payout_comment_candidates(y_row)[0]
    assert x_comment != y_comment

    async def fake_markers() -> set[str]:
        return {y_comment}

    captured: dict[str, object] = {}

    async def fake_transfer(dest, amount, comment):
        captured["dest"], captured["amount"], captured["comment"] = dest, amount, comment
        return "bcast:123"

    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            x = await session.get(Payout, x_id)
            y = await session.get(Payout, y_id)
        # X: суффикса нет в истории — не верим, что ушло, шлём заново.
        assert x.status == "sent" and x.tx_hash == "bcast:123"
        assert captured["comment"] == x_comment
        # Y: свой суффикс в истории — отметка sent без повторной отправки.
        assert y.status == "sent" and y.tx_hash is None
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, x_id))
            await session.delete(await session.get(Payout, y_id))
            await session.commit()
