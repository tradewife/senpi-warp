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
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    record_heartbeat,
    add_pending_entry,
    git_sync,
    POSITION_STATE_DIR,
    CONFIG_DIR,
)


SHARK_CONFIG_FILE = CONFIG_DIR / "shark-config.json"
SHARK_STATE_FILE = POSITION_STATE_DIR / "shark-state.json"
SHARK_OI_HISTORY_FILE = POSITION_STATE_DIR / "shark-oi-history.json"
SHARK_LIQ_MAP_FILE = POSITION_STATE_DIR / "shark-liq-map.json"

# BTC-correlated assets — max 1 correlated position at a time
BTC_CORRELATED = {
    "BTC",
    "ETH",
    "SOL",
    "AVAX",
    "DOGE",
    "SHIB",
    "PEPE",
    "WIF",
    "BONK",
    "ADA",
    "DOT",
    "LINK",
    "MATIC",
    "NEAR",
    "APT",
    "SUI",
    "SEI",
    "TIA",
    "INJ",
    "FET",
    "RNDR",
    "WLD",
    "ARB",
    "OP",
    "STRK",
    "JUP",
    "PYTH",
    "JTO",
    "MEME",
    "ORDI",
    "STX",
    "XRP",
    "LTC",
    "BCH",
    "ETC",
}


# ─── Helpers ──────────────────────────────────────────────────


def load_config() -> dict:
    return load_json(SHARK_CONFIG_FILE, default={})


def load_state() -> dict:
    return load_json(
        SHARK_STATE_FILE,
        default={
            "stalking": [],
            "strike": [],
            "activePositions": {},
            "lastRunAt": None,
        },
    )


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
    """Scanner depowered — SM direction check disabled. Only evaluator may call MCP."""
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
    """Scanner depowered — OI tracker disabled. Only evaluator may call MCP."""
    return {}


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


def score_asset(
    asset: str, zones: dict, entries: list[dict], config: dict
) -> list[dict]:
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

        oi_score = (
            min(1.0, buildup_usd / (oi_min * 5))
            if buildup_usd >= oi_min
            else buildup_usd / oi_min * 0.5
        )

        lev_score = (
            min(1.0, (leverage - 5) / 15)
            if leverage >= 10
            else max(0, (leverage - 5) / 10)
        )

        distance = (
            abs(current_price - zone_price) / current_price
            if current_price > 0
            else 1.0
        )
        prox_score = (
            1.0 - (distance / prox_threshold) if distance <= prox_threshold else 0.0
        )

        momentum = price_momentum_from_snapshots(entries, lookback=3)
        mom_threshold = 0.03 / leverage if leverage > 0 else 0.03
        if direction == "SHORT":
            mom_score = max(0, min(1.0, -momentum / mom_threshold))
        else:
            mom_score = max(0, min(1.0, momentum / mom_threshold))

        thin_score = 0.5

        total = (
            oi_score * 0.25
            + lev_score * 0.20
            + prox_score * 0.25
            + mom_score * 0.20
            + thin_score * 0.10
        )

        candidates.append(
            {
                "asset": asset,
                "zone_key": zone_key,
                "zone_price": zone_price,
                "direction": direction,
                "score": round(total, 3),
                "distance_pct": round(distance * 100, 2),
                "buildup_usd": buildup_usd,
                "leverage": leverage,
            }
        )

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
    """Scanner depowered — volume check disabled. Only evaluator may call MCP."""
    return 0.0
    candle_data = data.get("data", data)
    candles = candle_data.get("candles", {}).get("15m", [])
    if len(candles) < 6:
        return 0.0
    recent = sum(
        float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-3:]
    )
    earlier = [
        float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-12:-3]
    ]
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

    return mom_score * 0.30 + oi_crack * 0.30 + vol_surge * 0.20 + book_thin * 0.20


def run_proximity(
    stalking: list[dict], oi_history: dict, config: dict, state: dict
) -> list[dict]:
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


def check_anti_patterns(
    asset: str,
    direction: str,
    entries: list[dict],
    zone_price: float,
    leverage: float,
    active_positions: dict,
    config: dict,
) -> tuple[bool, str]:
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


def detect_triggers(
    asset: str, direction: str, entries: list[dict], zone_price: float, config: dict
) -> list[dict]:
    """Scanner depowered — trigger detection disabled. Only evaluator may call MCP."""
    return []


def check_candle_confirmation(asset: str, direction: str) -> bool:
    """Scanner depowered — candle confirmation disabled. Only evaluator may call MCP."""
    return True


def build_dsl_state(
    asset: str, direction: str, trigger_count: int, entry_price: float, config: dict
) -> dict:
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
        "stagnationTp": dsl_cfg.get(
            "stagnationTp", {"enabled": True, "roeMin": 10, "hwStaleMin": 45}
        ),
    }


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

    for candidate in strike_candidates:
        asset = candidate["asset"]
        direction = candidate["direction"]
        log(
            f"SHARK STALKER: {direction} {asset} "
            f"score={candidate['score']} distance={candidate.get('distance_pct', 0):.1f}%"
        )
        add_pending_entry(
            {
                "asset": asset,
                "direction": direction,
                "autoEntered": False,
                "score": candidate["score"],
                "source": "shark",
                "mode": "STALKER",
            }
        )

    save_state(state)

    stalking_ct = len(state.get("stalking", []))
    strike_ct = len(state.get("strike", []))
    if stalking_ct > 0 or strike_ct > 0:
        log(f"SHARK: stalking={stalking_ct} strike={strike_ct}")


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
