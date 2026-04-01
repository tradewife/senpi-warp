#!/usr/bin/env python3
"""
suguru_decide.py — Hermes decision layer for suguru candidates.

Reads suguru-candidates.json, sends to Hermes for deliberation,
writes suguru-recommendation.json for user approval.

Does NOT execute trades — only produces a recommendation.

Usage:
    python3 scripts/vps/suguru_decide.py
    python3 scripts/vps/suguru_decide.py --dry-run
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
OUTPUTS_DIR = STATE_DIR / "outputs"
CONFIG_DIR = STATE_DIR / "config"

CANDIDATES_FILE = OUTPUTS_DIR / "suguru-candidates.json"
RECOMMENDATION_FILE = OUTPUTS_DIR / "suguru-recommendation.json"
REGIME_FILE = CONFIG_DIR / "risk-regime.json"
BRAIN_FILE = OUTPUTS_DIR / "autonomous-brain.json"

DRY_RUN = "--dry-run" in sys.argv


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def find_hermes() -> str:
    hermes = os.environ.get("HERMES_BIN_PATH", "/usr/local/bin/hermes")
    if os.path.isfile(hermes):
        return hermes
    found = shutil.which("hermes")
    if found:
        return found
    raise RuntimeError("hermes binary not found")


def build_prompt(candidates: dict, regime: dict, brain: dict) -> str:
    risk_mode = regime.get("riskMode", "UNKNOWN")
    equity = candidates.get("account_equity", 0)
    slots = candidates.get("available_slots", 0)
    cands = candidates.get("candidates", [])

    if not cands:
        return "No candidates to evaluate. Return: REJECT — no tradeable signals."

    cand_lines = []
    for i, c in enumerate(cands):
        scores = c.get("sub_scores", {})
        cand_lines.append(
            f"{i+1}. {c['direction']} {c['asset']} | GSS={c['gss']:.2f} | "
            f"px={c['entry_price']} | lev={c['leverage']}x | "
            f"margin=${c['margin_usd']:.0f} | risk={c['risk_pct']:.1f}% | "
            f"netRR={c['net_rr']:.2f} | funding={c.get('funding', 0):.4f}\n"
            f"   scores: confluence={scores.get('scanner_confluence', 0):.2f} "
            f"whale={scores.get('SM_whale_bias', 0):.2f} "
            f"momentum={scores.get('momentum', 0):.2f} "
            f"regime={scores.get('regime_align', 0):.2f}\n"
            f"   scanners: {json.dumps(c.get('scanner_bias', {}), separators=(',', ':'))}"
        )

    brain_mode = brain.get("mode", "UNKNOWN")

    prompt = f"""You are SUGURU, a Hyperliquid perps trading advisor.

CURRENT STATE:
- Risk regime: {risk_mode}
- Brain mode: {brain_mode}
- Account equity: ${equity}
- Available slots: {slots}
- Candidates: {len(cands)}

CANDIDATES:
{chr(10).join(cand_lines)}

Analyze each candidate and recommend a trade (or no trade).

CONSIDER:
1. Signal quality — multiple scanners agreeing vs 1 weak signal
2. Risk/Reward — net_rr >= 1.5? risk_pct reasonable (<5%)?
3. Timing — funding about to flip? OI spiking?
4. Regime fit — direction matching regime bias?
5. Correlation — would this overlap existing exposure?

Pick the BEST candidate (or REJECT all if none are strong enough).

