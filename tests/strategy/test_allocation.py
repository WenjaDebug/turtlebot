"""Tests for slot allocation across blocks."""

from __future__ import annotations

from tests.factories import position
from turtlebot.config import Block1Settings, Block2Settings, RankBy
from turtlebot.domain.models import Block, Candidate, PositionStatus
from turtlebot.strategy.allocation import block_candidates, plan_block_entries, select_entries

B1 = Block1Settings(max_positions=3)
B2 = Block2Settings(max_positions=3)


def cand(ticker: str, strength: float, volume_ratio: float = 2.0) -> Candidate:
    return Candidate(ticker, price=10.0, stop=9.0, strength=strength, volume_ratio=volume_ratio)


class TestSelectEntries:
    def test_highest_score_first(self) -> None:
        assert select_entries({"A": 0.1, "B": 0.3, "C": 0.2}, 2) == ["B", "C"]

    def test_ties_keep_input_order(self) -> None:
        assert select_entries({"A": 0.1, "B": 0.1, "C": 0.1}, 2) == ["A", "B"]

    def test_no_free_slots(self) -> None:
        assert select_entries({"A": 1.0}, 0) == []
        assert select_entries({"A": 1.0}, -3) == []


def test_block2_strength_filter_is_inclusive() -> None:
    cands = [cand("A", 0.0999), cand("B", 0.10), cand("C", 0.25)]
    assert [c.ticker for c in block_candidates(cands, B2)] == ["B", "C"]
    assert len(block_candidates(cands, B1)) == 3


def test_free_slots_count_pending_and_open_of_own_block() -> None:
    active = [
        position("X", block=Block.ONE),
        position("Y", block=Block.ONE, status=PositionStatus.PENDING),
        position("Z", block=Block.TWO),
    ]
    plan = plan_block_entries([cand("A", 0.2), cand("B", 0.1)], B1, active, traded_today=())
    assert plan.free_slots == 1
    assert [c.ticker for c in plan.chosen] == ["A"]


def test_tickers_held_by_other_block_or_traded_today_are_excluded() -> None:
    active = [position("HELD", block=Block.TWO)]
    cands = [cand("HELD", 0.5), cand("SOLD", 0.4), cand("FREE", 0.3)]
    plan = plan_block_entries(cands, B1, active, traded_today={"SOLD"})
    assert [c.ticker for c in plan.chosen] == ["FREE"]
    assert plan.pool_size == 1


def test_block2_does_not_get_block1_entries_of_same_scan() -> None:
    cands = [cand("A", 0.30), cand("B", 0.20), cand("C", 0.15), cand("D", 0.05)]
    b1 = plan_block_entries(cands, Block1Settings(max_positions=2), [], traded_today=())
    traded = {c.ticker for c in b1.chosen}
    b2 = plan_block_entries(cands, B2, [], traded_today=traded)
    assert [c.ticker for c in b1.chosen] == ["A", "B"]
    assert [c.ticker for c in b2.chosen] == ["C"]  # D is below 10 % strength


def test_rank_by_volume_ratio() -> None:
    cands = [cand("A", 0.30, volume_ratio=2.0), cand("B", 0.10, volume_ratio=5.0)]
    plan = plan_block_entries(
        cands, Block1Settings(max_positions=1), [], (), rank_by=RankBy.VOLUME_RATIO
    )
    assert [c.ticker for c in plan.chosen] == ["B"]


def test_full_block_chooses_nothing() -> None:
    active = [position(s) for s in ("A", "B", "C")]
    plan = plan_block_entries([cand("D", 0.9)], B1, active, ())
    assert plan.free_slots == 0
    assert plan.chosen == []
