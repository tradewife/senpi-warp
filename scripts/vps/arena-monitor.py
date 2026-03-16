#!/usr/bin/env python3
"""
Arena Monitor — runs every 15 minutes via cron.

Polls the Senpi Predators performance tracker (JSON-RPC 2.0) and writes
a structured summary to arena-state.json for Oz cloud agents to consume
during strategic decisions.
"""

import json
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso,
    load_json, save_json, OUTPUTS_DIR,
)

ARENA_STATE_FILE = OUTPUTS_DIR / "arena-state.json"
ARENA_API_URL = "https://ypofdvbavcdgseguddey.supabase.co/functions/v1/mcp-server"


def rpc_call(tool_name: str, arguments: dict) -> dict | None:
    """Make a JSON-RPC 2.0 call to the Senpi agent tracker."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(
        ARENA_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        log(f"Arena RPC failed ({tool_name}): {e}")
        return None

    try:
        text = body["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log(f"Arena RPC bad response ({tool_name}): {e}")
        return None


def fetch_leaderboard() -> list | None:
    return rpc_call("get_leaderboard", {"sort_by": "roi", "limit": 15})


def fetch_performance(slug: str) -> dict | None:
    return rpc_call("get_performance", {"slug": slug})


def compare_strategies(slugs: list[str]) -> dict | None:
    return rpc_call("compare_strategies", {"slugs": slugs})


def compute_insights(leaderboard: list, top_performers: list) -> dict:
    """Analyze leaderboard data and derive actionable insights."""
    if not leaderboard:
        return {}

    # Identify best strategy
    best = leaderboard[0] if leaderboard else {}
    best_slug = best.get("slug", best.get("name", "unknown"))
    best_roi = float(best.get("roi", best.get("roiPct", 0)))

    # Classify winners vs losers
    winners = []
    losers = []
    for entry in leaderboard:
        roi = float(entry.get("roi", entry.get("roiPct", 0)))
        if roi > 0:
            winners.append(entry)
        else:
            losers.append(entry)

    # Calculate trade frequencies
    def trades_per_day(entry: dict) -> float:
        trades = float(entry.get("totalTrades", entry.get("trades", 0)))
        days = float(entry.get("activeDays", entry.get("days", 1))) or 1
        return trades / days

    winner_tpd = [trades_per_day(w) for w in winners] if winners else [0]
    loser_tpd = [trades_per_day(l) for l in losers] if losers else [0]
    avg_winner_tpd = sum(winner_tpd) / len(winner_tpd)
    avg_loser_tpd = sum(loser_tpd) / len(loser_tpd)

    # Average trades across all
    all_trades = [float(e.get("totalTrades", e.get("trades", 0))) for e in leaderboard]
    avg_trades = sum(all_trades) / len(all_trades) if all_trades else 0

    # Winning / losing traits
    winning_traits = []
    losing_traits = []

    if avg_winner_tpd < avg_loser_tpd and losers:
        winning_traits.append("fewer trades")
        losing_traits.append("over-trading")

    # Check for high-conviction pattern among winners
    if winners:
        avg_winner_trades = sum(float(w.get("totalTrades", w.get("trades", 0))) for w in winners) / len(winners)
        avg_loser_trades = sum(float(l.get("totalTrades", l.get("trades", 0))) for l in losers) / len(losers) if losers else avg_winner_trades
        if avg_winner_trades < avg_loser_trades:
            winning_traits.append("higher conviction")

    # Fee drag detection
    fee_drag_strategies = []
    for entry in leaderboard:
        trades = float(entry.get("totalTrades", entry.get("trades", 0)))
        pnl = float(entry.get("pnl", entry.get("totalPnl", 0)))
        if trades > avg_trades and pnl < 0:
            fee_drag_strategies.append(entry.get("slug", entry.get("name", "unknown")))
            if "fee drag" not in losing_traits:
                losing_traits.append("fee drag")

    # Recommendations
    recommendations = []
    if winning_traits and "fewer trades" in winning_traits:
        recommendations.append(f"Match {best_slug.upper()}'s selectivity")
    if any(p.get("highWaterMark") or p.get("hwm") for p in top_performers if isinstance(p, dict)):
        recommendations.append("Use DSL High Water")
    if fee_drag_strategies:
        recommendations.append(f"Review fee drag on: {', '.join(fee_drag_strategies[:3])}")
    if not recommendations:
        recommendations.append("Continue current approach")

    return {
        "bestStrategy": best_slug,
        "bestRoi": round(best_roi, 2),
        "avgTrades": round(avg_trades),
        "winningTraits": winning_traits or ["higher conviction"],
        "losingTraits": losing_traits or ["over-trading"],
        "recommendations": recommendations,
    }


def main():
    if not acquire_lock("arena-monitor"):
        return

    try:
        # 1. Fetch full leaderboard
        leaderboard = fetch_leaderboard()
        if not leaderboard:
            log("Arena monitor: no leaderboard data — skipping")
            return

        # Normalize: might be nested under a key
        if isinstance(leaderboard, dict):
            leaderboard = leaderboard.get("leaderboard", leaderboard.get("data", []))
        if not isinstance(leaderboard, list):
            log("Arena monitor: unexpected leaderboard format")
            return

        log(f"Arena monitor: fetched {len(leaderboard)} strategies")

        # 2. Fetch detailed performance for top 3
        top_slugs = []
        for entry in leaderboard[:3]:
            slug = entry.get("slug", entry.get("name", ""))
            if slug:
                top_slugs.append(slug)

        top_performers = []
        for slug in top_slugs:
            perf = fetch_performance(slug)
            if perf:
                top_performers.append(perf)

        # 3. Compare top 5
        compare_slugs = []
        for entry in leaderboard[:5]:
            slug = entry.get("slug", entry.get("name", ""))
            if slug:
                compare_slugs.append(slug)

        comparison = {}
        if len(compare_slugs) >= 2:
            comparison = compare_strategies(compare_slugs) or {}

        # 4. Compute insights
        insights = compute_insights(leaderboard, top_performers)

        # 5. Write output
        arena_state = {
            "updatedAt": now_iso(),
            "leaderboard": leaderboard,
            "topPerformers": top_performers,
            "comparison": comparison,
            "insights": insights,
        }
        save_json(ARENA_STATE_FILE, arena_state)
        log(f"Arena monitor: wrote {ARENA_STATE_FILE.name} — "
            f"best={insights.get('bestStrategy')} roi={insights.get('bestRoi')}")

    finally:
        release_lock("arena-monitor")


if __name__ == "__main__":
    main()
