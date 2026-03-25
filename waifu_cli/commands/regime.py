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
    btc_4h = sc.mcporter_call("market_get_candles", {"asset": "BTC", "interval": "4h", "limit": 10})

    if not btc_4h.get("success", False) and "error" in btc_4h:
        return "BASELINE", "No candle data available"

    candles = btc_4h.get("data", btc_4h.get("candles", []))
    if not candles or len(candles) < 5:
        return "BASELINE", "Insufficient candle data"

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
