"""
status.py — Read-only system status summary.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc


@click.command()
def status():
    """Show current system state (read-only)."""
    # Regime
    regime = sc.load_regime()
    mode = regime.get("riskMode", "UNKNOWN")
    reason = regime.get("reason", "")
    updated_at = regime.get("updatedAt", "?")

    click.echo(f"{'=' * 50}")
    click.echo(f"  WAIFU STATUS — {sc.now_iso()}")
    click.echo(f"{'=' * 50}")

    # Risk regime
    click.echo(f"\n📊 Regime: {mode}")
    click.echo(f"   Reason: {reason[:80]}")
    click.echo(f"   Updated: {updated_at}")

    # Effective params
    params = sc.current_regime_params()
    click.echo(f"\n⚙️  Effective params:")
    click.echo(f"   Max slots: {params.get('maxSlots', '?')}")
    click.echo(f"   Max leverage: {params.get('maxLeverageCrypto', '?')}x")
    click.echo(f"   Alloc/slot: {params.get('allocPctPerSlot', '?')}%")
    click.echo(f"   Auto-entry: {params.get('autoEntryEnabled', '?')}")
    click.echo(f"   Entries allowed: {params.get('newEntriesAllowed', '?')}")

    # Guardrails
    guardrails = sc.load_global_guardrails()
    click.echo(f"\n🛡  Guardrails:")
    click.echo(f"   Leverage: {guardrails.get('minLeverage', 7)}-{guardrails.get('maxLeverage', 10)}x")
    click.echo(f"   Max positions: {guardrails.get('maxPositionsTotal', 3)}")
    click.echo(f"   Daily loss limit: {guardrails.get('dailyLossLimitPct', 10)}%")
    click.echo(f"   Catastrophic DD: {guardrails.get('catastrophicDrawdownPct', 20)}%")
    click.echo(f"   Cooldown: {guardrails.get('perAssetCooldownMinutes', 120)}min")

    # Open positions
    positions = sc.get_all_open_positions()
    click.echo(f"\n📈 Open positions: {len(positions)}")
    for pos in positions:
        asset = pos.get("asset", "?")
        direction = pos.get("direction", "?")
        roe = float(pos.get("currentRoe", 0) or 0)
        click.echo(f"   {asset} {direction} ({roe:+.1f}% ROE)")

    # Pending entries
    pending = sc.load_pending_entries()
    click.echo(f"\n📋 Pending entries: {len(pending)}")
    for entry in pending[:5]:
        asset = entry.get("asset", entry.get("symbol", "?"))
        scanner = entry.get("scanner", entry.get("source", "?"))
        click.echo(f"   {asset} via {scanner}")
    if len(pending) > 5:
        click.echo(f"   ... and {len(pending) - 5} more")

    # Stale heartbeats
    stale = sc.check_stale_heartbeats()
    if stale:
        click.echo(f"\n⚠️  Stale crons: {', '.join(stale)}")
    else:
        click.echo(f"\n✅ All mechanical crons healthy")

    # Latest report alerts
    report = sc.load_json(sc.OUTPUTS_DIR / "latest-report.json", default={})
    alerts = report.get("alerts", [])
    if alerts:
        click.echo(f"\n🚨 Active alerts:")
        for alert in alerts:
            click.echo(f"   {alert}")

    click.echo(f"\n{'=' * 50}")
