---
name: elite-trader
description: >-
  Elite-tier Hyperliquid USDC-margined perpetuals trader. Executes a full
  research-to-execution loop: regime-gated universe scan → multi-source signal
  scoring (on-chain, HL-native, waifu programmatic scanners) → GraphSignalScore
  ranking → risk-sized ALO limit orders → DSL high-water handoff → SM-flip
  kill-switch → AEST time-based hard exit.

  Integrates with the senpi-waifu hybrid scanner stack:
  compute_regime.py / arena-monitor.py / sm-flip-cron.py / orca / mantis /
  fox / komodo / condor / polar / sentinel / rhino / dsl-runner.py /
  risk-arbiter.py.

  Trigger phrases: "run elite trader", "top 2 trades", "best HL perp trades",
  "elite scan", "what should I trade", "trade now".
license: Apache-2.0
compatibility: "Python 3, OpenClaw, Senpi MCP, Railway worker, senpi-waifu hybrid"
metadata:
  author: tradewife / Perplexity Pro
  version: "1.0.0"
  platform: senpi
  exchange: hyperliquid
  risk_profile: AGGRESSIVE
  min_budget_usd: 100
---

# ELITE-TRADER Skill — senpi-waifu Hybrid Edition

Produces the top 2 actionable Hyperliquid USDC-margined perpetual trades
for BTC, ETH, SOL + today's top-5 trending HL markets. Fully integrated
with the waifu programmatic scanner stack for maximum signal confluence.

---

## AGENT BEHAVIOUR

Silent execution between steps. No narration, no status updates.
Output only the final trade cards + Telegram alerts as specified below.

---

## PRECONDITIONS

```python
import os, json
from pathlib import Path

# 1. Require auth token
if not os.environ.get("SENPI_AUTH_TOKEN") and not os.environ.get("SENPI_API_KEY"):
    raise RuntimeError("No Senpi auth token — aborting ELITE-TRADER")

# 2. Read risk-regime.json (written by compute_regime.py / hermes-regime-classifier.sh)
STATE_DIR   = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
regime_cfg  = json.loads((STATE_DIR / "config" / "risk-regime.json").read_text())
risk_mode   = regime_cfg.get("riskMode", "BASELINE")

# 3. Gate on RISK_OFF
if risk_mode == "RISK_OFF":
    send_telegram("⛔ ELITE-TRADER: RISK_OFF regime — no new entries.")
    raise SystemExit("RISK_OFF")

# 4. Slot + leverage limits from active regime block
active  = regime_cfg["regimes"][risk_mode]
MAX_SLOTS    = min(active.get("maxSlots", 2), 2)   # elite cap = 2
MAX_LEVERAGE = min(active.get("maxLeverageCrypto", 10), 12)
MIN_LEVERAGE = 9
MAX_RISK_PCT = 0.20
```

> `compute_regime.py` owns `risk-regime.json`. ELITE-TRADER reads it
> **read-only**. Never write to it from this skill.

---

## PHASE 0 — WAIFU STATE BOOTSTRAP

```python
import sys
sys.path.insert(0, str(STATE_DIR / "scripts" / "lib"))
from senpi_common import (
    load_json, mcporter_call, send_telegram, log, now_iso,
    load_pending_entries, get_open_positions, get_enabled_strategies,
    acquire_lock, release_lock, record_heartbeat, save_json
)

OUTPUTS = STATE_DIR / "outputs"
MEMORY  = STATE_DIR / "memory"
STATE   = STATE_DIR / "state"

# Arena insights from arena-monitor.py → outputs/arena-state.json
arena_state  = load_json(OUTPUTS / "arena-state.json") or {}

# All queued scanner signals (orca/mantis/fox/komodo/condor/polar/sentinel/rhino)
pending_entries = load_pending_entries()

# Count open slots
open_positions = []
for strat in get_enabled_strategies():
    open_positions.extend(get_open_positions(strat["_key"]))
open_slot_count  = len(open_positions)
available_slots  = MAX_SLOTS - open_slot_count
if available_slots <= 0:
    send_telegram("ℹ️ ELITE-TRADER: no free slots.")
    raise SystemExit("No free slots")

# KG init
KG_TRIPLES   = []
KG_FRONTIER  = ["funding","OI_delta","whale_flow","orderbook","basis","catalyst"]
KG_CONFLICTS = []
INTERNAL_MEMORY_USED = False
```

---

## PHASE 1 — REGIME CONTEXT FROM WAIFU SOURCES

