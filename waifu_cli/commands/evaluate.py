#!/usr/bin/env python3
"""
evaluate.py — Trade Evaluator command.

Processes queued scanner signals and executes approved trades.
Refactored into TradeEvaluator class with process_queue() method.
"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import senpi_common as sc
from waifu_cli.runtime import (
    sync_before,
    sync_after,
    acquire_command_lock,
    release_command_lock,
)
from waifu_cli.safety import evaluate_entry, GateResult


class Recommendation(Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass
class DecisionObject:
    signal: dict
    gate_result: GateResult
    recommendation: Recommendation
    reasons: List[str] = field(default_factory=list)


class TradeEvaluator:
    """
    TradeEvaluator processes pending scanner signals through the 10-gate pipeline
    and returns DecisionObjects with recommendations (APPROVE, REJECT, MANUAL_REVIEW).
    """

    PENDING_ENTRIES_FILE = sc.POSITION_STATE_DIR / "pending-entries.json"

    MIN_SCORES = {
        "orca": 6,
        "mantis": 7,
        "fox": 7,
        "komodo": 10,
        "condor": 10,
        "polar": 10,
        "sentinel": 5,
        "rhino": 5,
    }

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.processed: List[dict] = []
        self.remaining: List[dict] = []

    def process_queue(self) -> List[DecisionObject]:
        """
        Read pending-entries.json and apply the 10 gates.
        Returns a list of DecisionObjects containing signal, gate results, and recommendation.
        """
        decisions: List[DecisionObject] = []

        pending = sc.load_json(self.PENDING_ENTRIES_FILE, default=[])
        if not pending:
            return decisions

        regime = sc.load_regime()
        mode = regime.get("riskMode", "BASELINE")

        if mode == "RISK_OFF":
            return decisions

        params = sc.current_regime_params()
        strategies = sc.get_enabled_strategies()
        if not strategies:
            return decisions

        strategy = strategies[0]
        strat_key = strategy["_key"]
        strategy_id = strategy.get("strategyId", "")

        for entry in pending:
            gate = evaluate_entry(entry, strategy)
            recommendation = self._determine_recommendation(entry, gate, strategy)
            decision = DecisionObject(
                signal=entry,
                gate_result=gate,
                recommendation=recommendation,
                reasons=list(gate.reasons),
            )
            decisions.append(decision)

            if recommendation == Recommendation.APPROVE:
                self._handle_approval(entry, strategy, gate, strat_key, strategy_id)
                self.processed.append(entry)
            elif recommendation == Recommendation.MANUAL_REVIEW:
                self._handle_manual_review(entry, gate)
                self.remaining.append(entry)
            else:
                self.remaining.append(entry)

        sc.save_json(self.PENDING_ENTRIES_FILE, self.remaining)
        return decisions

    def _determine_recommendation(
        self, entry: dict, gate: GateResult, strategy: dict
    ) -> Recommendation:
        """Determine recommendation based on gate results and additional logic."""
        if not gate.approved:
            return Recommendation.REJECT

        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()

        for s_name in self.MIN_SCORES:
            if s_name in scanner:
                scanner = s_name
                break

        score = float(entry.get("score", entry.get("totalScore", 0)))
        min_score = self.MIN_SCORES.get(scanner, 6)

        if score < min_score:
            return Recommendation.REJECT

        if gate.clamped_leverage < 7 or gate.clamped_leverage > 10:
            return Recommendation.REJECT

        strategy_id = strategy.get("strategyId", "")
        if not strategy_id or strategy_id.startswith("REPLACE"):
            return Recommendation.REJECT

        brain = sc.load_brain_state()
        blocked = brain.get("blockedScanners", []) if isinstance(brain, dict) else []
        if scanner in blocked:
            return Recommendation.REJECT

        return Recommendation.APPROVE

    def _handle_approval(
        self,
        entry: dict,
        strategy: dict,
        gate: GateResult,
        strat_key: str,
        strategy_id: str,
    ):
        """Handle an approved signal - execute trade if not dry-run."""
        asset = entry.get("asset", entry.get("symbol", ""))
        direction = entry.get("direction", entry.get("side", ""))
        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()
        score = float(entry.get("score", entry.get("totalScore", 0)))
        leverage = gate.clamped_leverage
        margin = gate.effective_margin

        click.echo(
            f"  APPROVE {asset} {direction} @ {leverage}x (score={score}, scanner={scanner})"
        )

        if self.dry_run:
            click.echo(f"    DRY-RUN: would open {asset} {direction} @ {leverage}x")
            return

        try:
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
                        "entrySource": scanner,
                        "strategyKey": strat_key,
                        "score": score,
                        "realizedPnl": 0,
                    }
                )
            else:
                click.echo(f"    FAILED: {resp.get('error', 'unknown')}")
        except Exception as e:
            click.echo(f"    ERROR: {e}")

    def _handle_manual_review(self, entry: dict, gate: GateResult):
        """Handle a signal requiring manual review - send Telegram notification."""
        asset = entry.get("asset", entry.get("symbol", ""))
        direction = entry.get("direction", entry.get("side", ""))
        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()
        score = float(entry.get("score", entry.get("totalScore", 0)))

        click.echo(
            f"  MANUAL_REVIEW {asset} {direction} (score={score}, scanner={scanner})"
        )

        sc.send_telegram(
            f"📋 *MANUAL REVIEW REQUIRED*\n"
            f"Scanner: {scanner}\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Score: {score}\n"
            f"Reasons: {', '.join(gate.reasons[:4])}"
        )


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Evaluate signals without executing trades."
)
def evaluate(dry_run):
    """Process pending scanner signals and execute approved trades."""
    if not acquire_command_lock("evaluate"):
        click.echo("[evaluate] Another instance running — skipping")
        return

    try:
        click.echo(
            f"[evaluate] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}"
        )
        sync_before()

        params = sc.current_regime_params()
        regime = sc.load_regime()
        mode = regime.get("riskMode", "BASELINE")
        click.echo(f"[evaluate] regime: {mode}")

        if mode == "RISK_OFF":
            click.echo("[evaluate] RISK_OFF — skipping all entries")
            return

        evaluator = TradeEvaluator(dry_run=dry_run)
        decisions = evaluator.process_queue()

        processed_count = len(evaluator.processed)
        remaining_count = len(evaluator.remaining)

        click.echo(
            f"[evaluate] {processed_count} processed, {remaining_count} remaining"
        )

        if not dry_run:
            sync_after("waifu evaluate: process pending entries")

        click.echo(f"[evaluate] {sc.now_iso()} done")
    finally:
        release_command_lock("evaluate")
