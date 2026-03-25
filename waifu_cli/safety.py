"""
safety.py — Centralized entry gate pipeline for the strategic layer.

Every trade entry flows through `evaluate_entry()`. This is the SINGLE
point where all hard safety constraints are enforced.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from senpi_common import (
    clamp_leverage,
    check_hard_cooldown,
    count_open_slots,
    current_regime_params,
    is_asset_banned,
    is_entries_allowed,
    is_auto_entry_enabled,
    load_brain_state,
    load_global_guardrails,
    check_directional_exposure_limit,
)


@dataclass
class GateResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    clamped_leverage: int = 8
    effective_margin: float = 0.0

    def add(self, passed: bool, msg: str) -> bool:
        if not passed:
            self.approved = False
            self.reasons.append(msg)
        return passed


def evaluate_entry(entry: dict, strategy: dict) -> GateResult:
    """Run the full gate pipeline on a pending entry.

    Returns a GateResult with approved=True if all gates pass, or
    approved=False with a list of rejection reasons.
    """
    result = GateResult(approved=True)
    params = current_regime_params()
    guardrails = load_global_guardrails()

    asset = entry.get("asset", entry.get("symbol", ""))
    direction = entry.get("direction", entry.get("side", ""))
    scanner = str(
        entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
    ).lower()
    score = float(entry.get("score", entry.get("totalScore", 0)))
    leverage = float(entry.get("leverage", 8))

    # Gate 1: Entries allowed by regime
    result.add(
        is_entries_allowed(),
        f"Entries not allowed (regime {params.get('riskMode', '?')})",
    )

    # Gate 2: Auto-entry enabled
    result.add(
        is_auto_entry_enabled(),
        "Auto-entry disabled by regime/brain policy",
    )

    # Gate 3: Valid strategy
    strategy_id = strategy.get("strategyId", "")
    result.add(
        bool(strategy_id) and not strategy_id.startswith("REPLACE"),
        "No valid strategy ID configured",
    )

    # Gate 4: Slots available
    open_slots = count_open_slots(strategy)
    result.add(
        open_slots > 0,
        f"No open slots (max {guardrails.get('maxPositionsTotal', 3)})",
    )

    # Gate 5: Scanner not blocked by brain
    brain_ctx = entry.get("brainContext", {})
    result.add(
        not brain_ctx.get("blockedScanner", False),
        f"Scanner {scanner} blocked by brain policy",
    )

    # Gate 6: Score threshold
    min_scores = {
        "orca": 6,
        "mantis": 7,
        "fox": 7,
        "komodo": 10,
        "condor": 10,
        "polar": 10,
        "sentinel": 5,
        "rhino": 5,
    }
    # Normalize scanner name
    for s_name in min_scores:
        if s_name in scanner:
            scanner = s_name
            break
    min_score = min_scores.get(scanner, 6)
    result.add(
        score >= min_score,
        f"Score {score} < min {min_score} for {scanner}",
    )

    # Gate 7: XYZ ban (HARD — non-negotiable)
    result.add(
        not is_asset_banned(asset),
        f"Asset {asset} is BANNED",
    )

    # Gate 8: 120-minute cooldown (HARD — non-negotiable)
    result.add(
        not check_hard_cooldown(asset),
        f"Asset {asset} in 120min cooldown",
    )

    # Gate 9: Directional exposure cap
    margin = float(entry.get("marginUsd", 0) or params.get("allocPctPerSlot", 25))
    allowed, snapshot = check_directional_exposure_limit(
        direction, margin, leverage
    )
    result.add(
        allowed,
        f"Directional exposure would breach {snapshot.get('capPct', 70)}% cap",
    )

    # Gate 10: Leverage clamp (HARD — non-negotiable, 7-10x)
    result.clamped_leverage = clamp_leverage(leverage)
    result.effective_margin = margin

    return result
