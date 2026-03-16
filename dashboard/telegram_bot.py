"""
Senpi Telegram Bot — full control interface for the ORCA hybrid trading agent.

Runs as an async background task inside the dashboard FastAPI app.
On startup, registers the command menu with BotFather automatically.

Architecture:
  VPS (Railway) runs mechanical cron jobs — ORCA scanner, KOMODO momentum,
  DSL trailing stops, Risk Arbiter. No LLM, sub-2s execution.

  Oz Cloud (Warp) runs strategic LLM agents — regime classification,
  trade evaluation, portfolio review, nightly HOWL self-improvement.

  This bot gives you full visibility and manual override from your phone.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_STATE_DIR", "/app"))
CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHILD_ENV = {**os.environ, "SENPI_STATE_DIR": str(STATE_DIR)}

# Command descriptions — registered with BotFather and shown in /help.
# Each tuple: (command, short_desc_for_menu, detailed_desc_for_help)
COMMANDS = [
    # Status & Monitoring
    ("status",   "Dashboard snapshot",
     "Regime, open positions, daily PnL, equity, and arbiter status — everything at a glance."),
    ("positions", "Open position details",
     "Each position's direction, asset, entry price, leverage, DSL tier, high-water mark, breach count, and age."),
    ("trades",   "Recent trade history",
     "Last 10 trades with PnL, close reason, and entry source (ORCA STALKER/STRIKER or KOMODO)."),
    ("equity",   "Equity & drawdown",
     "Current equity, day-start equity, peak, drawdown from peak, and daily change percentage."),
    ("regime",   "Risk regime details",
     "Active regime (RISK\\_ON / BASELINE / RISK\\_OFF), who set it, why, and the parameter block (slots, leverage, alloc)."),
    ("pending",  "Queued scanner signals",
     "Signals from ORCA and KOMODO scanners awaiting Oz evaluation or already auto-entered."),

    # Control
    ("risk_on",  "⚡ Set RISK_ON",
     "Unlock max 3 slots, 7-10x leverage, 35% allocation per slot. Use when macro trend is clear."),
    ("risk_off", "🛑 Set RISK_OFF",
     "Block all new entries immediately. Existing positions are still managed by DSL trailing stops."),
    ("baseline", "Set BASELINE",
     "Default regime: 2 slots, 7-10x leverage, 30% allocation. Balanced risk."),
    ("flatten",  "🚨 Emergency close ALL",
     "Sets RISK\\_OFF locally and dispatches an Oz cloud agent to close every open position via mcporter."),

    # Manual Triggers
    ("scan",     "Run ORCA scanner now",
     "Manually trigger the ORCA dual-mode scanner (STALKER accumulation + STRIKER explosion detection). Normally runs every 60s."),
    ("komodo",   "Run KOMODO scanner now",
     "Manually trigger the KOMODO momentum event consensus scanner. Normally runs every 5 minutes."),
    ("arbiter",  "Run Risk Arbiter now",
     "Check daily loss limits, catastrophic drawdown, and consecutive stop-outs. Normally runs every 30s."),
    ("health",   "Run health check + git sync",
     "Pull latest config, reconcile closed positions into trade journal, push state. Normally runs every 10 minutes."),
    ("arena",    "Run arena monitor now",
     "Poll the Senpi Predators performance tracker and update arena-state.json. Normally runs every 15 minutes."),

    # Reports
    ("howl",     "Last HOWL nightly report",
     "The most recent Hunt-Optimize-Win-Learn analysis: win rates, scanner comparison, fee drag, arena benchmarking."),
    ("journal",  "Trade journal statistics",
     "Lifetime stats: total PnL, win rate, profit factor, avg win/loss, and breakdown by entry source."),
    ("arena_insights", "Arena leaderboard analysis",
     "Top 5 predator strategies, winning/losing traits, and data-driven recommendations from the arena."),

    # Meta
    ("help",     "Show all commands",
     "This message."),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def relative_time(iso_str: str) -> str:
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return iso_str


def is_authorized(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            await update.message.reply_text(
                "⛔ Unauthorized. This bot only responds to its configured owner."
            )
            return
        return await func(update, context)
    return wrapper


async def run_script_async(cmd: list[str], timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=CHILD_ENV,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stderr.decode().strip() or stdout.decode().strip()
        return output[-1500:] if output else "(no output)"
    except asyncio.TimeoutError:
        return "⏱ Script timed out"
    except Exception as e:
        return f"❌ Error: {e}"


def _count_open_positions() -> tuple[int, list[dict]]:
    """Return (count, position_list) across all strategies."""
    strategies = load_json(CONFIG_DIR / "wolf-strategies.json")
    positions = []
    for key, strat in strategies.get("strategies", {}).items():
        strat_dir = POSITION_STATE_DIR / key
        if not strat_dir.exists():
            continue
        for f in strat_dir.glob("dsl-*.json"):
            state = load_json(f)
            if state and state.get("active"):
                state["_strategy"] = strat.get("name", key)
                state["_key"] = key
                positions.append(state)
    return len(positions), positions


def _daily_stats(journal: list[dict]) -> dict:
    """Compute today's trading stats from the journal."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    closes = [t for t in journal
              if t.get("recordedAt", "").startswith(today) and t.get("action") == "CLOSE"]
    pnl = sum(float(t.get("realizedPnl", 0)) for t in closes)
    wins = sum(1 for t in closes if float(t.get("realizedPnl", 0)) > 0)
    count = len(closes)
    return {"pnl": pnl, "wins": wins, "count": count,
            "wr": round(wins / count * 100, 1) if count > 0 else 0}


