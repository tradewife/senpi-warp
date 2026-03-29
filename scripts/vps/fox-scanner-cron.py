#!/usr/bin/env python3
"""
FOX v2.0 — Dual-Mode Emerging Movers Scanner (Hardened + minReasons + Streak Gate).

FOX v2.0 applies all live trading lessons:
  - Stalker minReasons = 3: entries must have at least 3 distinct scoring reasons
  - Stalker streak gate: 3 consecutive Stalker losses → minScore raised to 9
  - Stalker minScore raised from 6 to 7 (score 6 entries were 100% losers)
  - Stalker minTotalClimb raised from 5 to 8 (weak climbs were noise)
  - STRONG_4H bonus (+1 if |4h change| > 3%)
  - DEEP_SM bonus (+1 if traders >= 30)

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
    is_rotation_cooled_down,
    POSITION_STATE_DIR,
    CONFIG_DIR,
    add_pending_entry,
    record_heartbeat,
)

FOX_CONFIG_FILE = CONFIG_DIR / "fox-config.json"
SCAN_HISTORY_FILE = POSITION_STATE_DIR / "fox-scan-history.json"
TRADE_COUNTER_FILE = POSITION_STATE_DIR / "fox-trade-counter.json"
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
    """Scanner depowered — market fetching disabled. Only evaluator may call MCP."""
    return None


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
    """Scanner depowered — volume check disabled. Only evaluator may call MCP."""
    return 0, False


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


# ─── Streak Tracking (v2.0) ──────────────────────────────────


def load_trade_counter():
    tc = load_json(TRADE_COUNTER_FILE, default={"date": "", "stalkerResults": []})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if tc.get("date") != today:
        return {"date": today, "stalkerResults": []}
    return tc


def save_trade_counter(tc):
    save_json(TRADE_COUNTER_FILE, tc)


def is_stalker_streak_active():
    """Check if last 3 Stalker results were all losses."""
    tc = load_trade_counter()
    results = tc.get("stalkerResults", [])
    return len(results) >= 3 and all(r == "L" for r in results[-3:])


# ─── MODE A: STALKER ──────────────────────────────────────────────────


def detect_stalker_signals(current_scan, history, stalker_cfg):
    min_consecutive = stalker_cfg.get("minConsecutiveScans", 3)
    min_total_climb = stalker_cfg.get("minTotalClimb", 8)
    min_score = stalker_cfg.get("minScore", 7)
    # FOX v2.0 - minReasons=3
    min_reasons = stalker_cfg.get("minReasons", 3)
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

        if len(valid_c) >= 2:
            deltas = [valid_c[i + 1] - valid_c[i] for i in range(len(valid_c) - 1)]
            vel = sum(deltas) / len(deltas)
            if vel > 0.001:
                score += 2
                reasons.append(f"CONTRIB_ACCEL +{vel * 100:.3f}%/scan")
            elif vel > 0:
                score += 1
                reasons.append(f"CONTRIB_POSITIVE +{vel * 100:.4f}%/scan")

        if m["traders"] >= 10:
            score += 1
            reasons.append(f"SM_ACTIVE {m['traders']} traders")
        if recent_r[0] >= 30:
            score += 1
            reasons.append(f"DEEP_START from #{recent_r[0]}")

        # v2.0: STRONG_4H bonus
        p4h = abs(m.get("price_chg_4h", 0))
        if p4h > 3:
            score += 1
            reasons.append(f"STRONG_4H {m.get('price_chg_4h', 0):+.1f}%")

        # v2.0: DEEP_SM bonus
        if m["traders"] >= 30:
            score += 1
            reasons.append(f"DEEP_SM ({m['traders']}t)")

        tmod, treas = time_of_day_modifier()
        score += tmod
        if treas:
            reasons.append(treas)

        # v2.0: Streak gate — 3 consecutive Stalker losses → minScore raised to 9
        effective_min_score = min_score
        if is_stalker_streak_active() and score < 9:
            effective_min_score = 9  # Suppress low-score Stalkers during losing streak

        if score >= effective_min_score and len(reasons) >= min_reasons:
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

        # v2.0: STRONG_4H bonus
        p4h = abs(m.get("price_chg_4h", 0))
        if p4h > 3:
            score += 1
            reasons.append(f"STRONG_4H {m.get('price_chg_4h', 0):+.1f}%")

        # v2.0: DEEP_SM bonus
        if m["traders"] >= 30:
            score += 1
            reasons.append(f"DEEP_SM ({m['traders']}t)")

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


def run():
    config = load_json(FOX_CONFIG_FILE)
    if not config:
        return

    rm = fetch_markets()
    if not rm:
        return
    cur = parse_scan(rm)

    hist = load_json(SCAN_HISTORY_FILE, default={"scans": []})

    stalker = detect_stalker_signals(cur, hist, config.get("entry", {}))
    striker = detect_striker_signals(cur, hist, config.get("entry", {}))

    hist["scans"].append(cur)
    if len(hist["scans"]) > 60:
        hist["scans"] = hist["scans"][-60:]
    save_json(SCAN_HISTORY_FILE, hist)

    cd_min = config.get("entry", {}).get("assetCooldownMinutes", 120)
    stalker = [s for s in stalker if not is_rotation_cooled_down(s["token"], cd_min)]
    striker = [s for s in striker if not is_rotation_cooled_down(s["token"], cd_min)]

    striker.sort(key=lambda x: x["score"], reverse=True)
    stalker.sort(key=lambda x: x["score"], reverse=True)

    st_tk = {s["token"] for s in striker}
    comb = striker + [s for s in stalker if s["token"] not in st_tk]
    comb.sort(key=lambda x: x["score"], reverse=True)

    for sig in comb:
        log(
            f"FOX {sig['mode']}: {sig['direction']} {sig['token']} "
            f"score={sig['score']} reasons={sig['reasons']}"
        )
        add_pending_entry(
            {
                "asset": sig["token"],
                "direction": sig["direction"],
                "autoEntered": False,
                "score": sig["score"],
                "source": "fox",
                "mode": sig["mode"],
            }
        )


if __name__ == "__main__":
    if not acquire_lock("fox-scanner"):
        sys.exit(0)
    try:
        record_heartbeat("fox")
        run()
    finally:
        release_lock("fox-scanner")
