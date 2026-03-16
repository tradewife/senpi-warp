#!/usr/bin/env python3
"""
Senpi Dashboard — mobile-first monitoring + chat interface.

Runs on the VPS alongside cron jobs. Reads senpi-waifu JSON files
for real-time state. Chat commands dispatch locally or to Oz cloud agents.

Usage:
    pip install -r requirements.txt
    SENPI_STATE_DIR=/opt/senpi/senpi-waifu WARP_API_KEY=... uvicorn server:app --host 0.0.0.0 --port 8420
"""

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

try:
    from dashboard.telegram_bot import create_bot_application, start_polling, stop_polling
except ImportError:
    from telegram_bot import create_bot_application, start_polling, stop_polling

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_STATE_DIR", "/opt/senpi/senpi-waifu"))
WARP_API_KEY = os.environ.get("WARP_API_KEY", "")
OZ_ENV_ID = os.environ.get("OZ_ENVIRONMENT_ID", "")
DASH_TOKEN = os.environ.get("DASH_TOKEN", "")  # Simple bearer token for auth

CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"

_tg_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start Telegram bot on startup, stop on shutdown."""
    global _tg_app
    _tg_app = create_bot_application()
    if _tg_app:
        await start_polling(_tg_app)
        print("[dashboard] Telegram bot started (polling)")
    else:
        print("[dashboard] Telegram bot disabled (TELEGRAM_BOT_TOKEN not set)")
    yield
    if _tg_app:
        await stop_polling(_tg_app)
        print("[dashboard] Telegram bot stopped")


app = FastAPI(title="Senpi Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Connected WebSocket clients for live chat
ws_clients: list[WebSocket] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return iso_str


# ---------------------------------------------------------------------------
# Dashboard state aggregator
# ---------------------------------------------------------------------------

def get_dashboard_state() -> dict:
    """Aggregate all state into a single snapshot for the dashboard."""
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    strategies = load_json(CONFIG_DIR / "wolf-strategies.json")
    scanner_config = load_json(CONFIG_DIR / "scanner-config.json")
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json")
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])

    # Collect all open positions across strategies
    positions = []
    for key, strat in strategies.get("strategies", {}).items():
        strat_dir = POSITION_STATE_DIR / key
        if not strat_dir.exists():
            continue
        for f in strat_dir.glob("dsl-*.json"):
            state = load_json(f)
            if state and state.get("active", False):
                state["_strategy"] = strat.get("name", key)
                state["_strategyKey"] = key
                state["_age"] = relative_time(state.get("createdAt", ""))
                positions.append(state)

    # Recent trades (last 20)
    recent_trades = []
    for t in journal[-20:]:
        t["_age"] = relative_time(t.get("recordedAt", ""))
        recent_trades.append(t)
    recent_trades.reverse()

    # Daily PnL from journal
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in journal if t.get("recordedAt", "").startswith(today)]
    daily_pnl = sum(float(t.get("realizedPnl", 0)) for t in today_trades if t.get("action") == "CLOSE")
    daily_count = len([t for t in today_trades if t.get("action") == "CLOSE"])
    daily_wins = len([t for t in today_trades if t.get("action") == "CLOSE" and float(t.get("realizedPnl", 0)) > 0])

    return {
        "regime": {
            "mode": regime.get("riskMode", "UNKNOWN"),
            "updatedAt": relative_time(regime.get("updatedAt", "")),
            "reason": regime.get("reason", ""),
            "updatedBy": regime.get("updatedBy", ""),
        },
        "equity": {
            "current": arbiter.get("lastEquity", arbiter.get("dayStartEquity", 0)),
            "peak": arbiter.get("peakEquity", 0),
            "dayStart": arbiter.get("dayStartEquity", 0),
            "lastCheck": relative_time(arbiter.get("lastCheckAt", "")),
        },
        "positions": positions,
        "positionCount": len(positions),
        "pendingSignals": len(pending),
        "recentTrades": recent_trades,
        "daily": {
            "pnl": round(daily_pnl, 2),
            "trades": daily_count,
            "wins": daily_wins,
            "winRate": round(daily_wins / daily_count * 100, 1) if daily_count > 0 else 0,
        },
        "timestamp": now_iso(),
    }


# ---------------------------------------------------------------------------
# Auth middleware (simple token check)
# ---------------------------------------------------------------------------

def check_auth(request: Request) -> bool:
    if not DASH_TOKEN:
        return True  # No auth configured
    token = request.query_params.get("token", "")
    if not token:
        auth = request.headers.get("authorization", "")
        token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    return token == DASH_TOKEN


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_auth(request):
        return HTMLResponse("<h1>Unauthorized</h1><p>Add ?token=YOUR_TOKEN to the URL</p>", status_code=401)
    state = get_dashboard_state()
    return templates.TemplateResponse("index.html", {"request": request, "state": state, "token": DASH_TOKEN})


@app.get("/api/state")
async def api_state(request: Request):
    if not check_auth(request):
        return {"error": "unauthorized"}
    return get_dashboard_state()


@app.post("/api/chat")
async def api_chat(request: Request):
    """Handle a chat message. Slash commands run locally, free text goes to Oz."""
    if not check_auth(request):
        return {"error": "unauthorized"}

    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return {"reply": "Empty message."}

    reply = await handle_chat_message(message)
    return {"reply": reply}


@app.post("/api/regime/{mode}")
async def api_set_regime(mode: str, request: Request):
    """Quick regime toggle from dashboard buttons."""
    if not check_auth(request):
        return {"error": "unauthorized"}
    if mode not in ("RISK_ON", "BASELINE", "RISK_OFF"):
        return {"error": f"Invalid mode: {mode}"}

    regime = load_json(CONFIG_DIR / "risk-regime.json")
    regime["riskMode"] = mode
    regime["updatedAt"] = now_iso()
    regime["updatedBy"] = "dashboard"
    regime["reason"] = f"Manual override via dashboard"

    tmp = (CONFIG_DIR / "risk-regime.json").with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(regime, f, indent=2)
        f.write("\n")
    tmp.rename(CONFIG_DIR / "risk-regime.json")

    return {"ok": True, "mode": mode}


# ---------------------------------------------------------------------------
# Chat command handler
# ---------------------------------------------------------------------------

HELP_TEXT = """**Commands:**
`/status` — Current regime, positions, daily PnL
`/positions` — Open positions detail
`/trades` — Recent 10 trades
`/regime` — Current regime info
`/risk-off` — Set RISK_OFF immediately
`/risk-on` — Set RISK_ON
`/baseline` — Set BASELINE
`/flatten` — Emergency close all (via Oz)
`/pending` — Show queued signals
`/howl` — Last HOWL report summary
`/help` — This message

