"""Пакет rounds: жизненный цикл дня, голосование и выплаты.

Сюжетные модули (rendering, narrative, anchor, materialization) содержат
минимальные шаблонные реализации: механика работает без LLM/арта.
"""
from __future__ import annotations

from .anchor import default_anchor, get_run_anchor, parse_anchor  # noqa: F401
from .lifecycle import (  # noqa: F401
    claim_announcement,
    close_voting,
    create_next_round,
    create_next_round_detailed,
    ensure_current_round,
    finish_tally,
    heal_stale_rounds,
    public_round_view,
    reset_game,
    unclaim_announcement,
)
from .materialization import _materialize_round, _stamp_day_money_mode  # noqa: F401
from .narrative import write_epilogue  # noqa: F401
from .pot import round_pot  # noqa: F401
from .queries import get_active_round, get_latest_round, get_round  # noqa: F401
from .rendering import (  # noqa: F401
    PREPARED_PAYLOAD_VERSION,
    _plan_and_render,
)
from .time import _ROMAN, _day_window, _next_hour_slot, _now, utc_aware  # noqa: F401
from .voting import (  # noqa: F401
    _TIE_THEATER,
    _decisive_counts,
    _winner_and_tied,
    count_stakes_for_tally,
    count_votes_for_tally,
    pick_winner,
    plain_vote_counts,
    tie_seed,
    tied_positions,
)

__all__ = [
    "get_run_anchor",
    "default_anchor",
    "parse_anchor",
    "claim_announcement",
    "unclaim_announcement",
    "close_voting",
    "create_next_round",
    "create_next_round_detailed",
    "ensure_current_round",
    "finish_tally",
    "heal_stale_rounds",
    "public_round_view",
    "reset_game",
    "_materialize_round",
    "_stamp_day_money_mode",
    "write_epilogue",
    "round_pot",
    "get_active_round",
    "get_latest_round",
    "get_round",
    "PREPARED_PAYLOAD_VERSION",
    "_plan_and_render",
    "_ROMAN",
    "_day_window",
    "_next_hour_slot",
    "_now",
    "utc_aware",
    "_TIE_THEATER",
    "_decisive_counts",
    "_winner_and_tied",
    "count_stakes_for_tally",
    "count_votes_for_tally",
    "pick_winner",
    "plain_vote_counts",
    "tie_seed",
    "tied_positions",
]