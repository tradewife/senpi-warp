#!/usr/bin/env python3
"""
suguru_decide.py — Hermes decision layer for suguru candidates.

Reads suguru-candidates.json, sends to Hermes for APPROVE/REJECT/DEFER
decisions, writes suguru-approved.json for execution.

Usage:
    python3 scripts/vps/suguru_decide.py
    python3 scripts/vps/suguru_decide.py --dry-run   # show what hermes would approve
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
OUTPUTS_DIR = STATE_DIR / "outputs"
CONFIG_DIR = STATE_DIR / "config"

CANDIDATES_FILE = OUTPUTS_DIR / "suguru-candidates.json"
APPROVED_FILE = OUTPUTS_DIR / "suguru-approved.json"
REGIME_FILE = CONFIG_DIR / "risk-regime.json"
BRAIN_FILE = OUTPUTS_DIR / "autonomous-brain.json"

DRY_RUN = "--dry-run" in sys.argv


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def find_hermes() -> str:
    """Find hermes binary."""
    hermes = os.environ.get("HERMES_BIN_PATH", "/usr/local/bin/hermes")
    if os.path.isfile(hermes):
        return hermes
    found = shutil.which("hermes")
    if found:
        return found
    raise RuntimeError("hermes binary not found")


def build_decision_prompt(candidates: dict, regime: dict, brain: dict) -> str:
    """Build the prompt for hermes to evaluate suguru candidates."""
    risk_mode = regime.get("riskMode", "UNKNOWN")
    equity = candidates.get("account_equity", 0)
    slots = candidates.get("available_slots", 0)
    cands = candidates.get("candidates", [])

    if not cands:
        return "No candidates to evaluate. Return: {\"verdicts\": [], \"reason\": \"no candidates\"}"

    # Build candidate summaries
    cand_lines = []
    for i, c in enumerate(cands):
        scores = c.get("sub_scores", {})
        cand_lines.append(
            f"{i+1}. {c['direction']} {c['asset']} | GSS={c['gss']:.2f} | "
            f"px={c['entry_price']} | lev={c['leverage']}x | "
            f"margin=${c['margin_usd']:.0f} | risk={c['risk_pct']:.1f}% | "
            f"netRR={c['net_rr']:.2f} | funding={c.get('funding', 0):.4f}\n"
            f"   sub-scores: scanner_confluence={scores.get('scanner_confluence', 0):.2f}, "
            f"SM_whale_bias={scores.get('SM_whale_bias', 0):.2f}, "
            f"momentum={scores.get('momentum', 0):.2f}, "
            f"regime_align={scores.get('regime_align', 0):.2f}\n"
            f"   scanners: {json.dumps(c.get('scanner_bias', {}), separators=(',', ':'))}"
        )

    brain_mode = brain.get("mode", "UNKNOWN")
    brain_block = brain.get("blockNewEntries", False)

    prompt = f"""You are SUGURU, a Hyperliquid perps trading decision engine.

CURRENT STATE:
- Risk regime: {risk_mode}
- Brain mode: {brain_mode}, block={brain_block}
- Account equity: ${equity}
- Available slots: {slots}
- Candidates scored: {len(cands)}

CANDIDATES:
{chr(10).join(cand_lines)}

TASK: Evaluate each candidate and decide APPROVE, REJECT, or DEFER.

DECISION CRITERIA (weight in order):
1. Macro context — Is BTC at a key level? Weekend/holiday liquidity?
2. Correlation risk — Would this overlap existing exposure?
3. Signal quality — Are multiple scanners agreeing or just 1 weak signal?
4. Risk/Reward — Is net_rr >= 1.5? Is risk_pct reasonable (<5%)?
5. Timing — Funding about to flip? OI spiking (liquidation cascade)?
6. Regime fit — Does direction match current regime bias?

RULES:
- APPROVE at most {slots} candidates (slot limit)
- Prefer higher GSS scores when signals are otherwise equal
- REJECT if net_rr < 1.0 or risk_pct > 8%
- REJECT if only 1 scanner and it's weak (confluence < 0.2)
- DEFER if signal is borderline — better to wait for confirmation

