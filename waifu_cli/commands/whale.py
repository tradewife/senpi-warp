"""
whale.py — Whale Index Manager command.

Daily copy-trade portfolio review and rebalance.
Ported from scripts/waifu-whale-index.sh.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import sync_before, sync_after, acquire_command_lock, release_command_lock


def _score_trader(t: dict) -> float:
    """Score a trader candidate using weighted formula."""
    wr = float(t.get("winRate", 0))
    consistency = float(t.get("consistency", 0))
    hold_time = float(t.get("avgHoldTime", 0))
    drawdown = float(t.get("maxDrawdown", 100))
    return 0.35 * 50 + 0.25 * wr + 0.20 * consistency + 0.10 * min(hold_time, 100) + 0.10 * (100 - drawdown)


@click.command()
@click.option("--dry-run", is_flag=True, help="Analyze without saving changes.")
def whale(dry_run):
    """Daily copy-trade portfolio review and rebalance."""
    if not acquire_command_lock("whale"):
        click.echo("[whale] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("whale")


def _run(dry_run: bool):
    click.echo(f"[whale] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    state_path = sc.OUTPUTS_DIR / "whale-index-state.json"
    state = sc.load_json(state_path, default={
        "slots": [], "watchlist": {}, "notes": [],
        "budget": 1000, "riskTolerance": "conservative", "targetSlots": 2,
    })
    state["updatedAt"] = sc.now_iso()

    # Discover top traders
    traders = sc.mcporter_call("discovery_get_top_traders", {"limit": 50, "timeframe": "30d"})
    top = traders.get("data", traders.get("traders", []))

    if not top:
        click.echo("  No trader data available — skipping")
        state["notes"].append(f"{sc.now_iso()}: No discovery data available")
        if not dry_run:
            sc.save_json(state_path, state)
        return

    click.echo(f"  Discovery returned {len(top)} traders")

    # Filter by risk tolerance
    risk = state.get("riskTolerance", "conservative")
    allowed_labels = {
        "conservative": ["ELITE"],
        "moderate": ["ELITE", "RELIABLE"],
        "aggressive": ["ELITE", "RELIABLE", "BALANCED"],
    }.get(risk, ["ELITE"])

    allowed = [t for t in top if t.get("consistencyLabel", t.get("label", "")) in allowed_labels]
    click.echo(f"  After risk filter ({risk}): {len(allowed)} candidates")

    # Score and sort
    scored = [(_score_trader(t), t) for t in allowed]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Exclude already-active traders
    active_addresses = {s.get("traderAddress", "") for s in state.get("slots", [])}
    new_candidates = [(s, t) for s, t in scored if t.get("address", t.get("traderAddress", "")) not in active_addresses]

    # Monitor existing slots
    for slot in state.get("slots", []):
        addr = slot.get("traderAddress", "")
        trader = next((t for t in top if t.get("address", "") == addr), None)
        if not trader:
            slot["status"] = "WATCH"
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            click.echo(f"  SLOT {addr[:12]}...: WATCH (trader not found)")
            continue

        rank = next((i + 1 for i, t in enumerate(top) if t.get("address", "") == addr), 99)
        slot["lastRank"] = rank
        slot["lastCheckedAt"] = sc.now_iso()

        if rank <= 50:
            slot["status"] = "HOLD"
            slot["watchCount"] = 0
            click.echo(f"  SLOT {addr[:12]}...: HOLD (rank {rank})")
        elif rank <= 75:
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            slot["status"] = "WATCH" if slot["watchCount"] >= 2 else "HOLD"
            click.echo(f"  SLOT {addr[:12]}...: {slot['status']} (rank {rank})")
        else:
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            slot["status"] = "WATCH"
            click.echo(f"  SLOT {addr[:12]}...: WATCH (rank {rank})")

    # Fill empty slots
    target_slots = state.get("targetSlots", 2)
    active_count = sum(1 for s in state.get("slots", []) if s.get("status") in ("HOLD", "WATCH"))
    empty_slots = target_slots - active_count

    if empty_slots > 0 and new_candidates:
        click.echo(f"  Filling {min(empty_slots, len(new_candidates))} empty slot(s)")
        for score, trader in new_candidates[:empty_slots]:
            addr = trader.get("address", trader.get("traderAddress", ""))
            new_slot = {
                "traderAddress": addr,
                "traderLabel": trader.get("consistencyLabel", trader.get("label", "UNKNOWN")),
                "status": "HOLD",
                "watchCount": 0,
                "createdAt": sc.now_iso(),
                "lastCheckedAt": sc.now_iso(),
                "lastRank": next((i + 1 for i, t in enumerate(top) if t.get("address", "") == addr), 99),
                "score": round(score, 1),
                "winRate": trader.get("winRate", 0),
                "totalPnl": trader.get("totalPnl", 0),
            }
            state["slots"].append(new_slot)
            click.echo(f"    Added: {addr[:12]}... (score={score:.1f})")

    click.echo(f"  Portfolio: {len(state['slots'])} slots")

    if dry_run:
        click.echo("  DRY-RUN: state not saved")
        return

    sc.save_json(state_path, state)
    sync_after("waifu whale: daily rebalance")
    click.echo(f"[whale] {sc.now_iso()} done")