# ---------------------------------------------------------------------------
# /start — Onboarding
# ---------------------------------------------------------------------------

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🐺 *Welcome to Senpi*
_Autonomous hybrid trading agent_

Senpi runs a two-layer architecture for crypto perpetual futures:

⚙️ *Mechanical Layer* (this server)
Runs every 30-60 seconds. No LLM, no cloud credits.
• 🐋 *ORCA Scanner* — dual-mode entry detection
  ↳ STALKER: spots SM accumulation before explosions
  ↳ STRIKER: catches violent first-jump breakouts
• 🦎 *KOMODO Scanner* — momentum event consensus
  ↳ 2+ elite traders crossing PnL thresholds on same asset
• 🔒 *DSL High Water* — 7-tier infinite trailing stop (up to 90% of peak)
• 🚨 *Risk Arbiter* — hard safety limits, no LLM dependency

🧠 *Strategic Layer* (Oz Cloud)
LLM-powered agents on scheduled intervals.
• Trade evaluation, regime classification, portfolio review
• Nightly HOWL self-improvement + arena benchmarking

🛡 *Hardcoded Safety Gates*
These are in the code, not config — agents cannot override them:
• XYZ equities banned (net negative across all 22 agents)
• 7-10x leverage only (Dire Wolf 25x blowup lesson)
• Max 3 concurrent positions
• 10% daily loss limit
• 2-hour per-asset cooldown after exits
• Stagnation TP mandatory (10% ROE / 45 min)

━━━━━━━━━━━━━━━━━━━━
Type /help to see all commands, or just send a message to talk to Oz.

