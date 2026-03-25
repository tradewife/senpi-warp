"""
arena.py — Arena Strategy Learner command.

Studies Senpi Predators leaderboard for actionable intelligence.
Ported from scripts/waifu-arena-learner.sh.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import sync_before, sync_after, acquire_command_lock, release_command_lock


@click.command()
@click.option("--dry-run", is_flag=True, help="Analyze without saving changes.")
def arena(dry_run):
    """Study Senpi Predators leaderboard for intelligence."""
    if not acquire_command_lock("arena"):
        click.echo("[arena] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("arena")


def _run(dry_run: bool):
    click.echo(f"[arena] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    # Fetch leaderboard
    leaderboard = sc.mcporter_call("discovery_get_top_traders", {"limit": 50, "timeframe": "30d"})

    # Our stats
    journal = sc.load_trade_journal()
    our_closes = [t for t in journal if t.get("action") == "CLOSE"]
    our_wins = [t for t in our_closes if float(t.get("realizedPnl", 0)) > 0]
    our_wr = len(our_wins) / len(our_closes) * 100 if our_closes else 0
    our_pnl = sum(float(t.get("realizedPnl", 0)) for t in our_closes)

    click.echo(f"  Our stats: {len(our_closes)} closes, {our_wr:.1f}% WR, ${our_pnl:,.2f} PnL")

    # Analyze leaderboard
    top_traders = leaderboard.get("data", leaderboard.get("traders", []))
    recommendations = []

    if not top_traders:
        click.echo("  No leaderboard data available")
        learnings = {
            "generatedAt": sc.now_iso(),
            "recommendations": [],
            "note": "No leaderboard data available",
        }
        if not dry_run:
            sc.save_json(sc.OUTPUTS_DIR / "arena-learnings.json", learnings)
        return

    # Top 5 stats
    top5 = top_traders[:5]
    avg_top5_wr = sum(float(t.get("winRate", 0)) for t in top5) / len(top5) if top5 else 0

    click.echo(f"  Top 5 avg WR: {avg_top5_wr:.1f}%")
    for t in top5[:3]:
        click.echo(f"    {str(t.get('user', t.get('address', '?')))[:16]}... WR={t.get('winRate', 0)}%")

    # Generate recommendations
    if our_wr < 40 and len(our_closes) >= 10:
        recommendations.append({
            "action": "tighten_scores",
            "confidence": "high",
            "reason": f"Win rate {our_wr:.0f}% < 40% across {len(our_closes)} trades. Tighten entry scores.",
            "risk": "reducing",
        })

    if our_wr > 55 and len(our_closes) >= 10:
        recommendations.append({
            "action": "slightly_loosen",
            "confidence": "medium",
            "reason": f"Win rate {our_wr:.0f}% is strong. Could capture more edge (requires manual approval).",
            "risk": "increasing",
        })

    if avg_top5_wr > our_wr + 15:
        recommendations.append({
            "action": "study_top_strategies",
            "confidence": "medium",
            "reason": f"Arena top 5 avg WR ({avg_top5_wr:.0f}%) exceeds ours ({our_wr:.0f}%). Study their patterns.",
            "risk": "neutral",
        })

    if len(our_closes) > 50 and our_pnl < 0:
        recommendations.append({
            "action": "reduce_frequency",
            "confidence": "high",
            "reason": f"{len(our_closes)} trades with negative PnL. Over-trading detected.",
            "risk": "reducing",
        })

    recommendations.append({
        "action": "reminder_max_leverage",
        "confidence": "absolute",
        "reason": "Max leverage is 10x. Never increase. Proven across 22 agents.",
        "risk": "rule",
    })

    # Save learnings
    learnings = {
        "generatedAt": sc.now_iso(),
        "ourStats": {
            "closes": len(our_closes),
            "winRate": round(our_wr, 1),
            "totalPnl": round(our_pnl, 2),
        },
        "arenaTop5AvgWinRate": round(avg_top5_wr, 1),
        "recommendations": recommendations,
        "appliedChanges": [],
    }

    click.echo(f"  Recommendations: {len(recommendations)}")
    for r in recommendations:
        click.echo(f"    [{r['confidence']}] {r['action']}: {r['reason'][:80]}")

    if dry_run:
        click.echo("  DRY-RUN: learnings not saved")
        return

    sc.save_json(sc.OUTPUTS_DIR / "arena-learnings.json", learnings)
    sync_after("waifu arena: update learnings")
    click.echo(f"[arena] {sc.now_iso()} done")
