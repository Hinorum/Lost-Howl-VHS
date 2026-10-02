"""Честная жеребьёвка на цепи: энтропия мастерхчейн-блока TON при ничьей.

Основные свойства:
- энтропия снимается ровно один раз (в close_voting) и фиксируется в дне;
- heal/пересчёт (finish_tally) используют сохранённую энтропию — исход не
  зависит от состояния сети в момент повторного подсчёта;
- при недоступном TON день откатывается на прежний детерминированный
  жребий без энтропии — ничья не зависает на сетевой ошибке.
"""

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models import Card, Player, Round, RoundStatus, Vote, WinRule
from app.rounds import close_voting, finish_tally, pick_winner, tie_seed
from app.rounds.voting import tied_positions


def _round(day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title=f"День {day_index}",
        chapter_text="Текст.",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(minutes=5),
        tally_ends_at=now + timedelta(minutes=5),
    )
    for position in range(3):
        round_row.cards.append(
            Card(
                position=position,
                title=f"Тропа {position}",
                description="д",
                consequence="Канон дня.",
                image_path="",
            )
        )
    return round_row


async def _seed_tied_day(session, day_index: int) -> Round:
    """День с ничьёй: пути 0 и 1 по одному голосу (MAJORITY)."""
    round_row = _round(day_index)
    session.add(round_row)
    await session.commit()
    for path, pid in ((0, 900_010), (1, 900_011)):
        session.add(Player(id=pid, username=f"tie-p{pid}"))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    await session.commit()
    return round_row


async def _win_without_entropy(round_row) -> int:
    """Победитель по старому правилу (сид без энтропии)."""
    counts = {0: 1, 1: 1, 2: 0}
    return pick_winner(counts, round_row.win_rule, f"{round_row.day_index}:{round_row.win_rule.value}")


def test_pick_winner_depends_on_entropy() -> None:
    """Разные блоки мастерчейна — разные исходы, один блок — один исход."""
    counts = {0: 1, 1: 1, 2: 0}
    rule = WinRule.MAJORITY
    assert len(tied_positions(counts, rule)) == 2
    a = pick_winner(counts, rule, f"7:{rule.value}:100:aaa")
    b = pick_winner(counts, rule, f"7:{rule.value}:100:aaa")
    c = pick_winner(counts, rule, f"7:{rule.value}:101:bbb")
    assert a == b
    assert c in (0, 1)
    # Энтропия реально меняет исход хотя бы на каком-то блоке (жеребьёвка честная).
    outcomes = {
        pick_winner(counts, rule, f"7:{rule.value}:9:{i}") for i in range(64)
    }
    assert outcomes == {0, 1}


def test_pick_winner_uses_last_hash_digit() -> None:
    """Пересчёт в уме: последняя цифра root_hash по модулю претендентов."""
    counts = {0: 1, 1: 1, 2: 0}
    rule = WinRule.MAJORITY
    assert pick_winner(counts, rule, "7:majority:100:f0") == 0
    assert pick_winner(counts, rule, "7:majority:100:f1") == 1
    # Три претендента — модуль 3.
    three = {0: 1, 1: 1, 2: 1}
    rule = WinRule.MINORITY
    assert tied_positions(three, rule) == [0, 1, 2]
    assert pick_winner(three, rule, "5:minority:9:b") == 2  # 11 % 3
    assert pick_winner(three, rule, "5:minority:9:c") == 0  # 12 % 3
    assert pick_winner(three, rule, "5:minority:9:d") == 1  # 13 % 3


def test_pick_winner_hex_and_bad_hash_fall_back() -> None:
    """Верхний регистр хеша — как есть; неразборчивый «хеш» — легаси-сид."""
    counts = {0: 1, 1: 1, 2: 0}
    rule = WinRule.MAJORITY
    assert int("F", 16) % 2 == pick_winner(counts, rule, "7:majority:100:0F")
    a = pick_winner(counts, rule, "7:majority:100:zzz")
    b = pick_winner(counts, rule, "7:majority:100:zzz")
    assert a == b
    assert a in (0, 1)


async def test_tie_seed_embeds_entropy() -> None:
    round_row = Round(
        day_index=7,
        win_rule=WinRule.MAJORITY,
        status=RoundStatus.OPEN,
        chapter_title="t",
        chapter_text="t",
        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC),
        tally_ends_at=datetime.now(UTC),
    )
    assert tie_seed(round_row) == "7:majority"
    round_row.tie_entropy = "93123949:abcd1234"
    assert tie_seed(round_row) == "7:majority:93123949:abcd1234"