Anything else is sent to Oz as a free-text prompt."""


async def handle_chat_message(message: str) -> str:
    """Route chat messages to local handlers or Oz."""
    msg = message.strip()
    cmd = msg.lower().split()[0] if msg else ""

    if cmd == "/help":
        return HELP_TEXT

    if cmd == "/status":
        return _cmd_status()

    if cmd == "/positions":
        return _cmd_positions()

    if cmd == "/trades":
        return _cmd_trades()

    if cmd == "/regime":
        regime = load_json(CONFIG_DIR / "risk-regime.json")
        return (f"**Regime:** {regime.get('riskMode', '?')}\n"
                f"**Updated:** {relative_time(regime.get('updatedAt', ''))}\n"
                f"**By:** {regime.get('updatedBy', '?')}\n"
                f"**Reason:** {regime.get('reason', '?')}")

    if cmd == "/risk-off":
        await _set_regime("RISK_OFF", "Manual /risk-off from dashboard chat")
        return "✅ Regime set to **RISK_OFF**. No new entries. Existing positions managed by DSL."

    if cmd == "/risk-on":
        await _set_regime("RISK_ON", "Manual /risk-on from dashboard chat")
        return "✅ Regime set to **RISK_ON**. Max slots and leverage unlocked."

    if cmd == "/baseline":
        await _set_regime("BASELINE", "Manual /baseline from dashboard chat")
        return "✅ Regime set to **BASELINE**."

    if cmd == "/flatten":
        return await _cmd_flatten()

    if cmd == "/pending":
        pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
        if not pending:
            return "No pending signals."
        lines = [f"**{len(pending)} pending signals:**"]
        for p in pending[-10:]:
            lines.append(f"• {p.get('signalType', '?')} {p.get('direction', '?')} "
                        f"**{p.get('asset', '?')}** rank={p.get('rank', '?')} "
                        f"reasons={p.get('reasons', [])} "
                        f"{'✅ auto-entered' if p.get('autoEntered') else '⏳ queued'}")
        return "\n".join(lines)

    if cmd == "/howl":
        return _cmd_howl()

    # Free text → dispatch to Oz
    return await _dispatch_to_oz(msg)


def _cmd_status() -> str:
    state = get_dashboard_state()
    r = state["regime"]
    d = state["daily"]
    lines = [
        f"**Regime:** {r['mode']} ({r['updatedAt']})",
        f"**Positions:** {state['positionCount']} open | {state['pendingSignals']} pending",
        f"**Daily PnL:** ${d['pnl']:+.2f} | {d['trades']} trades | {d['winRate']}% WR",
    ]
    if state["positions"]:
        for p in state["positions"]:
            lines.append(f"  • {p.get('direction', '?')} **{p.get('asset', '?')}** "
                        f"T{p.get('currentTierIndex', -1) + 1} | {p['_age']}")
    return "\n".join(lines)


def _cmd_positions() -> str:
    state = get_dashboard_state()
    if not state["positions"]:
        return "No open positions."
    lines = [f"**{len(state['positions'])} open positions:**"]
    for p in state["positions"]:
        tier = p.get("currentTierIndex", -1) + 1
        phase = "Phase 2" if tier > 0 else "Phase 1"
        lines.append(
            f"\n**{p.get('direction', '?')} {p.get('asset', '?')}** ({p['_strategy']})\n"
            f"  Entry: ${p.get('entryPrice', 0):.4f} | Lev: {p.get('leverage', 0)}x\n"
            f"  {phase} Tier {tier} | HW: ${p.get('highWaterPrice', 0):.4f}\n"
            f"  Age: {p['_age']} | Breaches: {p.get('currentBreachCount', 0)}"
        )
    return "\n".join(lines)


def _cmd_trades() -> str:
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    recent = journal[-10:]
    if not recent:
        return "No trades recorded."
    lines = ["**Last 10 trades:**"]
    for t in reversed(recent):
        action = t.get("action", "?")
        pnl = f" ${float(t.get('realizedPnl', 0)):+.2f}" if action == "CLOSE" else ""
        age = relative_time(t.get("recordedAt", ""))
        lines.append(f"• {action} {t.get('direction', '?')} **{t.get('asset', '?')}**{pnl} ({age})")
    return "\n".join(lines)


def _cmd_howl() -> str:
    """Return the most recent HOWL report summary."""
    howl_files = sorted(MEMORY_DIR.glob("howl-*.md"), reverse=True)
    if not howl_files:
        return "No HOWL reports yet."
    content = howl_files[0].read_text()
    # Return first 1500 chars (mobile-friendly)
    name = howl_files[0].name
    if len(content) > 1500:
        return f"**{name}:**\n\n{content[:1500]}...\n\n_(truncated — full report in memory/{name})_"
    return f"**{name}:**\n\n{content}"


async def _set_regime(mode: str, reason: str):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    regime["riskMode"] = mode
    regime["updatedAt"] = now_iso()
    regime["updatedBy"] = "dashboard-chat"
    regime["reason"] = reason
    tmp = (CONFIG_DIR / "risk-regime.json").with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(regime, f, indent=2)
        f.write("\n")
    tmp.rename(CONFIG_DIR / "risk-regime.json")


async def _cmd_flatten() -> str:
    """Emergency flatten via Oz cloud agent."""
    if not WARP_API_KEY or not OZ_ENV_ID:
        return ("⚠️ Oz API not configured. Set `WARP_API_KEY` and `OZ_ENVIRONMENT_ID`.\n\n"
                "Manual alternative: set `/risk-off` then wait for DSL + Risk Arbiter to close positions.")

    result = await _dispatch_to_oz(
        "EMERGENCY: Close ALL open positions immediately via mcporter. "
        "Set config/risk-regime.json riskMode to RISK_OFF with reason 'Emergency flatten from dashboard'. "
        "Commit and push all changes."
    )
    # Also set RISK_OFF locally for immediate effect
    await _set_regime("RISK_OFF", "Emergency flatten from dashboard")
    return f"🚨 RISK_OFF set locally. Oz flatten dispatched.\n\n{result}"


async def _dispatch_to_oz(prompt: str) -> str:
    """Send a prompt to Oz cloud agent and return the run info."""
    if not WARP_API_KEY:
        return "⚠️ Oz API not configured. Set `WARP_API_KEY` environment variable."

    payload = {
        "prompt": prompt,
        "config": {},
    }
    if OZ_ENV_ID:
        payload["config"]["environment_id"] = OZ_ENV_ID

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://app.warp.dev/api/v1/agent/run",
                headers={
                    "Authorization": f"Bearer {WARP_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200 or resp.status_code == 201:
                data = resp.json()
                run_id = data.get("id", data.get("run_id", "unknown"))
                return f"✅ Oz agent dispatched.\n**Run ID:** `{run_id}`\n\nTrack: `oz run get {run_id}`"
            else:
                return f"❌ Oz API error {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"❌ Failed to reach Oz API: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("DASH_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port)
