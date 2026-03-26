#!/usr/bin/env python3
"""
jido.py — Autonomous trade executor with tiered governance.

For every APPROVED signal:
  - If scanner ROI > 15%: execute trade immediately via mcporter_call
  - If scanner ROI < 15%: send Telegram for manual approval
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import (
    sync_before,
    sync_after,
    acquire_command_lock,
    release_command_lock,
)
from waifu_cli.commands.evaluate import TradeEvaluator, DecisionObject, Recommendation
from waifu_cli.safety import GateResult


ARENA_LEARNINGS_FILE = sc.OUTPUTS_DIR / "arena-learnings.json"
ROI_THRESHOLD_AUTO = 0.15


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Process signals without executing trades."
)
def jido(dry_run: bool):
    """Autonomous trade executor with tiered governance."""
    if not acquire_command_lock("jido"):
        click.echo("[jido] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("jido")


def _run(dry_run: bool):
    click.echo(f"[jido] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")
    sync_before()

    regime = sc.load_regime()
    mode = regime.get("riskMode", "BASELINE")
    click.echo(f"[jido] regime: {mode}")

    if mode == "RISK_OFF":
        click.echo("[jido] RISK_OFF — skipping all entries")
        return

    evaluator = TradeEvaluator(dry_run=dry_run)
    decisions = evaluator.process_queue()

    if not decisions:
        click.echo("[jido] No pending entries")
        return

    click.echo(f"[jido] {len(decisions)} decisions to process")

    arena_learnings = sc.load_json(ARENA_LEARNINGS_FILE, default={})

    approved_count = 0
    manual_review_count = 0
    rejected_count = 0

    for decision in decisions:
        signal = decision.signal
        gate_result = decision.gate_result
        scanner = str(
            signal.get(
                "scanner", signal.get("source", signal.get("entryMode", "unknown"))
            )
        ).lower()

        if decision.recommendation == Recommendation.APPROVE:
            scanner_roi = _get_scanner_roi(scanner, arena_learnings)

            if scanner_roi and scanner_roi >= ROI_THRESHOLD_AUTO:
                click.echo(
                    f"  AUTO-EXECUTE {signal.get('asset', signal.get('symbol', ''))} "
                    f"{signal.get('direction', signal.get('side', ''))} @ {gate_result.clamped_leverage}x "
                    f"(scanner={scanner}, ROI={scanner_roi:.1%})"
                )

                if dry_run:
                    click.echo("    DRY-RUN: would execute trade")
                else:
                    _execute_approved_trade(signal, gate_result, scanner)
                approved_count += 1

            else:
                click.echo(
                    f"  MANUAL_REVIEW {signal.get('asset', signal.get('symbol', ''))} "
                    f"(scanner={scanner}, ROI={scanner_roi:.1% if scanner_roi else 'N/A'})"
                )
                _request_manual_approval(signal, gate_result, scanner)
                manual_review_count += 1

        elif decision.recommendation == Recommendation.REJECT:
            reasons = "; ".join(gate_result.reasons)
            click.echo(
                f"  REJECT {signal.get('asset', signal.get('symbol', ''))}: {reasons}"
            )
            rejected_count += 1

        elif decision.recommendation == Recommendation.MANUAL_REVIEW:
            click.echo(
                f"  MANUAL_REVIEW {signal.get('asset', signal.get('symbol', ''))} "
                f"(scanner={scanner}) — requires human decision"
            )
            _request_manual_approval(signal, gate_result, scanner)
            manual_review_count += 1

    remaining = evaluator.remaining
    sc.save_json(sc.PENDING_ENTRIES_FILE, remaining)

    click.echo(
        f"[jido] Summary: {approved_count} auto-executed, {manual_review_count} manual review, "
        f"{rejected_count} rejected, {len(remaining)} remaining"
    )

    if not dry_run:
        sync_after("waifu jido: process pending entries")

    click.echo(f"[jido] {sc.now_iso()} done")


def _get_scanner_roi(scanner: str, arena_learnings: dict) -> Optional[float]:
    """Get scanner ROI from arena-learnings.json arenaTop5 rankings."""
    scanner_name_map = {
        "orca": "Orca",
        "mantis": "Mantis",
        "fox": "Fox",
        "polar": "Polar",
        "komodo": "Komodo",
        "condor": "Condor",
        "sentinel": "Sentinel",
        "rhino": "Rhino",
        "shark": "Shark",
    }
    name_match = scanner_name_map.get(scanner, scanner.capitalize())
    arena_top5 = arena_learnings.get("arenaTop5", [])
    for entry in arena_top5:
        strategy_name = entry.get("strategy", "")
        if name_match.lower() in strategy_name.lower():
            return entry.get("roi", 0) / 100.0
    return None


def _execute_approved_trade(
    signal: dict, gate_result: GateResult, scanner: str
) -> None:
    """Execute trade via mcporter_call with trade lock acquired."""
    strategies = sc.get_enabled_strategies()
    if not strategies:
        click.echo("[jido] No enabled strategies")
        return

    strategy = strategies[0]
    strategy_id = strategy.get("strategyId", "")
    strat_key = strategy.get("_key", "")

    asset = signal.get("asset", signal.get("symbol", ""))
    direction = signal.get("direction", signal.get("side", ""))
    leverage = gate_result.clamped_leverage
    margin = gate_result.effective_margin
    score = float(signal.get("score", signal.get("totalScore", 0)))

    with sc.acquire_trade_lock():
        resp = sc.mcporter_call(
            "strategy_open_position",
            {
                "strategyId": strategy_id,
                "asset": asset,
                "direction": direction,
                "leverage": leverage,
                "marginUsd": margin,
                "lockMode": "pct_of_high_water",
            },
        )

    if resp.get("success", False):
        click.echo(f"    OPENED: {asset} {direction}")
        sc.record_trade(
            {
                "action": "OPEN",
                "asset": asset,
                "direction": direction,
                "leverage": leverage,
                "marginUsd": margin,
                "entrySource": f"jido-{scanner}",
                "strategyKey": strat_key,
                "score": score,
                "realizedPnl": 0,
            }
        )
        sc.send_telegram(
            f"🟢 JIDO AUTO-EXECUTE: {direction} {asset}\n"
            f"Scanner: {scanner} | Score: {score}\n"
            f"Leverage: {leverage}x | Margin: ${margin:.0f}\n"
            f"Strategy: {strategy.get('name', strat_key)}"
        )
    else:
        click.echo(f"    FAILED: {resp.get('error', 'unknown')}")


def _request_manual_approval(
    signal: dict, gate_result: GateResult, scanner: str
) -> None:
    """Send Telegram notification for manual approval."""
    asset = signal.get("asset", signal.get("symbol", ""))
    direction = signal.get("direction", signal.get("side", ""))
    score = float(signal.get("score", signal.get("totalScore", 0)))
    leverage = gate_result.clamped_leverage
    margin = gate_result.effective_margin

    sc.send_telegram(
        f"🔔 JIDO MANUAL APPROVAL REQUEST\n"
        f"Asset: {direction} {asset}\n"
        f"Scanner: {scanner} | Score: {score}\n"
        f"Leverage: {leverage}x | Margin: ${margin:.0f}\n"
        f"Reasons: {', '.join(gate_result.reasons[:4])}\n"
        f"Reply 'approve' to execute or 'reject' to skip."
    )
