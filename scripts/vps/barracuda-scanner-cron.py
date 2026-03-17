#!/usr/bin/env python3
"""
BARRACUDA v1.0 — Funding Decay Collector.
Finds assets where extreme funding persists for 6+ hours, confirmed by SM alignment and trend structure.
"""

import sys
import os
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso, load_json, save_json,
    mcporter_call, send_telegram, current_regime_params,
    count_open_slots, get_enabled_strategies, get_strategy_state_dir,
    POSITION_STATE_DIR, CONFIG_DIR, record_trade, add_pending_entry
)


BARRACUDA_CONFIG_FILE = CONFIG_DIR / "barracuda-config.json"
FUNDING_HISTORY_FILE = POSITION_STATE_DIR / "funding-history.json"


# ─── Tech Helpers ─────────────────────────────────────────────

def sma(candles, periods):
    if len(candles) < periods: return 0
    closes = [float(c.get("close", c.get("c", 0))) for c in candles[-periods:]]
    return sum(closes) / len(closes)

def rsi(candles, period=14):
    if len(candles) < period + 1: return 50.0
    closes = [float(c.get("close", c.get("c", 0))) for c in candles[-(period+10):]]
    
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    val = 100 - (100 / (1 + rs))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0: val = 100.0
        else:
            rs = avg_gain / avg_loss
            val = 100 - (100 / (1 + rs))

    return val


def get_sm_data():
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result: return {}
    
    markets = result.get("data", result)
    if isinstance(markets, dict): markets = markets.get("markets", [])
    if not isinstance(markets, list): return {}

    sm = {}
    for m in markets:
        if isinstance(m, dict):
            asset = m.get("token", m.get("asset", ""))
            direction = m.get("direction", m.get("side", "")).upper()
            pct = float(m.get("longPct", 50))
            if direction == "SHORT": pct = 100 - pct
            traders = int(m.get("traderCount", m.get("traders", 0)))
            sm[asset] = {"direction": direction, "pct": pct, "traders": traders}
    return sm


# ─── Funding History ──────────────────────────────────────────

def update_funding_history(instruments, config):
    history = load_json(FUNDING_HISTORY_FILE, default={})
    now = time.time()
    min_ann = config.get("entry", {}).get("minFundingAnnPct", 30)

    for inst in instruments:
        name = inst.get("name", "")
        if not name or inst.get("is_delisted"): continue
        
        ctx = inst.get("context", {})
        funding = float(ctx.get("funding", 0))
        funding_ann = abs(funding) * 3 * 365 * 100

        if name not in history:
            history[name] = {"snapshots": [], "currentDirection": None, "streakStarted": None}

        entry = history[name]
        current_dir = "SHORT" if funding > 0 else "LONG" if funding < 0 else None

        if current_dir and funding_ann >= min_ann:
            if entry.get("currentDirection") != current_dir:
                entry["currentDirection"] = current_dir
                entry["streakStarted"] = now
        else:
            entry["currentDirection"] = None
            entry["streakStarted"] = None

        entry["snapshots"].append({"ts": now, "funding": funding, "ann": funding_ann})
        entry["snapshots"] = entry["snapshots"][-48:] # Keep 12h at 15m intervals

    save_json(FUNDING_HISTORY_FILE, history)
    return history


def get_funding_persistence_hours(asset, history):
    entry = history.get(asset, {})
    started = entry.get("streakStarted")
    if not started or not entry.get("currentDirection"): return 0
    return (time.time() - started) / 3600


# ─── Analysis ────────────────────────────────────────────────

def analyze_opportunity(asset, ctx, history, sm_data, config):
    entry_cfg = config.get("entry", {})
    funding = float(ctx.get("funding", 0))
    funding_ann = abs(funding) * 3 * 365 * 100

    if funding_ann < entry_cfg.get("minFundingAnnPct", 30): return None
    direction = "SHORT" if funding > 0 else "LONG"

    # Gate 1: Persistence
    hours = get_funding_persistence_hours(asset, history)
    if hours < entry_cfg.get("minPersistenceHours", 6): return None

    # Gate 2: SM Alignment (Must agree)
    sm_info = sm_data.get(asset, {})
    if sm_info.get("direction") != direction: return None

    # Gate 3: 4H Trend
    data = mcporter_call("market_get_asset_data", {"asset": asset, "candle_intervals": ["1h", "4h"]})
    if "error" in data: return None
    candle_data = data.get("data", data)
    
    candles_4h = candle_data.get("candles", {}).get("4h", [])
    if len(candles_4h) < 25: return None

    sma_20 = sma(candles_4h, 20)
    sma_20_prev5 = sma(candles_4h[:-5] if len(candles_4h)>5 else candles_4h, 20)
    sma_trend = "UP" if sma_20 > sma_20_prev5 else "DOWN"

    trend_aligned = (direction == "LONG" and sma_trend == "UP") or \
                    (direction == "SHORT" and sma_trend == "DOWN")
    if not trend_aligned: return None

    # Gate 4: RSI limits
    current_rsi = rsi(candles_4h, 14)
    if direction == "LONG" and current_rsi > 72: return None
    if direction == "SHORT" and current_rsi < 28: return None

    # Yield Calc
    lev = min(ctx.get("max_leverage", 10), config["leverage"]["max"])
    if lev < config["leverage"]["min"]: return None
    daily_yield = abs(funding) * 3 * lev * 100

    score = 5 # 3 for funding, 2 for persistence
    reasons = [f"funding_{funding_ann:.0f}%_ann", f"persistent_{hours:.1f}h", f"sm_aligned_{sm_info.get('pct',0):.0f}%"]
    score += 2 # SM Aligned
    score += 1 # Trend Confirmed
    reasons.append(f"trend_confirmed_{sma_trend}")

    if daily_yield > 5:
        score += 1
        reasons.append(f"high_yield_{daily_yield:.1f}%/day")
        
    if sm_info.get("pct", 0) >= 70:
        score += 1
        reasons.append("sm_strongly_tilted")

    return {
        "asset": asset, "direction": direction, "score": score, "reasons": reasons,
        "fundingRate": funding, "fundingAnnPct": funding_ann, "persistenceHours": hours,
        "dailyYieldPct": daily_yield, "leverage": lev, "rsi": current_rsi
    }


