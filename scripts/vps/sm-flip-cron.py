#!/usr/bin/env python3
"""
Position Supervisor — flip, conviction collapse, and dead-weight rotation.

Runs every 5 minutes. This is intentionally deterministic:
  - close on hard smart-money direction flip
  - close when the original conviction has materially collapsed
  - rotate dead weight when queued opportunities have higher priority
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    load_pending_entries,
    get_enabled_strategies,
    get_open_positions,
    mcporter_call,
    send_telegram,
    record_trade,
    git_sync,
    record_heartbeat,
    compute_roe_pct,
)


MIN_POSITION_AGE_MIN = 10


def fetch_leaderboard_snapshot() -> dict[str, dict]:
    """Return per-asset smart-money direction and conviction stats."""
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        log(f"Supervisor: leaderboard fetch failed: {result['error']}")
        return {}

    data = result.get("data", result)
    markets = data.get("markets", data)
    if isinstance(markets, dict):
        markets = markets.get("markets", [])
    if not isinstance(markets, list):
        return {}

    snapshot = {}
    for market in markets:
        if not isinstance(market, dict):
            continue
        asset = market.get("token", market.get("asset", ""))
        if not asset:
            continue
        snapshot[asset] = {
            "direction": str(market.get("direction", market.get("side", ""))).upper(),
            "conviction": float(market.get("conviction", 0) or 0),
            "traders": int(market.get("traderCount", market.get("traders", 0)) or 0),
            "concentration": float(
                market.get("contribution", market.get("pct_of_top_traders_gain", market.get("pctOfTotal", 0))) or 0
            ),
        }
    return snapshot


def get_current_price(asset: str) -> float | None:
    data = mcporter_call("market_get_asset_data", {"asset": asset}, timeout=15)
    if "error" in data:
        return None
    payload = data.get("data", data)
    price = payload.get("markPrice", payload.get("price", payload.get("lastPrice")))
    if price is not None:
        return float(price)
    market = payload.get("market", {})
    price = market.get("markPrice", market.get("price"))
    return float(price) if price is not None else None


def position_age_minutes(dsl_state: dict) -> float:
    created = dsl_state.get("createdAt", "")
    try:
        entry_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
    except (ValueError, TypeError):
        return 0.0


def highest_pending_priority() -> tuple[int, int]:
    pending = load_pending_entries()
    highest = 0
    best_scanner = "unknown"
    for entry in pending:
        priority = int(entry.get("brainContext", {}).get("priority", 0) or 0)
        if priority > highest:
            highest = priority
            best_scanner = str(
                entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
            ).lower()
    return highest, len(pending), best_scanner


def close_position(dsl_state: dict, reason: str, detail: str):
    """Close an active position and record the supervisor reason."""
    asset = dsl_state["asset"]
    strategy_id = dsl_state.get("strategyId")

    log(f"SUPERVISOR CLOSE: {asset} reason={reason} detail={detail}")
    mcporter_call("strategy_close_position", {"strategyId": strategy_id, "asset": asset}, timeout=15)

    dsl_state["active"] = False
    dsl_state["closedAt"] = now_iso()
    dsl_state["closeReason"] = reason
    dsl_state["supervisorDetail"] = detail
    save_json(Path(dsl_state["_file"]), dsl_state)

    record_trade({
        "action": "CLOSE",
        "asset": asset,
        "direction": dsl_state.get("direction", ""),
        "entryPrice": dsl_state.get("entryPrice", 0),
        "closePrice": 0,
        "size": dsl_state.get("size", 0),
        "leverage": dsl_state.get("leverage", 0),
        "strategyKey": dsl_state.get("strategyKey", ""),
        "entrySource": dsl_state.get("entryMode", "unknown"),
        "entryScore": dsl_state.get("entryScore", 0),
        "entryMode": dsl_state.get("entryMode", ""),
        "closeReason": reason,
        "realizedPnl": 0,
        "entryCreatedAt": dsl_state.get("createdAt", ""),
        "closedAt": dsl_state["closedAt"],
        "highWaterRoe": dsl_state.get("highWaterRoe", 0),
        "finalTierIndex": dsl_state.get("currentTierIndex", -1),
    })

    send_telegram(
        f"🧠 SUPERVISOR CLOSE: {dsl_state.get('direction', '')} {asset}\n"
        f"Reason: {reason}\n"
        f"Detail: {detail}\n"
        f"Strategy: {dsl_state.get('strategyKey', '?')}"
    )


def check_flip(dsl_state: dict, market: dict | None, age_min: float) -> tuple[bool, str]:
    if age_min < MIN_POSITION_AGE_MIN or not market:
        return False, ""
    pos_dir = str(dsl_state.get("direction", "")).upper()
    market_dir = market.get("direction", "")
    conviction = float(market.get("conviction", 0) or 0)
    traders = int(market.get("traders", 0) or 0)
    if market_dir in ("LONG", "SHORT") and market_dir != pos_dir and conviction >= 4 and traders >= 100:
        return True, f"sm {market_dir} conviction={conviction:.1f} traders={traders}"
    return False, ""


def check_collapse(dsl_state: dict, market: dict | None, age_min: float) -> tuple[bool, str]:
    if age_min < MIN_POSITION_AGE_MIN:
        return False, ""

    playbook = dsl_state.get("playbook", {})
    collapse = playbook.get("collapse", {})
    snapshot = playbook.get("smSnapshot", {})
    if not snapshot:
        return False, ""

    entry_traders = int(snapshot.get("traderCount", 0) or 0)
    entry_conviction = float(snapshot.get("conviction", dsl_state.get("entrySmConviction", 0)) or 0)
    entry_concentration = float(snapshot.get("concentration", dsl_state.get("entrySmConcentration", 0)) or 0)
    current_traders = int((market or {}).get("traders", 0) or 0)
    current_conviction = float((market or {}).get("conviction", 0) or 0)
    current_concentration = float((market or {}).get("concentration", 0) or 0)

    trader_ratio = float(collapse.get("minTraderRatio", 0.2) or 0.2)
    trader_floor = int(collapse.get("minTraderCountFloor", 24) or 24)
    conviction_ratio = float(collapse.get("minConvictionRatio", 0.5) or 0.5)
    concentration_ratio = float(collapse.get("minConcentrationRatio", 0.5) or 0.5)

    if entry_traders >= trader_floor * 2 and current_traders <= max(trader_floor, int(entry_traders * trader_ratio)):
        return True, f"traders {entry_traders}->{current_traders}"

    if entry_conviction > 0 and current_conviction > 0 and current_conviction <= entry_conviction * conviction_ratio:
        return True, f"conviction {entry_conviction:.2f}->{current_conviction:.2f}"

    if entry_concentration > 0 and current_concentration > 0 and current_concentration <= entry_concentration * concentration_ratio:
        return True, f"concentration {entry_concentration:.3f}->{current_concentration:.3f}"

    if entry_traders >= 60 and market is None:
        return True, "asset dropped off smart-money board"

    return False, ""


def check_dead_weight(
    dsl_state: dict,
    age_min: float,
    pending_priority: int,
    pending_count: int,
    pending_scanner: str,
) -> tuple[bool, str]:
    playbook = dsl_state.get("playbook", {})
    rotation = playbook.get("rotation", {})
    if not rotation.get("eligible", True):
        return False, ""

    dead_weight_min = float(rotation.get("deadWeightMin", 20) or 20)
    min_high_water = float(rotation.get("minHighWaterRoe", 2.0) or 2.0)
    priority_gap = int(rotation.get("priorityGap", 8) or 8)
    if age_min < dead_weight_min:
        return False, ""

    current_price = get_current_price(dsl_state.get("asset", ""))
    if current_price is None:
        return False, ""

    roe = compute_roe_pct(
        float(dsl_state.get("entryPrice", 0) or 0),
        current_price,
        str(dsl_state.get("direction", "LONG")),
        float(dsl_state.get("leverage", 1) or 1),
    )
    high_water_roe = float(dsl_state.get("highWaterRoe", 0) or 0)
    position_priority = int(playbook.get("priority", 50) or 50)
    pending_pressure = pending_count > 0 and pending_priority >= position_priority + priority_gap
    scanner = str(playbook.get("scanner", dsl_state.get("scanner", "unknown"))).lower()
    same_family_pending = pending_scanner == scanner and pending_scanner != "unknown"

    if roe < 0 and high_water_roe <= min_high_water and (
        (pending_pressure and not same_family_pending) or age_min >= dead_weight_min * 2
    ):
        return True, (
            f"roe={roe:.1f}% hw={high_water_roe:.1f}% "
            f"pending={pending_count} bestPriority={pending_priority} posPriority={position_priority} gap={priority_gap}"
        )
    return False, ""


def main():
    if not acquire_lock("sm-flip"):
        return

    try:
        record_heartbeat("sm-flip")
        strategies = get_enabled_strategies()
        if not strategies:
            return

        pending_priority, pending_count, pending_scanner = highest_pending_priority()
        leaderboard = fetch_leaderboard_snapshot()
        changed = False

        for strat in strategies:
            positions = get_open_positions(strat["_key"])
            for pos in positions:
                age_min = position_age_minutes(pos)
                asset = pos.get("asset", "")
                market = leaderboard.get(asset)

                flip, detail = check_flip(pos, market, age_min)
                if flip:
                    close_position(pos, "sm_flip", detail)
                    changed = True
                    continue

                collapse, detail = check_collapse(pos, market, age_min)
                if collapse:
                    close_position(pos, "conviction_collapse", detail)
                    changed = True
                    continue

                dead_weight, detail = check_dead_weight(
                    pos, age_min, pending_priority, pending_count, pending_scanner
                )
                if dead_weight:
                    close_position(pos, "dead_weight_rotation", detail)
                    changed = True

        if changed:
            git_sync("auto: position supervisor close")
    finally:
        release_lock("sm-flip")


if __name__ == "__main__":
    main()
