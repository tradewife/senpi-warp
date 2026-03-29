#!/usr/bin/env python3
"""
ORCA Scanner v1.3 — Dual-Mode Emerging Movers (Hardened + Entry Cap).

The A/B experiment: does Stalker add value on top of Striker?
Roach = Striker only (+8.2%). Orca v1.3 = Stalker + Striker.

v1.0 → v1.3 changes:
  - Daily entry cap: MAX_DAILY_ENTRIES = 8 (v1.1 did 30/day, bled $80+/day in fees)
  - Stalker minScore raised from 6 to 7 (score 6 entries were 100% losers in Fox data)
  - Stalker minTotalClimb raised from 5 to 8 (weak +5/+6 climbs were noise)
  - Stalker momentum gate: score 7-8 needs 4H > 1% aligned AND traders > 15
  - STRONG_4H bonus (+1 if |4h change| > 3%)
  - DEEP_SM bonus (+1 if traders >= 30)
  - Leverage reduced to 7x

Every protective gate is HARDCODED:
  - XYZ equities banned at scan level
  - Leverage 7x
  - Max 3 positions
  - 10% daily loss limit
  - 2-hour per-asset cooldown
  - Stagnation TP mandatory
  - 8 entries/day max

Runs every 3 minutes.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    git_pull,
    git_sync,
    log,
    now_iso,
    load_json,
    save_json,
    POSITION_STATE_DIR,
    add_pending_entry,
    record_heartbeat,
)

# ---------------------------------------------------------------------------
# HARDCODED CONSTANTS — learned from 30+ live agents across 5+ days.
# These are NOT configurable. They are in the code.
# ---------------------------------------------------------------------------
MIN_LEVERAGE = 7
MAX_LEVERAGE = 7  # v1.3: reduced from 10x to 7x
MAX_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 10
MAX_DAILY_ENTRIES = 8  # v1.3: v1.1 was doing 30/day, bleeding $80+/day in fees
XYZ_BANNED = True
COOLDOWN_MINUTES = 120
STAGNATION_TP = {"enabled": True, "roeMin": 10, "hwStaleMin": 45}

MAX_SCAN_HISTORY = 60  # v1.3: ~180 min at 3min intervals
TOP_N = 50
ERRATIC_REVERSAL_THRESHOLD = 5

SCAN_HISTORY_FILE = POSITION_STATE_DIR / "orca-scan-history.json"
COOLDOWN_FILE = POSITION_STATE_DIR / "orca-cooldowns.json"
TRADE_COUNTER_FILE = POSITION_STATE_DIR / "orca-trade-counter.json"


# ---------------------------------------------------------------------------
# Fetch & Parse
# ---------------------------------------------------------------------------


def fetch_markets() -> list[dict] | None:
    """Scanner depowered — market fetching disabled. Only evaluator may call MCP."""
    return None


def parse_scan(raw_markets: list[dict]) -> dict:
    """Parse raw markets, filtering XYZ equities at scan level."""
    scan = {"time": now_iso(), "markets": []}
    for i, m in enumerate(raw_markets[:TOP_N]):
        if not isinstance(m, dict):
            continue
        token = m.get("token", m.get("asset", ""))
        dex = m.get("dex", "")
        # HARDCODED: ban XYZ equities
        if XYZ_BANNED:
            if dex and dex.lower() == "xyz":
                continue
            if token.lower().startswith("xyz:"):
                continue
        scan["markets"].append(
            {
                "token": token,
                "dex": dex,
                "rank": i + 1,
                "direction": m.get("direction", m.get("side", "")),
                "contribution": float(
                    m.get(
                        "contribution",
                        m.get("pct_of_top_traders_gain", m.get("pctOfTotal", 0)),
                    )
                ),
                "traders": int(
                    m.get("traderCount", m.get("trader_count", m.get("traders", 0)))
                ),
                "price_chg_4h": float(
                    m.get("token_price_change_pct_4h", m.get("priceChange4h", 0)) or 0
                ),
            }
        )
    return scan


def get_market_in_scan(scan: dict, token: str, dex: str = "") -> dict | None:
    for m in scan["markets"]:
        if m["token"] == token and m.get("dex", "") == dex:
            return m
    return None


# ---------------------------------------------------------------------------
# Shared gates
# ---------------------------------------------------------------------------


def check_4h_alignment(direction: str, price_chg_4h: float) -> bool:
    if direction.upper() == "LONG" and price_chg_4h < 0:
        return False
    if direction.upper() == "SHORT" and price_chg_4h > 0:
        return False
    return True


def is_erratic_history(rank_history: list, exclude_last: bool = False) -> bool:
    nums = [r for r in rank_history if r is not None]
    if exclude_last and len(nums) > 1:
        nums = nums[:-1]
    if len(nums) < 3:
        return False
    reversals = 0
    for i in range(1, len(nums) - 1):
        prev_delta = nums[i] - nums[i - 1]
        next_delta = nums[i + 1] - nums[i]
        if prev_delta < 0 and next_delta > ERRATIC_REVERSAL_THRESHOLD:
            reversals += 1
        if prev_delta > 0 and next_delta < -ERRATIC_REVERSAL_THRESHOLD:
            reversals += 1
    return reversals > 5


def time_of_day_modifier() -> tuple[int, str | None]:
    hour = datetime.now(timezone.utc).hour
    if 4 <= hour < 14:
        return 1, "time_bonus_optimal_window"
    elif hour >= 18 or hour < 2:
        return -2, "time_penalty_chop_zone"
    return 0, None


def is_asset_cooled_down(token: str) -> bool:
    cooldowns = load_json(COOLDOWN_FILE, default={})
    if token not in cooldowns:
        return False
    try:
        cd_time = datetime.fromisoformat(cooldowns[token].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < cd_time + timedelta(
            minutes=COOLDOWN_MINUTES
        )
    except (ValueError, TypeError):
        return False


def set_asset_cooldown(token: str):
    cooldowns = load_json(COOLDOWN_FILE, default={})
    cooldowns[token] = now_iso()
    save_json(COOLDOWN_FILE, cooldowns)


def check_asset_volume(token: str, dex: str = "") -> tuple[float, bool]:
    """Scanner depowered — volume check disabled. Only evaluator may call MCP."""
    return 0, False


# ---------------------------------------------------------------------------
# MODE A: STALKER (accumulation detection)
# ---------------------------------------------------------------------------


def detect_stalker_signals(current_scan: dict, history: list[dict]) -> list[dict]:
    # v1.3: minScore raised to 7, minTotalClimb raised to 8
    min_consecutive = 3
    min_total_climb = 8  # v1.3: was 5 — weak +5/+6 climbs were noise (Fox data)
    min_score = 7  # v1.3: was 6 — score 6 entries were 100% losers (Fox data)
    momentum_gate_score = 9  # Below 9, need momentum event confirmation

    if len(history) < min_consecutive:
        return []

    signals = []
    for market in current_scan["markets"]:
        token = market["token"]
        dex = market.get("dex", "")
        rank = market["rank"]
        direction = market["direction"].upper()

        if rank <= 10:
            continue
        if not check_4h_alignment(direction, market.get("price_chg_4h", 0)):
            continue
        if is_asset_cooled_down(token):
            continue

        # Build rank & contrib history
        rank_history, contrib_history = [], []
        for scan in history[-(min_consecutive + 2) :]:
            m = get_market_in_scan(scan, token, dex)
            if m:
                rank_history.append(m["rank"])
                contrib_history.append(m["contribution"])
            else:
                rank_history.append(None)
                contrib_history.append(None)
        rank_history.append(rank)
        contrib_history.append(market["contribution"])

        valid_ranks = [(i, r) for i, r in enumerate(rank_history) if r is not None]
        if len(valid_ranks) < min_consecutive + 1:
            continue

        recent_ranks = [r for _, r in valid_ranks[-(min_consecutive + 1) :]]
        is_climbing = all(
            recent_ranks[i] >= recent_ranks[i + 1] for i in range(len(recent_ranks) - 1)
        )
        total_climb = recent_ranks[0] - recent_ranks[-1]

        if not is_climbing or total_climb < min_total_climb:
            continue
        if is_erratic_history(rank_history, exclude_last=True):
            continue

        # Contribution building check
        valid_contribs = [c for c in contrib_history if c is not None]
        if len(valid_contribs) >= 3:
            recent_c = valid_contribs[-3:]
            if not all(
                recent_c[i] <= recent_c[i + 1] for i in range(len(recent_c) - 1)
            ):
                continue

        # Score (v1.3: added STRONG_4H and DEEP_SM bonuses)
        score = 0
        reasons = []

        score += 3
        reasons.append(f"STALKER_CLIMB +{total_climb} over {len(recent_ranks)} scans")

        if len(valid_contribs) >= 2:
            deltas = [
                valid_contribs[i + 1] - valid_contribs[i]
                for i in range(len(valid_contribs) - 1)
            ]
            vel = sum(deltas) / len(deltas)
            if vel > 0.001:
                score += 2
                reasons.append(f"CONTRIB_ACCEL +{vel * 100:.3f}%/scan")
            elif vel > 0:
                score += 1
                reasons.append(f"CONTRIB_POSITIVE +{vel * 100:.4f}%/scan")

        if market["traders"] >= 10:
            score += 1
            reasons.append(f"SM_ACTIVE {market['traders']} traders")
        if recent_ranks[0] >= 30:
            score += 1
            reasons.append(f"DEEP_START from #{recent_ranks[0]}")

        # v1.3: STRONG_4H bonus
        p4h = abs(market.get("price_chg_4h", 0))
        if p4h > 3:
            score += 1
            reasons.append(f"STRONG_4H {market.get('price_chg_4h', 0):+.1f}%")

        # v1.3: DEEP_SM bonus
        if market["traders"] >= 30:
            score += 1
            reasons.append(f"DEEP_SM ({market['traders']}t)")

        tod_mod, tod_reason = time_of_day_modifier()
        score += tod_mod
        if tod_reason:
            reasons.append(tod_reason)

        if score < min_score:
            continue

        # v1.3: Momentum gate — score 7-8 Stalkers without momentum backing
        # are catching chop, not accumulation. Fox data: 17.6% WR at score 6-7.
        if score < momentum_gate_score:
            p4h_aligned = (
                direction == "LONG" and market.get("price_chg_4h", 0) > 1.0
            ) or (direction == "SHORT" and market.get("price_chg_4h", 0) < -1.0)
            deep_sm = market["traders"] >= 15
            if not (p4h_aligned and deep_sm):
                continue

        signals.append(
            {
                "asset": token,
                "dex": dex if dex else None,
                "direction": direction,
                "mode": "STALKER",
                "signalType": "STALKER_CLIMB",
                "score": score,
                "reasons": reasons,
                "rank": rank,
                "contribution": round(market["contribution"] * 100, 3),
                "traderCount": market["traders"],
                "totalClimb": total_climb,
                "erratic": False,
                "timestamp": now_iso(),
            }
        )

    return signals


# ---------------------------------------------------------------------------
# MODE B: STRIKER (explosion detection)
# ---------------------------------------------------------------------------


def detect_striker_signals(current_scan: dict, history: list[dict]) -> list[dict]:
    min_score = 9
    min_reasons = 4
    min_rank_jump = 15
    min_velocity_override = 15
    min_velocity_floor = 10

    if not history:
        return []

    prev_scan = history[-1]
    oldest = history[-min(len(history), 5)]
    prev_tokens = {(m["token"], m.get("dex", "")) for m in prev_scan["markets"]}

    signals = []
    for market in current_scan["markets"]:
        token = market["token"]
        dex = market.get("dex", "")
        rank = market["rank"]
        direction = market["direction"].upper()
        contrib = market["contribution"]

        if rank <= 10:
            continue
        if not check_4h_alignment(direction, market.get("price_chg_4h", 0)):
            continue
        if is_asset_cooled_down(token):
            continue

        prev_market = get_market_in_scan(prev_scan, token, dex)
        if not prev_market:
            continue

        rank_jump = prev_market["rank"] - rank
        reasons = []
        is_first_jump = False
        is_immediate = False
        is_contrib_explosion = False

        if rank_jump >= 10 and prev_market["rank"] >= 25:
            is_immediate = True
            reasons.append(f"IMMEDIATE_MOVER +{rank_jump} from #{prev_market['rank']}")
            if (token, dex) not in prev_tokens or prev_market["rank"] >= 30:
                is_first_jump = True
                reasons.append(f"FIRST_JUMP #{prev_market['rank']}->{rank}")

        if prev_market["contribution"] > 0:
            ratio = contrib / prev_market["contribution"]
            if ratio >= 3.0:
                is_contrib_explosion = True
                reasons.append(f"CONTRIB_EXPLOSION {ratio:.1f}x")

        if not is_first_jump and not is_immediate:
            continue

        # Velocity
        recent_contribs = []
        for scan in history[-5:]:
            m = get_market_in_scan(scan, token, dex)
            if m:
                recent_contribs.append(m["contribution"])
        recent_contribs.append(contrib)
        contrib_velocity = 0
        if len(recent_contribs) >= 2:
            deltas = [
                recent_contribs[i + 1] - recent_contribs[i]
                for i in range(len(recent_contribs) - 1)
            ]
            contrib_velocity = sum(deltas) / len(deltas) * 100
        abs_vel = abs(contrib_velocity)

        if rank_jump < min_rank_jump and abs_vel < min_velocity_override:
            continue
        if abs_vel < min_velocity_floor:
            if not (is_first_jump and contrib_velocity > 0):
                continue

        # Score (v1.3: added STRONG_4H and DEEP_SM bonuses)
        score = 0
        if is_first_jump:
            score += 3
        if is_immediate:
            score += 2
        if is_contrib_explosion:
            score += 2
        if abs_vel > 10:
            score += 2
            reasons.append(f"HIGH_VELOCITY {abs_vel:.1f}")

        if prev_market["rank"] >= 40:
            score += 1
            reasons.append("DEEP_CLIMBER")

        old_market = get_market_in_scan(oldest, token, dex)
        if old_market:
            total_climb = old_market["rank"] - rank
            if total_climb >= 10:
                score += 1
                reasons.append(f"CLIMBING +{total_climb}")

        # v1.3: STRONG_4H bonus
        p4h = abs(market.get("price_chg_4h", 0))
        if p4h > 3:
            score += 1
            reasons.append(f"STRONG_4H {market.get('price_chg_4h', 0):+.1f}%")

        # v1.3: DEEP_SM bonus
        if market["traders"] >= 30:
            score += 1
            reasons.append(f"DEEP_SM ({market['traders']}t)")

        tod_mod, tod_reason = time_of_day_modifier()
        score += tod_mod
        if tod_reason:
            reasons.append(tod_reason)

        if score < min_score or len(reasons) < min_reasons:
            continue

        # Volume confirmation (the PUMP filter)
        vol_ratio, vol_strong = check_asset_volume(token, dex)
        if not vol_strong:
            continue
        reasons.append(f"VOL_CONFIRMED {vol_ratio:.1f}x")

        signals.append(
            {
                "asset": token,
                "dex": dex if dex else None,
                "direction": direction,
                "mode": "STRIKER",
                "signalType": "FIRST_JUMP" if is_first_jump else "IMMEDIATE_MOVER",
                "score": score,
                "reasons": reasons,
                "rank": rank,
                "prevRank": prev_market["rank"],
                "rankJump": rank_jump,
                "contribution": round(contrib * 100, 3),
                "contribVelocity": round(contrib_velocity, 4),
                "volRatio": round(vol_ratio, 2),
                "traderCount": market["traders"],
                "erratic": False,
                "timestamp": now_iso(),
            }
        )

    return signals


# ---------------------------------------------------------------------------
# Trade Counter (v1.3: daily entry cap)
# ---------------------------------------------------------------------------


def load_trade_counter() -> dict:
    """Load daily trade counter. Resets at midnight UTC."""
    tc = load_json(TRADE_COUNTER_FILE, default={"date": "", "entries": 0})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if tc.get("date") != today:
        return {"date": today, "entries": 0}
    return tc


def save_trade_counter(tc: dict):
    save_json(TRADE_COUNTER_FILE, tc)


def increment_trade_counter():
    tc = load_trade_counter()
    tc["entries"] = tc.get("entries", 0) + 1
    save_trade_counter(tc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not acquire_lock("orca-scanner"):
        return

    try:
        record_heartbeat("orca")
        git_pull()

        # v1.3: Check daily entry cap BEFORE fetching markets
        tc = load_trade_counter()
        if tc.get("entries", 0) >= MAX_DAILY_ENTRIES:
            log(f"ORCA: Daily entry limit ({MAX_DAILY_ENTRIES}) reached — skipping")
            return

        raw = fetch_markets()
        if raw is None:
            return

        current_scan = parse_scan(raw)
        history_data = load_json(SCAN_HISTORY_FILE, default={"scans": []})
        if isinstance(history_data, list):
            history = history_data
        else:
            history = history_data.get("scans", [])
        if len(history) % 10 == 0:
            log(f"ORCA scan #{len(history)}: {len(current_scan['markets'])} markets")

        history.append(current_scan)
        history = history[-MAX_SCAN_HISTORY:]

        save_json(SCAN_HISTORY_FILE, {"scans": history})
        stalker_signals = detect_stalker_signals(current_scan, history)
        striker_signals = detect_striker_signals(current_scan, history)

        save_json(SCAN_HISTORY_FILE, {"scans": history})

        striker_assets = {s["asset"] for s in striker_signals}
        combined = striker_signals + [
            s for s in stalker_signals if s["asset"] not in striker_assets
        ]
        combined.sort(key=lambda s: s["score"], reverse=True)

        if not combined:
            return

        # v1.3: Cap signals to remaining daily entries
        remaining_entries = MAX_DAILY_ENTRIES - tc.get("entries", 0)
        combined = combined[:remaining_entries]

        for sig in combined:
            log(
                f"ORCA {sig['mode']}: {sig['direction']} {sig['asset']} "
                f"score={sig['score']} reasons={sig['reasons']}"
            )
            add_pending_entry({**sig, "autoEntered": False, "scanner": "orca"})
            increment_trade_counter()

        git_sync("auto: ORCA scan")

    finally:
        release_lock("orca-scanner")


if __name__ == "__main__":
    main()
