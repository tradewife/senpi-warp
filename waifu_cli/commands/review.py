"""
review.py — Portfolio Review command.

Checks risk rails, reviews open positions, writes structured report.
Ported from scripts/waifu-portfolio-review.sh.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import sync_before, sync_after, acquire_command_lock, release_command_lock


@click.command()
@click.option("--dry-run", is_flag=True, help="Generate report without saving.")
def review(dry_run):
    """Check risk rails, review open positions, write structured report."""
    if not acquire_command_lock("review"):
        click.echo("[review] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("review")


def _run(dry_run: bool):
    click.echo(f"[review] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    # Get portfolio from Senpi
    portfolio = sc.mcporter_call("account_get_portfolio", {})

    # Load state
    regime = sc.load_regime()
    arbiter = sc.load_json(sc.OUTPUTS_DIR / "arbiter-state.json", default={})
    journal = sc.load_trade_journal()

    # Count open positions
    open_positions = sc.get_all_open_positions()

    # Compute daily PnL
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    daily_closes = [
        t for t in journal
        if t.get("action") == "CLOSE" and t.get("recordedAt", "").startswith(today)
    ]
    daily_pnl = sum(float(t.get("realizedPnl", 0)) for t in daily_closes)
    daily_wins = sum(1 for t in daily_closes if float(t.get("realizedPnl", 0)) > 0)

    # Equity tracking
    equity = portfolio.get("total_balance_usd", arbiter.get("lastEquity", 0))
    peak = arbiter.get("peakEquity", equity)
    if equity and float(equity) > float(peak or 0):
        peak = equity
        arbiter["peakEquity"] = peak
    arbiter["lastEquity"] = equity
    arbiter["lastCheckAt"] = sc.now_iso()

    day_start = arbiter.get("dayStartEquity", equity)
    if arbiter.get("dayStartDate") != today:
        arbiter["dayStartDate"] = today
        arbiter["dayStartEquity"] = equity

    equity_f = float(equity or 0)
    peak_f = float(peak or 0)
    day_start_f = float(day_start or equity_f)

    drawdown_pct = (peak_f - equity_f) / peak_f * 100 if peak_f > 0 else 0
    daily_loss_pct = (day_start_f - equity_f) / day_start_f * 100 if day_start_f > 0 else 0

    # Guardrail check
    guardrails = sc.load_global_guardrails()
    mode = regime.get("riskMode", "BASELINE")
    alerts = []

    if daily_loss_pct >= guardrails.get("dailyLossLimitPct", 10):
        alerts.append(f"DAILY LOSS LIMIT: {daily_loss_pct:.1f}%")
    if drawdown_pct >= guardrails.get("catastrophicDrawdownPct", 20):
        alerts.append(f"CATASTROPHIC DRAWDOWN: {drawdown_pct:.1f}%")

    # Dead weight detection
    dead_weight = []
    for pos in open_positions:
        roe = float(pos.get("currentRoe", 0) or 0)
        sm_conv = float(pos.get("entrySmConviction", 0) or 0)
        opened = pos.get("openedAt", "")
        if opened:
            try:
                opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                minutes_open = (now - opened_dt).total_seconds() / 60
                if roe < -2 and sm_conv == 0 and minutes_open > 30:
                    dead_weight.append(
                        f"{pos.get('asset', '?')} ({roe:.1f}% ROE, {minutes_open:.0f}min)"
                    )
            except (ValueError, TypeError):
                pass

    # Build report
    daily_wr = round(daily_wins / len(daily_closes) * 100, 1) if daily_closes else 0
    report = {
        "generatedAt": sc.now_iso(),
        "regime": mode,
        "equity": round(equity_f, 2),
        "peakEquity": round(peak_f, 2),
        "drawdownPct": round(drawdown_pct, 2),
        "dailyPnl": round(daily_pnl, 2),
        "dailyCloses": len(daily_closes),
        "dailyWinRate": daily_wr,
        "openPositions": len(open_positions),
        "alerts": alerts,
        "deadWeight": dead_weight,
        "guardrails": guardrails,
    }

    click.echo(f"  Regime: {mode}")
    click.echo(f"  Equity: ${equity_f:,.2f} | Peak: ${peak_f:,.2f}")
    click.echo(f"  Drawdown: {drawdown_pct:.1f}% | Daily PnL: ${daily_pnl:,.2f}")
    click.echo(f"  Open: {len(open_positions)} | Daily closes: {len(daily_closes)} ({daily_wr}% WR)")
    if alerts:
        click.echo(f"  ALERTS: {' | '.join(alerts)}")
    if dead_weight:
        click.echo(f"  DEAD WEIGHT: {', '.join(dead_weight)}")

    if dry_run:
        click.echo("  DRY-RUN: report not saved")
        return

    sc.save_json(sc.OUTPUTS_DIR / "arbiter-state.json", arbiter)
    sc.save_json(sc.OUTPUTS_DIR / "latest-report.json", report)

    sync_after("waifu review: update report")
    click.echo(f"[review] {sc.now_iso()} done")
