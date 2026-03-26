#!/usr/bin/env python3
"""
[LEGACY — REPLACED BY orca-scanner-cron.py]

Job 1: Emerging Movers Scanner — runs every 60 seconds.

Calls Senpi's leaderboard_get_markets, detects acceleration signals,
and auto-enters on FIRST_JUMP/CONTRIB_EXPLOSION when criteria are met.

DEPRECATED: This script uses DSL-Tight mode (fixed % tiers) instead of
High Water Mode. Use orca-scanner-cron.py (STALKER + STRIKER dual-mode
with hardcoded safety gates) instead.
"""

import sys
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    git_pull,
    git_sync,
    log,
    load_json,
    save_json,
    now_iso,
    SCAN_HISTORY_FILE,
    POSITION_STATE_DIR,
    SCANNER_CONFIG_FILE,
    load_regime,
    add_pending_entry,
    send_telegram,
    mcporter_read,
)

MAX_SCAN_HISTORY = 60  # Keep last 60 scans (~1 hour at 60s)


def fetch_leaderboard() -> list[dict]:
    """Single API call to get SM profit concentration leaderboard."""
    result = mcporter_read("leaderboard_get_markets", {})
    if "error" in result:
        log(f"Leaderboard fetch failed: {result['error']}")
        return []
    # Normalize: result may be nested under various keys depending on mcporter version
    markets = result.get("markets", result.get("data", result))
    if isinstance(markets, list):
        return markets
    return []


def detect_signals(current: list[dict], history: list[dict]) -> list[dict]:
    """Compare current scan with history to detect acceleration signals."""
    if not current:
        return []

    prev = history[-1]["markets"] if history else []
    prev_by_asset = {m.get("asset", ""): m for m in prev}

    signals = []
    for i, market in enumerate(current[:50]):  # Top 50 only
        asset = market.get("asset", "")
        rank = i + 1
        direction = market.get("direction", market.get("side", ""))
        contrib = float(market.get("contribution", market.get("pctOfTotal", 0)))
        traders = int(market.get("traderCount", market.get("traders", 0)))

        prev_market = prev_by_asset.get(asset)
        prev_rank = None
        prev_contrib = 0
        if prev_market:
            prev_rank = prev.index(prev_market) + 1 if prev_market in prev else None
            prev_contrib = float(
                prev_market.get("contribution", prev_market.get("pctOfTotal", 0))
            )

        # Build reasons list
        reasons = []
        signal_type = None

        # FIRST_JUMP: 10+ rank jump from #25+ in ONE scan
        if prev_rank and prev_rank >= 25 and (prev_rank - rank) >= 10:
            reasons.append("FIRST_JUMP")
            signal_type = "FIRST_JUMP"
        elif prev_rank is None and rank <= 20:
            reasons.append("NEW_ENTRY_DEEP")
            signal_type = "NEW_ENTRY_DEEP"

        # CONTRIB_EXPLOSION: 3x+ contribution increase
        if prev_contrib > 0 and contrib >= prev_contrib * 3 and rank <= 20:
            reasons.append("CONTRIB_EXPLOSION")
            if signal_type != "FIRST_JUMP":
                signal_type = "CONTRIB_EXPLOSION"

        # DEEP_CLIMBER: 5+ rank jump from #25+
        if (
            prev_rank
            and prev_rank >= 25
            and (prev_rank - rank) >= 5
            and signal_type is None
        ):
            reasons.append("DEEP_CLIMBER")
            signal_type = "DEEP_CLIMBER"

        # RANK_UP: 2+ positions
        if prev_rank and (prev_rank - rank) >= 2 and not reasons:
            reasons.append("RANK_UP")

        # Compute velocity from history
        velocity = 0
        if len(history) >= 2:
            older = history[-2]["markets"] if len(history) >= 2 else []
            older_by_asset = {m.get("asset", ""): m for m in older}
            if asset in older_by_asset:
                old_contrib = float(older_by_asset[asset].get("contribution", 0))
                if old_contrib > 0:
                    velocity = (contrib - old_contrib) / old_contrib

        # Quality filters (v3.1)
        erratic = _check_erratic(asset, history)
        low_velocity = (
            velocity < 0.03
            if signal_type in ("FIRST_JUMP", "CONTRIB_EXPLOSION")
            else False
        )

        if reasons:
            signals.append(
                {
                    "asset": asset,
                    "direction": direction,
                    "rank": rank,
                    "prevRank": prev_rank,
                    "contribution": contrib,
                    "traderCount": traders,
                    "reasons": reasons,
                    "signalType": signal_type,
                    "contribVelocity": round(velocity, 4),
                    "erratic": erratic,
                    "lowVelocity": low_velocity
                    and signal_type != "FIRST_JUMP",  # First jumps exempt
                    "timestamp": now_iso(),
                }
            )

    return signals


def _check_erratic(asset: str, history: list[dict]) -> bool:
    """Check if asset has >5 rank reversals in scan history."""
    ranks = []
    for scan in history[-10:]:
        for i, m in enumerate(scan.get("markets", [])[:50]):
            if m.get("asset") == asset:
                ranks.append(i + 1)
                break
    if len(ranks) < 3:
        return False
    reversals = 0
    for i in range(2, len(ranks)):
        if (ranks[i] - ranks[i - 1]) * (ranks[i - 1] - ranks[i - 2]) < 0:
            reversals += 1
    return reversals > 5


def main():
    if not acquire_lock("emerging-movers"):
        return  # Previous run still active

    try:
        git_pull()

        # Fetch leaderboard
        markets = fetch_leaderboard()
        if not markets:
            log("No leaderboard data — skipping")
            return

        # Load scan history
        history = load_json(SCAN_HISTORY_FILE, default=[])

        # Detect signals
        signals = detect_signals(markets, history)

        # Save current scan to history
        history.append(
            {
                "timestamp": now_iso(),
                "markets": markets[:50],
            }
        )
        # Trim to MAX_SCAN_HISTORY
        if len(history) > MAX_SCAN_HISTORY:
            history = history[-MAX_SCAN_HISTORY:]
        save_json(SCAN_HISTORY_FILE, history)

        if not signals:
            return  # Silent — no alerts

        # Log signals
        for sig in signals:
            log(
                f"Signal: {sig['signalType']} {sig['direction']} {sig['asset']} "
                f"rank={sig['rank']} reasons={sig['reasons']} "
                f"vel={sig['contribVelocity']:.3f}"
            )

        for sig in signals:
            add_pending_entry(
                {**sig, "autoEntered": False, "source": "emerging-movers"}
            )

        git_sync("auto: EM scan")

    finally:
        release_lock("emerging-movers")


if __name__ == "__main__":
    main()
