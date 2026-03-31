#!/usr/bin/env python3
"""
ELITE-TRADER — Elite-tier Hyperliquid USDC-margined perpetuals trader.

Executes a full research-to-execution loop:
  - Regime-gated universe scan
  - Multi-source signal scoring (on-chain, HL-native, waifu scanners)
  - GraphSignalScore ranking
  - Risk-sized ALO limit orders
  - DSL high-water handoff
  - Stale order management

Integrates with the senpi-waifu hybrid scanner stack:
  compute_regime.py / arena-monitor.py / sm-flip-cron.py / orca / mantis /
  fox / komodo / condor / polar / sentinel / rhino / dsl-runner.py / risk-arbiter.py
"""

import os
import json
import uuid
import sys
import math
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    load_json,
    save_json,
    mcporter_call,
    mcporter_call_retry,
    send_telegram,
    log,
    now_iso,
    load_regime,
    load_pending_entries,
    add_pending_entry,
    get_enabled_strategies,
    get_open_positions,
    acquire_lock,
    release_lock,
    record_heartbeat,
    record_trade,
    git_sync,
    OUTPUTS_DIR,
    MEMORY_DIR,
    STATE_DIR,
    CONFIG_DIR,
)

AEST = ZoneInfo("Australia/Sydney")
UTC = timezone.utc

CORE_SYMBOLS = ["BTC", "ETH", "SOL"]
MIN_VOL_24H_USD = 50_000_000
MAX_ELITE_SLOTS = 2
MIN_LEVERAGE = 9
MAX_LEVERAGE = 12
MAX_RISK_PCT = 0.20

WEIGHTS = {
    "funding_stretch": 0.0,
    "OI_delta": 0.0,
    "basis": 1.0,
    "SM_whale_bias": 2.0,
    "scanner_confluence": 2.5,
    "regime_alignment": 1.0,
}


def get_world_stats() -> list:
    path = MEMORY_DIR / "world_stats.json"
    if path.exists():
        return load_json(path, default=[])
    return []


def load_graph_db() -> sqlite3.Connection:
    db_path = MEMORY_DIR / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_edges (
            subject TEXT,
            predicate TEXT,
            object TEXT,
            attrs_json TEXT,
            source_name TEXT,
            source_tier TEXT,
            source_link TEXT,
            ts_utc TEXT,
            confidence REAL,
            PRIMARY KEY (subject, predicate, object)
        )
    """)
    conn.commit()
    return conn


def append_graph_triples(triples: list):
    if not triples:
        return 0
    conn = load_graph_db()
    count = 0
    for t in triples:
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO graph_edges
                (subject, predicate, object, attrs_json, source_name, source_tier, source_link, ts_utc, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    t.get("subject", ""),
                    t.get("predicate", ""),
                    t.get("object", ""),
                    json.dumps(t.get("attrs", {})),
                    t.get("source_name", ""),
                    t.get("source_tier", ""),
                    t.get("source_link", ""),
                    t.get("ts_utc", now_iso()),
                    t.get("confidence", 0.5),
                ),
            )
            count += 1
        except Exception as e:
            log(f"Graph triple insert error: {e}")
    conn.commit()
    conn.close()
    return count


def load_journal_db() -> sqlite3.Connection:
    db_path = MEMORY_DIR / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_aest TEXT,
            intent TEXT,
            symbol TEXT,
            setup TEXT,
            entry REAL,
            stop REAL,
            tp1 REAL,
            tp2 REAL,
            action_taken TEXT,
            result_R REAL,
            max_fav_excursion REAL,
            max_adv_excursion REAL,
            exit_reason TEXT,
            provenance_tags TEXT
        )
    """)
    conn.commit()
    return conn


