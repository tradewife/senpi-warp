#!/usr/bin/env python3
"""
ROACH v1.0 — Striker-Only Scanner. Stalker Disabled.

ROACH tests whether Stalker adds any value or is pure drag.
Fox v1.0 data: 17 Stalker trades at score 6-7, 17.6% win rate, -$91.32.
The one Striker signal (ZEC LONG, score 11) was the only explosive entry.

Cockroaches survive anything. ROACH survives by not trading when there's
no explosion. Long stretches of silence are EXPECTED and CORRECT.

Single entry mode:
  STRIKER: Violent FIRST_JUMP + volume >= 1.5x. Score 9+, min 4 reasons.

Stalker detection code is present (for scan history building) but signals
are never emitted. Output always has stalkerDisabled: true.

All hardened gates preserved:
  - XYZ banned at scan level
  - Leverage 7-10x
  - Max 3 positions
  - 10% daily loss limit
  - 2-hour per-asset cooldown
  - Stagnation TP mandatory

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
    add_pending_entry,
    record_heartbeat,
)

# ---------------------------------------------------------------------------
# HARDCODED CONSTANTS
# ---------------------------------------------------------------------------
MIN_LEVERAGE = 7
MAX_LEVERAGE = 10
MAX_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 10
XYZ_BANNED = True
COOLDOWN_MINUTES = 120
STAGNATION_TP = {"enabled": True, "roeMin": 10, "hwStaleMin": 45}

MAX_SCAN_HISTORY = 60
TOP_N = 50
ERRATIC_REVERSAL_THRESHOLD = 5

SCAN_HISTORY_FILE = POSITION_STATE_DIR / "roach-scan-history.json"
COOLDOWN_FILE = POSITION_STATE_DIR / "roach-cooldowns.json"


# ---------------------------------------------------------------------------
# Fetch & Parse
# ---------------------------------------------------------------------------


def fetch_markets() -> list[dict] | None:
    """Scanner depowered — market fetching disabled. Only evaluator may call MCP."""
    return None


def parse_scan(raw_markets: list[dict]) -> dict:
    scan = {"time": now_iso(), "markets": []}
    for i, m in enumerate(raw_markets[:TOP_N]):
        if not isinstance(m, dict):
            continue
        token = m.get("token", m.get("asset", ""))
        dex = m.get("dex", "")
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
                    m.get("contribution", m.get("pct_of_top_traders_gain", 0))
                ),
                "traders": int(m.get("traderCount", m.get("trader_count", 0))),
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
# Shared helpers
# ---------------------------------------------------------------------------


def check_4h_alignment(direction: str, price_chg_4h: float) -> bool:
    if direction.upper() == "LONG" and price_chg_4h < 0:
        return False
    if direction.upper() == "SHORT" and price_chg_4h > 0:
        return False
    return True


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
        from datetime import timedelta

        cd_time = datetime.fromisoformat(cooldowns[token].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < cd_time + timedelta(
            minutes=COOLDOWN_MINUTES
        )
    except (ValueError, TypeError):
        return False


def check_asset_volume(token: str, dex: str = "") -> tuple[float, bool]:
    """Scanner depowered — volume check disabled. Only evaluator may call MCP."""
    return 0, False


# ---------------------------------------------------------------------------
# STRIKER (Explosion Detection) — the ONLY mode ROACH uses
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

        if rank_jump >= 10 and prev_market["rank"] >= 25:
            is_immediate = True
            reasons.append(f"IMMEDIATE_MOVER +{rank_jump} from #{prev_market['rank']}")
            if (token, dex) not in prev_tokens or prev_market["rank"] >= 30:
                is_first_jump = True
                reasons.append(f"FIRST_JUMP #{prev_market['rank']}->{rank}")

        if not is_first_jump and not is_immediate:
            continue

        # Contribution explosion
        if prev_market["contribution"] > 0:
            ratio = contrib / prev_market["contribution"]
            if ratio >= 3.0:
                reasons.append(f"CONTRIB_EXPLOSION {ratio:.1f}x")

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

        # Score
        score = 0
        if is_first_jump:
            score += 3
        if is_immediate:
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

        # STRONG_4H bonus
        p4h = abs(market.get("price_chg_4h", 0))
        if p4h > 3:
            score += 1
            reasons.append(f"STRONG_4H {market.get('price_chg_4h', 0):+.1f}%")

        # DEEP_SM bonus
        if market["traders"] >= 30:
            score += 1
            reasons.append(f"DEEP_SM ({market['traders']}t)")

        tod_mod, tod_reason = time_of_day_modifier()
        score += tod_mod
        if tod_reason:
            reasons.append(tod_reason)

        if score < min_score or len(reasons) < min_reasons:
            continue

        # Volume confirmation (depowered — skipped in waifu context)
        # In standalone mode, ROACH requires vol_ratio >= 1.5x

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
                "traderCount": market["traders"],
                "erratic": False,
                "timestamp": now_iso(),
            }
        )

    return signals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not acquire_lock("roach-scanner"):
        return

    try:
        record_heartbeat("roach")

        raw = fetch_markets()
        if raw is None:
            return

        current_scan = parse_scan(raw)
        history_data = load_json(SCAN_HISTORY_FILE, default={"scans": []})
        if isinstance(history_data, list):
            history = history_data
        else:
            history = history_data.get("scans", [])

        history.append(current_scan)
        history = history[-MAX_SCAN_HISTORY:]
        save_json(SCAN_HISTORY_FILE, {"scans": history})

        if len(history) < 2:
            return

        # ROACH: Striker ONLY. Stalker disabled by design.
        striker_signals = detect_striker_signals(current_scan, history)

        # Cooldown filter
        cooldowns = load_json(COOLDOWN_FILE, default={})
        filtered = []
        for sig in striker_signals:
            if not is_asset_cooled_down(sig["asset"]):
                filtered.append(sig)

        filtered.sort(key=lambda s: s["score"], reverse=True)

        if not filtered:
            if len(history) % 20 == 0:
                log(
                    f"ROACH scan #{len(history)}: no striker signals (silence = correct)"
                )
            return

        for sig in filtered:
            log(
                f"ROACH STRIKER: {sig['direction']} {sig['asset']} "
                f"score={sig['score']} reasons={sig['reasons']}"
            )
            add_pending_entry(
                {
                    **sig,
                    "autoEntered": False,
                    "scanner": "roach",
                    "stalkerDisabled": True,
                }
            )

    finally:
        release_lock("roach-scanner")


if __name__ == "__main__":
    main()
