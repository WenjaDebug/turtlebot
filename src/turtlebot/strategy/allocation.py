"""Slot allocation across the two strategy blocks.

Block 1 chooses first. Block 2 only sees candidates that pass its minimum breakout strength and
that neither block holds nor traded today (including block-1 entries of the same scan).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from turtlebot.config import BlockSettings, RankBy
from turtlebot.domain.models import Candidate, Position


def select_entries(scores: Mapping[str, float], free_slots: int) -> list[str]:
    """Pick the best candidates for the free slots (highest score first, stable for ties).

    Args:
        scores: Mapping ticker -> score.
        free_slots: Number of free slots (negative values are treated as zero).

    Returns:
        Selected tickers in descending score order.
    """
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [ticker for ticker, _ in ranked[: max(free_slots, 0)]]


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Candidates chosen for one block in one scan."""

    free_slots: int
    pool_size: int
    chosen: list[Candidate]


def block_candidates(candidates: Sequence[Candidate], block: BlockSettings) -> list[Candidate]:
    """Apply the block's minimum breakout strength filter."""
    if block.min_strength is None:
        return list(candidates)
    threshold = block.min_strength
    return [c for c in candidates if c.strength >= threshold]


def plan_block_entries(
    candidates: Sequence[Candidate],
    block: BlockSettings,
    active_positions: Sequence[Position],
    traded_today: Collection[str],
    rank_by: RankBy = RankBy.BREAKOUT_STRENGTH,
) -> AllocationPlan:
    """Fill a block's free slots with the strongest eligible candidates.

    Args:
        candidates: All signals of the scan.
        block: Block configuration.
        active_positions: Pending and open positions of **both** blocks.
        traded_today: Tickers bought or sold today by either block.
        rank_by: Ranking attribute of the candidates.

    Returns:
        The allocation plan; ``chosen`` may be empty.
    """
    taken = {p.symbol for p in active_positions} | set(traded_today)
    in_block = sum(1 for p in active_positions if p.block == block.block)
    free = block.max_positions - in_block
    pool = {c.ticker: c for c in block_candidates(candidates, block) if c.ticker not in taken}
    scores = {t: getattr(c, _ATTR[rank_by]) for t, c in pool.items()}
    chosen = [pool[t] for t in select_entries(scores, free)]
    return AllocationPlan(free_slots=free, pool_size=len(pool), chosen=chosen)


_ATTR: dict[RankBy, str] = {
    RankBy.BREAKOUT_STRENGTH: "strength",
    RankBy.VOLUME_RATIO: "volume_ratio",
}
