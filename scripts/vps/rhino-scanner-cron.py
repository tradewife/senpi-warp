#!/usr/bin/env python3
"""
RHINO v1.0 — Momentum pyramider.

Stage 1: scout with 30% of max size.
Stage 2: add 40% at +10% ROE if thesis still holds.
Stage 3: add final 30% at +20% ROE if thesis still holds.

Runs every 3 minutes via APScheduler.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    CONFIG_DIR,
    POSITION_STATE_DIR,
    acquire_lock,
    add_pending_entry,
    count_open_slots,
    current_regime_params,
    get_enabled_strategies,
    get_open_positions,
    get_strategy_state_dir,
    git_sync,
    is_entries_allowed,
    is_rotation_cooled_down,
    load_json,
    load_trade_journal,
    log,
    mcporter_call,
    now_iso,
    record_heartbeat,
    record_trade,
    release_lock,
    save_json,
    send_telegram,
)

RHINO_CONFIG_FILE = CONFIG_DIR / "rhino-config.json"
RHINO_STATE_FILE = POSITION_STATE_DIR / "rhino-state.json"


def load_config() -> dict:
    return load_json(RHINO_CONFIG_FILE, default={})


def load_state() -> dict:
    return load_json(RHINO_STATE_FILE, default={"pyramids": {}, "updatedAt": None})


def save_state(state: dict):
    state["updatedAt"] = now_iso()
    save_json(RHINO_STATE_FILE, state)


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def price_momentum(candles: list[dict], n_bars: int = 1) -> float:
    if len(candles) < n_bars + 1:
        return 0.0
    old = _safe_float(candles[-(n_bars + 1)].get("close", candles[-(n_bars + 1)].get("c", 0)))
    new = _safe_float(candles[-1].get("close", candles[-1].get("c", 0)))
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def trend_structure(candles: list[dict], lookback: int = 6) -> tuple[str, float]:
    if len(candles) < lookback:
        return "NEUTRAL", 0.0
    lows = [_safe_float(c.get("low", c.get("l", 0))) for c in candles[-lookback:]]
    highs = [_safe_float(c.get("high", c.get("h", 0))) for c in candles[-lookback:]]
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    total = lookback - 1
    if higher_lows >= total * 0.6:
        return "BULLISH", higher_lows / total
    if lower_highs >= total * 0.6:
        return "BEARISH", lower_highs / total
    return "NEUTRAL", 0.0


def volume_ratio(candles: list[dict], lookback: int = 10) -> float:
    if len(candles) < lookback + 1:
        return 1.0
    vols = [_safe_float(c.get("volume", c.get("v", c.get("vlm", 0)))) for c in candles[-(lookback + 1):-1]]
    avg = sum(vols) / len(vols) if vols else 1.0
    latest = _safe_float(candles[-1].get("volume", candles[-1].get("v", candles[-1].get("vlm", 0))))
    return latest / avg if avg > 0 else 1.0


def calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for idx in range(1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    g = gains[-period:]
    l = losses[-period:]
    avg_g = sum(g) / period
    avg_l = sum(l) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def get_top_assets(n: int = 10) -> list[dict]:
    result = mcporter_call("market_list_instruments", {})
    if "error" in result:
        return []
    instruments = result.get("data", result)
    if isinstance(instruments, dict):
        instruments = instruments.get("instruments", instruments.get("universe", []))
    if not isinstance(instruments, list):
        return []

    assets = []
    for inst in instruments:
        if not isinstance(inst, dict):
            continue
        coin = inst.get("coin") or inst.get("name", inst.get("token", ""))
        ctx = inst.get("context", inst)
        oi = _safe_float(ctx.get("openInterest", ctx.get("oi", 0)))
        mark_px = _safe_float(ctx.get("markPx", ctx.get("midPx", ctx.get("price", 0))))
        vol = _safe_float(ctx.get("dayNtlVlm", ctx.get("volume24h", 0)))
        if not coin or coin.startswith("xyz:"):
            continue
        oi_usd = oi * mark_px if mark_px > 0 else 0
        if oi_usd > 5_000_000 and vol > 0:
            assets.append({"coin": coin, "oi_usd": oi_usd, "volume": vol, "price": mark_px})
    assets.sort(key=lambda item: item["oi_usd"] + item["volume"], reverse=True)
    return assets[:n]


def get_sm_direction(coin: str) -> tuple[str | None, float]:
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        return None, 0.0
    markets = result.get("data", result)
    if isinstance(markets, dict):
        markets = markets.get("markets", markets.get("leaderboard", []))
    if not isinstance(markets, list):
        return None, 0.0

    for market in markets:
        asset = market.get("coin", market.get("asset", market.get("token", "")))
        if asset != coin:
            continue
        long_pct = _safe_float(market.get("longPct", market.get("pctOfGainsLong", 50)))
        if long_pct > 58:
            return "LONG", long_pct
        if long_pct < 42:
            return "SHORT", 100 - long_pct
        return "NEUTRAL", 50.0
    return None, 0.0


def get_asset_data(coin: str, intervals: list[str]) -> dict | None:
    result = mcporter_call("market_get_asset_data", {
        "asset": coin,
        "candle_intervals": intervals,
        "include_funding": True,
        "include_order_book": False,
    })
    if "error" in result:
        return None
    data = result.get("data", result)
    return data if isinstance(data, dict) else None


def build_thesis(coin: str, entry_cfg: dict) -> dict | None:
    data = get_asset_data(coin, ["15m", "1h", "4h"])
    if not data:
        return None

    candles_15m = data.get("candles", {}).get("15m", [])
    candles_1h = data.get("candles", {}).get("1h", [])
    candles_4h = data.get("candles", {}).get("4h", [])
    funding = _safe_float(data.get("funding", 0))

    if len(candles_1h) < 8 or len(candles_4h) < 6:
        return None

    price = _safe_float(candles_15m[-1].get("close", candles_15m[-1].get("c", 0))) if candles_15m else 0.0
    trend_4h, strength_4h = trend_structure(candles_4h)
    if trend_4h == "NEUTRAL":
        return None
    direction = "LONG" if trend_4h == "BULLISH" else "SHORT"

    trend_1h, _ = trend_structure(candles_1h)
    if trend_1h != trend_4h:
        return None

    mom_1h = price_momentum(candles_1h, 2)
    if direction == "LONG" and mom_1h < 0.3:
        return None
    if direction == "SHORT" and mom_1h > -0.3:
        return None

    score = 0
    reasons = []

    score += 3
    reasons.append(f"4h_{trend_4h.lower()}_{strength_4h:.0%}")
    score += 2
    reasons.append(f"1h_confirms_{mom_1h:+.2f}%")

    sm_dir, sm_pct = get_sm_direction(coin)
    if sm_dir == direction:
        score += 2
        reasons.append(f"sm_aligned_{sm_pct:.0f}%")
    elif sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        return None

    if (direction == "LONG" and funding < 0) or (direction == "SHORT" and funding > 0):
        score += 2
        reasons.append(f"funding_aligned_{funding:+.4f}")
    elif (direction == "LONG" and funding > 0.008) or (direction == "SHORT" and funding < -0.005):
        score -= 1
        reasons.append("funding_crowded")

    vol = volume_ratio(candles_1h)
    if vol >= 1.3:
        score += 1
        reasons.append(f"vol_{vol:.1f}x")

    closes_1h = [_safe_float(c.get("close", c.get("c", 0))) for c in candles_1h]
    rsi = calc_rsi(closes_1h)
    if direction == "LONG" and rsi > entry_cfg.get("rsiMaxLong", 74):
        return None
    if direction == "SHORT" and rsi < entry_cfg.get("rsiMinShort", 26):
        return None
    if (direction == "LONG" and rsi < 55) or (direction == "SHORT" and rsi > 45):
        score += 1
        reasons.append(f"rsi_room_{rsi:.0f}")

    mom_4h = price_momentum(candles_4h, 1)
    if abs(mom_4h) > 1.0:
        score += 1
        reasons.append(f"4h_momentum_{mom_4h:+.1f}%")

    return {
        "coin": coin,
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "price": price,
        "trend_4h": trend_4h,
        "momentum_1h": mom_1h,
        "sm_direction": sm_dir,
        "funding": funding,
        "rsi": rsi,
    }


def get_current_price(coin: str) -> float | None:
    data = get_asset_data(coin, ["15m"])
    if not data:
        return None
    candles_15m = data.get("candles", {}).get("15m", [])
    if candles_15m:
        return _safe_float(candles_15m[-1].get("close", candles_15m[-1].get("c", 0)))
    return _safe_float(data.get("markPx", data.get("price", 0))) or None


def compute_roe(entry_price: float, current_price: float, direction: str, leverage: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if direction.upper() == "LONG":
        return ((current_price - entry_price) / entry_price) * leverage * 100
    return ((entry_price - current_price) / entry_price) * leverage * 100


def evaluate_add(coin: str, direction: str, current_roe: float, current_stage: int, entry_cfg: dict) -> tuple[bool, dict | None, list[str]]:
    pyramid_cfg = entry_cfg.get("pyramid", {})
    if not pyramid_cfg.get("enabled", True):
        return False, None, ["pyramid_disabled"]

    next_stage = None
    for stage in pyramid_cfg.get("stages", []):
        if stage["stage"] > current_stage and current_roe >= stage["triggerRoe"]:
            next_stage = stage
            break
    if not next_stage:
        return False, None, ["no_stage_triggered"]

    data = get_asset_data(coin, ["1h", "4h"])
    if not data:
        return False, None, ["data_unavailable"]
    candles_4h = data.get("candles", {}).get("4h", [])
    candles_1h = data.get("candles", {}).get("1h", [])
    if len(candles_4h) < 6:
        return False, None, ["insufficient_data"]

    trend_4h, _ = trend_structure(candles_4h)
    expected = "BULLISH" if direction == "LONG" else "BEARISH"
    if trend_4h != expected:
        return False, None, ["4h_trend_broken"]

    sm_dir, _ = get_sm_direction(coin)
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        return False, None, ["sm_flipped"]

    if candles_1h and volume_ratio(candles_1h) < 0.5:
        return False, None, ["volume_died"]

    reasons = [
        f"stage_{next_stage['stage']}_triggered",
        f"roe_{current_roe:+.1f}%",
        f"add_{next_stage['addPct']}%_of_max",
        "4h_trend_intact",
        f"sm_{sm_dir or 'unknown'}",
    ]
    return True, next_stage, reasons


def build_dsl_state(coin: str, direction: str, score: int, config: dict, entry_price: float, leverage: float) -> dict:
    conviction = config.get("dsl", {}).get("convictionTiers", [])
    selected = conviction[-1] if conviction else {}
    for tier in conviction:
        if score >= tier.get("minScore", 0):
            selected = tier
            break
    dsl_cfg = config.get("dsl", {})
    return {
        "active": True,
        "asset": coin,
        "direction": direction,
        "entryPrice": entry_price,
        "leverage": leverage,
        "phase": 1,
        "lockMode": dsl_cfg.get("lockMode", "pct_of_high_water"),
        "phase2TriggerRoe": dsl_cfg.get("phase2TriggerRoe", 8),
        "highWaterPrice": entry_price,
        "highWaterRoe": 0,
        "currentTierIndex": -1,
        "currentBreachCount": 0,
        "createdAt": now_iso(),
        "highWaterUpdatedAt": now_iso(),
        "entryMode": "RHINO_SCOUT",
        "entryScore": score,
        "phase1": {
            "absoluteFloorRoe": selected.get("absoluteFloorRoe", -20),
            "hardTimeoutSec": selected.get("hardTimeoutMin", 0) * 60,
            "weakPeakCutSec": selected.get("weakPeakCutMin", 0) * 60,
            "deadWeightCutMin": selected.get("deadWeightCutMin", 0),
        },
        "tiers": dsl_cfg.get("tiers", []),
        "stagnationTp": dsl_cfg.get("stagnationTp", {
            "enabled": True,
            "roeMin": 15,
            "hwStaleMin": 90,
        }),
        "_rhino_version": "1.0",
    }


def count_daily_rhino_entries(strategy_key: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    journal = load_trade_journal()
    return sum(
        1 for trade in journal
        if trade.get("action") == "OPEN"
        and trade.get("strategyKey") == strategy_key
        and trade.get("entrySource") == "auto-rhino"
        and trade.get("recordedAt", "").startswith(today)
    )


def execute_add(strategy: dict, dsl_state: dict, pyramid_state: dict, stage: dict, reasons: list[str], config: dict) -> bool:
    max_margin = _safe_float(pyramid_state.get("maxMargin", 0))
    if max_margin <= 0:
        return False
    add_margin = round(max_margin * (_safe_float(stage.get("addPct", 0)) / 100), 2)
    if add_margin <= 0:
        return False

    leverage = _safe_float(dsl_state.get("leverage", config.get("leverage", {}).get("default", 10)))
    result = mcporter_call("strategy_create_position", {
        "strategyId": strategy.get("strategyId"),
        "asset": dsl_state["asset"],
        "direction": dsl_state["direction"],
        "marginUsd": add_margin,
        "leverage": leverage,
        "orderType": config.get("execution", {}).get("entryOrderType", "FEE_OPTIMIZED_LIMIT"),
    })
    if "error" in result:
        log(f"RHINO add failed for {dsl_state['asset']}: {result.get('error')}")
        return False

    pyramid_state["stage"] = stage["stage"]
    pyramid_state["lastAddedAt"] = now_iso()
    pyramid_state["currentMargin"] = round(_safe_float(pyramid_state.get("currentMargin", 0)) + add_margin, 2)

    dsl_state["entryPrice"] = _safe_float(result.get("entryPrice", dsl_state.get("entryPrice", 0)), dsl_state.get("entryPrice", 0))
    dsl_state["size"] = _safe_float(result.get("size", dsl_state.get("size", 0)), dsl_state.get("size", 0))
    dsl_state["entryMode"] = f"RHINO_STAGE_{stage['stage']}"
    dsl_state["entryScore"] = max(int(dsl_state.get("entryScore", 0)), 10)
    save_json(Path(dsl_state["_file"]), dsl_state)

    send_telegram(
        f"🦏 RHINO ADD: Stage {stage['stage']} {dsl_state['direction']} {dsl_state['asset']}\n"
        f"Added: ${add_margin:.0f} | Total margin: ${pyramid_state['currentMargin']:.0f}\n"
        f"Reasons: {', '.join(reasons)}"
    )
    record_trade({
        "action": "OPEN",
        "asset": dsl_state["asset"],
        "direction": dsl_state["direction"],
        "entryPrice": dsl_state["entryPrice"],
        "size": dsl_state.get("size", 0),
        "margin": add_margin,
        "leverage": leverage,
        "strategyKey": strategy["_key"],
        "entrySource": "auto-rhino",
        "entryMode": f"RHINO_STAGE_{stage['stage']}",
        "entryScore": dsl_state.get("entryScore", 0),
    })
    add_pending_entry({
        "asset": dsl_state["asset"],
        "direction": dsl_state["direction"],
        "autoEntered": True,
        "strategyKey": strategy["_key"],
        "entryPrice": dsl_state["entryPrice"],
        "margin": add_margin,
        "leverage": leverage,
        "score": dsl_state.get("entryScore", 0),
        "source": "rhino",
        "mode": f"RHINO_STAGE_{stage['stage']}",
        "reasons": reasons,
    })
    return True


def effective_daily_entry_limit(config: dict, strategy_key: str) -> int:
    dynamic = config.get("entry", {}).get("dynamicSlots", {})
    if not dynamic.get("enabled", False):
        return config.get("risk", {}).get("maxEntriesPerDay", 5)
    daily_pnl = sum(
        _safe_float(trade.get("realizedPnl", 0))
        for trade in load_trade_journal()
        if trade.get("action") == "CLOSE"
        and trade.get("strategyKey") == strategy_key
        and trade.get("recordedAt", "").startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    )
    effective = dynamic.get("baseMax", 3)
    for threshold in sorted(dynamic.get("unlockThresholds", []), key=lambda x: x.get("pnl", 0)):
        if daily_pnl >= threshold.get("pnl", 0):
            effective = threshold.get("maxEntries", effective)
    return min(effective, dynamic.get("absoluteMax", effective))


def scan() -> bool:
    config = load_config()
    if not config or not is_entries_allowed():
        return False

    state = load_state()
    pyramids = state.get("pyramids", {})

    # Priority 1: add to existing winners
    for strategy in get_enabled_strategies():
        for pos in get_open_positions(strategy["_key"]):
            asset = pos.get("asset")
            if asset not in pyramids:
                continue
            pyramid_state = pyramids[asset]
            current_stage = int(pyramid_state.get("stage", 1))
            if current_stage >= 3:
                continue
            current_price = get_current_price(asset)
            if current_price is None:
                continue
            roe = compute_roe(
                _safe_float(pos.get("entryPrice", 0)),
                current_price,
                pos.get("direction", "LONG"),
                _safe_float(pos.get("leverage", 1)),
            )
            should_add, stage, reasons = evaluate_add(asset, pos.get("direction", "LONG"), roe, current_stage, config.get("entry", {}))
            if should_add and stage:
                if execute_add(strategy, pos, pyramid_state, stage, reasons, config):
                    state["pyramids"] = pyramids
                    save_state(state)
                    git_sync("auto: RHINO pyramid add")
                    return True

    # Priority 2: scout new positions
    target_strategy = None
    for strategy in get_enabled_strategies():
        if count_open_slots(strategy) > 0:
            target_strategy = strategy
            break
    if not target_strategy:
        save_state(state)
        return False

    if count_daily_rhino_entries(target_strategy["_key"]) >= effective_daily_entry_limit(config, target_strategy["_key"]):
        save_state(state)
        return False

    active_assets = {
        pos.get("asset")
        for strategy in get_enabled_strategies()
        for pos in get_open_positions(strategy["_key"])
    }
    cooldown_min = config.get("risk", {}).get("cooldownMinutes", 90)

    candidates = []
    for asset in get_top_assets(config.get("topAssets", 10)):
        coin = asset["coin"]
        if coin in active_assets or is_rotation_cooled_down(coin, cooldown_min):
            continue
        thesis = build_thesis(coin, config.get("entry", {}))
        if thesis and thesis["score"] >= config.get("entry", {}).get("minScore", 10):
            candidates.append(thesis)

    if not candidates:
        save_state(state)
        return False

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]

    budget = _safe_float(target_strategy.get("budget", 1000))
    alloc_pct = current_regime_params().get("allocPctPerSlot", 30) / 100
    max_margin_pct = config.get("entry", {}).get("marginPctMax", 0.30)
    scout_pct = config.get("entry", {}).get("pyramid", {}).get("scoutPct", 30)
    max_margin = budget * alloc_pct * (max_margin_pct / 0.30)
    scout_margin = round(max_margin * (scout_pct / 100), 2)
    leverage = min(
        config.get("leverage", {}).get("default", 10),
        config.get("leverage", {}).get("max", 15),
        current_regime_params().get("maxLeverageCrypto", 10),
    )
    if leverage < config.get("leverage", {}).get("min", 7):
        save_state(state)
        return False

    result = mcporter_call("strategy_create_position", {
        "strategyId": target_strategy.get("strategyId"),
        "asset": best["coin"],
        "direction": best["direction"],
        "marginUsd": scout_margin,
        "leverage": leverage,
        "orderType": config.get("execution", {}).get("entryOrderType", "FEE_OPTIMIZED_LIMIT"),
    })
    if "error" in result:
        log(f"RHINO scout entry failed for {best['coin']}: {result.get('error')}")
        save_state(state)
        return False

    entry_price = _safe_float(result.get("entryPrice", 0))
    size = _safe_float(result.get("size", 0))
    dsl = build_dsl_state(best["coin"], best["direction"], best["score"], config, entry_price, leverage)
    dsl["wallet"] = target_strategy.get("wallet")
    dsl["strategyId"] = target_strategy.get("strategyId")
    dsl["strategyKey"] = target_strategy["_key"]
    dsl["size"] = size
    state_dir = get_strategy_state_dir(target_strategy["_key"])
    save_json(state_dir / f"dsl-{best['coin']}.json", dsl)

    pyramids[best["coin"]] = {
        "stage": 1,
        "direction": best["direction"],
        "strategyKey": target_strategy["_key"],
        "scoutedAt": now_iso(),
        "scoutScore": best["score"],
        "maxMargin": round(max_margin, 2),
        "currentMargin": round(scout_margin, 2),
    }
    state["pyramids"] = pyramids
    save_state(state)

    send_telegram(
        f"🦏 RHINO SCOUT: {best['direction']} {best['coin']}\n"
        f"Score: {best['score']} | Margin: ${scout_margin:.0f} ({scout_pct}% of max)\n"
        f"Reasons: {', '.join(best['reasons'][:4])}\n"
        f"Adds at +10% and +20% ROE if thesis holds"
    )
    record_trade({
        "action": "OPEN",
        "asset": best["coin"],
        "direction": best["direction"],
        "entryPrice": entry_price,
        "size": size,
        "margin": scout_margin,
        "leverage": leverage,
        "strategyKey": target_strategy["_key"],
        "entrySource": "auto-rhino",
        "entryMode": "RHINO_SCOUT",
        "entryScore": best["score"],
    })
    add_pending_entry({
        "asset": best["coin"],
        "direction": best["direction"],
        "autoEntered": True,
        "strategyKey": target_strategy["_key"],
        "entryPrice": entry_price,
        "margin": scout_margin,
        "leverage": leverage,
        "score": best["score"],
        "source": "rhino",
        "mode": "RHINO_SCOUT",
        "reasons": best["reasons"],
    })
    git_sync("auto: RHINO scan")
    return True


def main():
    if not acquire_lock("rhino-scanner"):
        return
    try:
        record_heartbeat("rhino")
        scan()
    finally:
        release_lock("rhino-scanner")


if __name__ == "__main__":
    main()