async def test_close_voting_captures_entropy_once(
    session, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """Ничья в close_voting снимает энтропию и фиксирует её в дне."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    captured: list[str] = []
    async def fake_fetch() -> str:
        captured.append("fetch")
        return "93123949:abcdef"
    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = await _seed_tied_day(session, 810)
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        assert loaded.tie_entropy == "93123949:abcdef"  # сохранён в день
        assert captured == ["fetch"]  # сняли ровно один раз
        seed = tie_seed(loaded)
        counts = {0: 1, 1: 1, 2: 0}
        expected = pick_winner(counts, loaded.win_rule, seed)
        assert loaded.winner_card == expected  # победитель из энтропии
        assert expected in (0, 1)
        # Детерминизм: повторный подсчёт тем же сидом даёт того же победителя.
        assert expected == pick_winner(counts, loaded.win_rule, tie_seed(loaded))
    finally:
        await session.rollback()


async def test_heal_recomputes_same_winner(
    session, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """Повторный подсчёт (heal) с сохранённой энтропией даёт того же победителя."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_fetch() -> str:
        return "93123949:abcdef"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = await _seed_tied_day(session, 820)
    try:
        # close_voting: победитель по энтропии, Tie_entropy сохранён.
        await close_voting(session, round_row)
        first = await session.get(Round, round_row.id)
        winner_at_close = first.winner_card
        entropy_saved = first.tie_entropy
        assert entropy_saved is not None

        # finish_tally: пересчёт с теми же голосами и той же энтропией.
        first.status = RoundStatus.TALLYING
        await finish_tally(session, first)
        loaded = await session.get(Round, round_row.id)
        assert loaded.winner_card == winner_at_close
        assert loaded.tie_note is not None
        assert "блока TON №93123949" in loaded.tie_note  # проверяемо в эксплорере
        assert "tonviewer.com/block/" in loaded.tie_note
        assert len(loaded.tie_note) <= 200
    finally:
        await session.rollback()


async def test_tie_without_ton_falls_back_to_legacy_seed(
    session, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """TON выключен: ничья решается старым детерминированным сидом без энтропии."""
    monkeypatch.setattr(settings, "ton_enabled", False)
    round_row = await _seed_tied_day(session, 830)
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        assert loaded.tie_entropy is None
        assert loaded.winner_card == await _win_without_entropy(loaded)
        assert "блока TON" not in (loaded.tie_note or "")
    finally:
        await session.rollback()


async def test_tie_note_reaches_results_post(
    session, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """Конечная связь: tie_note с блоком реально уходит в пост итогов (format_results)."""
    from app.broadcast import results_message

    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_fetch() -> str:
        return "93123949:abcdef"

    monkeypatch.setattr("app.ton_pay.fetch_masterchain_entropy", fake_fetch)

    round_row = await _seed_tied_day(session, 840)
    try:
        await close_voting(session, round_row)
        loaded = await session.get(Round, round_row.id)
        loaded.status = RoundStatus.TALLYING
        await finish_tally(session, loaded)
        closed = await session.get(Round, round_row.id)
        text = await results_message(closed, session)
        assert "блока TON №93123949" in text
        assert "93123949" in text  # seqno блока виден игрокам — можно перепроверить
        assert "tonviewer.com/block/" in text
    finally:
        await session.rollback()


async def test_epilogue_escaped_in_results_html(session) -> None:
    """Эпилог сюжетного слоя — не доверенный HTML: в пост итогов идёт экранированным."""
    from app.broadcast import results_message

    round_row = await _seed_tied_day(session, 860)
    try:
        loaded = await session.get(Round, round_row.id)
        loaded.status = RoundStatus.CLOSED
        loaded.winner_card = 0
        loaded.epilogue_text = "<b>хитрость</b> & <i>вставка</i>"
        await session.commit()

        text = await results_message(loaded, session)
        assert "<b>хитрость</b>" not in text  # сырой HTML не попадает в пост
        assert "&lt;b&gt;хитрость&lt;/b&gt; &amp; &lt;i&gt;вставка&lt;/i&gt;" in text
    finally:
        await session.rollback()