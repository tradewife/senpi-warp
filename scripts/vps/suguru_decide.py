#!/usr/bin/env python3
"""
suguru_decide.py — GLM decision layer for suguru candidates.

Calls GLM API directly (no hermes subprocess).
Reads suguru-candidates.json, writes suguru-recommendation.json.

Usage: python3 scripts/vps/suguru_decide.py
       python3 scripts/vps/suguru_decide.py --dry-run
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
OUTPUTS_DIR = STATE_DIR / "outputs"
CONFIG_DIR = STATE_DIR / "config"

CANDIDATES_FILE = OUTPUTS_DIR / "suguru-candidates.json"
RECOMMENDATION_FILE = OUTPUTS_DIR / "suguru-recommendation.json"
REGIME_FILE = CONFIG_DIR / "risk-regime.json"

DRY_RUN = "--dry-run" in sys.argv


def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def call_glm(prompt):
    api_key = os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("HERMES_MODEL", "glm-5-turbo").strip()

    if not api_key or not base_url:
        raise RuntimeError("GLM_API_KEY or GLM_BASE_URL not set")

    url = base_url.rstrip("/") + "/chat/completions"

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a trading advisor. Reply ONLY with valid JSON, no markdown, no code blocks."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    resp = urllib.request.urlopen(req, timeout=40)
    data = json.loads(resp.read())
    msg = data["choices"][0]["message"]
    # GLM-5-turbo puts output in reasoning_content, leaves content empty
    return msg.get("content") or msg.get("reasoning_content") or ""


def build_prompt(cands, regime_mode, equity, slots):
    lines = []
    for i, c in enumerate(cands[:5]):
        s = c.get("sub_scores", {})
        asset = c["asset"]
        direction = c["direction"]
        gss = c["gss"]
        px = c["entry_price"]
        lev = c["leverage"]
        rr = c["net_rr"]
        risk = c.get("risk_pct", 0)
        sl = c.get("stop_price", 0)
        tp1 = c.get("tp1_price", 0)
        tp2 = c.get("tp2_price", 0)
        atr = c.get("atr", 0)
        margin = c.get("margin_usd", 0)
        conf = s.get("scanner_confluence", 0)
        whale = s.get("SM_whale_bias", 0)
        regime_align = s.get("regime_alignment", 0)
        basis = s.get("basis", 0)
        oi_delta = s.get("OI_delta", 0)
        funding = c.get("funding", 0)
        vol24 = c.get("vol24", 0)
        oi = c.get("oi", 0)
        sm = c.get("sm", {})
        sm_dir = sm.get("direction", "none")
        sm_conv = sm.get("conviction", 0)
        sm_traders = sm.get("traders", 0)
        sb = c.get("scanner_bias", {})
        sb_long = sb.get("long", 0)
        sb_short = sb.get("short", 0)
        sb_scanners = sb.get("scanners", [])

        lines.append(
            f"{i+1}. {direction} {asset} @ ${px} lev={lev}x\n"
            f"   GSS={gss:.2f} netRR={rr:.2f} risk={risk:.1f}% margin=${margin:.2f}\n"
            f"   SL=${sl} TP1=${tp1} TP2=${tp2} ATR={atr:.6f}\n"
            f"   Sub-scores: conf={conf:.2f} whale={whale:.2f} regime={regime_align:.2f} "
            f"basis={basis:.2f} OI={oi_delta:.2f}\n"
            f"   Funding={funding:.6f} Vol24=${vol24:,.0f} OI=${oi:,.0f}\n"
            f"   SM: {sm_dir} conv={sm_conv} traders={sm_traders}\n"
            f"   Scanner bias: {sb_long}L/{sb_short}S from {', '.join(sb_scanners) if sb_scanners else 'none'}"
        )

    return (
        f"You are evaluating {len(cands)} trade candidates for a ${equity} account.\n"
        f"Regime={regime_mode} | Open slots={slots} | Max risk=20% per trade\n\n"
        f"CANDIDATES:\n"
        + "\n".join(lines)
        + '\n\nEvaluate each candidate considering:\n'
        + '- Risk/reward ratio (netRR > 1.5 preferred)\n'
        + '- Sub-score strength (confluence + whale bias most important)\n'
        + '- Smart money alignment\n'
        + '- Scanner agreement\n'
        + '- Regime alignment\n'
        + '- Funding rate extremes\n\n'
        + 'Reply ONLY this JSON:\n'
        + '{"recommendation":"TRADE|REJECT","asset":"SYMBOL","direction":"LONG|SHORT",'
        + '"leverage":8,"margin_pct":25,"reasoning":"2-3 sentence justification",'
        + '"confidence":0.7,"rejected_reason":"if REJECT, why all are bad"}'
    )


def parse_rec(output):
    output = output.strip()
    output = re.sub(r"^```(?:json)?\s*", "", output)
    output = re.sub(r"\s*```$", "", output)
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        match = re.search(r"\{[^}]+\}", output)
        if match:
            return json.loads(match.group())
    raise ValueError(f"Could not parse: {output[:200]}")


def main():
    candidates = load_json(CANDIDATES_FILE)
    if not candidates:
        print("[suguru-decide] No candidates file")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT", "reasoning": "No scan data.",
        })
        return

    cands = candidates.get("candidates", [])
    if not cands:
        print("[suguru-decide] 0 candidates")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT", "reasoning": "No tradeable candidates.",
        })
        return

    regime = load_json(REGIME_FILE, default={})
    regime_mode = regime.get("riskMode", "UNKNOWN")
    equity = candidates.get("account_equity", 100)
    slots = candidates.get("available_slots", 2)

    prompt = build_prompt(cands, regime_mode, equity, slots)

    if DRY_RUN:
        print(f"[suguru-decide] DRY-RUN: {len(cands)} candidates")
        print(prompt)
        return

    print(f"[suguru-decide] Evaluating {len(cands)} candidates via GLM...")
    try:
        output = call_glm(prompt)
        print(f"[suguru-decide] GLM: {output[:200]}")
    except Exception as e:
        print(f"[suguru-decide] GLM failed: {e}")
        top = cands[0]
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "TRADE",
            "asset": top["asset"],
            "direction": top["direction"],
            "leverage": top["leverage"],
            "margin_pct": 25,
            "reasoning": f"GLM unavailable. Top: {top['asset']} {top['direction']} GSS={top['gss']:.2f}",
            "confidence": 0.3,
            "source": "fallback",
            "candidates_count": len(cands),
            "candidates": cands[:3],
            "trade_params": {
                "entry_price": top["entry_price"],
                "stop_price": top["stop_price"],
                "tp1_price": top["tp1_price"],
                "tp2_price": top["tp2_price"],
                "leverage": top["leverage"],
                "margin_usd": top["margin_usd"],
                "netRr": top["net_rr"],
                "gss": top["gss"],
            },
        })
        print(f"[suguru-decide] Fallback: {top['asset']} {top['direction']}")
        return

    try:
        rec = parse_rec(output)
    except ValueError as e:
        print(f"[suguru-decide] Parse error: {e}")
        save_json(RECOMMENDATION_FILE, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": "REJECT",
            "reasoning": f"Parse error: {str(e)[:100]}",
        })
        return

    rec["timestamp"] = datetime.now(timezone.utc).isoformat()
    rec["source"] = "glm"
    rec["candidates_count"] = len(cands)
    rec["candidates"] = cands[:3]

    if rec.get("recommendation") == "TRADE":
        match = next((c for c in cands if c["asset"] == rec.get("asset")), None)
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
    action = rec.get("recommendation", "?")
    asset = rec.get("asset", "")
    d = rec.get("direction", "")
    conf = rec.get("confidence", 0)
    print(f"[suguru-decide] {action} {d} {asset} (confidence={conf:.0%})")


if __name__ == "__main__":
    main()
