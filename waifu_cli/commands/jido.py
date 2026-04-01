#!/usr/bin/env python3
"""
jido.py — Autonomous trade executor with tiered governance.

For every APPROVED signal:
  - If scanner ROI > threshold: execute trade immediately via mcporter_call
  - If scanner ROI < threshold: send Telegram for manual approval

Strategic Sovereignty: user-rules.json overrides (TP/SL, partial exits)
are injected into the trade execution AFTER the 10-gate safety pipeline
approves the signal. These trade-level rules cannot bypass account-level
safety (max 3 positions, XYZ ban, 7-10x leverage).
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
from waifu_cli.commands.evaluate import (
    TradeEvaluator,
    DecisionObject,
    Recommendation,
    build_strategic_overrides,
    build_dsl_state,
)
from waifu_cli.safety import GateResult


ARENA_LEARNINGS_FILE = sc.OUTPUTS_DIR / "arena-learnings.json"
USER_RULES_FILE = sc.CONFIG_DIR / "user-rules.json"
DEFAULT_ROI_THRESHOLD = 0.15


def _load_user_rules() -> dict:
    """Hot-reload user rules from config/user-rules.json."""
    try:
        return sc.load_json(USER_RULES_FILE, default={})
    except Exception:
        return {}


def _get_roi_threshold() -> float:
    """Get Jido ROI threshold from user rules, with fallback to default."""
    rules = _load_user_rules()
    jido_rules = rules.get("jido", {})
    return float(jido_rules.get("roi_threshold_auto", DEFAULT_ROI_THRESHOLD))


def _get_jido_auto_execute_enabled() -> bool:
    """Check if Jido auto-execute is enabled from user rules."""
    rules = _load_user_rules()
    jido_rules = rules.get("jido", {})
    return bool(jido_rules.get("autoExecuteEnabled", True))


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

    user_rules = _load_user_rules()
    roi_threshold = float(
        user_rules.get("jido", {}).get("roi_threshold_auto", DEFAULT_ROI_THRESHOLD)
    )
    auto_execute_enabled = bool(
        user_rules.get("jido", {}).get("autoExecuteEnabled", True)
    )
    strategic = build_strategic_overrides(user_rules)

    click.echo(
        f"[jido] rules: roi_threshold={roi_threshold:.2f}, auto_execute={auto_execute_enabled}"
    )
    active_overrides = list(strategic.keys()) if strategic else []
    if active_overrides:
        click.echo(f"[jido] strategic overrides: {', '.join(active_overrides)}")

    regime = sc.load_regime()
    mode = regime.get("riskMode", "BASELINE")
    click.echo(f"[jido] regime: {mode}")

    if mode == "RISK_OFF":
        click.echo("[jido] RISK_OFF — skipping all entries")
        return

    if not auto_execute_enabled:
        click.echo(
            "[jido] Auto-execute disabled by user rules — all signals require manual approval"
        )

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

            if auto_execute_enabled and scanner_roi and scanner_roi >= roi_threshold:
                click.echo(
                    f"  AUTO-EXECUTE {signal.get('asset', signal.get('symbol', ''))} "
                    f"{signal.get('direction', signal.get('side', ''))} @ {gate_result.clamped_leverage}x "
                    f"(scanner={scanner}, ROI={scanner_roi:.1%})"
                )

                if dry_run:
                    click.echo("    DRY-RUN: would execute trade")
                else:
                    _execute_approved_trade(signal, gate_result, scanner, strategic)
                approved_count += 1

            else:
                reason = (
                    "auto-execute disabled"
                    if not auto_execute_enabled
                    else f"ROI {scanner_roi:.1% if scanner_roi else 'N/A'} < {roi_threshold:.0%}"
                )
                click.echo(
                    f"  MANUAL_REVIEW {signal.get('asset', signal.get('symbol', ''))} "
                    f"(scanner={scanner}, {reason})"
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

    # --- SUGURU: optional LLM scan ---
    suguru_enabled = bool(user_rules.get("jido", {}).get("suguru_enabled", False))
    if suguru_enabled:
        suguru_result = _run_suguru_pipeline(dry_run, user_rules)
        if suguru_result:
            approved_count += 1

    click.echo(
        f"[jido] Summary: {approved_count} auto-executed, {manual_review_count} manual review, "
        f"{rejected_count} rejected, {len(remaining)} remaining"
    )

    if not dry_run:
        sync_after("waifu jido: process pending entries")

    click.echo(f"[jido] {sc.now_iso()} done")


def _run_suguru_pipeline(dry_run: bool, user_rules: dict) -> bool:
    """Run suguru scan → hermes decide → auto-execute. Returns True if a trade was executed."""
    import json as _json
    import subprocess as _sp

    suguru_script = sc.STATE_DIR / "scripts" / "vps" / "suguru.py"
    decide_script = sc.STATE_DIR / "scripts" / "vps" / "suguru_decide.py"

    click.echo("[jido-suguru] Running scan...")

    # Step 1: Scan
    env = {**sc.os.environ}
    if "SENPI_WAIFU_DIR" not in env:
        env["SENPI_WAIFU_DIR"] = str(sc.STATE_DIR)

    scan_result = _sp.run(
        ["python3", str(suguru_script), "--scan-only"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if scan_result.returncode != 0:
        click.echo(f"[jido-suguru] Scan failed: {scan_result.stderr[:200]}")
        return False

    candidates_file = sc.OUTPUTS_DIR / "suguru-candidates.json"
    try:
        candidates = _json.loads(candidates_file.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        click.echo("[jido-suguru] No candidates file")
        return False

    cands = candidates.get("candidates", [])
    if not cands:
        click.echo("[jido-suguru] 0 candidates — no tradeable signals")
        return False

    click.echo(f"[jido-suguru] {len(cands)} candidates found")

    # Step 2: Hermes decides
    click.echo("[jido-suguru] Hermes deliberating...")
    decide_result = _sp.run(
        ["python3", str(decide_script)],
        capture_output=True, text=True, timeout=120, env=env,
    )

    rec_file = sc.OUTPUTS_DIR / "suguru-recommendation.json"
    try:
        rec = _json.loads(rec_file.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        click.echo("[jido-suguru] No recommendation file")
        return False

    if rec.get("recommendation") != "TRADE":
        reason = rec.get("reasoning", "no reason")[:100]
        click.echo(f"[jido-suguru] Hermes: {rec.get('recommendation', 'UNKNOWN')} — {reason}")
        return False

    # Step 3: Auto-execute using user's risk settings
    tp = rec.get("trade_params", {})
    asset = rec.get("asset", "")
    direction = rec.get("direction", "")
    confidence = float(rec.get("confidence", 0))

    click.echo(
        f"[jido-suguru] Hermes recommends: {direction} {asset} "
        f"(confidence={confidence:.0%})"
    )

    if dry_run:
        click.echo(f"[jido-suguru] DRY-RUN: would execute {direction} {asset}")
        return False

    # Apply user's risk settings from user-rules.json (flat under jido section)
    max_leverage = int(user_rules.get("jido", {}).get("suguru_max_leverage", 8))
    max_margin_pct = float(user_rules.get("jido", {}).get("suguru_max_margin_pct", 25))
    min_confidence = float(user_rules.get("jido", {}).get("suguru_min_confidence", 0.5))

    if confidence < min_confidence:
        click.echo(
            f"[jido-suguru] Confidence {confidence:.0%} < min {min_confidence:.0%} — skipping"
        )
        return False

    strategies = sc.get_enabled_strategies()
    if not strategies:
        click.echo("[jido-suguru] No enabled strategies")
        return False

    strategy = strategies[0]
    strategy_id = strategy.get("strategyId", "")
    strat_key = strategy.get("_key", "")

    leverage = min(int(rec.get("leverage", 8)), max_leverage)
    # Clamp leverage to 7-10x per guardrails
    leverage = max(7, min(leverage, 10))

    # Calculate margin from user's max_margin_pct
    account_equity = candidates.get("account_equity", 100)
    margin = account_equity * max_margin_pct / 100

    position_params = {
        "strategyId": strategy_id,
        "asset": asset,
        "direction": direction,
        "leverage": leverage,
        "marginUsd": margin,
        "lockMode": "pct_of_high_water",
    }

    # Inject strategic overrides if set
    strategic = build_strategic_overrides(user_rules)
    if strategic:
        if "strategicSlRoe" in strategic:
            position_params["stopLossRoe"] = strategic["strategicSlRoe"]
        if "strategicTpRoe" in strategic:
            position_params["takeProfitRoe"] = strategic["strategicTpRoe"]

    with sc.acquire_trade_lock():
        resp = sc.mcporter_call("strategy_open_position", position_params)

    if resp.get("success", False):
        entry_price = float(resp.get("entryPrice", 0))
        click.echo(f"[jido-suguru] OPENED: {asset} {direction} @ {leverage}x")

        sc.record_trade({
            "action": "OPEN",
            "asset": asset,
            "direction": direction,
            "leverage": leverage,
            "marginUsd": margin,
            "entrySource": "jido-suguru",
            "strategyKey": strat_key,
            "score": rec.get("trade_params", {}).get("gss", 0),
            "realizedPnl": 0,
        })

        dsl = build_dsl_state(
            asset, direction, entry_price, leverage, margin,
            strategy_id, strat_key, "suguru",
            rec.get("trade_params", {}).get("gss", 0),
            strategic,
        )
        state_dir = sc.get_strategy_state_dir(strat_key)
        sc.save_json(state_dir / f"dsl-{asset}.json", dsl)

        sc.send_telegram(
            f"🧠 SUGURU AUTO-EXECUTE: {direction} {asset}\n"
            f"Hermes confidence: {confidence:.0%}\n"
            f"Leverage: {leverage}x | Margin: ${margin:.0f}\n"
            f"Reasoning: {rec.get('reasoning', '')[:100]}"
        )
        return True
    else:
        click.echo(f"[jido-suguru] FAILED: {resp.get('error', 'unknown')}")
        return False


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
    signal: dict, gate_result: GateResult, scanner: str, strategic: dict
) -> None:
    """Execute trade via mcporter_call with trade lock acquired.

    Strategic overrides (TP/SL, partial exits) are injected into the
    position params. These trade-level rules sit above the DSL defaults
    but below the account-level safety floor enforced by the 10-gate pipeline.
    """
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

    # Base position params (safety-guaranteed by 10-gate pipeline)
    position_params = {
        "strategyId": strategy_id,
        "asset": asset,
        "direction": direction,
        "leverage": leverage,
        "marginUsd": margin,
        "lockMode": "pct_of_high_water",
    }

    # Inject strategic TP/SL/partial overrides (trade-level only)
    if "strategicSlRoe" in strategic:
        position_params["stopLossRoe"] = strategic["strategicSlRoe"]
    if "strategicTpRoe" in strategic:
        position_params["takeProfitRoe"] = strategic["strategicTpRoe"]
    if "partialTp" in strategic:
        position_params["partialTp"] = strategic["partialTp"]
    if "partialSl" in strategic:
        position_params["partialSl"] = strategic["partialSl"]

    with sc.acquire_trade_lock():
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
                "entrySource": f"jido-{scanner}",
                "strategyKey": strat_key,
                "score": score,
                "realizedPnl": 0,
            }
        )

        # Save DSL state with strategic overrides (DSL v1.1.1 pattern)
        wallet = strategy.get("wallet", "")
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
            wallet=wallet,
        )
        state_dir = sc.get_strategy_state_dir(strat_key)
        sc.save_json(state_dir / f"dsl-{asset}.json", dsl)

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