```python
# Arena insights (arena-monitor.py output)
best_strategy  = arena_state.get("insights", {}).get("bestStrategy", "unknown")
best_roi       = arena_state.get("insights", {}).get("bestRoi", 0)
winning_traits = arena_state.get("insights", {}).get("winningTraits", [])
recommendations= arena_state.get("insights", {}).get("recommendations", [])

# SM leaderboard (same call used by sm-flip-cron.py)
sm_raw  = mcporter_call("leaderboard_get_markets", {})
sm_markets = {}
for m in (sm_raw.get("data", {}).get("markets", []) or []):
    asset = m.get("token", m.get("asset", ""))
    if asset:
        sm_markets[asset] = {
            "direction":     str(m.get("direction","")).upper(),
            "conviction":    float(m.get("conviction",0) or 0),
            "traders":       int(m.get("traderCount", m.get("traders",0)) or 0),
            "concentration": float(m.get("contribution", m.get("pctOfTotal",0)) or 0),
        }

# Scanner confluence — aggregate waifu pending entries by asset
# This is the PRIMARY signal source (weight 0.25 in GraphSignalScore)
scanner_bias = {}   # asset → {long, short, max_priority, scanners[]}
for e in pending_entries:
    asset    = str(e.get("asset", e.get("token",""))).upper()
    direction= str(e.get("direction", e.get("side",""))).upper()
    priority = int((e.get("brainContext") or {}).get("priority", 0) or 0)
    scanner  = str(e.get("scanner", e.get("source", e.get("entryMode","")))).lower()
    if not asset or direction not in ("LONG","SHORT"):
        continue
    b = scanner_bias.setdefault(asset, {"long":0,"short":0,"max_priority":0,"scanners":[]})
    b["long"  if direction=="LONG" else "short"] += 1
    b["max_priority"] = max(b["max_priority"], priority)
    if scanner and scanner not in b["scanners"]:
        b["scanners"].append(scanner)

for asset, b in scanner_bias.items():
    net      = b["long"] - b["short"]
    conf_val = min(abs(net) / max(b["long"]+b["short"],1), 1.0)
    KG_TRIPLES.append((
        f"{asset}-PERP","has_signal","SCANNER_BIAS",
        {"net":net,"long":b["long"],"short":b["short"],
         "scanners":b["scanners"],"max_priority":b["max_priority"]},
        "waifu-scanners","HL-native","[no-link]",now_iso(),round(conf_val,2)
    ))
```

---

## PHASE 2 — UNIVERSE DISCOVERY + TRENDING TOP-5

```python
all_mkts_raw = mcporter_call("market_get_prices", {})
all_mkts     = all_mkts_raw.get("data", all_mkts_raw)
if isinstance(all_mkts, dict):
    all_mkts = list(all_mkts.values())

CORE_SYMBOLS    = ["BTC","ETH","SOL"]
MIN_VOL_24H_USD = 50_000_000

candidates = []
for m in all_mkts:
    if not isinstance(m, dict):
        continue
    asset = str(m.get("token", m.get("asset", m.get("symbol","")))).upper()
    vol24 = float(m.get("volume24h", m.get("vol24h",0)) or 0)
    oi    = float(m.get("openInterest", m.get("oi",0)) or 0)
    fr    = float(m.get("fundingRate",  m.get("funding",0)) or 0)
    if vol24 < MIN_VOL_24H_USD or asset in CORE_SYMBOLS:
        continue
    sc_bias    = scanner_bias.get(asset, {})
    sc_score   = sc_bias.get("max_priority",0)*0.3 + \
                 (sc_bias.get("long",0)+sc_bias.get("short",0))*5
    trend_score= (vol24/1e8) + (oi/1e8) + (abs(fr)*1000) + sc_score
    candidates.append({"asset":asset,"vol24":vol24,"oi":oi,
                        "funding":fr,"trend_score":trend_score})

candidates.sort(key=lambda x: x["trend_score"], reverse=True)
TRENDING_TOP5 = [c["asset"] for c in candidates[:5]]
UNIVERSE      = CORE_SYMBOLS + TRENDING_TOP5
```

---

## PHASE 3 — GRAPHSIGNALSCORE

**Weight table (sourced from your stack):**

