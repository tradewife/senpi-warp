#!/usr/bin/env python3
"""
CONDOR v1.0 — Multi-asset alpha hunter.
Follows the 3-mode lifecycle across BTC, ETH, SOL, HYPE.
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


CONDOR_STATE_FILE = POSITION_STATE_DIR / "condor-state.json"
CONDOR_CONFIG_FILE = CONFIG_DIR / "condor-config.json"


# ─── Tech Helpers ─────────────────────────────────────────────

def price_momentum(candles, periods: int) -> float:
    if len(candles) <= periods:
        return 0.0
    curr = float(candles[-1].get("close", candles[-1].get("c", 0)))
    prev = float(candles[-(periods+1)].get("close", candles[-(periods+1)].get("c", 0)))
    if prev == 0:
        return 0.0
    return ((curr - prev) / prev) * 100


def volume_ratio(candles) -> float:
    """Ratio of most recent candle volume to the average of the 4 before it."""
    if len(candles) < 6:
        return 1.0
    recent = float(candles[-2].get("volume", candles[-2].get("v", candles[-2].get("vlm", 0))))  # use -2 to avoid partial current candle
    vols = [float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-6:-2]]
    avg = sum(vols) / len(vols) if vols else 0
    return (recent / avg) if avg > 0 else 1.0


def sma(candles, periods):
    closes = [float(c.get("close", c.get("c", 0))) for c in candles[-periods:]]
    return sum(closes) / len(closes) if closes else 0


def trend_structure(candles, lookbacks=12):
    if len(candles) < lookbacks * 2:
        return "NEUTRAL", 0

    close1 = float(candles[-1].get("close", candles[-1].get("c", 0)))
    sma_short = sma(candles, lookbacks // 2)
    sma_long = sma(candles, lookbacks)

    if sma_long == 0:
        return "NEUTRAL", 0

    diff = (sma_short - sma_long) / dict(zip(range(1), [sma_long]))[0]

    if close1 > sma_short and sma_short > sma_long:
        return "BULLISH", abs(diff)
    if close1 < sma_short and sma_short < sma_long:
        return "BEARISH", abs(diff)

    return "NEUTRAL", abs(diff)


# ─── Data Fetching ───────────────────────────────────────────

def get_asset_data(asset):
    result = mcporter_call("market_get_asset_data", {
        "asset": asset,
        "candle_intervals": ["5m", "15m", "1h", "4h"],
        "include_funding": True
    })
    if "error" in result:
        return None
    return result.get("data", result)


def get_correlation_data(asset, corr_map):
    corr_asset = corr_map.get(asset, "BTC")
    data = get_asset_data(corr_asset)
    if not data:
        return None, None
    candles_15m = data.get("candles", {}).get("15m", [])
    candles_1h = data.get("candles", {}).get("1h", [])
    mom_15m = price_momentum(candles_15m, 1) if len(candles_15m) >= 2 else 0
    mom_1h = price_momentum(candles_1h, 1) if len(candles_1h) >= 2 else 0
    return mom_15m, mom_1h


def get_sm_direction(asset):
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        return None, 0, 0

    markets = result.get("data", result)
    if isinstance(markets, dict):
        markets = markets.get("markets", [])
    if not isinstance(markets, list):
        return None, 0, 0

    for m in markets:
        if isinstance(m, dict) and m.get("token", m.get("asset", "")) == asset:
            direction = m.get("direction", m.get("side", "")).upper()
            pct = float(m.get("longPct", 50))
            if direction == "SHORT":
                pct = 100 - pct
            traders = int(m.get("traderCount", m.get("traders", 0)))
            return direction, pct, traders

    return "NEUTRAL", 50, 0


# ─── Thesis Builder ──────────────────────────────────────────

def build_thesis(asset, config):
    entry_cfg = config.get("entry", {})
    asset_data = get_asset_data(asset)
    if not asset_data:
        return None

    candles_5m = asset_data.get("candles", {}).get("5m", [])
    candles_15m = asset_data.get("candles", {}).get("15m", [])
    candles_1h = asset_data.get("candles", {}).get("1h", [])
    candles_4h = asset_data.get("candles", {}).get("4h", [])
    funding = float(asset_data.get("funding", 0))

    if len(candles_5m) < 12 or len(candles_15m) < 8 or len(candles_1h) < 8 or len(candles_4h) < 6:
        return None

    price = float(candles_5m[-1].get("close", candles_5m[-1].get("c", 0)))

    trend_4h, _ = trend_structure(candles_4h)
    if trend_4h == "NEUTRAL":
        return None
    direction = "LONG" if trend_4h == "BULLISH" else "SHORT"

    trend_1h, _ = trend_structure(candles_1h)
    if trend_1h != trend_4h:
        return None

    mom_15m = price_momentum(candles_15m, 1)
    min_mom = entry_cfg.get("minMom15mPct", 0.1)
    if direction == "LONG" and mom_15m < min_mom: return None
    if direction == "SHORT" and mom_15m > -min_mom: return None

    score = 5  # 3 for 4h, 2 for 1h
    reasons = [f"4h_{trend_4h.lower()}", "1h_confirms"]
    
    score += 1
    reasons.append(f"15m_mom_{mom_15m:+.2f}%")

    trend_15m, _ = trend_structure(candles_15m, 4)
    trend_5m, _ = trend_structure(candles_5m, 6)
    if trend_15m == trend_4h and trend_5m == trend_4h:
        score += 1
        reasons.append("4TF_aligned")

    sm_dir, sm_pct, sm_count = get_sm_direction(asset)
    if sm_dir == direction:
        score += 2
        reasons.append(f"sm_aligned_{sm_pct:.0f}%")
        if (direction == "LONG" and sm_pct >= 70) or (direction == "SHORT" and sm_pct <= 30):
            score += 1
            reasons.append("sm_strongly_tilted")
    elif sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        return None # Hard block

    if direction == "LONG" and funding < 0:
        score += 1
        reasons.append("funding_pays_longs")
    elif direction == "SHORT" and funding > 0:
        score += 1
        reasons.append("funding_pays_shorts")

    vol = volume_ratio(candles_1h)
    if vol > 1.15:
        score += 1
        reasons.append("vol_rising")

    corr_15m, corr_1h = get_correlation_data(asset, config.get("correlationMap", {}))
    if corr_15m is not None and corr_1h is not None:
        corr_agrees = (direction == "LONG" and corr_15m > 0 and corr_1h > 0) or \
                      (direction == "SHORT" and corr_15m < 0 and corr_1h < 0)
        if corr_agrees:
            bonus = 2 if asset in config.get("bonusOnlyCorrelation", []) else 1
            score += bonus
            reasons.append("correlation_confirms")

    return {
        "asset": asset, "direction": direction, "score": score,
        "reasons": reasons, "price": price, "funding": funding
    }


# ─── Re-Evaluate Position ────────────────────────────────────

def evaluate_position(asset, direction, config):
    entry_cfg = config.get("entry", {})
    asset_data = get_asset_data(asset)
    if not asset_data: return True, ["data_unavailable"]

    candles_1h = asset_data.get("candles", {}).get("1h", [])
    candles_4h = asset_data.get("candles", {}).get("4h", [])
    funding = float(asset_data.get("funding", 0))

    if len(candles_4h) < 6: return True, []

    invs = []
    trend_4h, _ = trend_structure(candles_4h)
    if direction == "LONG" and trend_4h == "BEARISH": invs.append("4h_flipped_bearish")
    if direction == "SHORT" and trend_4h == "BULLISH": invs.append("4h_flipped_bullish")

    sm_dir, _, _ = get_sm_direction(asset)
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        invs.append(f"sm_flipped_{sm_dir}")

    thresh = entry_cfg.get("fundingExtremeThreshold", 0.012)
    if direction == "LONG" and funding > thresh: invs.append("funding_extreme_against")
    if direction == "SHORT" and funding < -thresh: invs.append("funding_extreme_against")

    if len(candles_1h) >= 12:
        recent_vols = [float(c.get("volume", c.get("v", 0))) for c in candles_1h[-3:]]
        avg_vol = sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-12:-3]) / 9
        if avg_vol > 0 and all(v < avg_vol * 0.3 for v in recent_vols):
            invs.append("volume_dried_up")

    if asset not in config.get("bonusOnlyCorrelation", []):
        _, corr_1h = get_correlation_data(asset, config.get("correlationMap", {}))
        if corr_1h is not None:
            if direction == "LONG" and corr_1h < -1.0: invs.append("correlation_diverging")
            if direction == "SHORT" and corr_1h > 1.0: invs.append("correlation_diverging")

    return len(invs) == 0, invs


# ─── Stalk Reload ────────────────────────────────────────────

def evaluate_reload(exit_state, config):
    direction = exit_state.get("exitDirection")
    asset = exit_state.get("exitAsset")
    exit_ts = exit_state.get("exitTimestamp", 0)
    exit_vol = exit_state.get("exitEntryVolRatio", 1.0)
    
    now_dt = datetime.now(timezone.utc)
    try:
        if isinstance(exit_ts, str):
            exit_dt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
        else:
            exit_dt = now_dt
    except:
        exit_dt = now_dt

    hours_stalking = (now_dt - exit_dt).total_seconds() / 3600
    stalk_cfg = config.get("entry", {}).get("stalking", {})
    
    if hours_stalking > stalk_cfg.get("maxStalkHours", 4):
        return False, ["stalk_timeout"]

    asset_data = get_asset_data(asset)
    if not asset_data: return False, ["data_unavailable"]

    candles_5m = asset_data.get("candles", {}).get("5m", [])
    candles_4h = asset_data.get("candles", {}).get("4h", [])

    kills = []
    checks = []

    trend_4h, _ = trend_structure(candles_4h)
    expected = "BULLISH" if direction == "LONG" else "BEARISH"
    if trend_4h != expected and trend_4h != "NEUTRAL": kills.append("4h_trend_reversed")

    sm_dir, _, _ = get_sm_direction(asset)
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction: kills.append("sm_flipped")

    if kills: return False, kills

    if hours_stalking < 0.5: checks.append("waiting_for_cooldown")

    if len(candles_5m) >= 3:
        mom = price_momentum(candles_5m, 1)
        if direction == "LONG" and mom > 0.15: checks.append("fresh_impulse_up")
        elif direction == "SHORT" and mom < -0.15: checks.append("fresh_impulse_down")
        else: checks.append("no_impulse")

    vol = volume_ratio(candles_5m)
    if vol >= exit_vol * stalk_cfg.get("minReloadVolPct", 50) / 100: checks.append("vol_ok")
    else: checks.append("vol_weak")

    if sm_dir == direction: checks.append("sm_aligned")
    elif sm_dir == "NEUTRAL": checks.append("sm_neutral")
    else: checks.append("sm_not_aligned")

    fails = [r for r in checks if r in ["no_impulse", "vol_weak", "sm_not_aligned", "waiting_for_cooldown"]]
    return len(fails) == 0, checks


# ─── DSL Builder ─────────────────────────────────────────────

def build_dsl_state(asset, direction, score, config, details):
    tier = config["dsl"]["convictionTiers"][-1]
    for ct in config["dsl"]["convictionTiers"]:
        if score >= ct["minScore"]:
            tier = ct
            break

    return {
        "active": True, "asset": asset, "direction": direction, "score": score,
        "entrySource": "auto-condor", "phase": 1,
        "highWaterPrice": details["price"], "highWaterRoe": 0,
        "currentTierIndex": -1, "consecutiveBreaches": 0, "floorPrice": None,
        "lockMode": config["dsl"]["lockMode"],
        "phase2TriggerRoe": config["dsl"]["phase2TriggerRoe"],
        "phase1": {
            "retraceThreshold": 0.03,
            "consecutiveBreachesRequired": 3,
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
    config = load_json(CONDOR_CONFIG_FILE)
    if not config:
        log("CONDOR: No config found.")
        return

    state = load_json(CONDOR_STATE_FILE, default={"currentMode": "HUNTING"})
    mode = state.get("currentMode", "HUNTING")

    # Find active Condor position
    strategies = get_enabled_strategies()
    condor_strat = None
    active_pos = None

    for strat in strategies:
        sdir = get_strategy_state_dir(strat["_key"])
        for f in sdir.glob("dsl-*.json"):
            ps = load_json(f)
            if ps and ps.get("active") and ps.get("entrySource") == "auto-condor":
                active_pos = ps
                condor_strat = strat
                break
        if active_pos: break

    # Fallback to general open slot check if HUNTING
    if not condor_strat and mode == "HUNTING":
        for strat in strategies:
            if count_open_slots(strat) > 0:
                condor_strat = strat
                break

    # RIDING MODE
    if active_pos and mode in ("RIDING", "HUNTING"):
        if mode != "RIDING":
            state["currentMode"] = "RIDING"
            state["activeAsset"] = active_pos["asset"]
            save_json(CONDOR_STATE_FILE, state)

        asset = active_pos["asset"]
        direction = active_pos["direction"]
        valid, reasons = evaluate_position(asset, direction, config)

        if not valid:
            log(f"CONDOR: Thesis failed for {asset} {direction}: {reasons}")
            mcporter_call("strategy_close_position", {
                "strategyId": active_pos.get("strategyId", condor_strat.get("strategyId")),
                "asset": asset
            })
            active_pos["active"] = False
            active_pos["closedAt"] = now_iso()
            active_pos["closeReason"] = "condor_thesis_exit"
            sfile = get_strategy_state_dir(condor_strat["_key"]) / f"dsl-{asset}.json"
            save_json(sfile, active_pos)
            
            send_telegram(f"🦅 CONDOR THESIS EXIT: {asset}\nReasons: {', '.join(reasons)}")
            
            state["currentMode"] = "HUNTING"
            state.pop("exitState", None)
            state.pop("activeAsset", None)
            save_json(CONDOR_STATE_FILE, state)
        return

    # DETECT DSL EXIT
    if not active_pos and mode == "RIDING":
        asset = state.get("activeAsset", "BTC")
        ast_data = get_asset_data(asset)
        evol = 1.0
        if ast_data:
            c5m = ast_data.get("candles", {}).get("5m", [])
            evol = volume_ratio(c5m) if c5m else 1.0

        state["currentMode"] = "STALKING"
        state["exitState"] = {
            "exitAsset": asset,
            "exitDirection": state.get("lastDirection", "LONG"),
            "exitTimestamp": now_iso(),
            "exitEntryVolRatio": evol
        }
        save_json(CONDOR_STATE_FILE, state)
        log(f"CONDOR: {asset} hit DSL stop — STALKING for reload.")
        return

    # STALKING MODE
    if mode == "STALKING":
        exst = state.get("exitState", {})
        if not exst:
            state["currentMode"] = "HUNTING"
            save_json(CONDOR_STATE_FILE, state)
        else:
            reload, reasons = evaluate_reload(exst, config)
            if reload and condor_strat:
                asset = exst["exitAsset"]
                dirn = exst["exitDirection"]
                
                budget = condor_strat.get("budget", 1000)
                alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
                margin = budget * alloc * 1.4  # Reloads get slightly higher margin 35% vs 25%
                lev = config["leverage"]["default"]
                allowed_exposure, exposure = check_directional_exposure_limit(dirn, margin, lev)
                if not allowed_exposure:
                    log(
                        f"CONDOR: directional cap blocked reload {asset} {dirn} "
                        f"projected={exposure['offendingPct']:.1f}% cap={exposure['capPct']:.1f}%"
                    )
                    return
                
                log(f"CONDOR: Reload triggered for {asset} {dirn}")
                res = mcporter_call("create_position", {
                    "strategyId": condor_strat.get("strategyId"), "asset": asset,
                    "direction": dirn, "margin": margin, "leverage": lev,
                    "orderType": config["execution"]["entryOrderType"]
                })
                
                if "error" not in res:
                    eprice = float(res.get("entryPrice", 0))
                    dsl = build_dsl_state(asset, dirn, 10, config, {"price": eprice})
                    dsl["wallet"] = condor_strat.get("wallet")
                    dsl["strategyId"] = condor_strat.get("strategyId")
                    dsl["strategyKey"] = condor_strat["_key"]
                    attach_position_playbook(
                        dsl,
                        scanner="condor",
                        margin=margin,
                        leverage=lev,
                        score=10,
                        reasons=reasons,
                        setup={"reload": True},
                    )
                    sfile = get_strategy_state_dir(condor_strat["_key"]) / f"dsl-{asset}.json"
                    save_json(sfile, dsl)
                    
                    state["currentMode"] = "RIDING"
                    state["activeAsset"] = asset
                    state["lastDirection"] = dirn
                    state.pop("exitState", None)
                    save_json(CONDOR_STATE_FILE, state)
                    
                    send_telegram(f"🦅 CONDOR RELOAD: {dirn} {asset}\nMargin: ${margin:.0f} | Lev: {lev}x")
                    
                    record_trade({
                        "action": "OPEN", "asset": asset, "direction": dirn,
                        "entryPrice": eprice, "size": float(res.get("size", 0)),
                        "margin": margin, "leverage": lev, "strategyKey": condor_strat["_key"],
                        "entrySource": "auto-condor", "entryMode": "CONDOR_RELOAD"
                    })
                return
            
            kills = [r for r in reasons if any(k in r for k in ["stalk_timeout", "4h_trend_reversed", "sm_flipped"])]
            if kills:
                state["currentMode"] = "HUNTING"
                state.pop("exitState", None)
                save_json(CONDOR_STATE_FILE, state)
                log(f"CONDOR: STALKING killed ({kills[0]}) -> RESET to HUNTING")
            return

    # HUNTING MODE (Default)
    if not condor_strat: return

    min_score = config["entry"].get("minScore", 10)
    theses = []
    
    for a in config.get("assets", ["BTC", "ETH", "SOL", "HYPE"]):
        t = build_thesis(a, config)
        if t and t["score"] >= min_score: theses.append(t)

    if not theses: return

    # Pick highest score
    theses.sort(key=lambda x: x["score"], reverse=True)
    best = theses[0]

    budget = condor_strat.get("budget", 1000)
    alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
    
    # Margin scales by conviction (score)
    if best["score"] >= 14: base_adj = 1.8  # 0.45 
    elif best["score"] >= 12: base_adj = 1.4 # 0.35
    else: base_adj = 1.0 # 0.25 (assuming alloc is ~0.25)
    
    margin = budget * alloc * base_adj
    lev = config["leverage"]["default"]

    allowed_exposure, exposure = check_directional_exposure_limit(best["direction"], margin, lev)
    if not allowed_exposure:
        log(
            f"CONDOR: directional cap blocked {best['asset']} {best['direction']} "
            f"projected={exposure['offendingPct']:.1f}% cap={exposure['capPct']:.1f}%"
        )
        return

    log(f"CONDOR: Entering {best['asset']} {best['direction']} at score {best['score']}")
    
    res = mcporter_call("create_position", {
        "strategyId": condor_strat.get("strategyId"), "asset": best["asset"],
        "direction": best["direction"], "margin": margin, "leverage": lev,
        "orderType": config["execution"]["entryOrderType"]
    })

    if "error" not in res:
        eprice = float(res.get("entryPrice", 0))
        dsl = build_dsl_state(best["asset"], best["direction"], best["score"], config, best)
        dsl["wallet"] = condor_strat.get("wallet")
        dsl["strategyId"] = condor_strat.get("strategyId")
        dsl["strategyKey"] = condor_strat["_key"]
        attach_position_playbook(
            dsl,
            scanner="condor",
            margin=margin,
            leverage=lev,
            score=best["score"],
            reasons=best["reasons"],
            setup={"asset": best["asset"]},
        )
        
        sfile = get_strategy_state_dir(condor_strat["_key"]) / f"dsl-{best['asset']}.json"
        save_json(sfile, dsl)

        state["currentMode"] = "RIDING"
        state["activeAsset"] = best["asset"]
        state["lastDirection"] = best["direction"]
        save_json(CONDOR_STATE_FILE, state)

        scores_str = ", ".join(f"{t['asset']}:{t['score']}" for t in theses)
        send_telegram(f"🦅 CONDOR ENTRY: {best['direction']} {best['asset']}\n"
                      f"Score: {best['score']} (Options: {scores_str})\n"
                      f"Reasons: {', '.join(best['reasons'])}\n"
                      f"Margin: ${margin:.0f} | Lev: {lev}x")

        record_trade({
            "action": "OPEN", "asset": best["asset"], "direction": best["direction"],
            "entryPrice": eprice, "size": float(res.get("size", 0)),
            "margin": margin, "leverage": lev, "strategyKey": condor_strat["_key"],
            "entrySource": "auto-condor", "entryMode": "CONDOR_HUNT",
            "entryScore": best["score"]
        })
        
        add_pending_entry({
            "asset": best["asset"], "direction": best["direction"], "autoEntered": True,
            "strategyKey": condor_strat["_key"], "entryPrice": eprice, "margin": margin,
            "leverage": lev, "score": best["score"], "source": "condor"
        })


def main():
    if not acquire_lock("condor-scanner"): return
    try:
        record_heartbeat("condor")
        scan()
    finally: release_lock("condor-scanner")

if __name__ == "__main__":
    main()
