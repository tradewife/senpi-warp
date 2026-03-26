#!/usr/bin/env python3
"""
KOMODO v1.0 — Momentum Event Consensus Scanner. Runs every 5 minutes.

Uses leaderboard_get_momentum_events (real-time threshold crossings:
$2M+/$5.5M+/$10M+ delta PnL) to detect when 2+ quality SM traders
cross momentum thresholds on the same asset/direction within 60 minutes.

Five-gate entry model:
  Gate 1: Momentum events → consensus (2+ traders same asset/direction)
  Gate 2: Trader quality filter (TCS/TAS/concentration)
  Gate 3: Market confirmation (aggregate SM concentration)
  Gate 4: Volume confirmation (1h vs 6h avg)
  Gate 5: Regime filter (penalty, not block)

Enters WITH the smart money momentum. DSL High Water Mode.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    git_pull,
    git_sync,
    log,
    load_json,
    save_json,
    now_iso,
    POSITION_STATE_DIR,
    SCANNER_CONFIG_FILE,
    load_regime,
    current_regime_params,
    is_entries_allowed,
    is_auto_entry_enabled,
    get_enabled_strategies,
    add_pending_entry,
    record_trade,
    send_telegram,
    mcporter_read,
    record_heartbeat,
)

# --- State files ---
KOMODO_EVENTS_FILE = POSITION_STATE_DIR / "komodo-events.json"
KOMODO_COOLDOWNS_FILE = POSITION_STATE_DIR / "komodo-cooldowns.json"
KOMODO_ENTRIES_FILE = POSITION_STATE_DIR / "komodo-entries.json"

# --- Constants ---
MIN_LEVERAGE = 7
MAX_LEVERAGE = 10
MAX_POSITIONS = 3
BASE_MAX_ENTRIES_PER_DAY = 3
PROFITABLE_DAY_MAX_ENTRIES = 6
DAILY_LOSS_LIMIT_PCT = 8
CONSECUTIVE_LOSS_COOLDOWN_MIN = 90
MAX_CONSECUTIVE_LOSSES = 3
PER_ASSET_COOLDOWN_MIN = 120
MIN_SCORE = 10

# --- Quality filters ---
ALLOWED_TCS = {"Elite", "Reliable"}
BLOCKED_TAS = {"Degen"}
MIN_CONCENTRATION = 0.4
MIN_CONSENSUS_TRADERS = 2
MIN_MARKET_TRADERS = 5
MIN_VOL_RATIO = 0.5


# ============================================================================
# Gate 1 — Momentum Events
# ============================================================================


def fetch_momentum_events() -> list[dict]:
    """Fetch real-time momentum threshold crossing events."""
    result = mcporter_read("leaderboard_get_momentum_events", {})
    if "error" in result:
        log(f"Momentum events fetch failed: {result['error']}")
        return []
    events = result.get("events", result.get("data", result))
    if isinstance(events, list):
        return events
    return []


def group_events_by_consensus(events: list[dict]) -> dict[str, list[dict]]:
    """
    Group momentum events by asset+direction.
    Returns only groups with 2+ unique traders (consensus).
    """
    groups: dict[str, list[dict]] = {}
    for event in events:
        positions = event.get("top_positions", [])
        for pos in positions:
            asset = pos.get("asset", "")
            direction = pos.get("direction", "")
            if not asset or not direction:
                continue
            key = f"{asset}:{direction}"
            groups.setdefault(key, [])
            # Deduplicate by trader
            trader_id = event.get("trader_id", event.get("traderId", ""))
            already = any(
                e.get("trader_id", e.get("traderId", "")) == trader_id
                for e in groups[key]
            )
            if not already:
                groups[key].append(
                    {
                        **event,
                        "_asset": asset,
                        "_direction": direction,
                        "_delta_pnl": float(
                            pos.get("delta_pnl", pos.get("deltaPnl", 0))
                        ),
                    }
                )

    # Only keep consensus groups (2+ unique traders)
    return {k: v for k, v in groups.items() if len(v) >= MIN_CONSENSUS_TRADERS}


# ============================================================================
# Gate 2 — Trader Quality Filter
# ============================================================================


def filter_by_quality(events: list[dict]) -> list[dict]:
    """Filter events by TCS, TAS, and concentration thresholds."""
    passed = []
    for event in events:
        tags = event.get("trader_tags", event.get("traderTags", {}))
        tcs = tags.get("TCS", tags.get("tcs", ""))
        tas = tags.get("TAS", tags.get("tas", ""))
        concentration = float(event.get("concentration", 0))

        if tcs not in ALLOWED_TCS:
            continue
        if tas in BLOCKED_TAS:
            continue
        if concentration < MIN_CONCENTRATION:
            continue

        passed.append(event)
    return passed


# ============================================================================
# Gate 3 — Market Confirmation
# ============================================================================


def check_market_confirmation(asset: str) -> tuple[bool, int]:
    """
    Check aggregate SM concentration on the asset.
    Returns (confirmed, trader_count).
    """
    result = mcporter_read("leaderboard_get_markets", {})
    if "error" in result:
        return False, 0

    markets = result.get("markets", result.get("data", result))
    if not isinstance(markets, list):
        return False, 0

    for market in markets:
        if market.get("asset", "") == asset:
            trader_count = int(market.get("traderCount", market.get("traders", 0)))
            return trader_count >= MIN_MARKET_TRADERS, trader_count

    return False, 0


# ============================================================================
# Gate 4 — Volume Confirmation
# ============================================================================


def check_volume_confirmation(asset: str) -> tuple[bool, float]:
    """
    Check 1h volume vs 6h average.
    Returns (confirmed, volume_ratio).
    """
    result = mcporter_read("market_get_asset_data", {"asset": asset})
    if "error" in result:
        return False, 0.0

    data = result.get("data", result)
    if not isinstance(data, dict):
        data = result

    vol_1h = float(data.get("volume1h", data.get("vol1h", 0)))
    vol_6h = float(data.get("volume6h", data.get("vol6h", 0)))

    if vol_6h <= 0:
        return False, 0.0

    avg_1h_from_6h = vol_6h / 6
    if avg_1h_from_6h <= 0:
        return False, 0.0

    ratio = vol_1h / avg_1h_from_6h
    return ratio >= MIN_VOL_RATIO, round(ratio, 2)


# ============================================================================
# Gate 5 — Regime Filter
# ============================================================================


def get_regime_adjustment(direction: str) -> int:
    """
    Check BTC regime alignment. Returns score adjustment.
    Counter-trend: -3, Aligned: +1, Neutral: 0.
    """
    regime = load_regime()
    btc_trend = regime.get("btcTrend", regime.get("trend", ""))

    if not btc_trend:
        return 0

    btc_trend_lower = btc_trend.lower()

    if direction == "LONG" and btc_trend_lower in ("bearish", "down"):
        return -3
    if direction == "SHORT" and btc_trend_lower in ("bullish", "up"):
        return -3
    if direction == "LONG" and btc_trend_lower in ("bullish", "up"):
        return 1
    if direction == "SHORT" and btc_trend_lower in ("bearish", "down"):
        return 1

    return 0


# ============================================================================
# Scoring
# ============================================================================


def score_consensus(
    events: list[dict],
    market_confirmed: bool,
    market_trader_count: int,
    volume_ratio: float,
    regime_adj: int,
) -> tuple[int, dict]:
    """
    Score a consensus group. Returns (total_score, breakdown).
    """
    trader_count = len(events)
    avg_tier = sum(int(e.get("tier", 1)) for e in events) / max(trader_count, 1)
    avg_conc = sum(float(e.get("concentration", 0)) for e in events) / max(
        trader_count, 1
    )

    # Trader count: 2 per trader
    trader_pts = trader_count * 2

    # Avg tier: 1-3 points
    tier_pts = min(int(avg_tier), 3)

    # Avg concentration: 1-2 points
    conc_pts = 1 if avg_conc >= 0.4 else 0
    if avg_conc >= 0.7:
        conc_pts = 2

    # Market confirmation: 1-2 points
    market_pts = 0
    if market_confirmed:
        market_pts = 1
        if market_trader_count >= 10:
            market_pts = 2

    # Volume strength: 0-1
    vol_pts = 1 if volume_ratio >= MIN_VOL_RATIO else 0

    total = trader_pts + tier_pts + conc_pts + market_pts + vol_pts + regime_adj

    breakdown = {
        "traderCount": trader_count,
        "traderPts": trader_pts,
        "avgTier": round(avg_tier, 1),
        "tierPts": tier_pts,
        "avgConcentration": round(avg_conc, 2),
        "concPts": conc_pts,
        "marketConfirmed": market_confirmed,
        "marketTraders": market_trader_count,
        "marketPts": market_pts,
        "volumeRatio": volume_ratio,
        "volPts": vol_pts,
        "regimeAdj": regime_adj,
        "total": total,
    }

    return total, breakdown


# ============================================================================
# Risk management
# ============================================================================


def load_cooldowns() -> dict:
    return load_json(KOMODO_COOLDOWNS_FILE, default={})


def save_cooldowns(data: dict):
    save_json(KOMODO_COOLDOWNS_FILE, data)


def load_entries() -> dict:
    return load_json(
        KOMODO_ENTRIES_FILE,
        default={
            "date": "",
            "count": 0,
            "consecutiveLosses": 0,
            "dailyPnl": 0.0,
        },
    )


def save_entries(data: dict):
    save_json(KOMODO_ENTRIES_FILE, data)


def get_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_risk_limits() -> tuple[bool, str]:
    """Check all risk management limits. Returns (allowed, reason)."""
    entries = load_entries()
    today = get_today()

    # Reset daily counters
    if entries.get("date") != today:
        entries["date"] = today
        entries["count"] = 0
        entries["consecutiveLosses"] = 0
        entries["dailyPnl"] = 0.0
        save_entries(entries)

    # Daily loss limit
    if entries.get("dailyPnl", 0) <= -(DAILY_LOSS_LIMIT_PCT):
        return False, f"Daily loss limit hit ({entries['dailyPnl']:.1f}%)"

    # Consecutive loss cooldown
    if entries.get("consecutiveLosses", 0) >= MAX_CONSECUTIVE_LOSSES:
        cooldown_until = entries.get("cooldownUntil", "")
        if cooldown_until:
            try:
                cutoff = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < cutoff:
                    return False, f"Consecutive loss cooldown until {cooldown_until}"
            except ValueError:
                pass
        # Cooldown expired — reset
        entries["consecutiveLosses"] = 0
        save_entries(entries)

    # Daily entry limit
    max_entries = BASE_MAX_ENTRIES_PER_DAY
    if entries.get("dailyPnl", 0) > 0:
        max_entries = PROFITABLE_DAY_MAX_ENTRIES
    if entries.get("count", 0) >= max_entries:
        return False, f"Max entries/day hit ({entries['count']}/{max_entries})"

    return True, ""


def check_asset_cooldown(asset: str) -> bool:
    """Returns True if asset is on cooldown."""
    cooldowns = load_cooldowns()
    last_entry = cooldowns.get(asset, "")
    if not last_entry:
        return False
    try:
        last_time = datetime.fromisoformat(last_entry.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < last_time + timedelta(
            minutes=PER_ASSET_COOLDOWN_MIN
        )
    except ValueError:
        return False


def set_asset_cooldown(asset: str):
    cooldowns = load_cooldowns()
    cooldowns[asset] = now_iso()
    save_cooldowns(cooldowns)


def count_komodo_open_positions() -> int:
    """Count open positions across all strategies that were entered by KOMODO."""
    strategies = get_enabled_strategies()
    count = 0
    for strat in strategies:
        state_dir = get_strategy_state_dir(strat["_key"])
        for f in state_dir.glob("dsl-*.json"):
            state = load_json(f)
            if (
                state
                and state.get("active")
                and state.get("entrySource", "").startswith("auto-komodo")
            ):
                count += 1
    return count


def increment_entry_count():
    entries = load_entries()
    today = get_today()
    if entries.get("date") != today:
        entries = {"date": today, "count": 0, "consecutiveLosses": 0, "dailyPnl": 0.0}
    entries["count"] = entries.get("count", 0) + 1
    save_entries(entries)


# ============================================================================
# Main scan loop
# ============================================================================


def scan():
    """Run the full five-gate scan."""
    # Gate 1: Fetch momentum events
    events = fetch_momentum_events()
    if not events:
        return

    # Group by asset+direction, find consensus
    consensus_groups = group_events_by_consensus(events)
    if not consensus_groups:
        return

    # Gate 2: Quality filter each group
    qualified_groups: dict[str, list[dict]] = {}
    for key, group_events in consensus_groups.items():
        filtered = filter_by_quality(group_events)
        if len(filtered) >= MIN_CONSENSUS_TRADERS:
            qualified_groups[key] = filtered

    if not qualified_groups:
        return

    # Save scan to event history
    scan_record = {
        "timestamp": now_iso(),
        "totalEvents": len(events),
        "consensusGroups": len(consensus_groups),
        "qualifiedGroups": len(qualified_groups),
        "groups": {
            k: {
                "traders": len(v),
                "avgTier": round(sum(int(e.get("tier", 1)) for e in v) / len(v), 1),
            }
            for k, v in qualified_groups.items()
        },
    }
    event_history = load_json(KOMODO_EVENTS_FILE, default=[])
    event_history.append(scan_record)
    if len(event_history) > 100:
        event_history = event_history[-100:]
    save_json(KOMODO_EVENTS_FILE, event_history)

    # Process each qualified consensus group through remaining gates
    entries_this_scan = 0

    for key, group_events in qualified_groups.items():
        if entries_this_scan >= 2:
            break

        asset = group_events[0]["_asset"]
        direction = group_events[0]["_direction"]

        # Gate 3: Market confirmation
        market_confirmed, market_trader_count = check_market_confirmation(asset)

        # Gate 4: Volume confirmation
        vol_confirmed, volume_ratio = check_volume_confirmation(asset)
        if not vol_confirmed:
            log(f"KOMODO: {asset} failed volume gate (ratio={volume_ratio})")
            continue

        # Gate 5: Regime filter
        regime_adj = get_regime_adjustment(direction)

        # Score
        score, breakdown = score_consensus(
            group_events,
            market_confirmed,
            market_trader_count,
            volume_ratio,
            regime_adj,
        )

        log(
            f"KOMODO: {direction} {asset} scored {score} "
            f"(traders={breakdown['traderCount']} tier={breakdown['avgTier']} "
            f"conc={breakdown['avgConcentration']} mkt={market_trader_count} "
            f"vol={volume_ratio} regime={regime_adj})"
        )

        if score < MIN_SCORE:
            continue

        # All gates passed — attempt entry
        if is_entries_allowed():
            add_pending_entry(
                {
                    "asset": asset,
                    "direction": direction,
                    "autoEntered": False,
                    "score": score,
                    "source": "komodo",
                    "mode": "KOMODO",
                    "reasons": [
                        f"CONSENSUS {breakdown.get('traderCount', 0)}",
                        f"TIER {breakdown.get('avgTier', 0)}",
                        f"CONC {breakdown.get('avgConcentration', 0)}",
                    ],
                    "scoreBreakdown": breakdown,
                }
            )
            entries_this_scan += 1


def main():
    if not acquire_lock("komodo-scanner"):
        return

    try:
        record_heartbeat("komodo")
        git_pull()
        scan()
        git_sync("auto: KOMODO scan")
    finally:
        release_lock("komodo-scanner")


if __name__ == "__main__":
    main()