| Signal | Weight | Primary waifu source |
|---|---|---|
| `scanner_confluence` | **0.25** | `load_pending_entries()` — all 8 scanners |
| `SM_whale_bias` | 0.20 | `sm-flip-cron.py` leaderboard |
| `funding_stretch` | 0.20 | Senpi `market_get_asset_data` |
| `OI_delta` | 0.15 | Senpi `market_get_asset_data` |
| `regime_alignment` | 0.10 | `config/risk-regime.json` |
| `basis` | 0.10 | Senpi mark vs index |

```python
WEIGHTS = {
    "funding_stretch":   0.20,
    "OI_delta":          0.15,
    "basis":             0.10,
    "SM_whale_bias":     0.20,
    "scanner_confluence":0.25,
    "regime_alignment":  0.10,
}

def compute_gss(asset, mkt):
    score = {}

    # Funding stretch
    fr   = float(mkt.get("fundingRate", mkt.get("funding",0)) or 0)
    fr8h = fr * 3
    score["funding_stretch"] = min(abs(fr8h)/0.0001, 1.0)

    # OI delta (neutral default; scanner data is the stronger signal)
    score["OI_delta"] = 0.5

    # Basis
    od   = mcporter_call("market_get_asset_data",{"asset":asset},timeout=15)
    mark = float((od.get("data",od) or {}).get("markPrice",0) or 0)
    idx  = float((od.get("data",od) or {}).get("indexPrice",0) or 0)
    basis_bp = (mark-idx)/idx*10000 if idx>0 else 0
    score["basis"] = min(abs(basis_bp)/20, 1.0)

    # SM whale bias (same leaderboard as sm-flip-cron.py)
    sm   = sm_markets.get(asset,{})
    conv = float(sm.get("conviction",0))
    trad = int(sm.get("traders",0))
    score["SM_whale_bias"] = min((conv/10)*0.5+(trad/500)*0.5, 1.0)

    # Scanner confluence — PRIMARY WAIFU SOURCE
    sc   = scanner_bias.get(asset,{})
    lc, sc_ = sc.get("long",0), sc.get("short",0)
    total = lc+sc_
    if total > 0:
        net_agree = abs(lc-sc_)/total
        sc_bonus  = min(len(sc.get("scanners",[]))/4,1.0)*0.3
        score["scanner_confluence"] = min(net_agree+sc_bonus,1.0)
    else:
        score["scanner_confluence"] = 0.0  # no waifu signal = do not trade

    # Regime alignment from compute_regime.py output
    score["regime_alignment"] = {"RISK_ON":1.0,"BASELINE":0.5}.get(risk_mode,0.0)

    gss = sum(score[k]*WEIGHTS[k] for k in WEIGHTS)

    # Direction: scanner_bias > SM leaderboard > funding carry
    if sc.get("long",0) > sc.get("short",0):        direction = "LONG"
    elif sc.get("short",0) > sc.get("long",0):      direction = "SHORT"
    elif sm.get("direction") in ("LONG","SHORT"):   direction = sm["direction"]
    elif fr8h < -0.005:                              direction = "SHORT"
    else:                                            direction = "LONG"

    return {"asset":asset,"gss":round(gss,4),"direction":direction,
            "sub_scores":score,"basis_bp":basis_bp,"sm":sm,
            "scanner_bias":sc,"mark":mark}

scored = []
for asset in UNIVERSE:
    mkt = next((m for m in all_mkts
                if str(m.get("token",m.get("asset",m.get("symbol","")))).upper()==asset), {})
    r = compute_gss(asset, mkt)
    # Hard gate: must have scanner OR strong SM signal
    if r["sub_scores"]["scanner_confluence"] < 0.15 and \
       r["sub_scores"]["SM_whale_bias"] < 0.35:
        continue
    scored.append(r)
    KG_TRIPLES.append((f"{asset}-PERP","has_signal","GSS",
        {"gss":r["gss"],"direction":r["direction"]},
        "elite-trader","HL-native","[no-link]",now_iso(),r["gss"]))

scored.sort(key=lambda x: x["gss"], reverse=True)
TOP3 = scored[:3]
```

---

## PHASE 4 — STOP MATH & ENTRY CONSTRUCTION

*Reuses `compute_regime.py` and `process_market_data.py` ATR logic verbatim.*