def build_dsl_state(asset, direction, score, config, details):
    return {
        "active": True, "asset": asset, "direction": direction, "score": score,
        "entrySource": "auto-barracuda", "phase": 1,
        "highWaterPrice": details.get("price", 0), "highWaterRoe": 0,
        "currentTierIndex": -1, "consecutiveBreaches": 0, "floorPrice": None,
        "lockMode": config["dsl"]["lockMode"],
        "phase2TriggerRoe": config["dsl"]["phase2TriggerRoe"],
        "phase1": {
            "retraceThreshold": 0.03,
            "consecutiveBreachesRequired": 3,
            "hardTimeoutMinutes": 0,
            "weakPeakCutMinutes": 0,
            "deadWeightCutMinutes": 30,
            "absoluteFloorRoe": -20
        },
        "tiers": config["dsl"]["tiers"],
        "stagnation": config["dsl"]["stagnationTp"],
        "createdAt": now_iso()
    }


# ─── Main ────────────────────────────────────────────────────

def scan():
    config = load_json(BARRACUDA_CONFIG_FILE)
    if not config:
        log("BARRACUDA: No config found.")
        return

    strategies = get_enabled_strategies()
    if not strategies: return

    # Pick a strategy with open slots
    active_strat = None
    for strat in strategies:
        if count_open_slots(strat) > 0:
            active_strat = strat
            break
            
    if not active_strat: return

    # Need all instruments to track funding history globally
    result = mcporter_call("market_get_all_instruments", {})
    if "error" in result: return
    instruments = result.get("data", [])

    history = update_funding_history(instruments, config)
    sm_data = get_sm_data()

    # Make sure we don't open duplicate positions
    sdir = get_strategy_state_dir(active_strat["_key"])
    active_assets = set()
    for f in sdir.glob("dsl-*.json"):
        ps = load_json(f)
        if ps and ps.get("active"): active_assets.add(ps["asset"])

    signals = []
    scanned = 0
    min_score = config.get("entry", {}).get("minScore", 8)

    for inst in instruments:
        name = inst.get("name", "")
        if not name or inst.get("is_delisted") or name.startswith("xyz:") or name in active_assets:
            continue
            
        ctx = inst.get("context", {})
        scanned += 1
        opp = analyze_opportunity(name, ctx, history, sm_data, config)
        if opp and opp["score"] >= min_score:
            signals.append(opp)

    signals.sort(key=lambda x: x["score"], reverse=True)
    
    if not signals:
        log(f"BARRACUDA: Scanned {scanned} assets. No funding opportunities.")
        return

    best = signals[0]
    asset = best["asset"]
    dirn = best["direction"]
    lev = best["leverage"]

    budget = active_strat.get("budget", 1000)
    alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
    margin = budget * alloc

    log(f"BARRACUDA: Entering {asset} {dirn} for funding yield (Score: {best['score']})")

    res = mcporter_call("strategy_create_position", {
        "strategyId": active_strat.get("strategyId"), "asset": asset,
        "direction": dirn, "marginUsd": margin, "leverage": lev,
        "orderType": config["execution"]["entryOrderType"]
    })

    if "error" not in res:
        eprice = float(res.get("entryPrice", 0))
        best["price"] = eprice
        dsl = build_dsl_state(asset, dirn, best["score"], config, best)
        dsl["wallet"] = active_strat.get("wallet")
        dsl["strategyId"] = active_strat.get("strategyId")
        dsl["strategyKey"] = active_strat["_key"]
        
        save_json(sdir / f"dsl-{asset}.json", dsl)

        send_telegram(f"🎣 BARRACUDA ENTRY: {dirn} {asset}\n"
                      f"Yield: {best['dailyYieldPct']:.1f}% / day\n"
                      f"Score: {best['score']} | Persisted: {best['persistenceHours']:.1f}h\n"
                      f"Reasons: {', '.join(best['reasons'])}\n"
                      f"Margin: ${margin:.0f} | Lev: {lev}x")

        record_trade({
            "action": "OPEN", "asset": asset, "direction": dirn,
            "entryPrice": eprice, "size": float(res.get("size", 0)),
            "margin": margin, "leverage": lev, "strategyKey": active_strat["_key"],
            "entrySource": "auto-barracuda", "entryMode": "BARRACUDA",
            "entryScore": best["score"]
        })
        
        add_pending_entry({
            "asset": asset, "direction": dirn, "autoEntered": True,
            "strategyKey": active_strat["_key"], "entryPrice": eprice, "margin": margin,
            "leverage": lev, "score": best["score"], "source": "barracuda"
        })


def main():
    if not acquire_lock("barracuda-scanner"): return
    try: scan()
    finally: release_lock("barracuda-scanner")

if __name__ == "__main__":
    main()
