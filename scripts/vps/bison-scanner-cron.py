#!/usr/bin/env python3
"""
BISON v1.2 — Conviction Trend Holder.
Scans Top 10 assets by volume for 4H/1H trend alignment and momentum, aiming for longer holds.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso, load_json, save_json,
    mcporter_call, send_telegram, current_regime_params,
    check_directional_exposure_limit, attach_position_playbook,
    count_open_slots, get_enabled_strategies, get_strategy_state_dir,
    POSITION_STATE_DIR, CONFIG_DIR, record_trade, add_pending_entry,
    record_heartbeat,
)


BISON_CONFIG_FILE = CONFIG_DIR / "bison-config.json"


# ─── Tech Helpers ─────────────────────────────────────────────

def price_momentum(candles, n_bars=1):
    if len(candles) < n_bars + 1: return 0
    old = float(candles[-(n_bars + 1)].get("close", candles[-(n_bars + 1)].get("c", 0)))
    new = float(candles[-1].get("close", candles[-1].get("c", 0)))
    if old == 0: return 0
    return ((new - old) / old) * 100

def trend_structure(candles, lookback=6):
    if len(candles) < lookback: return "NEUTRAL", 0
    lows = [float(c.get("low", c.get("l", 0))) for c in candles[-lookback:]]
    highs = [float(c.get("high", c.get("h", 0))) for c in candles[-lookback:]]
    
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    
    total = lookback - 1
    if higher_lows >= total * 0.6: return "BULLISH", higher_lows / total
    elif lower_highs >= total * 0.6: return "BEARISH", lower_highs / total
    return "NEUTRAL", 0

def volume_trend(candles, lookback=6):
    if len(candles) < lookback + 2: return 0
    vols = [float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-(lookback + 2):]]
    half = lookback // 2
    recent = sum(vols[-half:]) / half if half > 0 else 1
    earlier = sum(vols[:half]) / half if half > 0 else 1
    if earlier == 0: return 0
    return ((recent - earlier) / earlier) * 100

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    g, l = gains[-period:], losses[-period:]
    avg_g, avg_l = sum(g) / period, sum(l) / period
    if avg_l == 0: return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))

def get_sm_direction(coin):
    data = mcporter_call("leaderboard_get_markets", {})
    if "error" in data: return None, 0
    markets = data.get("data", data)
    if isinstance(markets, dict): markets = markets.get("markets", [])
    if not isinstance(markets, list): return None, 0

    for m in markets:
        if isinstance(m, dict):
            token = m.get("token", m.get("coin", m.get("asset", "")))
            if token == coin:
                direction = m.get("direction", m.get("side", "")).upper()
                pct = float(m.get("longPct", 50))
                if direction == "SHORT": pct = 100 - pct
                return direction, pct
    return "NEUTRAL", 50

def get_top_assets(n=10):
    data = mcporter_call("market_list_instruments", {})
    if "error" in data: return []
    instruments = data.get("data", [])
    if not instruments: return []
    
    assets = []
    for inst in instruments:
        if not inst.get("is_delisted"):
            coin = inst.get("name", "")
            vol = float(inst.get("volume24h", 0))
            if coin and vol > 0 and not coin.lower().startswith("xyz:"):
                assets.append({"coin": coin, "volume": vol})
                
    assets.sort(key=lambda x: x["volume"], reverse=True)
    return [a["coin"] for a in assets[:n]]


# ─── Analysis ────────────────────────────────────────────────

def build_thesis(coin, config):
    entry_cfg = config.get("entry", {})
    data = mcporter_call("market_get_asset_data", {
        "asset": coin, "candle_intervals": ["15m", "1h", "4h"], "include_funding": True
    })
    if "error" in data: return None
    asset_data = data.get("data", data)

    candles_15m = asset_data.get("candles", {}).get("15m", [])
    candles_1h = asset_data.get("candles", {}).get("1h", [])
    candles_4h = asset_data.get("candles", {}).get("4h", [])
    funding = float(asset_data.get("funding", 0))

    if len(candles_1h) < 8 or len(candles_4h) < 6: return None

    price = float(candles_15m[-1].get("close", candles_15m[-1].get("c", 0))) if candles_15m else 0

    trend_4h, trend_strength = trend_structure(candles_4h)
    if trend_4h == "NEUTRAL": return None
    direction = "LONG" if trend_4h == "BULLISH" else "SHORT"

    trend_1h, _ = trend_structure(candles_1h)
    if trend_1h != trend_4h: return None

    mom_1h = price_momentum(candles_1h, 2)
    if direction == "LONG" and mom_1h < entry_cfg.get("minMom1hPct", 0.5): return None
    if direction == "SHORT" and mom_1h > -entry_cfg.get("minMom1hPct", 0.5): return None

    score = 5 # 3 for 4h, 2 for 1h confirms
    reasons = [f"4h_{trend_4h.lower()}_{trend_strength:.0%}", f"1h_confirms_{mom_1h:+.2f}%"]

    sm_dir, sm_pct = get_sm_direction(coin)
    if sm_dir == direction:
        score += 2
        reasons.append(f"sm_aligned_{sm_pct:.0f}%")
    elif sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        if entry_cfg.get("smHardBlock", True): return None

    if (direction == "LONG" and funding < 0) or (direction == "SHORT" and funding > 0):
        score += 2
        reasons.append(f"funding_aligned")
    elif (direction == "LONG" and funding > 0.01) or (direction == "SHORT" and funding < -0.005):
        score -= 1
        reasons.append("funding_crowded")

    vol_1h = volume_trend(candles_1h)
    if vol_1h > entry_cfg.get("minVolTrendPct", 10):
        score += 1
        reasons.append(f"vol_rising_{vol_1h:+.0f}%")

    closes_1h = [float(c.get("close", c.get("c", 0))) for c in candles_1h]
    rsi = calc_rsi(closes_1h)
    if direction == "LONG" and rsi > entry_cfg.get("rsiMaxLong", 72): return None
    if direction == "SHORT" and rsi < entry_cfg.get("rsiMinShort", 28): return None

    if (direction == "LONG" and rsi < 55) or (direction == "SHORT" and rsi > 45):
        score += 1
        reasons.append(f"rsi_room_{rsi:.0f}")

    mom_4h = price_momentum(candles_4h, 1)
    if abs(mom_4h) > 1.5:
        score += 1
        reasons.append(f"4h_momentum_{mom_4h:+.1f}%")

    return {
        "coin": coin, "direction": direction, "score": score, "reasons": reasons, "price": price
    }


def evaluate_held_position(coin, direction, entry_cfg):
    data = mcporter_call("market_get_asset_data", {"asset": coin, "candle_intervals": ["1h", "4h"], "include_funding": True})
    if "error" in data: return True, ["data_unavailable"]
    asset_data = data.get("data", data)

    candles_1h = asset_data.get("candles", {}).get("1h", [])
    candles_4h = asset_data.get("candles", {}).get("4h", [])
    funding = float(asset_data.get("funding", 0))

    if len(candles_4h) < 6: return True, []

    invs = []
    trend_4h, _ = trend_structure(candles_4h)
    if direction == "LONG" and trend_4h == "BEARISH": invs.append("4h_flipped_bearish")
    if direction == "SHORT" and trend_4h == "BULLISH": invs.append("4h_flipped_bullish")

    sm_dir, _ = get_sm_direction(coin)
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        invs.append(f"sm_flipped_{sm_dir}")

    thresh = entry_cfg.get("fundingExtremeThreshold", 0.015)
    if direction == "LONG" and funding > thresh: invs.append("funding_extreme_against")
    if direction == "SHORT" and funding < -thresh: invs.append("funding_extreme_against")

    if len(candles_1h) >= 12:
        recent_vols = [float(c.get("volume", 0)) for c in candles_1h[-3:]]
        avg_vol = sum(float(c.get("volume", 0)) for c in candles_1h[-12:-3]) / 9
        if avg_vol > 0 and all(v < avg_vol * 0.3 for v in recent_vols):
            invs.append("volume_dried_up")

    return len(invs) == 0, invs


def build_dsl_state(coin, direction, score, config, price):
    tier = config["dsl"]["convictionTiers"][0]
    for ct in config["dsl"]["convictionTiers"]:
        if score >= ct["minScore"]: tier = ct

    return {
        "active": True, "asset": coin, "direction": direction, "score": score,
        "entrySource": "auto-bison", "phase": 1, "highWaterPrice": price, "highWaterRoe": 0,
        "currentTierIndex": -1, "consecutiveBreaches": 0, "floorPrice": None,
        "lockMode": config["dsl"]["lockMode"],
        "phase2TriggerRoe": config["dsl"]["phase2TriggerRoe"],
        "phase1": {
            "retraceThreshold": 0.03, "consecutiveBreachesRequired": 3,
            "hardTimeoutMinutes": tier["hardTimeoutMin"],
            "weakPeakCutMinutes": tier["weakPeakCutMin"],
            "deadWeightCutMinutes": tier["deadWeightCutMin"],
            "absoluteFloorRoe": tier["absoluteFloorRoe"]
        },
        "tiers": config["dsl"]["tiers"],
        "stagnation": config["dsl"]["stagnationTp"],
        "createdAt": now_iso()
    }


# ─── Main ────────────────────────────────────────────────────

def scan():
    config = load_json(BISON_CONFIG_FILE)
    if not config:
        log("BISON: No config found.")
        return

    strategies = get_enabled_strategies()
    if not strategies: return

    # Gather active Bison positions to re-examine or track
    bison_positions = []
    active_coins = set()
    bison_strat = None

    for strat in strategies:
        sdir = get_strategy_state_dir(strat["_key"])
        for f in sdir.glob("dsl-*.json"):
            ps = load_json(f)
            if ps and ps.get("active"):
                active_coins.add(ps["asset"])
                if ps.get("entrySource") == "auto-bison":
                    ps["_file"] = f
                    ps["_strat"] = strat
                    bison_positions.append(ps)
        
        # Keep track of a valid strategy to use for entries
        if not bison_strat and count_open_slots(strat) > 0:
            bison_strat = strat

    # Re-evaluate open holds
    for pos in bison_positions:
        valid, reasons = evaluate_held_position(pos["asset"], pos["direction"], config.get("entry", {}))
        if not valid:
            log(f"BISON: {pos['asset']} thesis invalidated: {reasons}")
            mcporter_call("strategy_close_position", {
                "strategyId": pos.get("strategyId", pos["_strat"]["strategyId"]),
                "asset": pos["asset"]
            })
            pos["active"] = False
            pos["closedAt"] = now_iso()
            pos["closeReason"] = "bison_thesis_exit"
            fpath = pos.pop("_file")
            pos.pop("_strat")
            save_json(fpath, pos)
            send_telegram(f"🦬 BISON THESIS EXIT: {pos['asset']}\nReasons: {', '.join(reasons)}")

    if not bison_strat:
        return

    # Scan for new entries
    top_n = config.get("topAssets", 10)
    candidates = get_top_assets(top_n)
    
    signals = []
    min_score = config.get("entry", {}).get("minScore", 8)

    for coin in candidates:
        if coin in active_coins: continue
        thesis = build_thesis(coin, config)
        if thesis and thesis["score"] >= min_score:
            signals.append(thesis)

    if not signals:
        log(f"BISON: Scanned top {len(candidates)} volume assets, no conviction thesis >= {min_score}")
        return

    signals.sort(key=lambda x: x["score"], reverse=True)
    best = signals[0]
    
    # Margin scales by conviction (score)
    base_pct = config.get("entry", {}).get("marginPctBase", 0.25)
    if best["score"] >= 12: margin_pct = base_pct * 1.5
    elif best["score"] >= 10: margin_pct = base_pct * 1.25
    else: margin_pct = base_pct

    budget = bison_strat.get("budget", 1000)
    alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
    margin = budget * alloc * (margin_pct / base_pct)
    
    lev = min(config.get("leverage", {}).get("default", 10), config.get("leverage", {}).get("max", 10))
    asset = best["coin"]
    dirn = best["direction"]

    allowed_exposure, exposure = check_directional_exposure_limit(dirn, margin, lev)
    if not allowed_exposure:
        log(
            f"BISON: directional cap blocked {asset} {dirn} "
            f"projected={exposure['offendingPct']:.1f}% cap={exposure['capPct']:.1f}%"
        )
        return

    log(f"BISON: Entering {asset} {dirn} at score {best['score']}")

    res = mcporter_call("strategy_create_position", {
        "strategyId": bison_strat.get("strategyId"), "asset": asset,
        "direction": dirn, "marginUsd": margin, "leverage": lev,
        "orderType": config["execution"]["entryOrderType"]
    })

    if "error" not in res:
        eprice = float(res.get("entryPrice", 0))
        dsl = build_dsl_state(asset, dirn, best["score"], config, eprice)
        dsl["wallet"] = bison_strat.get("wallet")
        dsl["strategyId"] = bison_strat.get("strategyId")
        dsl["strategyKey"] = bison_strat["_key"]
        attach_position_playbook(
            dsl,
            scanner="bison",
            margin=margin,
            leverage=lev,
            score=best["score"],
            reasons=best["reasons"],
            setup={"coin": asset},
        )
        
        sdir = get_strategy_state_dir(bison_strat["_key"])
        save_json(sdir / f"dsl-{asset}.json", dsl)

        send_telegram(f"🦬 BISON ENTRY: {dirn} {asset}\n"
                      f"Score: {best['score']} | Top 10 Vol Assured\n"
                      f"Reasons: {', '.join(best['reasons'])}\n"
                      f"Margin: ${margin:.0f} | Lev: {lev}x")

        record_trade({
            "action": "OPEN", "asset": asset, "direction": dirn,
            "entryPrice": eprice, "size": float(res.get("size", 0)),
            "margin": margin, "leverage": lev, "strategyKey": bison_strat["_key"],
            "entrySource": "auto-bison", "entryMode": "BISON",
            "entryScore": best["score"]
        })
        
        add_pending_entry({
            "asset": asset, "direction": dirn, "autoEntered": True,
            "strategyKey": bison_strat["_key"], "entryPrice": eprice, "margin": margin,
            "leverage": lev, "score": best["score"], "source": "bison"
        })


def main():
    if not acquire_lock("bison-scanner"): return
    try:
        record_heartbeat("bison")
        scan()
    finally: release_lock("bison-scanner")

if __name__ == "__main__":
    main()
