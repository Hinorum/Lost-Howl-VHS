"""Персональные итоги дня: игрок видит, за какую сцену голосовал и чем кончилось.

Блок «выиграла такая-то карта, ты выбрал такой-то путь» и его денежная судьба —
это ответ на жалобу «не помню за что голосовал / не понятно выиграл или проиграл».
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.broadcast import (
    _build_player_result_text,
    _player_result_texts,
    announce_player_results,
    scene_label,
)
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Payout, Player, Round, RoundStatus, Stake, Vote, WinRule

_CARDS = {0: "Казарма", 1: "Маяк", 2: "Волна"}


def _round(day_index: int, *, winner: int = 1, money: bool = True) -> Round:
    return Round(
        id=99_000 + day_index,
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        chapter_title="Глава",
        chapter_text="Текст",
        opens_at=datetime.now(UTC),
        voting_ends_at=datetime.now(UTC) + timedelta(hours=23),
        tally_ends_at=datetime.now(UTC) + timedelta(hours=24),
        winner_card=winner,
        money_mode=money,
        vote_counts_json='{"0":1,"1":2,"2":0}',
    )


def _stake(amount: int = 500_000_000, status: str = "confirmed") -> SimpleNamespace:
    return SimpleNamespace(amount_nanotons=amount, status=status)


def _payout(kind: str, amount: int) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, amount_nanotons=amount)


def test_scene_label_includes_title(monkeypatch) -> None:
    assert scene_label(_CARDS, 1) == "II. «Маяк»"
    assert scene_label({0: "Казарма"}, 2) == "III"


def test_scene_label_escapes_html(monkeypatch) -> None:
    assert scene_label({0: "Крыша <настоящая>"}, 0) == "I. «Крыша &lt;настоящая&gt;»"


def test_build_win_prize(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    text = _build_player_result_text(_round(1, winner=1), _CARDS, 1, _stake(), [_payout("prize", 4_750_000_000)])
    assert "Ты угадал сцену дня!" in text
    assert "II. «Маяк»" in text
    assert "0.5 Gram в выигрыш: +4.75 Gram" in text


def test_build_loss_refund(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    text = _build_player_result_text(_round(2, winner=1), _CARDS, 0, _stake(), [_payout("refund", 495_000_000)])
    assert "Твоя сцена не победила" in text
    assert "I. «Казарма»" in text
    assert "0.5 Gram возвращается: 0.495 Gram" in text


def test_build_loss_stake_stays_in_bank(monkeypatch) -> None:
    """Проигравшая подтверждённая ставка названа проигрышем, а не зачислением.

    Старая формулировка «принята в банк дня» читалась как «зачислена», и в
    момент проигрыша игрок не видел, что ставка не уцелела.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    text = _build_player_result_text(_round(5, winner=1), _CARDS, 0, _stake(), [])
    assert "Твоя сцена не победила" in text
    assert "не уцелела — деньги остались в банке дня" in text
    assert "принята в банк" not in text


def test_build_unstaked_voice(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    text = _build_player_result_text(_round(3, winner=2), _CARDS, 2, None, [])
    assert "Ставки в этот день не было" in text
    assert "Ты угадал сцену дня!" in text


def test_build_money_off_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    text = _build_player_result_text(
        _round(4, winner=1, money=False),
        _CARDS,
        1,
        _stake(),
        [_payout("prize", 4_750_000_000)],
    )
    assert "Gram" not in text
    assert "Ты угадал сцену дня!" in text


async def _seed_round(
    day_index: int,
    votes: list[tuple[int, int]],
    stakes: list[tuple[int, int]] | None = None,
    payouts: list[tuple[int, str, int]] | None = None,
    subtitles: dict[int, bool] | None = None,
) -> Round:
    """Раунд в глобальной БД: карты, голоса, ставки, выплаты игрокам."""
    round_row = _round(day_index)
    async with SessionLocal() as db:
        db.add(round_row)
        await db.flush()
        for position, title in _CARDS.items():
            db.add(
                Card(
                    round_id=round_row.id,
                    position=position,
                    title=title,
                    description="описание",
                    consequence="канон",
                    tag="care",
                )
            )
        # Игроки — родители голосов, ставок и выплат. Между мапперами нет
        # relationship(), поэтому SQLAlchemy не упорядочивает вставки по FK:
        # родителей сбрасываем явным flush() до детей, иначе Postgres
        # отвергнет INSERT ребёнка (SQLite внешние ключи не проверяет).
        pids = {pid for pid, _pos in votes}
        pids |= {pid for pid, _amount in (stakes or [])}
        pids |= {pid for pid, _kind, _amount in (payouts or [])}
        for pid in pids:
            db.add(
                Player(
                    id=pid,
                    username=f"p{pid}",
                    first_name="P",
                    dm_subscribed=bool(subtitles.get(pid, True)) if subtitles else True,
                )
            )
        await db.flush()
        for pid, pos in votes:
            db.add(Vote(round_id=round_row.id, player_id=pid, card_position=pos))
        for pid, amount in stakes or []:
            db.add(
                Stake(
                    round_id=round_row.id,
                    player_id=pid,
                    amount_nanotons=amount,
                    tx_hash=f"tx{day_index}:{pid}",
                    memo="",
                    network="mainnet",
                    status="confirmed",
                )
            )
        for pid, kind, amount in payouts or []:
            db.add(
                Payout(
                    round_id=round_row.id,
                    player_id=pid,
                    kind=kind,
                    amount_nanotons=amount,
                    dest_address="",
                    network="mainnet",
                )
            )
        await db.commit()
        db.expunge(round_row)
    return round_row


async def test_player_result_texts_honors_db(monkeypatch) -> None:
    """Из БД собираются только подписанные проголосовавшие: у каждого свой текст."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_win, pid_muted = 99_101, 99_102
    round_row = await _seed_round(
        101,
        votes=[(pid_win, 1), (pid_muted, 2)],
        subtitles={pid_muted: False},
        stakes=[(pid_win, 500_000_000)],
        payouts=[(pid_win, "prize", 4_750_000_000)],
    )
    texts = dict(await _player_result_texts(round_row))
    assert pid_win in texts and pid_muted not in texts
    body = texts[pid_win]
    assert "II. «Маяк»" in body
    assert "Ты угадал сцену дня!" in body
    assert "+4.75 Gram" in body


async def test_announce_player_results_delivers(monkeypatch) -> None:
    pid = 99_103
    round_row = await _seed_round(102, votes=[(pid, 1)], stakes=[(pid, 500_000_000)])
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace()))
    delivered = await announce_player_results(bot, round_row)
    assert delivered == 1
    bot.send_message.assert_awaited_once()
    sent_pid = bot.send_message.await_args.args[0]
    text = bot.send_message.await_args.args[1]
    assert sent_pid == pid
    assert "II. «Маяк»" in text


async def test_announce_player_results_respects_player_dm_flag(monkeypatch) -> None:
    pid = 99_104
    round_row = await _seed_round(103, votes=[(pid, 0)])
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(settings, "player_dm", False)
    try:
        delivered = await announce_player_results(bot, round_row)
        assert delivered == 0
        bot.send_message.assert_not_awaited()
    finally:
        monkeypatch.setattr(settings, "player_dm", True)


async def test_announce_player_results_bot_none_is_silent() -> None:
    pid = 99_105
    round_row = await _seed_round(104, votes=[(pid, 1)])
    assert await announce_player_results(None, round_row) == 0
