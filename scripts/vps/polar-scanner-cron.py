#!/usr/bin/env python3
"""
POLAR v1.0 — ETH Alpha Hunter with Position Lifecycle.

Single-asset focus. ETH only. Every signal source available (SM, funding, OI,
4-timeframe trend, volume, BTC correlation). Maximum conviction.

Three-mode position lifecycle:
  MODE 1 — HUNTING: normal scanning, all signals must align, score 10+ to enter
  MODE 2 — RIDING: position open, DSL trails, monitor thesis
  MODE 3 — STALKING: DSL closed, watch for reload on dip, or reset if thesis dies
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
    mcporter_read,
    send_telegram,
    current_regime_params,
    count_open_slots,
    get_enabled_strategies,
    get_strategy_state_dir,
    POSITION_STATE_DIR,
    CONFIG_DIR,
    add_pending_entry,
    record_heartbeat,
)

POLAR_STATE_FILE = POSITION_STATE_DIR / "polar-state.json"
POLAR_CONFIG_FILE = CONFIG_DIR / "polar-config.json"


# ─── Tech Helpers ─────────────────────────────────────────────


def price_momentum(candles, n_bars=1):
    if len(candles) < n_bars + 1:
        return 0
    old = float(candles[-(n_bars + 1)].get("close", candles[-(n_bars + 1)].get("c", 0)))
    new = float(candles[-1].get("close", candles[-1].get("c", 0)))
    if old == 0:
        return 0
    return ((new - old) / old) * 100


def trend_structure(candles, lookback=6):
    if len(candles) < lookback:
        return "NEUTRAL", 0
    lows = [float(c.get("low", c.get("l", 0))) for c in candles[-lookback:]]
    highs = [float(c.get("high", c.get("h", 0))) for c in candles[-lookback:]]
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    total = lookback - 1
    if higher_lows >= total * 0.6:
        return "BULLISH", higher_lows / total
    elif lower_highs >= total * 0.6:
        return "BEARISH", lower_highs / total
    return "NEUTRAL", 0


def volume_ratio(candles, lookback=10):
    if len(candles) < lookback + 1:
        return 1.0
    vols = [
        float(c.get("volume", c.get("v", c.get("vlm", 0))))
        for c in candles[-(lookback + 1) : -1]
    ]
    avg = sum(vols) / len(vols) if vols else 1
    latest = float(
        candles[-1].get("volume", candles[-1].get("v", candles[-1].get("vlm", 0)))
    )
    return latest / avg if avg > 0 else 1.0


def volume_trend(candles, lookback=6):
    if len(candles) < lookback + 2:
        return 0
    vols = [
        float(c.get("volume", c.get("v", c.get("vlm", 0))))
        for c in candles[-(lookback + 2) :]
    ]
    half = lookback // 2
    recent = sum(vols[-half:]) / half if half > 0 else 1
    earlier = sum(vols[:half]) / half if half > 0 else 1
    if earlier == 0:
        return 0
    return ((recent - earlier) / earlier) * 100


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    g, l = gains[-period:], losses[-period:]
    avg_g, avg_l = sum(g) / period, sum(l) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


# ─── Data Fetching ───────────────────────────────────────────


def get_eth_full_picture():
    result = mcporter_read(
        "market_get_asset_data",
        {
            "asset": "ETH",
            "candle_intervals": ["5m", "15m", "1h", "4h"],
            "include_funding": True,
        },
    )
    if "error" in result:
        return None
    return result.get("data", result)


def get_btc_correlation():
    result = mcporter_read(
        "market_get_asset_data",
        {"asset": "BTC", "candle_intervals": ["15m", "1h"], "include_funding": False},
    )
    if "error" in result:
        return None, None
    data = result.get("data", result)
    candles_15m = data.get("candles", {}).get("15m", [])
    candles_1h = data.get("candles", {}).get("1h", [])
    mom_15m = price_momentum(candles_15m, 1) if len(candles_15m) >= 2 else None
    mom_1h = price_momentum(candles_1h, 1) if len(candles_1h) >= 2 else None
    return mom_15m, mom_1h


def get_eth_sm_direction():
    result = mcporter_read("leaderboard_get_markets", {})
    if "error" in result:
        return None, 0, 0

    markets = result.get("data", result)
    if isinstance(markets, dict):
        markets = markets.get("markets", [])
    if not isinstance(markets, list):
        return None, 0, 0

    asset_long_pct = 0
    asset_short_pct = 0
    asset_traders = 0
    found = False

    for m in markets:
        if not isinstance(m, dict):
            continue
        token = m.get("token", m.get("coin", m.get("asset", "")))
        if token != "ETH":
            continue
        found = True
        direction = m.get("direction", m.get("side", "")).lower()
        pct = float(m.get("pct_of_top_traders_gain", m.get("longPct", 50)))
        traders = int(m.get("trader_count", m.get("traderCount", m.get("traders", 0))))
        if direction == "long":
            asset_long_pct = pct
            asset_traders += traders
        elif direction == "short":
            asset_short_pct = pct
            asset_traders += traders

    if not found:
        return None, 0, 0

    total = asset_long_pct + asset_short_pct
    if total == 0:
        return "NEUTRAL", 50, asset_traders

    long_ratio = (asset_long_pct / total) * 100 if total > 0 else 50
    if long_ratio > 58:
        return "LONG", long_ratio, asset_traders
    elif long_ratio < 42:
        return "SHORT", 100 - long_ratio, asset_traders
    return "NEUTRAL", 50, asset_traders


# ─── Thesis Builder ──────────────────────────────────────────


def build_eth_thesis(entry_cfg):
    eth_data = get_eth_full_picture()
    if not eth_data:
        return None

    candles_5m = eth_data.get("candles", {}).get("5m", [])
    candles_15m = eth_data.get("candles", {}).get("15m", [])
    candles_1h = eth_data.get("candles", {}).get("1h", [])
    candles_4h = eth_data.get("candles", {}).get("4h", [])
    funding = float(eth_data.get("asset_context", eth_data).get("funding", 0))

    if (
        len(candles_5m) < 12
        or len(candles_15m) < 8
        or len(candles_1h) < 8
        or len(candles_4h) < 6
    ):
        return None

    price = float(candles_5m[-1].get("close", candles_5m[-1].get("c", 0)))

    trend_4h, trend_strength_4h = trend_structure(candles_4h)
    if trend_4h == "NEUTRAL":
        return None  # No conviction without macro structure

    direction = "LONG" if trend_4h == "BULLISH" else "SHORT"

    trend_1h, _ = trend_structure(candles_1h)
    if trend_1h != trend_4h:
        return None

    mom_5m = price_momentum(candles_5m, 1)
    mom_15m = price_momentum(candles_15m, 1)
    mom_1h = price_momentum(candles_1h, 2)
    mom_4h = price_momentum(candles_4h, 1)

    min_mom_15m = entry_cfg.get("minMom15mPct", 0.1)
    if direction == "LONG" and mom_15m < min_mom_15m:
        return None
    if direction == "SHORT" and mom_15m > -min_mom_15m:
        return None

    score = 0
    reasons = []

    score += 3
    reasons.append(f"4h_{trend_4h.lower()}_{trend_strength_4h:.0%}")
    score += 2
    reasons.append(f"1h_confirms_{mom_1h:+.2f}%")

    if abs(mom_15m) > min_mom_15m * 2:
        score += 1
        reasons.append(f"15m_strong_{mom_15m:+.2f}%")
    else:
        reasons.append(f"15m_{mom_15m:+.2f}%")

    if (direction == "LONG" and mom_5m > 0) or (direction == "SHORT" and mom_5m < 0):
        score += 1
        reasons.append("4TF_aligned")

    sm_dir, sm_pct, sm_count = get_eth_sm_direction()
    if sm_dir == direction:
        score += 2
        reasons.append(f"sm_aligned_{sm_pct:.0f}%_{sm_count}traders")
        if sm_pct > 65:
            score += 1
            reasons.append("sm_strongly_tilted")
    elif sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        return None

    if direction == "LONG" and funding < 0:
        score += 2
        reasons.append(f"funding_pays_longs_{funding:+.4f}")
    elif direction == "SHORT" and funding > 0:
        score += 2
        reasons.append(f"funding_pays_shorts_{funding:+.4f}")
    elif (direction == "LONG" and funding > 0.005) or (
        direction == "SHORT" and funding < -0.005
    ):
        score -= 1
        reasons.append(f"funding_crowded_{funding:+.4f}")

    vol_1h = volume_ratio(candles_1h)
    min_vol = entry_cfg.get("minVolRatio", 1.2)
    if vol_1h >= min_vol:
        score += 1
        reasons.append(f"vol_{vol_1h:.1f}x")
    elif vol_1h < 0.7:
        score -= 1
        reasons.append("vol_weak")

    vol_trend_1h = volume_trend(candles_1h)
    if vol_trend_1h > 15:
        score += 1
        reasons.append(f"vol_rising_{vol_trend_1h:+.0f}%")

    vol_recent = sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-3:])
    vol_earlier = sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-6:-3])
    oi_proxy = (
        ((vol_recent - vol_earlier) / vol_earlier * 100) if vol_earlier > 0 else 0
    )
    if oi_proxy > 10:
        score += 1
        reasons.append(f"oi_growing_{oi_proxy:+.0f}%")

    corr_mom_15m, corr_mom_1h = get_btc_correlation()
    if corr_mom_15m is not None and corr_mom_1h is not None:
        corr_agrees = (
            direction == "LONG" and corr_mom_15m > 0 and corr_mom_1h > 0
        ) or (direction == "SHORT" and corr_mom_15m < 0 and corr_mom_1h < 0)
        if corr_agrees:
            score += 1
            reasons.append(f"btc_confirms_{corr_mom_1h:+.2f}%")

    closes_1h = [float(c.get("close", c.get("c", 0))) for c in candles_1h]
    rsi = calc_rsi(closes_1h)
    if direction == "LONG" and rsi > entry_cfg.get("rsiMaxLong", 74):
        return None
    if direction == "SHORT" and rsi < entry_cfg.get("rsiMinShort", 26):
        return None
    if (direction == "LONG" and rsi < 55) or (direction == "SHORT" and rsi > 45):
        score += 1
        reasons.append(f"rsi_room_{rsi:.0f}")

    if abs(mom_4h) > 1.0:
        score += 1
        reasons.append(f"4h_momentum_{mom_4h:+.1f}%")

    return {
        "asset": "ETH",
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "price": price,
    }


# ─── Re-Evaluate Position ────────────────────────────────────


def evaluate_eth_position(direction, entry_cfg):
    eth_data = get_eth_full_picture()
    if not eth_data:
        return True, ["data_unavailable_hold"]

    candles_1h = eth_data.get("candles", {}).get("1h", [])
    candles_4h = eth_data.get("candles", {}).get("4h", [])
    funding = float(eth_data.get("asset_context", eth_data).get("funding", 0))

    if len(candles_4h) < 6:
        return True, ["insufficient_data_hold"]

    invalidations = []

    trend_4h, _ = trend_structure(candles_4h)
    if direction == "LONG" and trend_4h == "BEARISH":
        invalidations.append("4h_trend_flipped_bearish")
    elif direction == "SHORT" and trend_4h == "BULLISH":
        invalidations.append("4h_trend_flipped_bullish")

    sm_dir, sm_pct, _ = get_eth_sm_direction()
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        invalidations.append(f"sm_flipped_{sm_dir}_{sm_pct:.0f}%")

    threshold = entry_cfg.get("fundingExtremeThreshold", 0.012)
    if direction == "LONG" and funding > threshold:
        invalidations.append(f"funding_extreme_{funding:+.4f}")
    elif direction == "SHORT" and funding < -threshold:
        invalidations.append(f"funding_extreme_{funding:+.4f}")

    if len(candles_1h) >= 12:
        recent_vols = [float(c.get("volume", c.get("v", 0))) for c in candles_1h[-3:]]
        avg_vol = (
            sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-12:-3]) / 9
        )
        if avg_vol > 0 and all(v < avg_vol * 0.3 for v in recent_vols):
            invalidations.append("volume_dried_up_3h")

    corr_15m, corr_1h = get_btc_correlation()
    if corr_1h is not None:
        if direction == "LONG" and corr_1h < -1.0:
            invalidations.append(f"btc_diverging_{corr_1h:+.1f}%")
        elif direction == "SHORT" and corr_1h > 1.0:
            invalidations.append(f"btc_diverging_{corr_1h:+.1f}%")

    return (len(invalidations) == 0), invalidations


# ─── Stalk Evaluation ────────────────────────────────────────


def evaluate_reload(exit_state, entry_cfg):
    stalk_cfg = entry_cfg.get("stalk", {})
    direction = exit_state.get("exitDirection")
    exit_ts = exit_state.get("exitTimestamp")
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

    max_stalk_hours = stalk_cfg.get("maxStalkHours", 6)
    if hours_stalking > max_stalk_hours:
        return False, ["stalk_timeout_{:.1f}h".format(hours_stalking)]

    eth_data = get_eth_full_picture()
    if not eth_data:
        return False, ["data_unavailable"]

    candles_5m = eth_data.get("candles", {}).get("5m", [])
    candles_1h = eth_data.get("candles", {}).get("1h", [])
    candles_4h = eth_data.get("candles", {}).get("4h", [])
    funding = float(eth_data.get("asset_context", eth_data).get("funding", 0))

    kill_reasons = []
    reload_checks = []

    trend_4h, _ = trend_structure(candles_4h)
    expected_trend = "BULLISH" if direction == "LONG" else "BEARISH"
    if trend_4h != expected_trend and trend_4h != "NEUTRAL":
        kill_reasons.append(f"4h_trend_reversed_{trend_4h}")

    sm_dir, sm_pct, _ = get_eth_sm_direction()
    if sm_dir and sm_dir != "NEUTRAL" and sm_dir != direction:
        kill_reasons.append(f"sm_flipped_{sm_dir}")

    funding_ann = abs(funding) * 8760
    max_funding = stalk_cfg.get("maxFundingAnnPct", 100)
    if (direction == "LONG" and funding > 0 and funding_ann > max_funding) or (
        direction == "SHORT" and funding < 0 and funding_ann > max_funding
    ):
        kill_reasons.append(f"funding_extreme_{funding_ann:.0f}%ann")

    if len(candles_1h) >= 6:
        recent_vols = [float(c.get("volume", c.get("v", 0))) for c in candles_1h[-3:]]
        earlier_vols = [
            float(c.get("volume", c.get("v", 0))) for c in candles_1h[-6:-3]
        ]
        avg_recent = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        avg_earlier = sum(earlier_vols) / len(earlier_vols) if earlier_vols else 1
        if avg_earlier > 0:
            oi_change = ((avg_recent - avg_earlier) / avg_earlier) * 100
            if oi_change < -20:
                kill_reasons.append(f"oi_collapsed_{oi_change:+.0f}%")

    if kill_reasons:
        return False, kill_reasons

    if hours_stalking < 0.5:
        reload_checks.append("waiting_for_1h_candle")

    if len(candles_5m) >= 3:
        mom_5m_1 = price_momentum(candles_5m, 1)
        mom_5m_2 = price_momentum(candles_5m[:-1], 1)
        if direction == "LONG":
            if mom_5m_1 > 0.15 and mom_5m_1 > mom_5m_2:
                reload_checks.append(f"fresh_5m_impulse_{mom_5m_1:+.2f}%")
            else:
                reload_checks.append("no_5m_impulse")
        else:
            if mom_5m_1 < -0.15 and mom_5m_1 < mom_5m_2:
                reload_checks.append(f"fresh_5m_impulse_{mom_5m_1:+.2f}%")
            else:
                reload_checks.append("no_5m_impulse")

    if len(candles_1h) >= 4:
        recent_v = (
            sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-2:]) / 2
        )
        earlier_v = (
            sum(float(c.get("volume", c.get("v", 0))) for c in candles_1h[-4:-2]) / 2
        )
        if earlier_v > 0 and recent_v >= earlier_v * 0.8:
            reload_checks.append("oi_stable")
        else:
            reload_checks.append("oi_declining")

    min_vol_pct = stalk_cfg.get("minReloadVolPct", 50)
    vol = volume_ratio(candles_5m)
    if vol >= exit_vol * min_vol_pct / 100:
        reload_checks.append(f"vol_sufficient_{vol:.1f}x")
    else:
        reload_checks.append(f"vol_weak_{vol:.1f}x")

    crowd_threshold = stalk_cfg.get("crowdedFundingAnnPct", 50)
    if (direction == "LONG" and (funding <= 0 or funding_ann < crowd_threshold)) or (
        direction == "SHORT" and (funding >= 0 or funding_ann < crowd_threshold)
    ):
        reload_checks.append("funding_ok")
    else:
        reload_checks.append(f"funding_crowded_{funding_ann:.0f}%ann")

    if sm_dir == direction:
        reload_checks.append(f"sm_aligned_{sm_pct:.0f}%")
    elif sm_dir == "NEUTRAL":
        reload_checks.append("sm_neutral_ok")
    else:
        reload_checks.append(f"sm_not_aligned_{sm_dir}")

    if trend_4h == expected_trend:
        reload_checks.append("4h_intact")
    else:
        reload_checks.append(f"4h_{trend_4h}")

    fails = [
        r
        for r in reload_checks
        if any(
            bad in r
            for bad in [
                "no_5m",
                "oi_declining",
                "vol_weak",
                "funding_crowded",
                "sm_not_aligned",
                "waiting_for",
            ]
        )
    ]

    if not fails:
        return True, reload_checks
    else:
        return False, reload_checks


# ─── DSL State Builder ───────────────────────────────────────


# ─── Main ────────────────────────────────────────────────────


def scan():
    config = load_json(POLAR_CONFIG_FILE)
    if not config:
        log("POLAR: No config found.")
        return

    state = load_json(POLAR_STATE_FILE, default={"currentMode": "HUNTING"})
    mode = state.get("currentMode", "HUNTING")

    strategies = get_enabled_strategies()
    target_strat = None
    active_pos = None

    for strat in strategies:
        sdir = get_strategy_state_dir(strat["_key"])
        dsl_file = sdir / "dsl-ETH.json"
        ps = load_json(dsl_file)
        if ps and ps.get("active") and ps.get("entrySource", "").startswith("polar"):
            active_pos = ps
            target_strat = strat
            break

    if not target_strat and mode == "HUNTING":
        for strat in strategies:
            if count_open_slots(strat) > 0:
                target_strat = strat
                break

    # RIDING MODE
    if active_pos and mode in ("RIDING", "HUNTING"):
        if mode != "RIDING":
            state["currentMode"] = "RIDING"
            save_json(POLAR_STATE_FILE, state)

        direction = active_pos["direction"]
        valid, reasons = evaluate_eth_position(direction, config.get("entry", {}))

        if not valid:
            log(f"POLAR: Thesis failed for ETH {direction}: {reasons}")
            active_pos["active"] = False
            active_pos["closedAt"] = now_iso()
            active_pos["closeReason"] = "polar_thesis_exit"
            sfile = get_strategy_state_dir(target_strat["_key"]) / "dsl-ETH.json"
            save_json(sfile, active_pos)
            send_telegram(f"🐻‍❄️ POLAR THESIS EXIT: ETH\nReasons: {', '.join(reasons)}")
            state["currentMode"] = "HUNTING"
            state.pop("exitState", None)
            save_json(POLAR_STATE_FILE, state)
        return

    # DETECT DSL EXIT
    if not active_pos and mode == "RIDING":
        ast_data = get_eth_full_picture()
        evol = 1.0
        if ast_data:
            c5m = ast_data.get("candles", {}).get("5m", [])
            evol = volume_ratio(c5m) if c5m else 1.0

        state["currentMode"] = "STALKING"
        state["exitState"] = {
            "exitDirection": state.get("lastDirection", "LONG"),
            "exitTimestamp": now_iso(),
            "exitEntryVolRatio": evol,
        }
        save_json(POLAR_STATE_FILE, state)
        log(f"POLAR: ETH hit DSL stop — STALKING for reload.")
        return

    # STALKING MODE
    if mode == "STALKING":
        exst = state.get("exitState", {})
        if not exst:
            state["currentMode"] = "HUNTING"
            save_json(POLAR_STATE_FILE, state)
        else:
            reload, reasons = evaluate_reload(exst, config.get("entry", {}))
            if reload and target_strat:
                dirn = exst["exitDirection"]
                budget = target_strat.get("budget", 1000)
                alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
                margin = budget * alloc * 1.3
                lev = config["leverage"]["default"]

                log(f"POLAR: Reload signal for ETH {dirn}")
                add_pending_entry(
                    {
                        "asset": "ETH",
                        "direction": dirn,
                        "autoEntered": False,
                        "strategyKey": target_strat["_key"],
                        "margin": margin,
                        "leverage": lev,
                        "score": 12,
                        "source": "polar",
                        "mode": "POLAR_RELOAD",
                        "reasons": reasons,
                    }
                )
                state["currentMode"] = "RIDING"
                state["lastDirection"] = dirn
                state.pop("exitState", None)
                save_json(POLAR_STATE_FILE, state)
                send_telegram(
                    f"🐻‍❄️ POLAR RELOAD SIGNAL: {dirn} ETH\nMargin: ${margin:.0f} | Lev: {lev}x"
                )
                return

            kills = [
                r
                for r in reasons
                if any(
                    k in r
                    for k in [
                        "stalk_timeout",
                        "4h_trend_reversed",
                        "sm_flipped",
                        "funding_extreme",
                        "oi_collapsed",
                    ]
                )
            ]
            if kills:
                state["currentMode"] = "HUNTING"
                state.pop("exitState", None)
                save_json(POLAR_STATE_FILE, state)
                log(f"POLAR: STALKING killed ({kills[0]}) -> RESET to HUNTING")
            return

    # HUNTING MODE (Default)
    if not target_strat:
        return

    entry_cfg = config.get("entry", {})
    min_score = entry_cfg.get("minScore", 10)

    thesis = build_eth_thesis(entry_cfg)
    if not thesis or thesis["score"] < min_score:
        return

    budget = target_strat.get("budget", 1000)
    alloc = current_regime_params().get("allocPctPerSlot", 30) / 100

    base_margin_pct = entry_cfg.get("marginPctBase", 0.30)
    # Using budget * alloc as account proxy.
    if thesis["score"] >= 14:
        base_adj = 1.5
    elif thesis["score"] >= 12:
        base_adj = 1.25
    else:
        base_adj = 1.0

    margin = budget * alloc * base_adj
    lev_cfg = config.get("leverage", {})
    if thesis["score"] >= 14:
        lev = lev_cfg.get("max", 20)
    elif thesis["score"] >= 12:
        lev = lev_cfg.get("high", 18)
    elif thesis["score"] >= 10:
        lev = lev_cfg.get("default", 15)
    else:
        lev = lev_cfg.get("min", 12)

    log(f"POLAR: Signal ETH {thesis['direction']} score {thesis['score']}")

    add_pending_entry(
        {
            "asset": "ETH",
            "direction": thesis["direction"],
            "autoEntered": False,
            "strategyKey": target_strat["_key"],
            "margin": margin,
            "leverage": lev,
            "score": thesis["score"],
            "source": "polar",
            "mode": "POLAR_HUNT",
            "reasons": thesis["reasons"],
        }
    )

    state["currentMode"] = "RIDING"
    state["lastDirection"] = thesis["direction"]
    save_json(POLAR_STATE_FILE, state)

    send_telegram(
        f"🐻‍❄️ POLAR SIGNAL: {thesis['direction']} ETH\n"
        f"Score: {thesis['score']}\n"
        f"Reasons: {', '.join(thesis['reasons'])}\n"
        f"Margin: ${margin:.0f} | Lev: {lev}x"
    )


def main():
    if not acquire_lock("polar-scanner"):
        return
    try:
        record_heartbeat("polar")
        scan()
    finally:
        release_lock("polar-scanner")


if __name__ == "__main__":
    main()
