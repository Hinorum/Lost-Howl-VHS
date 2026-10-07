from conftest import ensure_player, ensure_round

from app.db import SessionLocal
from app.models import Player, Round


async def test_ensure_helpers_are_idempotent() -> None:
    await ensure_player(999_999_001)
    await ensure_round(999_999_002)
    await ensure_player(999_999_001)
    await ensure_round(999_999_002)
    async with SessionLocal() as session:
        assert await session.get(Player, 999_999_001) is not None
        assert await session.get(Round, 999_999_002) is not None
