def _run(dry_run: bool):
    """Study Senpi Predators leaderboard for intelligence."""
    click.echo(f"[arena] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    try:
        # Fetch leaderboard using correct API
        leaderboard = sc.mcporter_call("arena_leaderboard", {"limit": 50})
        # Check if API call succeeded
        if not leaderboard or leaderboard.get("data", {}).get("entries"):
            click.echo("  No leaderboard data available")
            learnings = {
                "generatedAt": sc.now_iso(),
                "recommendations": [],
                "note": "No leaderboard data available",
            }
            if not dry_run:
                sc.save_json(sc.OUTPUTS_DIR / "arena-learnings.json", learnings)
            return
        # Parse leaderboard entries
        entries = leaderboard.get("data", {}).get("entries", [])
        if not entries:
            click.echo("  No leaderboard entries found")
            return
        # Top 5 stats
        top5 = entries[:5]
        if not top5:
            click.echo("  No top 5 entries found")
            return
        # Calculate average win rate for top 5
        avg_top5_wr = sum(float(e.get("roePct", 0)) for e in top5) / len(top5) if top5 else 0
        click.echo(f"  Top 5 avg WR: {avg_top5_wr:.1f}%")
        # Display detailed info for top 5
        for t in top5:
            # Use xHandle if available, senpiUserName as fallback
            x_handle = t.get("xHandle", t.get("senpiUserName", "?")
            if not x_handle:
                x_handle = t.get("senpiUserName", "?")
            click.echo(f"    {str(t.get('senpiUserName', t.get('xHandle', '?'))[:20]}... roePct: {t.get('roePct')}%")
            click.echo(f"    {str(x_handle, t.get('xHandle', '?') https://twitter.com/{x_handle}")
            else
                click.echo(f"    Tools: {len(t.get('toolsUsed', []))}, skills: {len(t.get('skillsUsed', []))}")
        else
            click.echo("  Top 5 avg WR: N/A")
            return
        # Our stats
        journal = sc.load_trade_journal()
        our_closes = [t for t in journal if t.get("action") == "CLOSE"]
        our_wins = [t for t in our_closes if float(t.get("realizedPnl", 0)) > 0]
        our_wr = len(our_wins) / len(our_closes) * 100 if our_closes else 1.0
        our_pnl = sum(float(t.get("realizedPnl", 0)) for t in our_closes)
        else 0.0
        click.echo(f"  Our stats: {len(our_closes)} closes, {our_wr:.1f}% WR, ${our_pnl:,.2f} PnL")
    # Analyze leaderboard
    top_traders = leaderboard.get("data", {}).get("traders", [])
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
        recommendations = []
        # Top 5 stats
        avg_top5_wr = sum(float(t.get("winRate", 0)) for t in top5) / len(top5) if top5 else 0
            click.echo(f"  Top 5 avg WR: {avg_top5_wr:.1f}%")
            for t in top5[:3]:
                click.echo(f"    {str(t.get('user', t.get('address', '?'))[:16]}... WR={t.get('winRate', 0)}%")
        # Generate recommendations
        if our_wr < 40 and len(our_closes) >= 10:
            recommendations.append({
                "action": "tighten_scores",
                "confidence": "high",
                "reason": f"Win rate {our_wr:.1f}% < 40% across {len(our_closes)} trades. Tighten entry scores.",
                "risk": "reducing",
            })
        if our_wr > 55 and len(our_closes) >= 10:
            recommendations.append({
                "action": "slightly_loosen",
                "confidence": "medium",
                "reason": f"Win rate {our_wr:.1f}% is strong. Could capture more edge (requires manual approval). for better alpha analysis.",
                "risk": "increasing",
            })
        if our_wr < 40 and len(our_closes) >= 10:
            recommendations.append({
                "action": "reduce_frequency",
                "confidence": "high",
                "reason": f"{len(our_closes)} trades with negative PnL. Over-trading detected",
                "risk": "reducing"
            })
        if len(our_closes) > 50 and our_pnl < 0:
            recommendations.append({
                "action": "reduce_frequency",
                "confidence": "high",
                "reason": f"{len(our_closes)} trades with negative PnL. Over-trading detected",
                "risk": "reducing"
            })
        recommendations.append({
            "action": "reminder_max_leverage",
            "confidence": "absolute",
            "reason": "Max leverage is 10x. Never increase. Proven across 22 agents.",
            "risk": "rule"
        })
        # Save learnings
        learnings = {
            "generatedAt": sc.now_iso(),
            "ourStats": {
                "closes": len(our_closes),
                "winRate": round(our_wr, 1),
                "totalPnl": round(our_pnl, 2)
            },
            "arenaTop5AvgWinRate": round(avg_top5_wr, 1),
            "recommendations": recommendations,
            "appliedChanges": []
        }
        if dry_run:
            click.echo("  DRY-RUN: learnings not saved")
            return
        sync_after("waifu arena: update learnings")
        click.echo(f"[arena] {sc.now_iso()} done")
