#!/usr/bin/env python3
"""
SHARK v1.0 — Liquidation Cascade Front-Runner.

Consolidated pipeline (runs every 2 min via APScheduler):
  1. OI Tracker   — snapshot OI for top 60 assets
  2. Liq Mapper   — estimate liquidation zones from OI buildup + funding
  3. Proximity    — score assets approaching their liq zones
  4. Entry        — fire if >= 2 cascade triggers + anti-patterns clear
  5. Risk Guard   — invalidate if OI increases >2% after entry (cascade dead)

State machine: SCANNING → STALKING → STRIKE → RIDING
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso, load_json, save_json,
    mcporter_call, mcporter_call_retry, send_telegram, current_regime_params,
    check_directional_exposure_limit, attach_position_playbook,
    count_open_slots, get_enabled_strategies, get_strategy_state_dir,
    is_entries_allowed, record_heartbeat, record_trade, add_pending_entry,
    is_rotation_cooled_down, git_sync,
    POSITION_STATE_DIR, CONFIG_DIR,
)


SHARK_CONFIG_FILE = CONFIG_DIR / "shark-config.json"
SHARK_STATE_FILE = POSITION_STATE_DIR / "shark-state.json"
SHARK_OI_HISTORY_FILE = POSITION_STATE_DIR / "shark-oi-history.json"
SHARK_LIQ_MAP_FILE = POSITION_STATE_DIR / "shark-liq-map.json"

# BTC-correlated assets — max 1 correlated position at a time
BTC_CORRELATED = {
    "BTC", "ETH", "SOL", "AVAX", "DOGE", "SHIB", "PEPE", "WIF", "BONK",
    "ADA", "DOT", "LINK", "MATIC", "NEAR", "APT", "SUI", "SEI", "TIA",
    "INJ", "FET", "RNDR", "WLD", "ARB", "OP", "STRK", "JUP", "PYTH",
    "JTO", "MEME", "ORDI", "STX", "XRP", "LTC", "BCH", "ETC",
}


# ─── Helpers ──────────────────────────────────────────────────

def load_config() -> dict:
    return load_json(SHARK_CONFIG_FILE, default={})


def load_state() -> dict:
    return load_json(SHARK_STATE_FILE, default={
        "stalking": [],
        "strike": [],
        "activePositions": {},
        "lastRunAt": None,
    })


def save_state(state: dict):
    state["lastRunAt"] = now_iso()
    save_json(SHARK_STATE_FILE, state)


def estimate_leverage_from_funding(funding_rate: float) -> float:
    """Estimate average leverage from funding rate (senpi-skills formula)."""
    abs_rate = abs(funding_rate)
    if abs_rate <= 0.0000125:
        return 5.0
    if abs_rate <= 0.0001:
        t = (abs_rate - 0.0000125) / (0.0001 - 0.0000125)
        return 5.0 + t * 4.0
    if abs_rate <= 0.0005:
        t = (abs_rate - 0.0001) / (0.0005 - 0.0001)
        return 9.0 + t * 8.5
    if abs_rate <= 0.001:
        t = (abs_rate - 0.0005) / (0.001 - 0.0005)
        return 17.5 + t * 7.5
    return 25.0


def is_btc_correlated(asset: str) -> bool:
    clean = asset.replace("xyz:", "").upper()
    return clean in BTC_CORRELATED


def has_correlated_position(active_positions: dict) -> bool:
    return any(is_btc_correlated(a) for a in active_positions)


def get_sm_direction(asset: str) -> tuple[str | None, float, int]:
    """Get SM direction, concentration %, and trader count for an asset."""
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
    return None, 0, 0


def price_momentum_from_snapshots(snapshots: list[dict], lookback: int = 3) -> float:
    """Compute price momentum from OI snapshots."""
    if len(snapshots) < lookback + 1:
        return 0.0
    old_price = snapshots[-(lookback + 1)].get("price", 0)
    new_price = snapshots[-1].get("price", 0)
    if old_price <= 0:
        return 0.0
    return (new_price - old_price) / old_price


# ─── Phase 1: OI Tracker ─────────────────────────────────────

def run_oi_tracker(config: dict) -> dict:
    """Snapshot OI for top assets. Returns current OI history."""
    tracker_cfg = config.get("oiTracker", {})
    max_assets = tracker_cfg.get("maxAssets", 60)
    max_snapshots = tracker_cfg.get("maxSnapshotsPerAsset", 288)

    result = mcporter_call_retry("market_list_instruments", {}, timeout=20)
    if "error" in result:
        log(f"SHARK OI: instrument fetch failed: {result.get('error')}")
        return load_json(SHARK_OI_HISTORY_FILE, default={})

    instruments = result.get("data", result)
    if isinstance(instruments, dict):
        instruments = instruments.get("instruments", instruments.get("universe", []))
    if not isinstance(instruments, list):
        return load_json(SHARK_OI_HISTORY_FILE, default={})

    now_ts = int(time.time())
    snapshots = []

    for inst in instruments:
        name = inst.get("name", inst.get("token", ""))
        if not name or inst.get("is_delisted"):
            continue
        if name.startswith("xyz:"):
            continue

        ctx = inst.get("context", inst)
        oi = float(ctx.get("openInterest", ctx.get("oi", 0)))
        price = float(ctx.get("markPx", ctx.get("price", ctx.get("markPrice", 0))))
        funding = float(ctx.get("funding", 0))

        if oi <= 0 or price <= 0:
            continue

        oi_usd = oi * price
        snapshots.append({
            "asset": name, "ts": now_ts, "oi": oi,
            "price": price, "funding": funding, "oi_usd": oi_usd,
        })

    snapshots.sort(key=lambda x: x["oi_usd"], reverse=True)
    top_assets = {s["asset"] for s in snapshots[:max_assets]}

    history = load_json(SHARK_OI_HISTORY_FILE, default={})
    for snap in snapshots[:max_assets]:
        asset = snap["asset"]
        if asset not in history:
            history[asset] = []
        history[asset].append({
            "ts": snap["ts"], "oi": snap["oi"], "price": snap["price"],
            "funding": snap["funding"], "oi_usd": snap["oi_usd"],
        })
        if len(history[asset]) > max_snapshots:
            history[asset] = history[asset][-max_snapshots:]

    cutoff = now_ts - 7200
    evict = [a for a in history if a not in top_assets
             and history[a][-1]["ts"] < cutoff]
    for a in evict:
        del history[a]

    save_json(SHARK_OI_HISTORY_FILE, history)
    return history


# ─── Phase 2: Liq Mapper ─────────────────────────────────────

def estimate_liq_zones(asset: str, entries: list[dict]) -> dict | None:
    """Estimate liquidation zones from OI buildup history."""
    if len(entries) < 6:
        return None

    current = entries[-1]
    current_funding = current["funding"]
    avg_leverage = estimate_leverage_from_funding(current_funding)

    long_buildup_w = 0.0
    long_buildup_t = 0.0
    short_buildup_w = 0.0
    short_buildup_t = 0.0

    for i in range(1, len(entries)):
        prev, curr = entries[i - 1], entries[i]
        oi_delta = curr["oi"] - prev["oi"]
        if oi_delta <= 0:
            continue
        oi_usd_added = oi_delta * curr["price"]
        if curr["funding"] >= 0:
            long_buildup_w += curr["price"] * oi_usd_added
            long_buildup_t += oi_usd_added
        else:
            short_buildup_w += curr["price"] * oi_usd_added
            short_buildup_t += oi_usd_added

    zones = {}
    if long_buildup_t > 0:
        we = long_buildup_w / long_buildup_t
        zones["long_liq"] = {
            "price": round(we * (1 - 1 / avg_leverage), 6),
            "direction": "SHORT",
            "buildup_usd": long_buildup_t,
            "avg_entry": round(we, 6),
            "avg_leverage": round(avg_leverage, 1),
        }
    if short_buildup_t > 0:
        we = short_buildup_w / short_buildup_t
        zones["short_liq"] = {
            "price": round(we * (1 + 1 / avg_leverage), 6),
            "direction": "LONG",
            "buildup_usd": short_buildup_t,
            "avg_entry": round(we, 6),
            "avg_leverage": round(avg_leverage, 1),
        }
    return zones if zones else None


def score_asset(asset: str, zones: dict, entries: list[dict], config: dict) -> list[dict]:
    """Score each liq zone for an asset."""
    mapper_cfg = config.get("liqMapper", {})
    oi_min = mapper_cfg.get("oiBuildupMinUsd", 5_000_000)
    prox_threshold = mapper_cfg.get("proximityThreshold", 0.07)
    current_price = entries[-1]["price"]
    candidates = []

    for zone_key, zone in zones.items():
        zone_price = zone["price"]
        buildup_usd = zone["buildup_usd"]
        direction = zone["direction"]
        leverage = zone["avg_leverage"]

        oi_score = min(1.0, buildup_usd / (oi_min * 5)) if buildup_usd >= oi_min \
            else buildup_usd / oi_min * 0.5

        lev_score = min(1.0, (leverage - 5) / 15) if leverage >= 10 \
            else max(0, (leverage - 5) / 10)

        distance = abs(current_price - zone_price) / current_price if current_price > 0 else 1.0
        prox_score = 1.0 - (distance / prox_threshold) if distance <= prox_threshold else 0.0

        momentum = price_momentum_from_snapshots(entries, lookback=3)
        mom_threshold = 0.03 / leverage if leverage > 0 else 0.03
        if direction == "SHORT":
            mom_score = max(0, min(1.0, -momentum / mom_threshold))
        else:
            mom_score = max(0, min(1.0, momentum / mom_threshold))

        thin_score = 0.5

        total = (oi_score * 0.25 + lev_score * 0.20 + prox_score * 0.25 +
                 mom_score * 0.20 + thin_score * 0.10)

        candidates.append({
            "asset": asset, "zone_key": zone_key, "zone_price": zone_price,
            "direction": direction, "score": round(total, 3),
            "distance_pct": round(distance * 100, 2),
            "buildup_usd": buildup_usd, "leverage": leverage,
        })

    return candidates


def run_liq_mapper(oi_history: dict, config: dict, state: dict) -> list[dict]:
    """Estimate liq zones and score all assets."""
    stalking_threshold = config.get("liqMapper", {}).get("stalkingThreshold", 0.42)
    all_candidates = []
    liq_map = {}

    for asset, entries in oi_history.items():
        zones = estimate_liq_zones(asset, entries)
        if not zones:
            continue
        liq_map[asset] = zones
        all_candidates.extend(score_asset(asset, zones, entries, config))

    save_json(SHARK_LIQ_MAP_FILE, liq_map)

    stalking = [c for c in all_candidates if c["score"] >= stalking_threshold]
    stalking.sort(key=lambda x: x["score"], reverse=True)
    state["stalking"] = [c["asset"] for c in stalking]
    return stalking


# ─── Phase 3: Proximity Scanner ──────────────────────────────

def compute_oi_crack(entries: list[dict], config: dict) -> float:
    """Check if OI is cracking (dropping)."""
    if len(entries) < 4:
        return 0.0
    oi_crack_pct = config.get("proximity", {}).get("oiCrackPct", 0.01)
    recent_oi = entries[-1]["oi"]
    lookback_oi = entries[-3]["oi"]
    if lookback_oi <= 0:
        return 0.0
    pct_change = (recent_oi - lookback_oi) / lookback_oi
    if pct_change >= 0:
        return 0.0
    drop = abs(pct_change)
    if drop >= oi_crack_pct:
        return min(1.0, drop / 0.05)
    return drop / oi_crack_pct * 0.2


def compute_volume_surge(asset: str, config: dict) -> float:
    """Check for volume explosion on 15m candles."""
    surge_mult = config.get("proximity", {}).get("volumeSurgeMult", 2.0)
    data = mcporter_call("market_get_asset_data", {
        "asset": asset, "candle_intervals": ["15m"]
    })
    if "error" in data:
        return 0.0
    candle_data = data.get("data", data)
    candles = candle_data.get("candles", {}).get("15m", [])
    if len(candles) < 6:
        return 0.0
    recent = sum(float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-3:])
    earlier = [float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-12:-3]]
    if not earlier:
        return 0.0
    avg = sum(earlier) / len(earlier) * 3
    if avg <= 0:
        return 0.0
    ratio = recent / avg
    if ratio >= surge_mult:
        return min(1.0, (ratio - 1) / 3)
    return 0.0


def score_proximity(candidate: dict, entries: list[dict], config: dict) -> float:
    """Score proximity of a stalking candidate to its liq zone."""
    distance_gate = config.get("proximity", {}).get("distanceGate", 0.05)
    distance_pct = candidate["distance_pct"] / 100
    if distance_pct > distance_gate:
        return 0.0

    direction = candidate["direction"]
    leverage = candidate["leverage"]

    momentum = price_momentum_from_snapshots(entries, lookback=3)
    mom_threshold = 0.02 / leverage if leverage > 0 else 0.02
    if direction == "SHORT":
        mom_score = max(0, min(1.0, -momentum / mom_threshold))
    else:
        mom_score = max(0, min(1.0, momentum / mom_threshold))

    oi_crack = compute_oi_crack(entries, config)
    vol_surge = compute_volume_surge(candidate["asset"], config)
    book_thin = 0.5

    return (mom_score * 0.30 + oi_crack * 0.30 +
            vol_surge * 0.20 + book_thin * 0.20)


def run_proximity(stalking: list[dict], oi_history: dict,
                  config: dict, state: dict) -> list[dict]:
    """Score proximity for stalking assets. Returns strike candidates."""
    strike_threshold = config.get("proximity", {}).get("strikeThreshold", 0.45)
    strike_candidates = []

    for candidate in stalking:
        entries = oi_history.get(candidate["asset"], [])
        if not entries:
            continue
        prox_score = score_proximity(candidate, entries, config)
        candidate["proximity_score"] = round(prox_score, 3)
        if prox_score >= strike_threshold:
            strike_candidates.append(candidate)

    state["strike"] = [c["asset"] for c in strike_candidates]
    return strike_candidates


# ─── Phase 4: Cascade Entry ──────────────────────────────────

def check_anti_patterns(asset: str, direction: str, entries: list[dict],
                        zone_price: float, leverage: float,
                        active_positions: dict, config: dict) -> tuple[bool, str]:
    """Check all anti-patterns. Returns (passed, reason)."""
    entry_cfg = config.get("entry", {})
    if len(entries) < 2:
        return False, "insufficient_data"

    current_price = entries[-1]["price"]
    recent_oi = entries[-1]["oi"]
    prev_oi = entries[-2]["oi"]

    # AP1: OI increasing
    oi_block = entry_cfg.get("oiIncreasingBlock", 0.005)
    if prev_oi > 0 and (recent_oi - prev_oi) / prev_oi > oi_block:
        return False, "oi_increasing"

    # AP2: Cascade already done (>10% OI drop in 30min)
    chase_threshold = entry_cfg.get("oiChaseThreshold", 0.10)
    lookback_idx = max(0, len(entries) - 7)
    lookback_oi = entries[lookback_idx]["oi"]
    if lookback_oi > 0:
        oi_drop_30m = (lookback_oi - recent_oi) / lookback_oi
        if oi_drop_30m > chase_threshold:
            return False, "cascade_already_done"

    # AP3: Price already through zone
    price_chase = entry_cfg.get("priceChasePct", 0.05)
    adj_chase = price_chase / leverage if leverage > 0 else price_chase
    if direction == "SHORT" and zone_price > 0:
        through = (zone_price - current_price) / zone_price
        if through > adj_chase:
            return False, "price_through_zone"
    elif direction == "LONG" and zone_price > 0:
        through = (current_price - zone_price) / zone_price
        if through > adj_chase:
            return False, "price_through_zone"

    # AP4: BTC correlation limit
    if is_btc_correlated(asset) and has_correlated_position(active_positions):
        return False, "btc_correlated_limit"

    return True, ""


def detect_triggers(asset: str, direction: str, entries: list[dict],
                    zone_price: float, config: dict) -> list[dict]:
    """Detect cascade triggers. Returns list of fired triggers."""
    entry_cfg = config.get("entry", {})
    triggers = []
    if len(entries) < 2:
        return triggers

    current = entries[-1]
    prev = entries[-2]
    current_price = current["price"]

    # T1: OI drop (HIGH)
    oi_thresh = entry_cfg.get("oiDropThreshold", 0.03)
    if prev["oi"] > 0:
        oi_change = (current["oi"] - prev["oi"]) / prev["oi"]
        if oi_change < -oi_thresh:
            triggers.append({"trigger": "oi_drop", "confidence": "HIGH",
                             "value": round(oi_change * 100, 2)})

    # T2: Price breaks into zone (HIGH)
    if direction == "SHORT" and current_price <= zone_price:
        triggers.append({"trigger": "zone_break", "confidence": "HIGH"})
    elif direction == "LONG" and current_price >= zone_price:
        triggers.append({"trigger": "zone_break", "confidence": "HIGH"})

    # T3: Funding spike (MEDIUM)
    spike_mult = entry_cfg.get("fundingSpikeMult", 2.0)
    if len(entries) >= 12:
        avg_recent = sum(abs(e["funding"]) for e in entries[-3:]) / 3
        older = [abs(e["funding"]) for e in entries[-12:-3]]
        avg_older = sum(older) / len(older) if older else 0
        if avg_older > 0 and avg_recent / avg_older >= spike_mult:
            triggers.append({"trigger": "funding_spike", "confidence": "MEDIUM",
                             "value": round(avg_recent / avg_older, 1)})

    # T4: Volume explosion (MEDIUM)
    vol_mult = entry_cfg.get("volumeExplosionMult", 3.0)
    vol_data = mcporter_call("market_get_asset_data", {
        "asset": asset, "candle_intervals": ["5m"]
    })
    if "error" not in vol_data:
        candle_data = vol_data.get("data", vol_data)
        candles = candle_data.get("candles", {}).get("5m", [])
        if len(candles) >= 12:
            recent_vol = float(candles[-1].get("volume", candles[-1].get("v",
                              candles[-1].get("vlm", 0))))
            avg_vols = [float(c.get("volume", c.get("v", c.get("vlm", 0))))
                        for c in candles[-12:-1]]
            avg_vol = sum(avg_vols) / len(avg_vols) if avg_vols else 0
            if avg_vol > 0 and recent_vol / avg_vol >= vol_mult:
                triggers.append({"trigger": "volume_explosion", "confidence": "MEDIUM",
                                 "value": round(recent_vol / avg_vol, 1)})

    # T5: SM already positioned (HIGH)
    sm_min = entry_cfg.get("smMinConcentration", 3.0)
    sm_dir, sm_pct, _ = get_sm_direction(asset)
    if sm_dir == direction and sm_pct > sm_min:
        triggers.append({"trigger": "sm_positioned", "confidence": "HIGH",
                         "value": round(sm_pct, 1)})

    return triggers


def check_candle_confirmation(asset: str, direction: str) -> bool:
    """Check 15m candle structure confirms direction."""
    data = mcporter_call("market_get_asset_data", {
        "asset": asset, "candle_intervals": ["15m"]
    })
    if "error" in data:
        return True  # Soft gate
    candle_data = data.get("data", data)
    candles = candle_data.get("candles", {}).get("15m", [])
    if len(candles) < 5:
        return True
    last_4 = candles[-4:]
    if direction == "LONG":
        lows = [float(c.get("low", c.get("l", 0))) for c in last_4]
        return sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1]) >= 2
    else:
        highs = [float(c.get("high", c.get("h", 0))) for c in last_4]
        return sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1]) >= 2


def build_dsl_state(asset: str, direction: str, trigger_count: int,
                    entry_price: float, config: dict) -> dict:
    """Build DSL state for a SHARK entry."""
    dsl_cfg = config.get("dsl", {})
    leverage = config.get("leverage", {}).get("default", 8)

    conviction = dsl_cfg.get("convictionTiers", [])
    p1_cfg = conviction[-1] if conviction else {}
    for tier in conviction:
        if trigger_count >= tier.get("minScore", 0):
            p1_cfg = tier
            break

    return {
        "active": True,
        "asset": asset,
        "direction": direction,
        "entryPrice": entry_price,
        "leverage": leverage,
        "phase": 1,
        "lockMode": dsl_cfg.get("lockMode", "pct_of_high_water"),
        "phase2TriggerRoe": dsl_cfg.get("phase2TriggerRoe", 7),
        "highWaterPrice": entry_price,
        "highWaterRoe": 0,
        "currentTierIndex": -1,
        "currentBreachCount": 0,
        "createdAt": now_iso(),
        "highWaterUpdatedAt": now_iso(),
        "entryMode": "SHARK_CASCADE",
        "entryScore": trigger_count,
        "phase1": {
            "absoluteFloorRoe": p1_cfg.get("absoluteFloorRoe", -20),
            "hardTimeoutSec": p1_cfg.get("hardTimeoutMin", 45) * 60,
            "weakPeakCutSec": p1_cfg.get("weakPeakCutMin", 20) * 60,
            "deadWeightCutMin": p1_cfg.get("deadWeightCutMin", 15),
        },
        "tiers": dsl_cfg.get("tiers", []),
        "stagnationTp": dsl_cfg.get("stagnationTp", {
            "enabled": True, "roeMin": 10, "hwStaleMin": 45
        }),
    }


def run_entry(strike_candidates: list[dict], oi_history: dict,
              config: dict, state: dict) -> bool:
    """Try to enter on strike candidates. Returns True if entry made."""
    entry_cfg = config.get("entry", {})
    min_triggers = entry_cfg.get("minTriggers", 2)
    active = state.get("activePositions", {})

    strategies = get_enabled_strategies()
    shark_strat = None
    for s in strategies:
        if count_open_slots(s) > 0:
            shark_strat = s
            break
    if not shark_strat or not is_entries_allowed():
        return False

    strike_candidates.sort(key=lambda c: c.get("proximity_score", 0), reverse=True)

    for candidate in strike_candidates:
        asset = candidate["asset"]
        direction = candidate["direction"]
        zone_price = candidate["zone_price"]
        leverage = candidate.get("leverage", 8)
        entries = oi_history.get(asset, [])

        if asset in active or asset.startswith("xyz:"):
            continue
        if is_rotation_cooled_down(asset, 45):
            continue

        passed, reason = check_anti_patterns(
            asset, direction, entries, zone_price, leverage, active, config)
        if not passed:
            log(f"SHARK {asset}: anti-pattern blocked ({reason})")
            continue

        triggers = detect_triggers(asset, direction, entries, zone_price, config)
        if len(triggers) < min_triggers:
            continue

        sm_dir, sm_pct, sm_traders = get_sm_direction(asset)
        if sm_dir and sm_dir != direction:
            continue

        if not check_candle_confirmation(asset, direction):
            continue

        # Place entry
        budget = float(shark_strat.get("budget", 1000))
        margin_pct = config.get("marginPct", 0.18)
        margin = budget * margin_pct
        lev = config.get("leverage", {}).get("default", 8)
        allowed_exposure, exposure = check_directional_exposure_limit(direction, margin, lev)
        if not allowed_exposure:
            log(
                f"SHARK: directional cap blocked {asset} {direction} "
                f"projected={exposure['offendingPct']:.1f}% cap={exposure['capPct']:.1f}%"
            )
            continue

        res = mcporter_call_retry("create_position", {
            "strategyId": shark_strat.get("strategyId"),
            "asset": asset, "direction": direction,
            "margin": round(margin, 2), "leverage": lev,
            "stopLossRoe": entry_cfg.get("slRoePct", 5.0),
            "orderType": config.get("execution", {}).get("entryOrderType",
                                                          "FEE_OPTIMIZED_LIMIT"),
        }, timeout=30)

        if "error" in res:
            log(f"SHARK entry failed for {asset}: {res.get('error')}")
            continue

        entry_price = float(res.get("entryPrice", 0))
        size = float(res.get("size", 0))

        dsl = build_dsl_state(asset, direction, len(triggers), entry_price, config)
        dsl["wallet"] = shark_strat.get("wallet")
        dsl["strategyId"] = shark_strat.get("strategyId")
        dsl["strategyKey"] = shark_strat["_key"]
        dsl["size"] = size
        attach_position_playbook(
            dsl,
            scanner="shark",
            margin=margin,
            leverage=lev,
            score=len(triggers),
            reasons=[t["trigger"] for t in triggers],
            sm_snapshot={
                "traderCount": sm_traders,
                "concentration": sm_pct,
            },
            setup={
                "zonePrice": zone_price,
                "distancePct": candidate.get("distance_pct"),
            },
        )
        sdir = get_strategy_state_dir(shark_strat["_key"])
        save_json(sdir / f"dsl-{asset}.json", dsl)

        cascade_oi = entries[-1]["oi"] if entries else 0
        active[asset] = {
            "direction": direction, "entry_price": entry_price,
            "opened_at": now_iso(), "cascade_oi_at_entry": cascade_oi,
            "zone_price": zone_price, "triggers": triggers,
            "margin": margin, "leverage": lev,
        }

        trigger_names = ", ".join(f"{t['trigger']}({t['confidence']})" for t in triggers)
        send_telegram(
            f"🦈 SHARK ENTRY: {direction} {asset}\n"
            f"Triggers: {trigger_names}\n"
            f"Zone: ${zone_price:.2f} | Entry: ${entry_price:.4f}\n"
            f"Dist: {candidate['distance_pct']:.1f}% | Margin: ${margin:.0f} | Lev: {lev}x"
        )

        record_trade({
            "action": "OPEN", "asset": asset, "direction": direction,
            "entryPrice": entry_price, "size": size,
            "margin": margin, "leverage": lev,
            "strategyKey": shark_strat["_key"],
            "entrySource": "auto-shark", "entryMode": "SHARK_CASCADE",
            "entryScore": len(triggers),
        })

        add_pending_entry({
            "asset": asset, "direction": direction, "autoEntered": True,
            "strategyKey": shark_strat["_key"], "entryPrice": entry_price,
            "margin": margin, "leverage": lev,
            "score": candidate["score"], "source": "shark",
        })

        return True

    return False


# ─── Phase 5: Risk Guardian ──────────────────────────────────

def run_risk_guard(oi_history: dict, config: dict, state: dict):
    """Check cascade invalidation on active SHARK positions."""
    oi_invalidation = config.get("risk", {}).get("oiInvalidationPct", 0.02)
    active = state.get("activePositions", {})
    if not active:
        return

    to_remove = []
    for asset, pos in list(active.items()):
        entries = oi_history.get(asset, [])
        if not entries:
            continue

        oi_at_entry = pos.get("cascade_oi_at_entry", 0)
        current_oi = entries[-1]["oi"]
        if oi_at_entry <= 0:
            continue

        oi_change = (current_oi - oi_at_entry) / oi_at_entry
        if oi_change > oi_invalidation:
            log(f"SHARK CASCADE INVALIDATION: {asset} OI +{oi_change*100:.1f}%")

            for strat in get_enabled_strategies():
                dsl_file = get_strategy_state_dir(strat["_key"]) / f"dsl-{asset}.json"
                if dsl_file.exists():
                    dsl_state = load_json(dsl_file)
                    if dsl_state.get("active"):
                        mcporter_call_retry("strategy_close_position", {
                            "strategyId": strat.get("strategyId",
                                                     dsl_state.get("strategyId")),
                            "asset": asset,
                        }, timeout=15)
                        dsl_state["active"] = False
                        dsl_state["closedAt"] = now_iso()
                        dsl_state["closeReason"] = "cascade_invalidation"
                        save_json(dsl_file, dsl_state)

                        record_trade({
                            "action": "CLOSE", "asset": asset,
                            "direction": pos.get("direction", ""),
                            "closeReason": "cascade_invalidation",
                            "strategyKey": strat["_key"],
                            "entrySource": "auto-shark",
                            "entryMode": "SHARK_CASCADE",
                        })

                        send_telegram(
                            f"🦈❌ SHARK INVALIDATED: {asset}\n"
                            f"OI +{oi_change*100:.1f}% since entry\n"
                            f"New positions opening, not liquidating — thesis dead"
                        )
                        break

            to_remove.append(asset)

    for asset in to_remove:
        del active[asset]

    # Clean stale entries (position closed externally or by DSL)
    stale = []
    for asset in active:
        found = False
        for strat in get_enabled_strategies():
            dsl_file = get_strategy_state_dir(strat["_key"]) / f"dsl-{asset}.json"
            if dsl_file.exists():
                if load_json(dsl_file).get("active"):
                    found = True
                    break
        if not found:
            stale.append(asset)
    for asset in stale:
        del active[asset]


# ─── Main Pipeline ───────────────────────────────────────────

def scan():
    config = load_config()
    if not config:
        log("SHARK: no config found — skipping")
        return

    state = load_state()
    oi_history = run_oi_tracker(config)
    stalking = run_liq_mapper(oi_history, config, state)
    strike_candidates = run_proximity(stalking, oi_history, config, state)

    entered = False
    if strike_candidates:
        entered = run_entry(strike_candidates, oi_history, config, state)

    run_risk_guard(oi_history, config, state)
    save_state(state)

    if entered:
        git_sync("auto: SHARK cascade entry")

    stalking_ct = len(state.get("stalking", []))
    strike_ct = len(state.get("strike", []))
    active_ct = len(state.get("activePositions", {}))
    if stalking_ct > 0 or strike_ct > 0 or active_ct > 0:
        log(f"SHARK: stalking={stalking_ct} strike={strike_ct} active={active_ct}")


def main():
    if not acquire_lock("shark-scanner"):
        return
    try:
        record_heartbeat("shark")
        scan()
    finally:
        release_lock("shark-scanner")


if __name__ == "__main__":
    main()
