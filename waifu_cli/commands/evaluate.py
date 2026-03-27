#!/usr/bin/env python3
"""
evaluate.py — Trade Evaluator command.

Processes queued scanner signals and executes approved trades.
Refactored into TradeEvaluator class with process_queue() method.

Strategic Sovereignty: user-rules.json defines trade-level overrides
(fixed TP/SL ROE, partial exits, DSL params) that are injected AFTER
the 10-gate safety pipeline approves a signal. Strategic rules manage
the TRADE; the Arbiter manages the ACCOUNT. Hardcoded safety floors
(max 3 positions, XYZ ban, 7-10x leverage) cannot be bypassed.
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


USER_RULES_FILE = sc.CONFIG_DIR / "user-rules.json"


def build_strategic_overrides(user_rules: dict) -> dict:
    """Build strategic TP/SL/partial overrides from user-rules.json.

    Strategic rules manage individual TRADE behavior (SL ROE, TP ROE,
    partial exits). They NEVER override account-level safety (max positions,
    leverage band, XYZ ban, daily loss limit). Account safety is enforced
    by the 10-gate pipeline in safety.py BEFORE this function is ever called.
    """
    overrides = {}

    fixed_tp = user_rules.get("fixed_tp_roe", {})
    if fixed_tp.get("enabled") and fixed_tp.get("tpRoePct") is not None:
        overrides["strategicTpRoe"] = float(fixed_tp["tpRoePct"])

    fixed_sl = user_rules.get("fixed_sl_roe", {})
    if fixed_sl.get("enabled") and fixed_sl.get("slRoePct") is not None:
        overrides["strategicSlRoe"] = float(fixed_sl["slRoePct"])

    partial_tp = user_rules.get("partial_tp", {})
    if partial_tp.get("enabled"):
        overrides["partialTp"] = {
            "tp1RoePct": float(partial_tp.get("tp1RoePct", 0)),
            "tp1ClosePct": float(partial_tp.get("tp1ClosePct", 50)),
            "tp2RoePct": float(partial_tp.get("tp2RoePct", 0)),
            "tp2ClosePct": float(partial_tp.get("tp2ClosePct", 25)),
        }

    partial_sl = user_rules.get("partial_sl", {})
    if partial_sl.get("enabled"):
        overrides["partialSl"] = {
            "sl1RoePct": float(partial_sl.get("sl1RoePct", 0)),
            "sl1ClosePct": float(partial_sl.get("sl1ClosePct", 50)),
            "sl2RoePct": float(partial_sl.get("sl2RoePct", 0)),
            "sl2ClosePct": float(partial_sl.get("sl2ClosePct", 25)),
        }

    dsl_override = user_rules.get("dsl_override", {})
    if dsl_override.get("enabled"):
        overrides["dslParams"] = dsl_override.get("overrides", {})

    return overrides


def build_dsl_state(
    asset: str,
    direction: str,
    entry_price: float,
    leverage: int,
    margin: float,
    strategy_id: str,
    strat_key: str,
    scanner: str,
    score: float,
    strategic: dict,
) -> dict:
    """Build DSL state file for a newly opened position.

    Includes strategic overrides from user-rules.json. These are
    trade-level parameters that sit above the DSL defaults but below
    the account-level safety floor.
    """
    dsl = {
        "active": True,
        "asset": asset,
        "direction": direction,
        "entryPrice": entry_price,
        "leverage": leverage,
        "margin": margin,
        "strategyId": strategy_id,
        "strategyKey": strat_key,
        "scanner": scanner,
        "entryScore": score,
        "phase": 1,
        "highWaterPrice": entry_price,
        "highWaterRoe": 0,
        "currentTierIndex": -1,
        "currentBreachCount": 0,
        "lockMode": "pct_of_high_water",
        "phase2TriggerRoe": 7,
        "createdAt": sc.now_iso(),
        "highWaterUpdatedAt": sc.now_iso(),
        "phase1": {
            "absoluteFloorRoe": -20,
            "hardTimeoutSec": 2700,
            "weakPeakCutSec": 1800,
            "deadWeightCutMin": 15,
        },
        "tiers": [
            {"triggerPct": 5, "lockHwPct": 20, "consecutiveBreachesRequired": 1},
            {"triggerPct": 10, "lockHwPct": 40, "consecutiveBreachesRequired": 1},
            {"triggerPct": 20, "lockHwPct": 55, "consecutiveBreachesRequired": 2},
            {"triggerPct": 30, "lockHwPct": 70, "consecutiveBreachesRequired": 2},
        ],
        "stagnationTp": {"enabled": True, "roeMin": 10, "hwStaleMin": 45},
    }

    if "strategicSlRoe" in strategic:
        dsl["strategicSlRoe"] = strategic["strategicSlRoe"]
    if "strategicTpRoe" in strategic:
        dsl["strategicTpRoe"] = strategic["strategicTpRoe"]
    if "partialTp" in strategic:
        dsl["partialTp"] = strategic["partialTp"]
    if "partialSl" in strategic:
        dsl["partialSl"] = strategic["partialSl"]
    if "dslParams" in strategic:
        dsl.update(strategic["dslParams"])

    return dsl


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

    The 10-gate pipeline (safety.py) enforces account-level safety BEFORE any
    strategic overrides are applied. Strategic rules from user-rules.json only
    affect trade-level behavior (TP/SL, partial exits, DSL params).
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
        self.user_rules: dict = {}
        self.effective_min_scores: dict = {}

    def process_queue(self) -> List[DecisionObject]:
        """
        Read pending-entries.json and apply the 10 gates.
        Returns a list of DecisionObjects containing signal, gate results, and recommendation.
        """
        decisions: List[DecisionObject] = []

        # Hot-load user rules at the start of every run
        self.user_rules = sc.load_json(USER_RULES_FILE, default={})
        evaluate_rules = self.user_rules.get("evaluate", {})
        user_min_score = evaluate_rules.get("minScore")

        if user_min_score is not None:
            self.effective_min_scores = {
                k: int(user_min_score) for k in self.MIN_SCORES
            }
            click.echo(f"[evaluate] user minScore override: {user_min_score}")
        else:
            self.effective_min_scores = dict(self.MIN_SCORES)

        strategic = build_strategic_overrides(self.user_rules)
        active_overrides = list(strategic.keys()) if strategic else []
        if active_overrides:
            click.echo(f"[evaluate] strategic overrides: {', '.join(active_overrides)}")

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
            # 10-gate safety pipeline runs FIRST (account-level: positions, leverage, XYZ, cooldown)
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
                # Strategic overrides applied AFTER gates pass (trade-level only)
                self._handle_approval(
                    entry, strategy, gate, strat_key, strategy_id, strategic
                )
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
        min_score = self.effective_min_scores.get(scanner, 6)

        if score < min_score:
            return Recommendation.REJECT

        # Hardcoded leverage band — CANNOT be overridden by strategic rules
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
        strategic: dict,
    ):
        """Handle an approved signal - execute trade if not dry-run.

        Strategic overrides (TP/SL, partial exits) are injected into the
        position params AFTER the 10-gate pipeline has already enforced
        account-level safety. These trade-level rules manage the position,
        while the Arbiter manages the account.
        """
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

        if strategic:
            parts = []
            if "strategicSlRoe" in strategic:
                parts.append(f"SL={strategic['strategicSlRoe']}%")
            if "strategicTpRoe" in strategic:
                parts.append(f"TP={strategic['strategicTpRoe']}%")
            if "partialTp" in strategic:
                parts.append("partialTP")
            if "partialSl" in strategic:
                parts.append("partialSL")
            if parts:
                click.echo(f"    strategic: {', '.join(parts)}")

        if self.dry_run:
            click.echo(f"    DRY-RUN: would open {asset} {direction} @ {leverage}x")
            return

        try:
            # Base position params (safety-guaranteed by 10-gate pipeline)
            position_params = {
                "strategyId": strategy_id,
                "asset": asset,
                "direction": direction,
                "leverage": leverage,
                "marginUsd": margin,
                "lockMode": "pct_of_high_water",
            }

            # Inject strategic TP/SL/partial overrides (trade-level, above DSL defaults)
            if "strategicSlRoe" in strategic:
                position_params["stopLossRoe"] = strategic["strategicSlRoe"]
            if "strategicTpRoe" in strategic:
                position_params["takeProfitRoe"] = strategic["strategicTpRoe"]
            if "partialTp" in strategic:
                position_params["partialTp"] = strategic["partialTp"]
            if "partialSl" in strategic:
                position_params["partialSl"] = strategic["partialSl"]

            resp = sc.mcporter_call("strategy_open_position", position_params)

            if resp.get("success", False):
                entry_price = float(resp.get("entryPrice", 0))
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

                # Save DSL state with strategic overrides
                dsl = build_dsl_state(
                    asset,
                    direction,
                    entry_price,
                    leverage,
                    margin,
                    strategy_id,
                    strat_key,
                    scanner,
                    score,
                    strategic,
                )
                state_dir = sc.get_strategy_state_dir(strat_key)
                sc.save_json(state_dir / f"dsl-{asset}.json", dsl)

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
