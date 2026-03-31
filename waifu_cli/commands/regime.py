"""
regime.py — Regime Classifier command.

Classifies macro market regime as RISK_ON / BASELINE / RISK_OFF
by analyzing BTC candle data.
Ported from scripts/waifu-regime-classifier.sh.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import sync_before, sync_after, acquire_command_lock, release_command_lock


def _classify_regime() -> tuple[str, str]:
    """Fetch BTC candles and classify regime. Returns (mode, reason)."""
    # Use market_get_asset_data with candle_intervals (market_get_candles doesn't exist)
    btc_data = sc.mcporter_call("market_get_asset_data", {
        "asset": "BTC",
        "candle_intervals": ["4h"],
        "include_order_book": False,
        "include_funding": False
    })

    if btc_data.get("error") or not btc_data.get("candles"):
        err = btc_data.get("error", "No candle data in response")
        return "BASELINE", f"No candle data available ({err})"

    candles = btc_data.get("candles", {}).get("4h", [])
    if not candles or len(candles) < 5:
        return "BASELINE", f"Insufficient candle data ({len(candles)} candles)"

    # Extract close prices
    closes = [
        float(c.get("close", c.get("c", 0)))
        for c in candles[-6:]
        if float(c.get("close", c.get("c", 0))) > 0
    ]
    if len(closes) < 3:
        return "BASELINE", "Not enough close prices"

    # MA slope (short vs long)
    ma_short = sum(closes[-3:]) / 3
    ma_long = sum(closes) / len(closes)
    slope = (ma_short - ma_long) / ma_long * 100 if ma_long > 0 else 0

    # Volatility (ATR approximation)
    highs = [float(c.get("high", c.get("h", 0))) for c in candles[-6:]]
    lows = [float(c.get("low", c.get("l", 0))) for c in candles[-6:]]
    atr = sum(h - l for h, l in zip(highs[-3:], lows[-3:])) / 3
    atr_pct = atr / ma_long * 100 if ma_long > 0 else 0

    click.echo(f"  BTC MA slope: {slope:.2f}%, ATR: {atr_pct:.2f}%")
    click.echo(f"  MA_short: {ma_short:.0f}, MA_long: {ma_long:.0f}")

    # Classification
    if abs(slope) > 1.5 and atr_pct < 5.0:
        direction = "BULLISH" if slope > 0 else "BEARISH"
        reason = f"Clear {direction} trend (slope={slope:.1f}%, ATR={atr_pct:.1f}%)"
        return "RISK_ON", reason
    elif atr_pct > 6.0 or (abs(slope) < 0.3 and atr_pct > 3.0):
        reason = f"High volatility/chop (slope={slope:.1f}%, ATR={atr_pct:.1f}%)"
        return "RISK_OFF", reason
    else:
        reason = f"Mixed signals (slope={slope:.1f}%, ATR={atr_pct:.1f}%)"
        return "BASELINE", reason


@click.command()
@click.option("--dry-run", is_flag=True, help="Classify without writing changes.")
def regime(dry_run):
    """Classify macro market regime (RISK_ON / BASELINE / RISK_OFF)."""
    if not acquire_command_lock("regime"):
        click.echo("[regime] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("regime")


def _run(dry_run: bool):
    click.echo(f"[regime] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    # Check arbiter state FIRST — never override catastrophic DD or daily loss
    arb_state = sc.load_json(sc.OUTPUTS_DIR / "arbiter-state.json", default={})
    peak_equity = float(arb_state.get("peakEquity", 0))
    last_equity = float(arb_state.get("lastEquity", 0))
    day_start_equity = float(arb_state.get("dayStartEquity", 0))
    regime_cfg = sc.load_regime()
    guardrails = regime_cfg.get("globalGuardrails", {})

    arbiter_override = False
    arbiter_reason = ""

    if peak_equity > 0 and last_equity > 0:
        peak_dd = (peak_equity - last_equity) / peak_equity * 100
        catastrophic_pct = float(guardrails.get("catastrophicDrawdownPct", 20))
        if peak_dd >= catastrophic_pct:
            arbiter_override = True
            arbiter_reason = f"Catastrophic DD {peak_dd:.1f}% (limit {catastrophic_pct}%)"

    if not arbiter_override and day_start_equity > 0 and last_equity > 0:
        daily_dd = (day_start_equity - last_equity) / day_start_equity * 100
        daily_loss_pct = float(guardrails.get("dailyLossLimitPct", 10))
        if daily_dd >= daily_loss_pct:
            arbiter_override = True
            arbiter_reason = f"Daily loss {daily_dd:.1f}% (limit {daily_loss_pct}%)"

    if arbiter_override:
        click.echo(f"  ARBITER OVERRIDE: forcing RISK_OFF — {arbiter_reason}")
        if not dry_run:
            current = sc.load_regime()
            current["riskMode"] = "RISK_OFF"
            current["updatedAt"] = sc.now_iso()
            current["updatedBy"] = "waifu-regime (arbiter-override)"
            current["reason"] = arbiter_reason
            sc.save_json(sc.RISK_REGIME_FILE, current)
            sync_after(f"waifu regime: RISK_OFF (arbiter override)")
        click.echo(f"[regime] {sc.now_iso()} done (arbiter override)")
        return

    mode, reason = _classify_regime()
    click.echo(f"  -> {mode} ({reason})")

    current = sc.load_regime()
    old_mode = current.get("riskMode", "BASELINE")

    if old_mode != mode:
        click.echo(f"  REGIME CHANGE: {old_mode} -> {mode}")
    else:
        click.echo(f"  Regime unchanged: {mode}")

    if dry_run:
        click.echo(f"  DRY-RUN: would set {mode}")
        return

    # Update regime — preserve existing structure (regimes, globalGuardrails)
    current["riskMode"] = mode
    current["updatedAt"] = sc.now_iso()
    current["updatedBy"] = "waifu-regime"
    current["reason"] = reason
    sc.save_json(sc.RISK_REGIME_FILE, current)

    sync_after(f"waifu regime: {mode}")
    click.echo(f"[regime] {sc.now_iso()} done")