def write_journal_row(row: dict):
    conn = load_journal_db()
    ts_aest = datetime.now(AEST).isoformat()
    conn.execute(
        """
        INSERT INTO trade_journal
        (ts_aest, intent, symbol, setup, entry, stop, tp1, tp2, action_taken, result_R,
         max_fav_excursion, max_adv_excursion, exit_reason, provenance_tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            ts_aest,
            row.get("intent", "ELITE_SCAN"),
            row.get("symbol", ""),
            json.dumps(row.get("setup", {})),
            row.get("entry", 0),
            row.get("stop", 0),
            row.get("tp1", 0),
            row.get("tp2", 0),
            row.get("action_taken", "OPEN_ATTEMPT"),
            row.get("result_R", 0),
            row.get("max_fav_excursion", 0),
            row.get("max_adv_excursion", 0),
            row.get("exit_reason", ""),
            json.dumps(row.get("provenance_tags", [])),
        ),
    )
    conn.commit()
    conn.close()


DRY_RUN = "--dry-run" in sys.argv


def check_preconditions() -> dict:
    if not os.environ.get("SENPI_AUTH_TOKEN") and not os.environ.get("SENPI_API_KEY"):
        raise RuntimeError("No Senpi auth token - aborting ELITE-TRADER")

    regime_cfg = load_regime()
    risk_mode = regime_cfg.get("riskMode", "BASELINE")

    if risk_mode == "RISK_OFF" and not DRY_RUN:
        send_telegram("? ELITE-TRADER: RISK_OFF regime - no new entries.")
        raise SystemExit("RISK_OFF")
    elif risk_mode == "RISK_OFF" and DRY_RUN:
        log("ELITE-TRADER: RISK_OFF bypassed (dry-run mode)")
        risk_mode = "BASELINE"  # Use BASELINE params for scan

    active = regime_cfg.get("regimes", {}).get(risk_mode, {})
    max_slots = min(active.get("maxSlots", 2), MAX_ELITE_SLOTS)
    max_leverage = min(active.get("maxLeverageCrypto", 10), MAX_LEVERAGE)

    return {
        "risk_mode": risk_mode,
        "max_slots": max_slots,
        "max_leverage": max_leverage,
        "min_leverage": MIN_LEVERAGE,
        "max_risk_pct": MAX_RISK_PCT,
    }


def count_open_slots() -> int:
    open_positions = []
    for strat in get_enabled_strategies():
        open_positions.extend(get_open_positions(strat["_key"]))
    return MAX_ELITE_SLOTS - len(open_positions)


def fetch_sm_markets() -> dict:
    sm_raw = mcporter_call("leaderboard_get_markets", {})
    sm_markets = {}
    raw_markets = sm_raw.get("data", sm_raw)
    if isinstance(raw_markets, dict):
        raw_markets = raw_markets.get("markets", [])
    if not isinstance(raw_markets, list):
        raw_markets = []
    for m in raw_markets:
        if not isinstance(m, dict):
            continue
        asset = m.get("token", m.get("asset", ""))
        if asset:
            sm_markets[asset] = {
                "direction": str(m.get("direction", "")).upper(),
                "conviction": float(m.get("conviction", 0) or 0),
                "traders": int(m.get("traderCount", m.get("traders", 0)) or 0),
                "concentration": float(
                    m.get("contribution", m.get("pctOfTotal", 0)) or 0
                ),
            }
    return sm_markets


def build_scanner_bias(pending_entries: list) -> dict:
    scanner_bias = {}
    for e in pending_entries:
        asset = str(e.get("asset", e.get("token", ""))).upper()
        direction = str(e.get("direction", "LONG")).upper()
        priority = int(e.get("brainContext", {}).get("priority", 50) or 50)
        scanner = e.get("scanner", e.get("source", "unknown"))
        if asset not in scanner_bias:
            scanner_bias[asset] = {
                "long": 0,
                "short": 0,
                "max_priority": 0,
                "scanners": [],
            }
        if direction == "LONG":
            scanner_bias[asset]["long"] += 1
        elif direction == "SHORT":
            scanner_bias[asset]["short"] += 1
        scanner_bias[asset]["max_priority"] = max(
            scanner_bias[asset]["max_priority"], priority
        )
        if scanner not in scanner_bias[asset]["scanners"]:
            scanner_bias[asset]["scanners"].append(scanner)
    return scanner_bias


def discover_universe(all_mkts: list, sm_markets: dict, scanner_bias: dict) -> list:
    candidates = []
    for mkt in all_mkts:
        if not isinstance(mkt, dict):
            continue
        asset = str(mkt.get("name", "")).upper()
        if not asset or asset in CORE_SYMBOLS or mkt.get("is_delisted"):
            continue

        # Instruments format: data under context sub-dict
        ctx = mkt.get("context", mkt)
        vol24 = float(ctx.get("dayNtlVlm", ctx.get("volume24h", 0)) or 0)
        if vol24 < MIN_VOL_24H_USD:
            continue

        oi = float(ctx.get("openInterest", 0) or 0)
        fr = float(ctx.get("funding", 0) or 0)
        sm = sm_markets.get(asset, {})
        sb = scanner_bias.get(asset, {})

        sm_dir = sm.get("direction", "")
        sm_score = 1.0 if sm_dir == "LONG" else (-1.0 if sm_dir == "SHORT" else 0.0)
        scanner_score = (
            1.0
            if sb.get("long", 0) > sb.get("short", 0)
            else (-1.0 if sb.get("short", 0) > sb.get("long", 0) else 0.0)
        )
        sc_score = sm_score * 0.5 + scanner_score * 0.5

        trend_score = (vol24 / 1e8) + (oi / 1e8) + (abs(fr) * 1000) + sc_score
        candidates.append(
            {
                "asset": asset,
                "trend_score": trend_score,
                "vol24": vol24,
                "oi": oi,
                "fr": fr,
            }
        )

    candidates.sort(key=lambda x: x["trend_score"], reverse=True)
    trending = [c["asset"] for c in candidates[:5]]
    return CORE_SYMBOLS + trending


def compute_gss(
    asset: str,
    mkt: dict,
    sm_markets: dict,
    scanner_bias: dict,
    risk_mode: str,
    kg_triples: list,
) -> dict:
    score = {k: 0.0 for k in WEIGHTS}

    ctx = mkt.get("context", mkt)
    fr = float(ctx.get("funding", mkt.get("fundingRate", 0)) or 0)
    fr8h = fr * 3
    score["funding_stretch"] = min(abs(fr8h) / 0.0001, 1.0)

    score["OI_delta"] = 0.5

    try:
        od = mcporter_call("market_get_asset_data", {"asset": asset}, timeout=15)
        data = od.get("data", od)
        mark = float(data.get("markPrice", data.get("mark", 0)) or 0)
        idx = float(data.get("indexPrice", data.get("index", 0)) or 0)
        if idx > 0:
            basis_bp = (mark - idx) / idx * 10000
            score["basis"] = min(abs(basis_bp) / 20, 1.0)
    except Exception:
        pass

    sm = sm_markets.get(asset, {})
    sm_dir = sm.get("direction", "")
    sm_conv = float(sm.get("conviction", 0) or 0)
    if sm_dir in ("LONG", "SHORT") and sm_conv >= 3:
        score["SM_whale_bias"] = min(sm_conv / 10, 1.0)

    sb = scanner_bias.get(asset, {})
    long_count = sb.get("long", 0)
    short_count = sb.get("short", 0)
    total = long_count + short_count
    if total > 0:
        majority = max(long_count, short_count) / total
        score["scanner_confluence"] = min(majority * 1.5, 1.0)

    regime_map = {"RISK_ON": 1.0, "BASELINE": 0.5, "RISK_OFF": 0.0}
    score["regime_alignment"] = regime_map.get(risk_mode, 0.5)

    gss = sum(score[k] * WEIGHTS[k] for k in WEIGHTS)

    direction = "LONG"
    if short_count > long_count:
        direction = "SHORT"
    elif sm_dir in ("LONG", "SHORT") and sm_dir != direction and sm_conv >= 4:
        direction = sm_dir
    elif fr < -0.0001:
        direction = "SHORT"

    kg_triples.append(
        {
            "subject": asset,
            "predicate": "HAS_GSS",
            "object": f"{gss:.2f}",
            "attrs": {"sub_scores": score, "direction": direction},
            "source_name": "elite_trader",
            "source_tier": "internal",
            "confidence": 0.7,
        }
    )

    return {"asset": asset, "gss": gss, "direction": direction, "sub_scores": score}


def build_trade(candidate: dict, account_equity: float, kg_triples: list) -> dict:
    asset = candidate["asset"]
    direction = candidate["direction"]
    gss = candidate["gss"]

    # Fetch candles + context via market_get_asset_data (single call)
    ad = mcporter_call("market_get_asset_data", {
        "asset": asset,
        "candle_intervals": ["1h", "4h"],
        "include_order_book": True,
        "include_funding": False,
    }, timeout=20)
    ad_data = ad.get("data", ad)

    # Parse candles
    candles_map = ad_data.get("candles", {})
    c1h = candles_map.get("1h", [])
    if not c1h or len(c1h) < 10:
        return None

    closes = [float(c.get("c", c.get("close", 0))) for c in c1h[-14:] if float(c.get("c", c.get("close", 0))) > 0]
    tr = [abs(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    atr = sum(tr) / len(tr) * closes[-1] if tr else closes[-1] * 0.01

    # 4h slope
    c4h = candles_map.get("4h", [])
    if c4h and len(c4h) >= 5:
        closes_4h = [float(c.get("c", c.get("close", 0))) for c in c4h]
        slope = (closes_4h[-1] - closes_4h[0]) / closes_4h[0] * 100 if closes_4h[0] > 0 else 0
        if direction == "LONG" and slope < -1.5:
            return None
        if direction == "SHORT" and slope > 1.5:
            return None

    # Mark price from asset context
    ctx = ad_data.get("asset_context", ad_data)
    mark = float(ctx.get("markPx", ctx.get("markPrice", closes[-1] if closes else 1)) or 1)

    # Order book from asset data (may be empty)
    ob = ad_data.get("order_book", {})
    levels = ob.get("levels", {})
    bids = levels.get("bids", []) if isinstance(levels, dict) else []
    asks = levels.get("asks", []) if isinstance(levels, dict) else []
    best_bid = float(bids[0][0]) if bids else mark * 0.9999
    best_ask = float(asks[0][0]) if asks else mark * 1.0001

    # Tick/lot from instruments data (cached in candidate if available)
    tick = 0.01
    lot = 0.001

    if direction == "LONG":
        alo_price = min(mark - 0.3 * atr, best_bid)
        alo_price = round(alo_price / tick) * tick
        stop_dist = mark * 0.02 + atr
        stop_price = round((mark - stop_dist) / tick) * tick
    else:
        alo_price = max(mark + 0.3 * atr, best_ask)
        alo_price = round(alo_price / tick) * tick
        stop_dist = mark * 0.02 + atr
        stop_price = round((mark + stop_dist) / tick) * tick

    risk_usd = MAX_RISK_PCT * account_equity
    qty = math.floor((risk_usd / stop_dist) / lot) * lot
    if qty < lot:
        qty = lot

    notional = qty * mark
    leverage = notional / account_equity if account_equity > 0 else 10
    if leverage < MIN_LEVERAGE:
        qty = math.floor((MIN_LEVERAGE * account_equity / mark) / lot) * lot
        notional = qty * mark
        leverage = MIN_LEVERAGE
    elif leverage > MAX_LEVERAGE:
        qty = math.floor((MAX_LEVERAGE * account_equity / mark) / lot) * lot
        notional = qty * mark
        leverage = MAX_LEVERAGE

    fee_cost = notional * 0.001
    tp1_dist = stop_dist * 2
    tp1_price = (
        round(
            (alo_price + tp1_dist if direction == "LONG" else alo_price - tp1_dist)
            / tick
        )
        * tick
    )
    tp2_dist = stop_dist * 4
    tp2_price = (
        round(
            (alo_price + tp2_dist if direction == "LONG" else alo_price - tp2_dist)
            / tick
        )
        * tick
    )
    net_rr = (tp1_dist - fee_cost) / stop_dist if stop_dist > 0 else 0

    trade = {
        "asset": asset,
        "direction": direction,
        "orderType": "LIMIT",
        "timeInForce": "ALO",
        "price": round(alo_price, 6),
        "qty": qty,
        "leverage": int(leverage),
        "notionalUsd": round(notional, 2),
        "marginUsd": round(notional / leverage, 2),
        "stopPrice": round(stop_price, 6),
        "tp1Price": round(tp1_price, 6),
        "tp2Price": round(tp2_price, 6),
        "netRr": round(net_rr, 2),
        "entryScore": round(gss, 2),
        "atr": round(atr, 6),
        "feeCost": round(fee_cost, 2),
    }

    kg_triples.append(
        {
            "subject": asset,
            "predicate": "TRADE_BUILT",
            "object": direction,
            "attrs": trade,
            "source_name": "elite_trader",
            "source_tier": "internal",
            "confidence": 0.0,
        }
    )

    return trade


def execute_trade(
    trade: dict, strategy_id: str, strategy_key: str, kg_triples: list
) -> dict:
    asset = trade["asset"]
    direction = trade["direction"]

    if DRY_RUN:
        log(f"ELITE-TRADER DRY-RUN: would queue {direction} {asset} "
            f"gss={trade['entryScore']:.2f} px={trade['price']} "
            f"lev={trade['leverage']}x margin=${trade['marginUsd']}")
        return {"queued": False, "asset": asset, "dry_run": True}

    add_pending_entry(
        {
            "asset": asset,
            "direction": direction,
            "autoEntered": False,
            "margin": trade["marginUsd"],
            "leverage": trade["leverage"],
            "score": trade["entryScore"],
            "source": "elite-trader",
            "mode": "elite-alo",
            "entrySource": "elite-trader",
            "strategyKey": strategy_key,
            "reasons": [
                f"gss={trade['entryScore']:.2f}",
                f"rr={trade['netRr']:.2f}",
                f"atr={trade['atr']:.6f}",
            ],
            "price": trade["price"],
            "stopPrice": trade["stopPrice"],
            "tp1Price": trade["tp1Price"],
            "tp2Price": trade["tp2Price"],
            "orderType": "LIMIT",
            "timeInForce": "ALO",
            "qty": trade["qty"],
        }
    )

    send_telegram(
        f"⚡ ELITE SIGNAL: {direction} {asset}\n"
        f"Price: {trade['price']} | Size: {trade['qty']}\n"
        f"Leverage: {trade['leverage']}x | Margin: ${trade['marginUsd']}\n"
        f"SL: {trade['stopPrice']} | TP1: {trade['tp1Price']} | TP2: {trade['tp2Price']}\n"
        f"GSS: {trade['entryScore']} | RR: {trade['netRr']}"
    )

    write_journal_row(
        {
            "intent": "ELITE_SIGNAL",
            "symbol": asset,
            "setup": trade,
            "entry": trade["price"],
            "stop": trade["stopPrice"],
            "tp1": trade["tp1Price"],
            "tp2": trade["tp2Price"],
            "action_taken": "SIGNAL_QUEUED",
            "provenance_tags": ["elite-trader", "alo", f"gss-{trade['entryScore']}"],
        }
    )

    kg_triples.append(
        {
            "subject": asset,
            "predicate": "SIGNAL_QUEUED",
            "object": direction,
            "attrs": {"price": trade["price"], "leverage": trade["leverage"]},
            "source_name": "elite_trader",
            "source_tier": "internal",
            "confidence": 1.0,
        }
    )

    return {"queued": True, "asset": asset}


def check_stale_elite_orders():
    now = datetime.now(UTC)
    now_aest = datetime.now(AEST)
    cancelled = []

    for dsl_file in STATE_DIR.rglob("dsl-*-elite.json"):
        try:
            dsl = load_json(dsl_file)
            if not dsl.get("active", False):
                continue

            asset = dsl.get("asset", "")
            created = dsl.get("createdAt", "")
            entry_price = float(dsl.get("entryPrice", 0) or 0)
            stop_price = float(dsl.get("stopPrice", 0) or 1)
            hard_exit = dsl.get("hardExitAt", "")

            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_min = (now - created_dt).total_seconds() / 60
            except (ValueError, TypeError):
                age_min = 0

            px_data = mcporter_call(
                "market_get_asset_data", {"asset": asset}, timeout=10
            )
            px = px_data.get("data", px_data)
            current_px = float(
                px.get("markPrice", px.get("mark", entry_price)) or entry_price
            )
            drift = abs(current_px - entry_price)
            stop_dist = abs(stop_price - entry_price)

            reason = None
            if age_min > 90:
                reason = f"age {int(age_min)}min > 90"
            elif stop_dist > 1 and drift > stop_dist * 0.8:
                reason = f"drift {drift:.4f} > 80% stop_dist"
            elif hard_exit:
                try:
                    hard_dt = datetime.fromisoformat(
                        hard_exit.replace("+10:00", "+00:00").replace("AEST", "+10:00")
                    )
                    if now_aest >= hard_dt:
                        reason = "hard exit time reached"
                except (ValueError, TypeError):
                    pass

            if reason:
                mcporter_call(
                    "strategy_close_position",
                    {
                        "strategyId": dsl.get("strategyId"),
                        "asset": asset,
                    },
                    timeout=15,
                )
                dsl["active"] = False
                dsl["closedAt"] = now_iso()
                dsl["closeReason"] = f"elite_stale:{reason}"
                save_json(dsl_file, dsl)
                cancelled.append(asset)
                send_telegram(
                    f"?? ELITE STALE CANCEL: {dsl.get('direction', '')} {asset}\nReason: {reason}"
                )

        except Exception as e:
            log(f"Stale check error for {dsl_file}: {e}")

    return cancelled


def get_account_equity(strategy_key: str) -> float:
    strategies = get_enabled_strategies()
    for strat in strategies:
        if strat.get("_key") == strategy_key:
            portfolio = mcporter_call("account_get_portfolio", {}, timeout=15)
            if "error" not in portfolio:
                data = portfolio.get("data", portfolio)
                return float(data.get("equity", data.get("totalEquity", 100)) or 100)
    return 100.0


def main():
    if not acquire_lock("elite-trader"):
        return

    run_id = str(uuid.uuid4())[:8]
    kg_triples = []
    kg_frontier = [
        "funding",
        "OI_delta",
        "whale_flow",
        "orderbook",
        "basis",
        "catalyst",
    ]
    kg_conflicts = []
    internal_memory_used = False

    try:
        record_heartbeat("elite-trader")

        pre = check_preconditions()
        risk_mode = pre["risk_mode"]
        max_slots = pre["max_slots"]

        available_slots = count_open_slots()
        if available_slots <= 0:
            send_telegram("?? ELITE-TRADER: no free slots.")
            return

        arena_state = load_json(OUTPUTS_DIR / "arena-state.json", default={})
        pending_entries = load_pending_entries()

        sm_markets = fetch_sm_markets()
        scanner_bias = build_scanner_bias(pending_entries)

        # Use market_list_instruments for volume/OI/funding (not just prices)
        all_mkts_raw = mcporter_call("market_list_instruments", {})
        all_mkts = all_mkts_raw.get("data", all_mkts_raw)
        if isinstance(all_mkts, dict):
            all_mkts = all_mkts.get("instruments", all_mkts.get("assets", list(all_mkts.values())))
        if not isinstance(all_mkts, list):
            all_mkts = []

        universe = discover_universe(all_mkts, sm_markets, scanner_bias)

        scored = []
        for asset in universe:
            mkt = next(
                (m for m in all_mkts if isinstance(m, dict) and m.get("name", "").upper() == asset),
                None,
            )
            if not mkt:
                continue
            gss_result = compute_gss(
                asset, mkt, sm_markets, scanner_bias, risk_mode, kg_triples
            )
            if (
                gss_result["sub_scores"]["scanner_confluence"] < 0.15
                and gss_result["sub_scores"]["SM_whale_bias"] < 0.35
            ):
                continue
            scored.append(gss_result)

        scored.sort(key=lambda x: x["gss"], reverse=True)
        top3 = scored[:3]

        strategies = get_enabled_strategies()
        if not strategies:
            send_telegram("?? ELITE-TRADER: no enabled strategies")
            return
        strategy = strategies[0]
        strategy_id = strategy.get("strategyId", "")
        strategy_key = strategy.get("_key", "wolf-primary")
        account_equity = get_account_equity(strategy_key)

        trades = []
        for candidate in top3[:available_slots]:
            trade = build_trade(candidate, account_equity, kg_triples)
            if trade:
                result = execute_trade(trade, strategy_id, strategy_key, kg_triples)
                if result:
                    trades.append(trade)

        graph_writes = append_graph_triples(kg_triples)
        internal_memory_used = graph_writes > 0

        output = {
            "run_id": run_id,
            "timestamp_aest": datetime.now(AEST).isoformat(),
            "trending_universe": universe,
            "signal_scores": top3,
            "trades": trades,
            "memory_writes": {"graph_edges": graph_writes, "journal_rows": len(trades)},
            "gaps": [],
            "internal_memory_used": internal_memory_used,
        }

        save_json(OUTPUTS_DIR / "elite-trader-state.json", output)
        log(
            f"ELITE-TRADER: {len(trades)} trades | GSS top={top3[0]['gss'] if top3 else 1:.2f}"
        )

        git_sync("auto: elite-trader run")

    finally:
        release_lock("elite-trader")


if __name__ == "__main__":
    if "--stale" in sys.argv:
        check_stale_elite_orders()
    else:
        main()