```python
def build_trade(candidate, account_equity):
    asset     = candidate["asset"]
    direction = candidate["direction"]

    # 1h candles → ATR (same formula as process_market_data.py calculate_atr)
    cd = mcporter_call("market_get_candles",
                       {"asset":asset,"interval":"1h","limit":60},timeout=20)
    candles = cd.get("data",cd)
    if isinstance(candles,dict):
        candles = candles.get("candles",candles.get("data",[]))

    if len(candles) >= 15:
        tr_list = []
        for i in range(1,len(candles)):
            h  = float(candles[i].get("h", candles[i].get("high",0)))
            l  = float(candles[i].get("l", candles[i].get("low",0)))
            pc = float(candles[i-1].get("c",candles[i-1].get("close",0)))
            tr_list.append(max(h-l, abs(h-pc), abs(l-pc)))
        atr_1h = sum(tr_list[-14:])/14
    else:
        atr_1h = candidate["mark"] * 0.004

    # 4h slope (same as compute_regime.py ma_slope)
    cd4 = mcporter_call("market_get_candles",
                         {"asset":asset,"interval":"4h","limit":10},timeout=20)
    c4  = cd4.get("data",cd4)
    if isinstance(c4,dict): c4 = c4.get("candles",c4.get("data",[]))
    slope_pct = ((float(c4[-1].get("c",0))-float(c4.get("c",1)))/
                  float(c4.get("c",1))*100) if len(c4)>=6 else 0.0

    # Per-asset regime check (mirrors compute_regime.py classify_regime)
    mark    = float(candles[-1].get("c",candles[-1].get("close",1))) if candles else 1.0
    atr_pct = (atr_1h/mark)*100
    if atr_pct>6.0 or (slope_pct<0.3 and atr_pct>5.0):
        return None   # RISK_OFF per-asset

    # Stop distance
    stop_dist = max(0.8*atr_1h, atr_1h*0.5)

    # Orderbook snapshot for passive anchor
    ob  = mcporter_call("market_get_orderbook",{"asset":asset,"depth":10},timeout=15)
    obd = ob.get("data",ob) or {}
    best_bid = float(obd.get("bids",[[mark]]))
    best_ask = float(obd.get("asks",[[mark]]))

    # Tick + lot sizes
    sp    = mcporter_call("market_get_instrument_specs",{"asset":asset},timeout=15)
    spd   = sp.get("data",sp) or {}
    tick  = float(spd.get("tickSize",0.01) or 0.01)
    lot   = float(spd.get("lotSize",0.001) or 0.001)

    # ALO passive limit price
    if direction == "LONG":
        anchor   = min(mark - 0.3*atr_1h, best_bid)
        entry_px = round(anchor/tick)*tick
        if entry_px > best_bid: entry_px = best_bid - tick
        stop_px  = entry_px - stop_dist
        tp1_px   = entry_px + 2.0*stop_dist
        tp2_px   = entry_px + 3.5*stop_dist
    else:
        anchor   = max(mark + 0.3*atr_1h, best_ask)
        entry_px = round(anchor/tick)*tick
        if entry_px < best_ask: entry_px = best_ask + tick
        stop_px  = entry_px + stop_dist
        tp1_px   = entry_px - 2.0*stop_dist
        tp2_px   = entry_px - 3.5*stop_dist

    # Position sizing — AGGRESSIVE 20% risk rule
    risk_usd = account_equity * MAX_RISK_PCT
    qty      = max(lot, (risk_usd/stop_dist//lot)*lot)
    notional = qty * entry_px
    leverage = notional / account_equity

    # Clamp to [MIN_LEVERAGE, MAX_LEVERAGE]
    if leverage < MIN_LEVERAGE:
        qty = max(lot,((MIN_LEVERAGE*account_equity/entry_px)//lot)*lot)
    if leverage > MAX_LEVERAGE:
        qty = max(lot,((MAX_LEVERAGE*account_equity/entry_px)//lot)*lot)
    notional = qty * entry_px
    leverage = notional / account_equity

    fee_cost = notional * 0.001
    net_rr   = round(((2.0*stop_dist*qty)-fee_cost)/(stop_dist*qty),2)

    return {"asset":asset,"direction":direction,
            "entry_px":round(entry_px,6),"stop_px":round(stop_px,6),
            "tp1_px":round(tp1_px,6),"tp2_px":round(tp2_px,6),
            "qty":qty,"notional":round(notional,2),
            "leverage":round(leverage,2),"risk_usd":round(stop_dist*qty,2),
            "risk_pct":round(stop_dist*qty/account_equity*100,1),
            "atr_1h":round(atr_1h,4),"atr_pct":round(atr_pct,3),
            "slope_pct":round(slope_pct,3),
            "best_bid":best_bid,"best_ask":best_ask,
            "snap_ts":now_iso(),"net_rr":net_rr,
            "stop_dist":round(stop_dist,6),"tick":tick,"lot":lot,
            "gss":candidate["gss"],"scanner_bias":candidate["scanner_bias"],
            "sm":candidate["sm"],"sub_scores":candidate["sub_scores"]}
```

