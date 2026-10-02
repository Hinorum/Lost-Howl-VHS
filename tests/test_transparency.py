"""Прозрачность для игроков: расписание суток, распределение фонда, /top."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.config import settings


async def test_start_explains_schedule_and_disclaimer(monkeypatch, tmp_path) -> None:
    """Игрок с порога знает: когда итоги, куда уходит фонд и кто рискует."""
    from unittest.mock import AsyncMock as AM

    from app import handlers as h
    from app.models import Card, Round, RoundStatus, WinRule

    now = datetime.now(UTC)
    fake_round = Round(
        day_index=901,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),

    )
    fake_round.cards = []
    for i in range(3):
        image = tmp_path / f"card{i}.jpg"
        image.write_bytes(b"jpeg")
        fake_round.cards.append(
            Card(position=i, title=f"T{i}", description="d", consequence="c",
                 tag="care", image_path=str(image))
        )
    monkeypatch.setattr(h, "_ensure_round", AM(return_value=fake_round))
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "revote_enabled", True)
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1, username="u", first_name="U"),
        answer=AM(),
        answer_media_group=AM(),
    )
    await h.cmd_start(message)
    text = message.answer.call_args_list[0].args[0]
    # Нарративный старт под бесшовные сутки: итоги придут сразу после.
    assert "придут сразу после" in text
    assert "час тайны урны" not in text
    assert "95%" in text
    assert "/top" in text
    assert "не отвечают" not in text
    assert "утраченные средства" not in text
    assert "сам решаешь" in text

    # Без TON-экономики — ни фонда, ни /top, и дисклеймера про деньги нет:
    # в бесплатной версии нечего терять, пугать игрока нечем.
    monkeypatch.setattr(settings, "ton_enabled", False)
    message2 = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=2, username="u2", first_name="U"),
        answer=AM(),
        answer_media_group=AM(),
    )
    await h.cmd_start(message2)
    text2 = message2.answer.call_args_list[0].args[0]
    assert "/top" not in text2 and "95%" not in text2
    assert "не отвечают" not in text2
    assert "утраченные средства" not in text2


def test_format_top_lists_leaders_and_pot() -> None:
    from app.handlers import _format_top
    from app.style import money_mark

    empty = _format_top([], 0.0, [], 0.0)
    assert "ещё нет" in empty and "0 Gram" in empty

    filled = _format_top([("Пёс", 7, True), ("Кот", 3, False)], 1.25, [("Барс", 9, True)], 2.5)
    lines = filled.splitlines()
    assert lines[0].startswith(f"{money_mark('week')} Копилка недели: 1.25 Gram")
    assert "Лидеры недели:" in filled
    assert "🥇 🎟 Пёс — 7" in filled and "🥈 🔒 Кот — 3" in filled
    assert f"{money_mark('top')} Копилка месяца: 2.5 Gram" in filled
    assert "1. 🎟 Барс — 9" in filled


async def test_wallet_view_shows_distribution_and_dyor(session, monkeypatch) -> None:

    from app.voting import upsert_player

    monkeypatch.setattr(settings, "ton_enabled", True)
    async with session:
        player = await upsert_player(session, SimpleNamespace(id=555, username="w", first_name="W"))
        player.wallet_address = "0:abc"
        await session.commit()

    from app.handlers import _wallet_view_text

    text = await _wallet_view_text(SimpleNamespace(id=555, username="w", first_name="W"))
    assert "Распределение фонда дня" in text
    for marker in ("95%", "1%", "2%", "0,5%", "Фонд Стаи", "копилка месяца", "пригласившим"):
        assert marker in text
    assert "возвращаются целиком" in text
    assert "DYOR" in text


async def test_status_text_shows_results_time() -> None:
    """В статусе дня видно и дедлайн голосования, и время итогов."""
    from app.broadcast import status_text
    from app.models import Card, Round, RoundStatus, WinRule

    now = datetime.now(UTC)
    round_row = Round(
        day_index=3,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        chapter_title="t",
        chapter_text="text",

        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now + timedelta(hours=24),
    )
    round_row.cards = [
        Card(position=0, title="T", description="d", consequence="c", tag="care")
    ]
    text = await status_text(round_row)
    assert "Голосование до:" in text and "Итоги и новый день:" in text
    assert "UTC" in text