Return ONLY the JSON below. Do not repeat yourself. Do not add any text before or after the JSON:
{{"recommendation": "TRADE|REJECT", "asset": "BTC", "direction": "LONG", "leverage": 8, "margin_pct": 25, "reasoning": "2-3 sentence explanation for the user", "confidence": 0.0-1.0, "alternatives": ["list of other viable candidates if any"]}}
"""
    return prompt


def call_hermes(prompt: str, timeout: int = 90) -> str:
    hermes_bin = find_hermes()
    hermes_home = os.environ.get("HERMES_HOME", "/root/.hermes")

    env = {
        **os.environ,
        "HERMES_HOME": hermes_home,
        "NO_COLOR": "1",
        "TERM": "dumb",
    }

    glm_key = os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    glm_base = os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    if glm_key:
        env["GLM_API_KEY"] = glm_key
    if glm_base:
        env["GLM_BASE_URL"] = glm_base

    # Get model/provider from env (same as telegram bot brain)
    hermes_model = os.environ.get("HERMES_MODEL", "glm-5-turbo").strip()
    hermes_provider = os.environ.get("HERMES_INFERENCE_PROVIDER", "zai").strip()

    cmd = [hermes_bin, "chat", "-Q", "-q", prompt]
    if hermes_model:
        cmd += ["-m", hermes_model]
    if hermes_provider:
        cmd += ["--provider", hermes_provider]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=env, cwd=str(STATE_DIR),
    )

    if proc.returncode != 0:
        raise RuntimeError(f"hermes exit {proc.returncode}: {proc.stderr[:500]}")

    return proc.stdout.strip()


def parse_recommendation(output: str) -> dict:
    output = output.strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[\s\S]*"recommendation"[\s\S]*\}', output)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse hermes output: {output[:300]}")


def main():
    candidates = load_json(CANDIDATES_FILE)
    if not candidates:
        print("[suguru-decide] No candidates file found")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT",
            "reasoning": "No scan data available. Run suguru scan first.",
        })
        sys.exit(0)

    cands = candidates.get("candidates", [])
    if not cands:
        print("[suguru-decide] 0 candidates — nothing to evaluate")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT",
            "reasoning": "No tradeable candidates found in scan.",
            "candidates_count": 0,
        })
        sys.exit(0)

    regime = load_json(REGIME_FILE, default={})
    brain = load_json(BRAIN_FILE, default={})

    prompt = build_prompt(candidates, regime, brain)

    if DRY_RUN:
        print(f"[suguru-decide] DRY-RUN: {len(cands)} candidates, prompt={len(prompt)} chars")
        print(prompt[:500])
        return

    print(f"[suguru-decide] Deliberating on {len(cands)} candidates...")
    try:
        output = call_hermes(prompt)
    except Exception as e:
        print(f"[suguru-decide] hermes failed: {e}")
        # Fallback: recommend top candidate with low confidence
        top = cands[0]
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "TRADE",
            "asset": top["asset"],
            "direction": top["direction"],
            "leverage": top["leverage"],
            "margin_pct": 25,
            "reasoning": f"Hermes unavailable — top GSS candidate ({top['asset']} {top['direction']} GSS={top['gss']:.2f}). Review manually before approving.",
            "confidence": 0.3,
            "source": "fallback",
            "candidates_count": len(cands),
            "candidates": cands[:3],
        })
        print(f"[suguru-decide] Fallback: recommended {top['asset']} {top['direction']}")
        return

    try:
        rec = parse_recommendation(output)
    except ValueError as e:
        print(f"[suguru-decide] Parse error: {e}")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT",
            "reasoning": f"Hermes output parse error. Raw: {output[:200]}",
            "source": "parse-error",
        })
        return

    # Enrich with candidate data for execution if approved
    rec["timestamp"] = datetime.now(timezone.utc).isoformat()
    rec["source"] = "hermes"
    rec["candidates_count"] = len(cands)
    rec["candidates"] = cands[:3]  # top 3 for reference

    # If TRADE, attach the full trade params from the matching candidate
    if rec.get("recommendation") == "TRADE":
        match = next(
            (c for c in cands if c["asset"] == rec.get("asset") and c["direction"] == rec.get("direction")),
            None,
        )
        if match:
            rec["trade_params"] = {
                "entry_price": match["entry_price"],
                "stop_price": match["stop_price"],
                "tp1_price": match["tp1_price"],
                "tp2_price": match["tp2_price"],
                "leverage": rec.get("leverage", match["leverage"]),
                "margin_usd": match["margin_usd"],
                "netRr": match["net_rr"],
                "gss": match["gss"],
            }

    save_json(RECOMMENDATION_FILE, rec)

    if rec.get("recommendation") == "TRADE":
        print(f"[suguru-decide] RECOMMEND: {rec['direction']} {rec['asset']} "
              f"(confidence={rec.get('confidence', 0):.0%})")
    else:
        print(f"[suguru-decide] RECOMMEND: {rec['recommendation']} — {rec.get('reasoning', '')[:80]}")


if __name__ == "__main__":
    main()
