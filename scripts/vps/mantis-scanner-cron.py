#!/usr/bin/env python3
"""
MANTIS v3.0 — Dual-Mode Emerging Movers Scanner (Hardened + Contrib Threshold).

MANTIS v3.0 is a variant of the hardened dual-mode scanner with all live
trading lessons applied, plus one experimental scoring tweak:

  Contribution acceleration threshold raised from 0.001 to 0.003, and the
  +1 tier (CONTRIB_POSITIVE) eliminated entirely.

Runs every 90 seconds.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    mcporter_call,
    send_telegram,
    current_regime_params,
    check_directional_exposure_limit,
    attach_position_playbook,
    count_open_slots,
    get_enabled_strategies,
    get_strategy_state_dir,
    is_entries_allowed,
    is_auto_entry_enabled,
    is_rotation_cooled_down,
    POSITION_STATE_DIR,
    CONFIG_DIR,
    record_trade,
    add_pending_entry,
    record_heartbeat,
    get_all_open_positions,
)

MANTIS_CONFIG_FILE = CONFIG_DIR / "mantis-config.json"
SCAN_HISTORY_FILE = POSITION_STATE_DIR / "mantis-scan-history.json"
TRADE_JOURNAL_FILE = (
    Path(__file__).resolve().parent.parent / "memory" / "trade-journal.json"
)

TOP_N = 50
ERRATIC_REVERSAL_THRESHOLD = 5
MIN_LEVERAGE = 7
MAX_LEVERAGE = 10
MAX_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 10
XYZ_BANNED = True

DSL_TIERS = [
    {"triggerPct": 7, "lockHwPct": 40, "consecutiveBreachesRequired": 3},
    {"triggerPct": 12, "lockHwPct": 55, "consecutiveBreachesRequired": 2},
    {"triggerPct": 15, "lockHwPct": 75, "consecutiveBreachesRequired": 2},
    {"triggerPct": 20, "lockHwPct": 85, "consecutiveBreachesRequired": 1},
]

CONVICTION_TIERS = [
    {
        "minScore": 6,
        "absoluteFloorRoe": -18,
        "hardTimeoutMin": 25,
        "weakPeakCutMin": 12,
        "deadWeightCutMin": 8,
    },
    {
        "minScore": 8,
        "absoluteFloorRoe": -25,
        "hardTimeoutMin": 45,
        "weakPeakCutMin": 20,
        "deadWeightCutMin": 15,
    },
    {
        "minScore": 10,
        "absoluteFloorRoe": -30,
        "hardTimeoutMin": 60,
        "weakPeakCutMin": 30,
        "deadWeightCutMin": 20,
    },
]

STAGNATION_TP = {"enabled": True, "roeMin": 10, "hwStaleMin": 45}


# ─── Tech Helpers ─────────────────────────────────────────────


def fetch_markets():
    result = mcporter_call("leaderboard_get_markets", {"limit": 100})
    if "error" in result:
        return None
    data = result.get("data", result)
    raw = data.get("markets", data)
    if isinstance(raw, dict):
        raw = raw.get("markets", [])
    return raw


def parse_scan(raw_markets):
    scan = {"time": now_iso(), "markets": []}
    for i, m in enumerate(raw_markets[:TOP_N]):
        if not isinstance(m, dict):
            continue
        token = m.get("token", m.get("coin", ""))
        dex = m.get("dex", "")
        if XYZ_BANNED and (dex.lower() == "xyz" or token.lower().startswith("xyz:")):
            continue
        scan["markets"].append(
            {
                "token": token,
                "dex": dex,
                "rank": i + 1,
                "direction": m.get("direction", m.get("side", "")),
                "contribution": round(
                    float(m.get("pct_of_top_traders_gain", m.get("longPct", 0))), 6
                ),
                "traders": int(m.get("trader_count", m.get("traders", 0))),
                "price_chg_4h": round(
                    float(m.get("token_price_change_pct_4h", 0) or 0), 4
                ),
            }
        )
    return scan


def get_market_in_scan(scan, token, dex=""):
    for m in scan["markets"]:
        if m["token"] == token and m.get("dex", "") == dex:
            return m
    return None


def check_asset_volume(token, dex=""):
    asset = f"{dex}:{token}" if dex else token
    res = mcporter_call(
        "market_get_asset_data",
        {"asset": asset, "candle_intervals": ["1h"], "include_funding": False},
    )
    if "error" in res:
        return 0, False
    d = res.get("data", res)
    candles = d.get("candles", {}).get("1h", []) if isinstance(d, dict) else []
    if len(candles) < 6:
        return 0, False
    vols = [float(c.get("volume", c.get("v", 0))) for c in candles[-6:]]
    avg_vol = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else 1
    latest_vol = vols[-1] if vols else 0
    ratio = latest_vol / avg_vol if avg_vol > 0 else 0
    return ratio, ratio >= 1.5


def is_erratic_history(rank_history, exclude_last=False):
    nums = [r for r in rank_history if r is not None]
    if exclude_last and len(nums) > 1:
        nums = nums[:-1]
    if len(nums) < 3:
        return False
    for i in range(1, len(nums) - 1):
        prev = nums[i] - nums[i - 1]
        nxt = nums[i + 1] - nums[i]
        if prev < 0 and nxt > ERRATIC_REVERSAL_THRESHOLD:
            return True
        if prev > 0 and nxt < -ERRATIC_REVERSAL_THRESHOLD:
            return True
    return False


def time_of_day_modifier():
    hour = datetime.now(timezone.utc).hour
    if 4 <= hour < 14:
        return 1, "time_bonus_optimal_window"
    if hour >= 18 or hour < 2:
        return -2, "time_penalty_chop_zone"
    return 0, None


def check_4h_alignment(direction, price_chg_4h):
    if direction == "LONG" and price_chg_4h < 0:
        return False
    if direction == "SHORT" and price_chg_4h > 0:
        return False
    return True


# ─── MODE A: STALKER ──────────────────────────────────────────────────


def detect_stalker_signals(current_scan, history, stalker_cfg):
    min_consecutive = stalker_cfg.get("minConsecutiveScans", 3)
    min_total_climb = stalker_cfg.get("minTotalClimb", 8)
    min_score = stalker_cfg.get("minScore", 7)
    require_vol = stalker_cfg.get("requireVolumeBuilding", True)

    prev = history.get("scans", [])
    if len(prev) < min_consecutive:
        return []

    signals = []

    for m in current_scan["markets"]:
        token, dex, rank = m["token"], m.get("dex", ""), m["rank"]
        direction = m["direction"].upper()
        if rank <= 10 or not check_4h_alignment(direction, m.get("price_chg_4h", 0)):
            continue

        rhist, chist = [], []
        for s in prev[-(min_consecutive + 2) :]:
            pm = get_market_in_scan(s, token, dex)
            rhist.append(pm["rank"] if pm else None)
            chist.append(pm["contribution"] if pm else None)
        rhist.append(rank)
        chist.append(m["contribution"])

        valid_r = [(i, r) for i, r in enumerate(rhist) if r is not None]
        if len(valid_r) < min_consecutive + 1:
            continue

        recent_r = [r for _, r in valid_r[-(min_consecutive + 1) :]]
        is_climbing = all(
            recent_r[i] >= recent_r[i + 1] for i in range(len(recent_r) - 1)
        )
        tot_climb = recent_r[0] - recent_r[-1]

        if not is_climbing or tot_climb < min_total_climb:
            continue
        if is_erratic_history(rhist, exclude_last=True):
            continue

        valid_c = [c for c in chist if c is not None]
        vol_build = True
        if require_vol and len(valid_c) >= 3:
            rec_c = valid_c[-3:]
            vol_build = all(rec_c[i] <= rec_c[i + 1] for i in range(len(rec_c) - 1))
        if require_vol and not vol_build:
            continue

        score = 3
        reasons = [f"STALKER_CLIMB +{tot_climb}"]

        THRESH = 0.003
        if len(valid_c) >= 2:
            deltas = [valid_c[i + 1] - valid_c[i] for i in range(len(valid_c) - 1)]
            vel = sum(deltas) / len(deltas)
            if vel > THRESH:
                score += 2
                reasons.append(f"CONTRIB_ACCEL +{vel * 100:.3f}%/scan")

        if m["traders"] >= 10:
            score += 1
            reasons.append("SM_ACTIVE")
        if recent_r[0] >= 30:
            score += 1
            reasons.append("DEEP_START")

        tmod, treas = time_of_day_modifier()
        score += tmod
        if treas:
            reasons.append(treas)

        if score >= min_score:
            signals.append(
                {
                    "token": token,
                    "direction": direction,
                    "mode": "STALKER",
                    "score": score,
                    "reasons": reasons,
                    "rank": rank,
                }
            )

    return signals


# ─── MODE B: STRIKER ──────────────────────────────────────────────────


def detect_striker_signals(current_scan, history, striker_cfg):
    min_score = striker_cfg.get("minScore", 9)
    min_reasons = striker_cfg.get("minReasons", 4)
    min_jump = striker_cfg.get("minRankJump", 15)
    min_vel_ovr = striker_cfg.get("minVelocityOverride", 15)
    min_vel_floor = striker_cfg.get("minVelocityFloor", 10)
    req_vol = striker_cfg.get("requireVolumeConfirmation", True)

    prev = history.get("scans", [])
    if not prev:
        return []

    lat_prev = prev[-1]
    old_av = prev[-min(len(prev), 5)]
    ptop = {(m["token"], m.get("dex", "")) for m in lat_prev["markets"]}

    signals = []

    for m in current_scan["markets"]:
        token, dex, rank = m["token"], m.get("dex", ""), m["rank"]
        dirn = m["direction"].upper()
        if rank <= 10 or not check_4h_alignment(dirn, m.get("price_chg_4h", 0)):
            continue

        pm = get_market_in_scan(lat_prev, token, dex)
        om = get_market_in_scan(old_av, token, dex)
        if not pm:
            continue

        jump = pm["rank"] - rank
        is_fj = False
        is_imm = False
        is_cexp = False
        reasons = []

        if jump >= 10 and pm["rank"] >= 25:
            is_imm = True
            reasons.append(f"IMMEDIATE_MOVER +{jump}")
            if not (token, dex) in ptop or pm["rank"] >= 30:
                is_fj = True
                reasons.append("FIRST_JUMP")

        if pm["contribution"] > 0:
            cratio = m["contribution"] / pm["contribution"]
            if cratio >= 3.0:
                is_cexp = True
                reasons.append(f"CONTRIB_EXPLOSION {cratio:.1f}x")

        if not is_fj and not is_imm:
            continue

        rcontribs = []
        for s in prev[-5:]:
            sm = get_market_in_scan(s, token, dex)
            if sm:
                rcontribs.append(sm["contribution"])
        rcontribs.append(m["contribution"])
        cvel = 0
        if len(rcontribs) >= 2:
            dels = [rcontribs[i + 1] - rcontribs[i] for i in range(len(rcontribs) - 1)]
            cvel = sum(dels) / len(dels) * 100

        avel = abs(cvel)
        if jump < min_jump and avel < min_vel_ovr:
            continue

        if avel < min_vel_floor and not (is_fj and cvel > 0):
            continue

        score = 0
        if is_fj:
            score += 3
        if is_imm:
            score += 2
        if is_cexp:
            score += 2
        if avel > 10:
            score += 2
            reasons.append(f"HIGH_VELOCITY {avel:.1f}")
        if pm["rank"] >= 40:
            score += 1
            reasons.append("DEEP_CLIMBER")
        if om and om["rank"] - rank >= 10:
            score += 1
            reasons.append("CLIMBING")

        tmod, treas = time_of_day_modifier()
        score += tmod
        if treas:
            reasons.append(treas)

        if score < min_score or len(reasons) < min_reasons:
            continue

        if req_vol:
            vrat, vstr = check_asset_volume(token, dex)
            if not vstr:
                continue
            reasons.append(f"VOL_CONFIRMED {vrat:.1f}x")

        signals.append(
            {
                "token": token,
                "direction": dirn,
                "mode": "STRIKER",
                "score": score,
                "reasons": reasons,
                "rank": rank,
            }
        )

    return signals


# ─── Entry Processing ─────────────────────────────────────────────────


def build_dsl_state(sig, config, price):
    score = sig.get("score", 6)
    tier = CONVICTION_TIERS[0]
    for ct in CONVICTION_TIERS:
        if score >= ct["minScore"]:
            tier = ct

    return {
        "active": True,
        "asset": sig["token"],
        "direction": sig["direction"],
        "mode": sig["mode"],
        "score": score,
        "phase": 1,
        "highWaterPrice": price,
        "highWaterRoe": 0,
        "currentTierIndex": -1,
        "consecutiveBreaches": 0,
        "lockMode": "pct_of_high_water",
        "phase2TriggerRoe": 7,
        "phase1": {
            "enabled": True,
            "retraceThreshold": 0.03,
            "consecutiveBreachesRequired": 3,
            "phase1MaxMinutes": tier["hardTimeoutMin"],
            "weakPeakCutMinutes": tier["weakPeakCutMin"],
            "deadWeightCutMin": tier["deadWeightCutMin"],
            "absoluteFloorRoe": tier["absoluteFloorRoe"],
        },
        "phase2": {
            "enabled": True,
            "retraceThreshold": 0.015,
            "consecutiveBreachesRequired": 2,
        },
        "tiers": DSL_TIERS,
        "stagnationTp": STAGNATION_TP,
        "convictionTiers": CONVICTION_TIERS,
        "createdAt": now_iso(),
    }


def try_auto_entry(sig, strategies, config):
    if len(get_all_open_positions()) >= MAX_POSITIONS:
        return

    target_strat = None
    for strat in strategies:
        if count_open_slots(strat) > 0:
            target_strat = strat
            break
    if not target_strat:
        return

    regime = current_regime_params()
    budget = target_strat.get("budget", 1000)
    alloc = regime.get("allocPctPerSlot", 30) / 100
    margin = budget * alloc
    lev = min(MAX_LEVERAGE, max(MIN_LEVERAGE, strat.get("leverage", 10)))

    allowed, exp = check_directional_exposure_limit(sig["direction"], margin, lev)
    if not allowed:
        return

    res = mcporter_call(
        "create_position",
        {
            "strategyId": target_strat.get("strategyId"),
            "asset": sig["token"],
            "direction": sig["direction"],
            "margin": margin,
            "leverage": lev,
            "orderType": "MARKET",
        },
    )

    if "error" not in res:
        eprice = float(res.get("entryPrice", 0))
        dsl = build_dsl_state(sig, config, eprice)
        dsl["wallet"] = target_strat.get("wallet")
        dsl["strategyId"] = target_strat.get("strategyId")
        dsl["strategyKey"] = target_strat["_key"]
        dsl["entrySource"] = f"mantis-{sig['mode'].lower()}"

        attach_position_playbook(
            dsl,
            scanner="mantis",
            margin=margin,
            leverage=lev,
            score=sig["score"],
            reasons=sig["reasons"],
        )
        sfile = (
            get_strategy_state_dir(target_strat["_key"]) / f"dsl-{sig['token']}.json"
        )
        save_json(sfile, dsl)

        log(
            f"MANTIS: Auto-entered {sig['mode']} {sig['direction']} {sig['token']} @ ${eprice:.2f}"
        )
        send_telegram(
            f"🦗 MANTIS ENTRY: {sig['mode']} {sig['direction']} {sig['token']}\nScore: {sig['score']}\nMargin: ${margin:.0f}"
        )

        record_trade(
            {
                "action": "OPEN",
                "asset": sig["token"],
                "direction": sig["direction"],
                "entryPrice": eprice,
                "size": float(res.get("size", 0)),
                "margin": margin,
                "leverage": lev,
                "strategyKey": target_strat["_key"],
                "entrySource": f"mantis-{sig['mode'].lower()}",
                "entryMode": sig["mode"],
            }
        )


def run():
    config = load_json(MANTIS_CONFIG_FILE)
    if not config:
        return

    rm = fetch_markets()
    if not rm:
        return
    cur = parse_scan(rm)

    hist = load_json(SCAN_HISTORY_FILE, default={"scans": []})

    # Check streak
    tr_hist = load_json(TRADE_JOURNAL_FILE, default=[])
    sr = []
    for t in reversed(tr_hist):
        if t.get("action") == "CLOSE" and t.get("entrySource") == "mantis-stalker":
            sr.append("W" if t.get("realizedPnl", 0) > 0 else "L")
        if len(sr) >= 3:
            break
    sr.reverse()
    streak_active = len(sr) >= 3 and all(r == "L" for r in sr[-3:])

    stalker = detect_stalker_signals(cur, hist, config.get("entry", {}))
    striker = detect_striker_signals(cur, hist, config.get("entry", {}))

    if streak_active:
        stalker = [s for s in stalker if s["score"] >= 9]

    hist["scans"].append(cur)
    if len(hist["scans"]) > 20:
        hist["scans"] = hist["scans"][-20:]
    save_json(SCAN_HISTORY_FILE, hist)

    cd_min = config.get("entry", {}).get("assetCooldownMinutes", 120)
    stalker = [s for s in stalker if not is_rotation_cooled_down(s["token"], cd_min)]
    striker = [s for s in striker if not is_rotation_cooled_down(s["token"], cd_min)]

    striker.sort(key=lambda x: x["score"], reverse=True)
    stalker.sort(key=lambda x: x["score"], reverse=True)

    st_tk = {s["token"] for s in striker}
    comb = striker + [s for s in stalker if s["token"] not in st_tk]
    comb.sort(key=lambda x: x["score"], reverse=True)

    if is_entries_allowed() and comb:
        strats = get_enabled_strategies()
        for sig in comb:
            if is_auto_entry_enabled():
                try_auto_entry(sig, strats, config)
            else:
                add_pending_entry(
                    {
                        "asset": sig["token"],
                        "direction": sig["direction"],
                        "autoEntered": False,
                        "score": sig["score"],
                        "source": "mantis",
                        "mode": sig["mode"],
                    }
                )


if __name__ == "__main__":
    if not acquire_lock("mantis-scanner"):
        sys.exit(0)
    try:
        record_heartbeat("mantis")
        run()
    finally:
        release_lock("mantis-scanner")
