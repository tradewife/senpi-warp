#!/usr/bin/env python3
"""
ORCA Scanner v1.0 — Dual-Mode Emerging Movers (Hardened).

Replaces the single-mode EM scanner with two entry modes:
  STALKER: SM accumulating over 3+ scans. Score 6+. Enter before the crowd.
  STRIKER: Violent FIRST_JUMP + volume >= 1.5x. Score 9+. Enter the explosion.

Every protective gate is HARDCODED — the agent/config cannot override them:
  - XYZ equities banned at scan level
  - Leverage 7-10x
  - Max 3 positions
  - 10% daily loss limit
  - 2-hour per-asset cooldown
  - Stagnation TP mandatory

Based on FOX v1.6 (+34.5% ROI) and hardened with lessons from 22 live agents.
Runs every 90 seconds.
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
    SCANNER_CONFIG_FILE,
    load_regime,
    current_regime_params,
    is_entries_allowed,
    is_auto_entry_enabled,
    get_enabled_strategies,
    count_open_slots,
    get_strategy_state_dir,
    check_directional_exposure_limit,
    attach_position_playbook,
    add_pending_entry,
    record_trade,
    send_telegram,
    mcporter_call,
    record_heartbeat,
)

# ---------------------------------------------------------------------------
# HARDCODED CONSTANTS — learned from 5 days of live trading across 22 agents.
# These are NOT configurable. They are in the code.
# ---------------------------------------------------------------------------
MIN_LEVERAGE = 7
MAX_LEVERAGE = 10
MAX_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 10
XYZ_BANNED = True
COOLDOWN_MINUTES = 120
STAGNATION_TP = {"enabled": True, "roeMin": 10, "hwStaleMin": 45}

MAX_SCAN_HISTORY = 40  # ~60 min at 90s intervals
TOP_N = 50
ERRATIC_REVERSAL_THRESHOLD = 5

SCAN_HISTORY_FILE = POSITION_STATE_DIR / "orca-scan-history.json"
COOLDOWN_FILE = POSITION_STATE_DIR / "orca-cooldowns.json"


# ---------------------------------------------------------------------------
# Fetch & Parse
# ---------------------------------------------------------------------------


def fetch_markets() -> list[dict] | None:
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        log(f"Leaderboard fetch failed: {result['error']}")
        return None
    data = result.get("data", result)
    raw = data.get("markets", data)
    if isinstance(raw, dict):
        raw = raw.get("markets", [])
    return raw if isinstance(raw, list) else None


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
    """Check if raw 1h volume is ≥ 1.5x of 6h average."""
    asset_name = f"{dex}:{token}" if dex else token
    data = mcporter_call("market_get_asset_data", {"asset": asset_name})
    if "error" in data:
        return 0, False
    candle_data = data.get("data", data)
    if isinstance(candle_data, dict):
        candles = candle_data.get("candles", {}).get("1h", [])
    else:
        return 0, False
    if len(candles) < 6:
        return 0, False
    vols = [float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-6:]]
    avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
    latest_vol = vols[-1] if vols else 0
    ratio = latest_vol / avg_vol if avg_vol > 0 else 0
    return ratio, ratio >= 1.5


# ---------------------------------------------------------------------------
# MODE A: STALKER (accumulation detection)
# ---------------------------------------------------------------------------


def detect_stalker_signals(current_scan: dict, history: list[dict]) -> list[dict]:
    min_consecutive = 3
    min_total_climb = 5
    min_score = 6

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

        # Score
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
                reasons.append("CONTRIB_POSITIVE")

        if market["traders"] >= 10:
            score += 1
            reasons.append("SM_ACTIVE")
        if recent_ranks[0] >= 30:
            score += 1
            reasons.append("DEEP_START")

        tod_mod, tod_reason = time_of_day_modifier()
        score += tod_mod
        if tod_reason:
            reasons.append(tod_reason)

        if score < min_score:
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

        # Score
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
# Auto-entry (with High Water DSL)
# ---------------------------------------------------------------------------


def try_auto_entry(signal: dict):
    log(
        f"ORCA try_auto_entry: {signal['asset']} mode={signal['mode']} score={signal['score']}"
    )
    from senpi_common import (
        current_regime_params,
        load_regime,
        load_brain_state,
        BRAIN_STATE_FILE,
        RISK_REGIME_FILE,
    )

    regime = load_regime()
    brain = load_brain_state()
    params = current_regime_params()
    log(
        f"ORCA debug: regime_mode={regime.get('riskMode')} regime_file={RISK_REGIME_FILE} exists={RISK_REGIME_FILE.exists()}"
    )
    log(
        f"ORCA debug: brain_file={BRAIN_STATE_FILE} exists={BRAIN_STATE_FILE.exists()} brain_keys={list(brain.keys())[:5]}"
    )
    log(
        f"ORCA debug: effective autoEntryEnabled={params.get('autoEntryEnabled')} newEntriesAllowed={params.get('newEntriesAllowed')}"
    )
    log(
        f"ORCA debug: regime BASELINE autoEntryEnabled={regime.get('regimes', {}).get('BASELINE', {}).get('autoEntryEnabled')}"
    )
    log(
        f"ORCA debug: brain blockNewEntries={brain.get('executionPolicy', {}).get('blockNewEntries')} allowAutoEntry={brain.get('executionPolicy', {}).get('allowAutoEntry')}"
    )
    if not is_auto_entry_enabled():
        log(f"ORCA auto-entry: auto entry disabled for {signal['asset']}")
        return
    if signal["score"] < 6:
        return
    if signal["score"] < 6:
        return
    if signal["score"] < 6:
        return

    regime_params = current_regime_params()
    strategies = get_enabled_strategies()
    target_strategy = None

    for strat in strategies:
        state_dir = get_strategy_state_dir(strat["_key"])
        dsl_file = state_dir / f"dsl-{signal['asset']}.json"
        existing = load_json(dsl_file, default=None)
        if existing and existing.get("active", False):
            continue
        if count_open_slots(strat) > 0:
            target_strategy = strat
            break

    if not target_strategy:
        log(f"ORCA auto-entry: no free slots for {signal['asset']}")
        return

    # Check global position limit
    from senpi_common import get_open_positions

    total_open = sum(len(get_open_positions(s["_key"])) for s in strategies)
    if total_open >= MAX_POSITIONS:
        log(f"ORCA: max {MAX_POSITIONS} positions reached")
        return

    budget = target_strategy.get("budget", 1000)
    alloc_pct = regime_params.get("allocPctPerSlot", 30) / 100
    leverage = min(
        max(target_strategy.get("defaultLeverage", 10), MIN_LEVERAGE),
        MAX_LEVERAGE,
        regime_params.get("maxLeverageCrypto", 10),
    )
    margin = budget * alloc_pct

    allowed_exposure, exposure = check_directional_exposure_limit(
        signal["direction"], margin, leverage
    )
    if not allowed_exposure:
        log(
            f"ORCA: directional cap blocked {signal['asset']} {signal['direction']} "
            f"projected={exposure['offendingPct']:.1f}% cap={exposure['capPct']:.1f}%"
        )
        return

    # STALKER: ALO (maker) for fee savings. STRIKER: MARKET for speed.
    order_type = "ALO" if signal["mode"] == "STALKER" else "MARKET"

    log(
        f"🐋 ORCA {signal['mode']}: {signal['direction']} {signal['asset']} | "
        f"score={signal['score']} margin=${margin:.0f} lev={leverage}x order={order_type} | "
        f"reasons={signal['reasons']}"
    )

    entry_params = {
        "strategyWalletAddress": target_strategy.get("wallet"),
        "orders": [
            {
                "coin": signal["asset"],
                "direction": signal["direction"],
                "leverage": int(leverage),
                "marginAmount": margin,
                "orderType": "MARKET" if order_type == "market" else "LIMIT",
            }
        ],
    }

    entry_result = mcporter_call("create_position", entry_params)

    if "error" in entry_result:
        log(f"ORCA entry FAILED for {signal['asset']}: {entry_result['error']}")
        return

    entry_price = float(entry_result.get("entryPrice", 0))
    size = float(entry_result.get("size", 0))

    # Conviction-scaled Phase 1
    s = signal["score"]
    if s >= 10:
        abs_floor_roe = -30
        hard_timeout = 3600
        weak_peak = 1800
    elif s >= 8:
        abs_floor_roe = -25
        hard_timeout = 2700
        weak_peak = 1200
    else:
        abs_floor_roe = -20
        hard_timeout = 1800
        weak_peak = 900

    dsl_state = {
        "active": True,
        "asset": signal["asset"],
        "direction": signal["direction"],
        "leverage": leverage,
        "entryPrice": entry_price,
        "size": size,
        "wallet": target_strategy.get("wallet"),
        "strategyWalletAddress": target_strategy.get("wallet"),
        "strategyKey": target_strategy["_key"],
        "phase": 1,
        "lockMode": "pct_of_high_water",
        "phase1": {
            "absoluteFloorRoe": abs_floor_roe,
            "hardTimeoutSec": hard_timeout,
            "weakPeakCutSec": weak_peak,
        },
        "phase2TriggerRoe": 5,
        "tiers": [
            {"triggerPct": 5, "lockHwPct": 20, "consecutiveBreachesRequired": 2},
            {"triggerPct": 10, "lockHwPct": 40, "consecutiveBreachesRequired": 2},
            {"triggerPct": 20, "lockHwPct": 55, "consecutiveBreachesRequired": 2},
            {"triggerPct": 30, "lockHwPct": 70, "consecutiveBreachesRequired": 1},
            {"triggerPct": 50, "lockHwPct": 80, "consecutiveBreachesRequired": 1},
            {"triggerPct": 75, "lockHwPct": 85, "consecutiveBreachesRequired": 1},
            {"triggerPct": 100, "lockHwPct": 90, "consecutiveBreachesRequired": 1},
        ],
        "stagnationTp": dict(STAGNATION_TP),
        "currentTierIndex": -1,
        "highWaterPrice": entry_price,
        "floorPrice": None,
        "currentBreachCount": 0,
        "entryScore": signal["score"],
        "entryMode": signal["mode"],
        "entryReasons": signal["reasons"],
        "createdAt": now_iso(),
    }
    attach_position_playbook(
        dsl_state,
        scanner="orca",
        margin=margin,
        leverage=leverage,
        score=signal["score"],
        reasons=signal["reasons"],
        sm_snapshot={
            "traderCount": signal.get("traderCount"),
            "concentration": signal.get("contribution"),
        },
        setup={
            "mode": signal.get("mode"),
            "rank": signal.get("rank"),
            "signalType": signal.get("signalType"),
        },
    )
    state_dir = get_strategy_state_dir(target_strategy["_key"])
    save_json(state_dir / f"dsl-{signal['asset']}.json", dsl_state)

    record_trade(
        {
            "action": "OPEN",
            "asset": signal["asset"],
            "direction": signal["direction"],
            "entryPrice": entry_price,
            "size": size,
            "margin": margin,
            "leverage": leverage,
            "strategyKey": target_strategy["_key"],
            "entrySource": f"orca-{signal['mode'].lower()}",
            "entryMode": signal["mode"],
            "entryScore": signal["score"],
            "orderType": order_type,
            "signal": signal,
        }
    )

    add_pending_entry(
        {
            **signal,
            "autoEntered": True,
            "strategyKey": target_strategy["_key"],
            "entryPrice": entry_price,
            "margin": margin,
            "leverage": leverage,
        }
    )

    mode_emoji = "🔍" if signal["mode"] == "STALKER" else "⚡"
    send_telegram(
        f"🐋 ORCA {mode_emoji} {signal['mode']}: {signal['direction']} {signal['asset']}\n"
        f"Score: {signal['score']} | {', '.join(signal['reasons'][:4])}\n"
        f"Entry: ${entry_price:.4f} | Margin: ${margin:.0f} | Lev: {leverage}x\n"
        f"Strategy: {target_strategy.get('name', target_strategy['_key'])}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not acquire_lock("orca-scanner"):
        return

    try:
        record_heartbeat("orca")
        git_pull()

        raw = fetch_markets()
        if raw is None:
            return

        current_scan = parse_scan(raw)
        history_data = load_json(SCAN_HISTORY_FILE, default={"scans": []})
        if isinstance(history_data, list):
            history = history_data
        else:
            history = history_data.get("scans", [])
        # Log scan status periodically
        if len(history) % 10 == 0 or len(history) < 4:
            log(f"ORCA scan #{len(history)}: {len(current_scan['markets'])} markets")

        history.append(current_scan)
        history = history[-MAX_SCAN_HISTORY:]

        save_json(SCAN_HISTORY_FILE, {"scans": history})
        stalker_signals = detect_stalker_signals(current_scan, history)
        striker_signals = detect_striker_signals(current_scan, history)

        save_json(SCAN_HISTORY_FILE, {"scans": history})

        # Combine — STRIKER takes priority for same asset
        striker_assets = {s["asset"] for s in striker_signals}
        combined = striker_signals + [
            s for s in stalker_signals if s["asset"] not in striker_assets
        ]
        combined.sort(key=lambda s: s["score"], reverse=True)

        if not combined:
            return

        for sig in combined:
            log(
                f"ORCA {sig['mode']}: {sig['direction']} {sig['asset']} "
                f"score={sig['score']} reasons={sig['reasons']}"
            )

        # Auto-enter on highest-conviction signals
        if is_entries_allowed():
            for sig in combined[:2]:
                if sig["mode"] == "STRIKER" and sig["score"] >= 9:
                    try_auto_entry(sig)
                elif sig["mode"] == "STALKER" and sig["score"] >= 6:
                    try_auto_entry(sig)

        # Queue non-auto-entered signals for review (only when entries allowed)
        auto_entered_assets = set()
        if is_entries_allowed():
            for sig in combined[:2]:
                if (sig["mode"] == "STRIKER" and sig["score"] >= 9) or (
                    sig["mode"] == "STALKER" and sig["score"] >= 6
                ):
                    auto_entered_assets.add(sig["asset"])
            for sig in combined:
                if sig["asset"] not in auto_entered_assets:
                    add_pending_entry({**sig, "autoEntered": False, "scanner": "orca"})

        git_sync("auto: ORCA scan")

    finally:
        release_lock("orca-scanner")


if __name__ == "__main__":
    main()
