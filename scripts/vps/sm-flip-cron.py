#!/usr/bin/env python3
"""
SM Flip Detector — runs every 5 minutes via APScheduler.

For each open position, checks if smart money conviction has flipped
against the position direction. If consensus flips and position is in
profit → close for profit. If consensus flips and position is losing →
alert but don't close (DSL handles stops).

Native Python implementation using senpi_common.py.
No dependency on senpi-skills or OpenClaw.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso,
    load_json, save_json,
    get_enabled_strategies, get_open_positions,
    mcporter_call, send_telegram, record_trade, git_sync,
    record_heartbeat,
)


def fetch_leaderboard_direction(asset: str) -> str | None:
    """
    Get dominant SM direction for an asset from the leaderboard.
    Only returns a direction if conviction >= 4 and traders >= 100.
    """
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        log(f"SM flip: leaderboard fetch failed: {result['error']}")
        return None

    data = result.get("data", result)
    markets = data.get("markets", data)
    if isinstance(markets, dict):
        markets = markets.get("markets", [])
    if not isinstance(markets, list):
        return None

    for m in markets:
        token = m.get("token", m.get("asset", ""))
        if token == asset:
            direction = m.get("direction", m.get("side", "")).upper()
            conviction = float(m.get("conviction", 0))
            traders = int(m.get("traderCount", m.get("traders", 0)))
            if direction in ("LONG", "SHORT") and conviction >= 4 and traders >= 100:
                return direction
            else:
                return None  # Below threshold = no flip conviction

    return None


def close_on_flip(dsl_state: dict, current_direction: str):
    """Close position due to SM conviction flip."""
    asset = dsl_state["asset"]
    strategy_id = dsl_state.get("strategyId")

    log(f"SM FLIP CLOSE: {asset} — was {dsl_state.get('direction')}, now SM says {current_direction}")

    mcporter_call("strategy_close_position", {
        "strategyId": strategy_id,
        "asset": asset,
    }, timeout=15)

    dsl_state["active"] = False
    dsl_state["closedAt"] = now_iso()
    dsl_state["closeReason"] = "sm_flip"
    save_json(Path(dsl_state["_file"]), dsl_state)

    record_trade({
        "action": "CLOSE",
        "asset": asset,
        "direction": dsl_state.get("direction", ""),
        "entryPrice": dsl_state.get("entryPrice", 0),
        "closePrice": 0,  # Will be filled by reconcile
        "size": dsl_state.get("size", 0),
        "leverage": dsl_state.get("leverage", 0),
        "strategyKey": dsl_state.get("strategyKey", ""),
        "entrySource": dsl_state.get("entryMode", "unknown"),
        "entryScore": dsl_state.get("entryScore", 0),
        "entryMode": dsl_state.get("entryMode", ""),
        "closeReason": "sm_flip",
        "realizedPnl": 0,
        "entryCreatedAt": dsl_state.get("createdAt", ""),
        "closedAt": dsl_state["closedAt"],
        "highWaterRoe": 0,
        "finalTierIndex": dsl_state.get("currentTierIndex", -1),
    })

    send_telegram(
        f"🔄 SM FLIP: {asset}\n"
        f"Position: {dsl_state.get('direction', '')} → SM now {current_direction}\n"
        f"Position closed. Entry: ${dsl_state.get('entryPrice', 0):.4f}\n"
        f"Strategy: {dsl_state.get('strategyKey', '?')}"
    )


def main():
    if not acquire_lock("sm-flip"):
        return

    try:
        record_heartbeat("sm-flip")
        strategies = get_enabled_strategies()
        if not strategies:
            return

        flipped = False
        for strat in strategies:
            positions = get_open_positions(strat["_key"])
            for pos in positions:
                asset = pos.get("asset", "")
                pos_dir = pos.get("direction", "").upper()

                sm_dir = fetch_leaderboard_direction(asset)
                if sm_dir is None:
                    # Asset not on leaderboard at all — could mean it's dropping
                    continue

                if sm_dir != pos_dir:
                    # SM has flipped against our position
                    log(f"SM flip detected: {asset} position={pos_dir} sm={sm_dir}")

                    # Only auto-close if position has been open long enough (>10 min)
                    # to avoid false flips on initial entry
                    from datetime import datetime, timezone, timedelta
                    created = pos.get("createdAt", "")
                    try:
                        entry_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
                        if age_min < 10:
                            log(f"SM flip: {asset} too new ({age_min:.0f}min) — skipping")
                            continue
                    except (ValueError, TypeError):
                        pass

                    close_on_flip(pos, sm_dir)
                    flipped = True

        if flipped:
            git_sync("auto: SM flip close")

    finally:
        release_lock("sm-flip")


if __name__ == "__main__":
    main()