---

## PHASE 5 — ORDER EXECUTION

```python
def execute_trade(trade, strategy_id):
    asset, direction = trade["asset"], trade["direction"]

    # Open ALO limit
    result = mcporter_call("strategy_open_position",{
        "strategyId": strategy_id, "asset": asset,
        "direction": direction, "size": trade["qty"],
        "leverage":  trade["leverage"], "orderType": "LIMIT",
        "price":     trade["entry_px"], "timeInForce": "ALO",
        "reduceOnly": False,
    }, timeout=30)
    if "error" in (result or {}):
        log(f"OPEN failed {asset}: {result['error']}")
        return False

    # Attach SL/TP (reduce-only)
    mcporter_call("strategy_set_sl_tp",{
        "strategyId":  strategy_id, "asset": asset,
        "stopLoss":    trade["stop_px"],
        "takeProfit1": trade["tp1_px"],
        "takeProfit2": trade["tp2_px"],
        "reduceOnly":  True,
    }, timeout=20)

    # Write DSL state for dsl-runner.py handoff
    # Schema matches sm-flip-cron.py exactly (playbook.smSnapshot, collapse, rotation)
    import zoneinfo
    from datetime import datetime, timedelta
    tz   = zoneinfo.ZoneInfo("Australia/Sydney")
    now  = datetime.now(tz)
    hard = (now+timedelta(days=1)).replace(hour=22,minute=0,second=0,microsecond=0)

    save_json(STATE_DIR/"state"/f"dsl-{asset.lower()}-elite.json", {
        "active":           True,
        "asset":            asset,
        "direction":        direction,
        "entryPrice":       trade["entry_px"],
        "stopPrice":        trade["stop_px"],
        "tp1":              trade["tp1_px"],
        "tp2":              trade["tp2_px"],
        "size":             trade["qty"],
        "leverage":         trade["leverage"],
        "strategyId":       strategy_id,
        "strategyKey":      f"elite-{asset.lower()}",
        "scanner":          "elite-trader",
        "entryMode":        "elite-trader",
        "entryScore":       trade["gss"],
        "createdAt":        now_iso(),
        "highWaterRoe":     0,
        "currentTierIndex": 0,
        "hardExitAt":       hard.isoformat(),
        "playbook": {
            "priority":    80,
            "scanner":     "elite-trader",
            "smSnapshot":  trade["sm"],
            "collapse": {
                "minTraderRatio":        0.20,
                "minTraderCountFloor":   24,
                "minConvictionRatio":    0.50,
                "minConcentrationRatio": 0.50,
            },
            "rotation": {
                "eligible":        True,
                "deadWeightMin":   20,
                "minHighWaterRoe": 2.0,
                "priorityGap":     8,
            },
        },
    })
    return True
```

---

## PHASE 6 — STALE ORDER MANAGEMENT

Add this function and call it from `scripts/vps/autonomous-brain.py` at the top of its main loop:

```python
def check_stale_elite_orders():
    import zoneinfo
    from datetime import datetime
    tz  = zoneinfo.ZoneInfo("Australia/Sydney")
    now = datetime.now(tz)

    for dsl_file in (STATE_DIR/"state").glob("dsl-*-elite.json"):
        dsl = load_json(dsl_file)
        if not dsl or not dsl.get("active"):
            continue
        asset      = dsl["asset"]
        entry_px   = float(dsl.get("entryPrice",0))
        stop_dist  = abs(entry_px - float(dsl.get("stopPrice",entry_px)))
        hard_str   = dsl.get("hardExitAt","")

        # Age check
        try:
            age_min = (now.astimezone()-datetime.fromisoformat(
                dsl.get("createdAt","")).astimezone()).total_seconds()/60
        except Exception:
            age_min = 0

        # Price drift check
        px_data    = mcporter_call("market_get_asset_data",{"asset":asset},timeout=10)
        current_px = float((px_data.get("data",px_data) or {}).get("markPrice",entry_px) or entry_px)
        drift      = abs(current_px - entry_px)

        # Hard exit check (22:00 AEST)
        past_hard = False
        if hard_str:
            try:
                past_hard = now.astimezone() >= datetime.fromisoformat(hard_str).astimezone()
            except Exception:
                pass

        reason = None
        if 