Tip: Start with /status for a quick dashboard snapshot."""
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /help — Full command reference
# ---------------------------------------------------------------------------

@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sections = {
        "📊 Status & Monitoring": [],
        "🎛 Control": [],
        "▶️ Manual Triggers": [],
        "📜 Reports": [],
        "ℹ️ Meta": [],
    }

    section_map = {
        "status": "📊 Status & Monitoring", "positions": "📊 Status & Monitoring",
        "trades": "📊 Status & Monitoring", "equity": "📊 Status & Monitoring",
        "regime": "📊 Status & Monitoring", "pending": "📊 Status & Monitoring",
        "risk_on": "🎛 Control", "risk_off": "🎛 Control",
        "baseline": "🎛 Control", "flatten": "🎛 Control",
        "scan": "▶️ Manual Triggers", "komodo": "▶️ Manual Triggers",
        "arbiter": "▶️ Manual Triggers", "health": "▶️ Manual Triggers",
        "arena": "▶️ Manual Triggers",
        "howl": "📜 Reports", "journal": "📜 Reports",
        "arena_insights": "📜 Reports",
        "help": "ℹ️ Meta",
    }

    for cmd_name, short_desc, detail in COMMANDS:
        section = section_map.get(cmd_name, "ℹ️ Meta")
        sections[section].append(f"/{cmd_name} — {detail}")

    lines = ["🐺 *Senpi Command Reference*\n"]
    for section_name, cmds in sections.items():
        if not cmds:
            continue
        lines.append(f"\n*{section_name}*")
        for c in cmds:
            lines.append(c)

    lines.append("\n_Any non-command text is sent to Oz as a free-text prompt._")
    lines.append("_Oz can execute mcporter calls, read state, and push config changes._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Status & Monitoring
# ---------------------------------------------------------------------------

@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json")
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    pos_count, _ = _count_open_positions()
    daily = _daily_stats(journal)

    mode = regime.get("riskMode", "UNKNOWN")
    mode_emoji = {"RISK_ON": "🟢", "BASELINE": "🟡", "RISK_OFF": "🔴"}.get(mode, "⚪")
    equity = arbiter.get("lastEquity", 0)
    peak = arbiter.get("peakEquity", 0)
    day_start = arbiter.get("dayStartEquity", 0)
    dd = (peak - equity) / peak * 100 if peak > 0 else 0

    pnl_emoji = "📈" if daily["pnl"] >= 0 else "📉"

    text = (
        f"🐺 *Senpi Status*\n\n"
        f"{mode_emoji} *Regime:* {mode}\n"
        f"↳ {regime.get('reason', 'No reason set')}\n"
        f"↳ Set by {regime.get('updatedBy', '?')} · {relative_time(regime.get('updatedAt', ''))}\n\n"
        f"📊 *Positions:* {pos_count}/3 open · {len(pending)} signals pending\n"
        f"{pnl_emoji} *Daily PnL:* {'+'if daily['pnl']>=0 else ''}${daily['pnl']:.2f} · "
        f"{daily['count']} trades · {daily['wr']}% WR\n\n"
        f"🏦 *Equity:* ${equity:,.2f}\n"
        f"↳ Day start: ${day_start:,.2f} · Peak: ${peak:,.2f}\n"
        f"↳ Drawdown from peak: {dd:.1f}%\n\n"
        f"🚨 *Arbiter:* last check {relative_time(arbiter.get('lastCheckAt', ''))}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pos_count, positions = _count_open_positions()

    if not positions:
        regime = load_json(CONFIG_DIR / "risk-regime.json")
        mode = regime.get("riskMode", "UNKNOWN")
        await update.message.reply_text(
            f"No open positions.\n\n"
            f"Regime is *{mode}* — "
            f"{'scanners are hunting for entries.' if mode != 'RISK_OFF' else 'entries are blocked. Use /baseline or /risk\\_on to re-enable.'}",
            parse_mode="Markdown",
        )
        return

    lines = [f"📊 *{len(positions)}/3 open positions:*\n"]
    for p in positions:
        tier = (p.get("currentTierIndex", -1) or -1) + 1
        phase = "Phase 2 (trailing)" if tier > 0 else "Phase 1 (proving)"
        direction = p.get("direction", "?")
        d_emoji = "🟢" if direction == "LONG" else "🔴"
        age = relative_time(p.get("createdAt", ""))
        mode = p.get("entryMode", p.get("lockMode", "?"))
        score = p.get("entryScore", "?")
        breaches = p.get("currentBreachCount", 0)

        lines.append(
            f"{d_emoji} *{direction} {p.get('asset', '?')}*  ·  {p['_strategy']}\n"
            f"   Entry ${p.get('entryPrice', 0):.4f} · {p.get('leverage', 0)}x · Score {score}\n"
            f"   {phase} · Tier {tier}/7 · HW ${p.get('highWaterPrice', 0):.4f}\n"
            f"   Breaches: {breaches} · {age} · via {mode}"
        )
    lines.append(f"\n_DSL High Water trails up to 90% of peak ROE across 7 tiers._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    recent = journal[-10:]
    if not recent:
        await update.message.reply_text(
            "No trades recorded yet.\n\n"
            "_Trades are recorded when ORCA or KOMODO opens a position, "
            "and when DSL, Risk Arbiter, or Oz closes one._"
        )
        return

    daily = _daily_stats(journal)
    lines = [
        f"📒 *Last 10 Trades*\n"
        f"Today: {'+'if daily['pnl']>=0 else ''}${daily['pnl']:.2f} · {daily['count']} closed · {daily['wr']}% WR\n"
    ]
    for t in reversed(recent):
        action = t.get("action", "?")
        asset = t.get("asset", "?")
        direction = t.get("direction", "?")
        age = relative_time(t.get("recordedAt", ""))
        if action == "CLOSE":
            pnl = float(t.get("realizedPnl", 0))
            emoji = "✅" if pnl >= 0 else "❌"
            reason = t.get("closeReason", "unknown")
            reason_label = {
                "dsl_breach": "DSL trailing stop",
                "phase1_autocut": "Phase 1 timeout",
                "stagnation": "Stagnation TP",
                "risk_arbiter_flatten": "Risk Arbiter",
                "manual": "Manual close",
            }.get(reason, reason)
            lines.append(f"{emoji} {direction} *{asset}* {'+'if pnl>=0 else ''}${pnl:.2f} · {reason_label} · {age}")
        else:
            source = t.get("entrySource", t.get("entryMode", "?"))
            score = t.get("entryScore", t.get("signal", {}).get("score", ""))
            score_str = f" · score {score}" if score else ""
            lines.append(f"📥 {direction} *{asset}* via {source}{score_str} · {age}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_equity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json")
    equity = arbiter.get("lastEquity", 0)
    peak = arbiter.get("peakEquity", 0)
    day_start = arbiter.get("dayStartEquity", 0)
    dd_peak = (peak - equity) / peak * 100 if peak > 0 else 0
    daily_chg = equity - day_start if day_start > 0 else 0
    daily_pct = daily_chg / day_start * 100 if day_start > 0 else 0

    # Guardrails context
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    guardrails = regime.get("globalGuardrails", {})
    daily_limit = guardrails.get("dailyLossLimitPct", 10)
    cat_limit = guardrails.get("catastrophicDrawdownPct", 20)

    text = (
        f"🏦 *Equity Snapshot*\n\n"
        f"*Current:* ${equity:,.2f}\n"
        f"*Day start:* ${day_start:,.2f} ({'+'if daily_chg>=0 else ''}{daily_pct:.1f}%)\n"
        f"*Peak:* ${peak:,.2f}\n"
        f"*Drawdown:* {dd_peak:.1f}%\n\n"
        f"🛡 *Safety Limits*\n"
        f"Daily loss limit: {daily_limit}% {'⚠️ close' if dd_peak > daily_limit * 0.7 else '✅ OK'}\n"
        f"Catastrophic: {cat_limit}% {'⚠️ close' if dd_peak > cat_limit * 0.5 else '✅ OK'}\n\n"
        f"_Last check: {relative_time(arbiter.get('lastCheckAt', ''))}_\n"
        f"_Risk Arbiter auto-sets RISK\\_OFF at {daily_limit}% daily loss and flattens all at {cat_limit}% drawdown._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "UNKNOWN")
    mode_emoji = {"RISK_ON": "🟢", "BASELINE": "🟡", "RISK_OFF": "🔴"}.get(mode, "⚪")
    params = regime.get("regimes", {}).get(mode, {})
    guardrails = regime.get("globalGuardrails", {})

    text = (
        f"{mode_emoji} *Regime: {mode}*\n\n"
        f"*Set by:* {regime.get('updatedBy', '?')}\n"
        f"*When:* {relative_time(regime.get('updatedAt', ''))}\n"
        f"*Reason:* {regime.get('reason', '?')}\n\n"
        f"*Active Parameters:*\n"
        f"  Max slots: {params.get('maxSlots', '?')}\n"
        f"  Leverage: {params.get('maxLeverageCrypto', '?')}x (hardcoded 7-10x band)\n"
        f"  Alloc/slot: {params.get('allocPctPerSlot', '?')}%\n"
        f"  New entries: {'✅ allowed' if params.get('newEntriesAllowed') else '❌ blocked'}\n"
        f"  Auto-entry: {'✅ enabled' if params.get('autoEntryEnabled') else '❌ disabled'}\n"
        f"  DSL preset: {params.get('dslPreset', '?')}\n\n"
        f"*Global Guardrails:*\n"
        f"  Daily loss limit: {guardrails.get('dailyLossLimitPct', '?')}%\n"
        f"  Catastrophic drawdown: {guardrails.get('catastrophicDrawdownPct', '?')}%\n"
        f"  Max consecutive stop-outs: {guardrails.get('maxConsecutiveStopOuts', '?')}\n\n"
        f"_Use /risk\\_on, /risk\\_off, or /baseline to change._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not pending:
        await update.message.reply_text(
            "No pending signals.\n\n"
            "_ORCA scans every 60s, KOMODO every 5 min. "
            "Signals appear here when detected and are cleared by the Oz Trade Evaluator (every 15 min)._"
        )
        return

    lines = [f"⏳ *{len(pending)} Pending Signals*\n"]
    for p in pending[-10:]:
        mode = p.get("mode", p.get("signalType", "?"))
        score = p.get("score", "?")
        scanner = p.get("scanner", p.get("source", "orca"))
        entered = "✅ auto-entered" if p.get("autoEntered") else "⏳ awaiting Oz review"
        age = relative_time(p.get("queuedAt", p.get("timestamp", "")))
        reasons = p.get("reasons", [])
        reason_str = f" · {', '.join(reasons[:3])}" if reasons else ""

        lines.append(
            f"• *{p.get('direction', '?')} {p.get('asset', '?')}* [{scanner}/{mode}]\n"
            f"  Score: {score}{reason_str}\n"
            f"  {entered} · {age}"
        )

    lines.append(
        f"\n_Auto-entry: STRIKER ≥9, STALKER ≥6, KOMODO ≥10._\n"
        f"_Signals below threshold queue for Oz Trade Evaluator review._"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

def _set_regime(mode: str, reason: str):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    regime["riskMode"] = mode
    regime["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    regime["updatedBy"] = "telegram-bot"
    regime["reason"] = reason
    tmp = (CONFIG_DIR / "risk-regime.json").with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(regime, f, indent=2)
        f.write("\n")
    tmp.rename(CONFIG_DIR / "risk-regime.json")


@authorized
async def cmd_risk_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_regime("RISK_ON", "Manual /risk_on from Telegram")
    await update.message.reply_text(
        "🟢 *RISK\\_ON activated*\n\n"
        "• Max 3 slots unlocked\n"
        "• Leverage: 7-10x\n"
        "• Allocation: 35% per slot\n"
        "• Auto-entry: enabled\n\n"
        "_Use when BTC/ETH macro trend is clearly aligned. "
        "The hourly Regime Classifier may override this if conditions deteriorate._",
        parse_mode="Markdown",
    )


@authorized
async def cmd_risk_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_regime("RISK_OFF", "Manual /risk_off from Telegram")
    pos_count, _ = _count_open_positions()
    pos_note = (
        f"\n⚠️ *{pos_count} position(s) still open* — DSL trailing stops continue managing them.\n"
        f"Use /flatten to close everything immediately."
        if pos_count > 0 else ""
    )
    await update.message.reply_text(
        f"🔴 *RISK\\_OFF activated*\n\n"
        f"• All new entries blocked\n"
        f"• Auto-entry: disabled\n"
        f"• Scanners still run (for monitoring) but won't open positions"
        f"{pos_note}\n\n"
        f"_The Regime Classifier or manual /baseline / /risk\\_on will re-enable entries._",
        parse_mode="Markdown",
    )


@authorized
async def cmd_baseline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_regime("BASELINE", "Manual /baseline from Telegram")
    await update.message.reply_text(
        "🟡 *BASELINE activated*\n\n"
        "• Max 2 slots\n"
        "• Leverage: 7-10x\n"
        "• Allocation: 30% per slot\n"
        "• Auto-entry: enabled\n"
        "• 60s cooldown after losses\n\n"
        "_Default balanced regime. Good for mixed or uncertain market conditions._",
        parse_mode="Markdown",
    )


@authorized
async def cmd_flatten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pos_count, positions = _count_open_positions()
    if pos_count == 0:
        _set_regime("RISK_OFF", "Emergency flatten from Telegram (no positions)")
        await update.message.reply_text(
            "🔴 No open positions to flatten.\nRegime set to RISK\\_OFF as precaution.",
            parse_mode="Markdown",
        )
        return

    pos_list = ", ".join(f"{p.get('direction','?')} {p.get('asset','?')}" for p in positions)
    await update.message.reply_text(
        f"🚨 *EMERGENCY FLATTEN*\n\n"
        f"Closing {pos_count} position(s): {pos_list}\n\n"
        f"Setting RISK\\_OFF and dispatching Oz agent...",
        parse_mode="Markdown",
    )
    _set_regime("RISK_OFF", "Emergency flatten from Telegram")

    warp_key = os.environ.get("WARP_API_KEY", "")
    oz_env = os.environ.get("OZ_ENVIRONMENT_ID", "")
    if not warp_key:
        await update.message.reply_text(
            "⚠️ *WARP\\_API\\_KEY not configured.*\n\n"
            "RISK\\_OFF set — no new entries. Existing positions will be managed by:\n"
            "• DSL trailing stops (every 3 min)\n"
            "• Risk Arbiter safety checks (every 30s)\n\n"
            "_Configure WARP\\_API\\_KEY in Railway to enable Oz-powered immediate flatten._",
            parse_mode="Markdown",
        )
        return

    try:
        import httpx
        payload = {
            "prompt": (
                "EMERGENCY: Close ALL open positions immediately via mcporter. "
                "Set config/risk-regime.json riskMode to RISK_OFF with reason "
                "'Emergency flatten from Telegram'. Commit and push all changes."
            ),
            "config": {},
        }
        if oz_env:
            payload["config"]["environment_id"] = oz_env

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://app.warp.dev/api/v1/agent/run",
                headers={"Authorization": f"Bearer {warp_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                run_id = data.get("id", data.get("run_id", "?"))
                await update.message.reply_text(
                    f"✅ *Oz flatten dispatched*\n\n"
                    f"Run ID: `{run_id}`\n"
                    f"RISK\\_OFF set locally for immediate effect.\n\n"
                    f"_Track: `oz run get {run_id}`_",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(f"❌ Oz API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Oz dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Manual Triggers
# ---------------------------------------------------------------------------

@authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐋 *Running ORCA scanner...*\n"
        "_Dual-mode: STALKER (accumulation) + STRIKER (explosion)_",
        parse_mode="Markdown",
    )
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/orca-scanner-cron.py")])
    if output == "(no output)":
        await update.message.reply_text("🐋 ORCA scan complete — no signals detected this cycle.")
    else:
        await update.message.reply_text(f"🐋 *ORCA scan complete:*\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_komodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦎 *Running KOMODO scanner...*\n"
        "_Momentum event consensus: 2+ elite traders, same asset/direction_",
        parse_mode="Markdown",
    )
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/komodo-scanner-cron.py")])
    if output == "(no output)":
        await update.message.reply_text("🦎 KOMODO scan complete — no momentum consensus detected.")
    else:
        await update.message.reply_text(f"🦎 *KOMODO scan complete:*\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_arbiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚨 *Running Risk Arbiter...*\n"
        "_Checks: daily loss limit, catastrophic drawdown, consecutive stop-outs_",
        parse_mode="Markdown",
    )
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/risk-arbiter.py")])
    if output == "(no output)":
        await update.message.reply_text("🚨 Risk Arbiter: all clear ✅")
    else:
        await update.message.reply_text(f"🚨 *Arbiter result:*\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏥 *Running health check...*\n"
        "_git pull → reconcile closes → health validation → git push_",
        parse_mode="Markdown",
    )
    output = await run_script_async(["bash", str(STATE_DIR / "scripts/vps/health-check-cron.sh")], timeout=90)
    if output == "(no output)":
        await update.message.reply_text("🏥 Health check complete ✅ — state synced to GitHub.")
    else:
        await update.message.reply_text(f"🏥 *Health check:*\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_arena(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Running arena monitor...*\n"
        "_Polling Senpi Predators performance tracker (24 competing strategies)_",
        parse_mode="Markdown",
    )
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/arena-monitor.py")])
    if output == "(no output)":
        await update.message.reply_text("📊 Arena monitor complete. Run /arena\\_insights to see results.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"📊 *Arena monitor:*\n```\n{output[:3000]}\n```", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@authorized
async def cmd_howl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    howl_files = sorted(MEMORY_DIR.glob("howl-*.md"), reverse=True)
    if not howl_files:
        await update.message.reply_text(
            "📜 No HOWL reports yet.\n\n"
            "_HOWL (Hunt-Optimize-Win-Learn) runs nightly at 23:55 UTC. "
            "It analyzes the day's trades, compares performance against the arena, "
            "and auto-applies risk-reducing improvements._"
        )
        return
    content = howl_files[0].read_text()
    name = howl_files[0].name
    if len(content) > 3500:
        content = content[:3500] + f"\n\n_(truncated — full report in memory/{name})_"
    await update.message.reply_text(f"📜 *{name}*\n\n{content}", parse_mode="Markdown")


@authorized
async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    if not journal:
        await update.message.reply_text(
            "📒 No trades recorded yet.\n\n"
            "_The trade journal is populated automatically when ORCA or KOMODO opens positions, "
            "and when the health check reconciles DSL closes every 10 minutes._"
        )
        return

    opens = [t for t in journal if t.get("action") == "OPEN"]
    closes = [t for t in journal if t.get("action") == "CLOSE"]
    total_pnl = sum(float(t.get("realizedPnl", 0)) for t in closes)
    wins = [t for t in closes if float(t.get("realizedPnl", 0)) > 0]
    losses = [t for t in closes if float(t.get("realizedPnl", 0)) < 0]
    wr = round(len(wins) / len(closes) * 100, 1) if closes else 0
    avg_win = sum(float(t.get("realizedPnl", 0)) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(float(t.get("realizedPnl", 0)) for t in losses) / len(losses) if losses else 0
    pf = abs(avg_win * len(wins)) / abs(avg_loss * len(losses)) if losses and avg_loss != 0 else 0

    # By source
    by_source = {}
    for t in closes:
        src = t.get("entrySource", t.get("entryMode", "unknown"))
        by_source.setdefault(src, {"count": 0, "pnl": 0, "wins": 0})
        by_source[src]["count"] += 1
        pnl = float(t.get("realizedPnl", 0))
        by_source[src]["pnl"] += pnl
        if pnl > 0:
            by_source[src]["wins"] += 1

    source_lines = []
    for src, data in sorted(by_source.items(), key=lambda x: x[1]["pnl"], reverse=True):
        src_wr = round(data["wins"] / data["count"] * 100) if data["count"] else 0
        source_lines.append(f"  {src}: {data['count']} trades · {'+'if data['pnl']>=0 else ''}${data['pnl']:.2f} · {src_wr}% WR")

    text = (
        f"📒 *Trade Journal — Lifetime Stats*\n\n"
        f"Entries: {len(opens)} · Exits: {len(closes)}\n"
        f"*Total PnL:* {'+'if total_pnl>=0 else ''}${total_pnl:.2f}\n"
        f"*Win rate:* {wr}% ({len(wins)}W / {len(losses)}L)\n"
        f"*Avg win:* +${avg_win:.2f} · *Avg loss:* ${avg_loss:.2f}\n"
        f"*Profit factor:* {pf:.2f}\n"
    )
    if source_lines:
        text += f"\n*Performance by entry source:*\n" + "\n".join(source_lines)
    text += (
        f"\n\n_Key insight from 22 agents: fewer trades + higher conviction = better performance. "
        f"FOX is #1 at +13.93% ROI with only 436 trades._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_arena_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arena = load_json(OUTPUTS_DIR / "arena-state.json")
    if not arena:
        await update.message.reply_text(
            "📊 No arena data yet.\n\n"
            "_Run /arena to fetch the latest Senpi Predators leaderboard, "
            "then use /arena\\_insights to analyze it._",
            parse_mode="Markdown",
        )
        return

    insights = arena.get("insights", {})
    updated = relative_time(arena.get("updatedAt", ""))

    lb = arena.get("leaderboard", [])
    top_lines = []
    for i, entry in enumerate(lb[:5]):
        slug = entry.get("slug", entry.get("name", "?"))
        roi = float(entry.get("roi", entry.get("roiPct", 0)))
        trades = entry.get("totalTrades", entry.get("trades", "?"))
        medal = ["🥇", "🥈", "🥉", "4.", "5."][i]
        top_lines.append(f"  {medal} *{slug}* · {roi:+.1f}% ROI · {trades} trades")

    recs = insights.get("recommendations", [])
    rec_lines = [f"  • {r}" for r in recs] if recs else ["  • Continue current approach"]

    text = (
        f"📊 *Arena Insights* (updated {updated})\n\n"
        f"*Top 5 Predators:*\n" + "\n".join(top_lines) + "\n\n"
        f"✅ *Winning traits:* {', '.join(insights.get('winningTraits', ['N/A']))}\n"
        f"❌ *Losing traits:* {', '.join(insights.get('losingTraits', ['N/A']))}\n\n"
        f"💡 *Recommendations:*\n" + "\n".join(rec_lines) + "\n\n"
        f"_The Arena Strategy Learner (Oz, every 4h) auto-applies risk-reducing changes. "
        f"Risk increases are flagged for manual review._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Free text → Oz
# ---------------------------------------------------------------------------

@authorized
async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    if not message:
        return

    warp_key = os.environ.get("WARP_API_KEY", "")
    if not warp_key:
        await update.message.reply_text(
            "⚠️ *Oz not configured*\n\n"
            "Set `WARP_API_KEY` and optionally `OZ_ENVIRONMENT_ID` in Railway to enable free-text prompts.\n\n"
            "_Oz cloud agents can execute mcporter API calls, analyze positions, and push config changes._",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("🧠 Dispatching to Oz cloud agent...")

    try:
        import httpx
        oz_env = os.environ.get("OZ_ENVIRONMENT_ID", "")
        payload = {"prompt": message, "config": {}}
        if oz_env:
            payload["config"]["environment_id"] = oz_env

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://app.warp.dev/api/v1/agent/run",
                headers={"Authorization": f"Bearer {warp_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                run_id = data.get("id", data.get("run_id", "?"))
                await update.message.reply_text(
                    f"✅ *Oz agent dispatched*\n\n"
                    f"Run ID: `{run_id}`\n\n"
                    f"_Oz will read state, execute your request, and push any changes. "
                    f"Results appear in the next /status or /trades update._",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(f"❌ Oz API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Oz dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

def create_bot_application() -> Optional[Application]:
    """Create and configure the Telegram bot. Returns None if token not set."""
    if not TELEGRAM_BOT_TOKEN:
        return None

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # /start onboarding
    app.add_handler(CommandHandler("start", cmd_start))

    # Status & monitoring
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("equity", cmd_equity))
    app.add_handler(CommandHandler("regime", cmd_regime))
    app.add_handler(CommandHandler("pending", cmd_pending))

    # Control
    app.add_handler(CommandHandler("risk_on", cmd_risk_on))
    app.add_handler(CommandHandler("risk_off", cmd_risk_off))
    app.add_handler(CommandHandler("baseline", cmd_baseline))
    app.add_handler(CommandHandler("flatten", cmd_flatten))

    # Manual triggers
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("komodo", cmd_komodo))
    app.add_handler(CommandHandler("arbiter", cmd_arbiter))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("arena", cmd_arena))

    # Reports
    app.add_handler(CommandHandler("howl", cmd_howl))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("arena_insights", cmd_arena_insights))

    # Free text → Oz
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))

    return app


async def start_polling(app: Application):
    """Start the bot polling loop and register command menu with BotFather."""
    await app.initialize()
    await app.start()

    # Register command menu — appears when user types "/" in the chat
    bot_commands = [
        BotCommand(cmd, short) for cmd, short, _ in COMMANDS
    ]
    try:
        await app.bot.set_my_commands(bot_commands)
    except Exception:
        pass  # Non-fatal if menu registration fails

    await app.updater.start_polling(drop_pending_updates=True)


async def stop_polling(app: Application):
    """Stop the bot gracefully."""
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