Return EXACTLY this JSON format (no other text):
{{"verdicts": [{{"asset": "BTC", "direction": "LONG", "verdict": "APPROVE|REJECT|DEFER", "reason": "brief reason", "confidence": 0.0-1.0}}], "summary": "1-line summary of decisions"}}
"""
    return prompt


def call_hermes(prompt: str, timeout: int = 90) -> str:
    """Call hermes CLI and return output."""
    hermes_bin = find_hermes()
    hermes_home = os.environ.get("HERMES_HOME", "/root/.hermes")

    env = {
        **os.environ,
        "HERMES_HOME": hermes_home,
        "NO_COLOR": "1",
        "TERM": "dumb",
    }

    # Sync GLM keys if available
    glm_key = os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    glm_base = os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    if glm_key:
        env["GLM_API_KEY"] = glm_key
    if glm_base:
        env["GLM_BASE_URL"] = glm_base

    cmd = [hermes_bin, "chat", "-Q", "-q", prompt]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(STATE_DIR),
    )

    output = proc.stdout.strip()
    if proc.returncode != 0:
        raise RuntimeError(f"hermes exit {proc.returncode}: {proc.stderr[:500]}")

    return output


def parse_verdicts(output: str) -> dict:
    """Extract JSON verdicts from hermes output."""
    # Try to find JSON in output
    output = output.strip()

    # Direct parse
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    # Find JSON block
    import re
    json_match = re.search(r'\{[\s\S]*"verdicts"[\s\S]*\}', output)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse hermes output: {output[:300]}")


def build_approved_trades(candidates: dict, verdicts: dict) -> list:
    """Match verdicts to candidates and build approved trade list."""
    cands = candidates.get("candidates", [])
    verdict_list = verdicts.get("verdicts", [])
    approved = []

    for v in verdict_list:
        if v.get("verdict") != "APPROVE":
            continue

        asset = v.get("asset", "")
        direction = v.get("direction", "")

        # Find matching candidate
        match = next(
            (c for c in cands if c["asset"] == asset and c["direction"] == direction),
            None,
        )
        if not match:
            continue

        approved.append({
            "asset": match["asset"],
            "direction": match["direction"],
            "entry_price": match["entry_price"],
            "stop_price": match["stop_price"],
            "tp1_price": match["tp1_price"],
            "tp2_price": match["tp2_price"],
            "leverage": match["leverage"],
            "margin_usd": match["margin_usd"],
            "entryScore": match["gss"],
            "netRr": match["net_rr"],
            "atr": match["atr"],
            "qty": match["notional"] / match["entry_price"] if match["entry_price"] > 0 else 0,
            "hermes_reason": v.get("reason", ""),
            "hermes_confidence": v.get("confidence", 0),
        })

    return approved


def main():
    candidates = load_json(CANDIDATES_FILE)
    if not candidates:
        print("[suguru-decide] No candidates file found — nothing to decide")
        sys.exit(0)

    cands = candidates.get("candidates", [])
    if not cands:
        print("[suguru-decide] 0 candidates — nothing to decide")
        sys.exit(0)

    regime = load_json(REGIME_FILE, default={})
    brain = load_json(BRAIN_FILE, default={})

    # Build prompt
    prompt = build_decision_prompt(candidates, regime, brain)

    if DRY_RUN:
        print(f"[suguru-decide] DRY-RUN: would send {len(cands)} candidates to hermes")
        print(f"[suguru-decide] Prompt length: {len(prompt)} chars")
        print("---")
        print(prompt[:500])
        return

    # Call hermes
    print(f"[suguru-decide] Sending {len(cands)} candidates to hermes...")
    try:
        output = call_hermes(prompt)
    except Exception as e:
        print(f"[suguru-decide] hermes call failed: {e}")
        # Fallback: approve top candidate by GSS if hermes is down
        top = cands[0] if cands else None
        if top:
            approved = [{
                "asset": top["asset"],
                "direction": top["direction"],
                "entry_price": top["entry_price"],
                "stop_price": top["stop_price"],
                "tp1_price": top["tp1_price"],
                "tp2_price": top["tp2_price"],
                "leverage": top["leverage"],
                "margin_usd": top["margin_usd"],
                "entryScore": top["gss"],
                "netRr": top["net_rr"],
                "atr": top["atr"],
                "qty": top["notional"] / top["entry_price"] if top["entry_price"] > 0 else 0,
                "hermes_reason": "fallback: hermes unavailable, top GSS",
                "hermes_confidence": 0.3,
            }]
            result = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "fallback-hermes-down",
                "approved": approved,
                "summary": f"hermes unavailable, approved top GSS ({top['asset']} {top['direction']})",
            }
            save_json(APPROVED_FILE, result)
            print(f"[suguru-decide] Fallback: approved {top['asset']} {top['direction']}")
        return

    # Parse verdicts
    try:
        verdicts = parse_verdicts(output)
    except ValueError as e:
        print(f"[suguru-decide] Parse error: {e}")
        return

    approved = build_approved_trades(candidates, verdicts)
    summary = verdicts.get("summary", "")

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "hermes",
        "approved": approved,
        "verdicts": verdicts.get("verdicts", []),
        "summary": summary,
    }

    save_json(APPROVED_FILE, result)
    print(f"[suguru-decide] {len(approved)} approved | {summary}")


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
