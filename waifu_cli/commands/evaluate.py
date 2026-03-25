"""
evaluate.py — Trade Evaluator command.

Validates queued scanner signals and executes approved trades.
Ported from scripts/waifu-trade-evaluator.sh with centralized safety gates.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import sync_before, sync_after, acquire_command_lock, release_command_lock
from waifu_cli.safety import evaluate_entry


@click.command()
@click.option("--dry-run", is_flag=True, help="Evaluate signals without executing trades.")
def evaluate(dry_run):
    """Process pending scanner signals and execute approved trades."""
    if not acquire_command_lock("evaluate"):
        click.echo("[evaluate] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("evaluate")


def _run(dry_run: bool):
    click.echo(f"[evaluate] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    # Check regime
    params = sc.current_regime_params()
    regime = sc.load_regime()
    mode = regime.get("riskMode", "BASELINE")
    click.echo(f"[evaluate] regime: {mode}")

    if mode == "RISK_OFF":
        click.echo("[evaluate] RISK_OFF — skipping all entries")
        return

    # Load pending entries
    pending = sc.load_pending_entries()
    if not pending:
        click.echo("[evaluate] No pending entries")
        return

    click.echo(f"[evaluate] {len(pending)} pending entries")

    # Get enabled strategy
    strategies = sc.get_enabled_strategies()
    if not strategies:
        click.echo("[evaluate] No enabled strategies")
        return

    strategy = strategies[0]
    strat_key = strategy["_key"]
    strategy_id = strategy.get("strategyId", "")
    click.echo(f"[evaluate] Using strategy: {strat_key} ({strategy_id[:12]}...)")

    processed = []
    journal = sc.load_trade_journal()
    alloc_pct = float(params.get("allocPctPerSlot", 25))

    for entry in pending:
        asset = entry.get("asset", entry.get("symbol", ""))
        direction = entry.get("direction", entry.get("side", ""))
        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()

        # Run centralized safety pipeline
        gate = evaluate_entry(entry, strategy)

        if not gate.approved:
            reasons = "; ".join(gate.reasons)
            click.echo(f"  REJECT {asset}: {reasons}")
            continue

        leverage = gate.clamped_leverage
        margin = entry.get("marginUsd", 0) or int(alloc_pct)
        score = float(entry.get("score", entry.get("totalScore", 0)))

        click.echo(f"  APPROVE {asset} {direction} @ {leverage}x (score={score}, scanner={scanner})")

        if dry_run:
            click.echo(f"    DRY-RUN: would open {asset} {direction} @ {leverage}x")
            processed.append(entry)
            continue

        # Execute trade via mcporter
        try:
            resp = sc.mcporter_call("strategy_open_position", {
                "strategyId": strategy_id,
                "asset": asset,
                "direction": direction,
                "leverage": leverage,
                "marginUsd": margin,
                "lockMode": "pct_of_high_water",
            })

            if resp.get("success", False):
                click.echo(f"    OPENED: {asset} {direction}")
                sc.record_trade({
                    "action": "OPEN",
                    "asset": asset,
                    "direction": direction,
                    "leverage": leverage,
                    "marginUsd": margin,
                    "entrySource": scanner,
                    "strategyKey": strat_key,
                    "score": score,
                    "realizedPnl": 0,
                })
            else:
                click.echo(f"    FAILED: {resp.get('error', 'unknown')}")
        except Exception as e:
            click.echo(f"    ERROR: {e}")

        processed.append(entry)

    # Remove processed entries from queue
    remaining = [e for e in pending if e not in processed]
    sc.save_pending_entries(remaining)

    click.echo(f"[evaluate] {len(processed)} processed, {len(remaining)} remaining")

    if not dry_run:
        sync_after("waifu evaluate: process pending entries")

    click.echo(f"[evaluate] {sc.now_iso()} done")
