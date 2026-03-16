"""
Senpi Telegram Bot — receive /commands and free-text prompts via Telegram.

Runs as an async background task inside the dashboard FastAPI app.
Reuses the same state reading, command logic, and Oz dispatch.

Commands:
  Status & Monitoring:
    /status      — regime, positions, daily PnL
    /positions   — open position details
    /trades      — recent 10 trades
    /equity      — equity snapshot
    /regime      — current regime info
    /pending     — queued scanner signals

  Control:
    /risk_on     — set RISK_ON regime
    /risk_off    — set RISK_OFF regime
    /baseline    — set BASELINE regime
    /flatten     — emergency close all (via Oz)

  Manual Triggers:
    /scan        — run ORCA scanner now
    /komodo      — run KOMODO scanner now
    /arbiter     — run risk arbiter now
    /health      — run health check + git sync
    /arena       — run arena monitor now

  Reports:
    /howl        — last HOWL nightly report
    /journal     — trade journal stats (win rate, PnL breakdown)
    /arena_insights — arena leaderboard insights

  Other:
    /help        — list commands
    free text    — dispatched to Oz cloud agent
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Update
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

# Environment for child processes
CHILD_ENV = {
    **os.environ,
    "SENPI_STATE_DIR": str(STATE_DIR),
}


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
    """Only respond to the configured chat ID."""
    if not TELEGRAM_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


async def run_script_async(cmd: list[str], timeout: int = 60) -> str:
    """Run a script in a subprocess and return stderr/stdout summary."""
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


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Status & Monitoring Commands
# ---------------------------------------------------------------------------

@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🐺 *Senpi Bot Commands*

*Status & Monitoring:*
/status — regime, positions, daily PnL
/positions — open position details
/trades — recent 10 trades
/equity — equity snapshot
/regime — current regime info
/pending — queued scanner signals

*Control:*
/risk\\_on — set RISK\\_ON regime
/risk\\_off — set RISK\\_OFF regime
/baseline — set BASELINE regime
/flatten — emergency close all

*Manual Triggers:*
/scan — run ORCA scanner now
/komodo — run KOMODO scanner now
/arbiter — run risk arbiter now
/health — run health check + git sync
/arena — run arena monitor now

*Reports:*
/howl — last HOWL nightly report
/journal — trade journal stats
/arena\\_insights — arena leaderboard insights

_Any other text is sent to Oz as a prompt._"""
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json")
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])

    # Count open positions
    strategies = load_json(CONFIG_DIR / "wolf-strategies.json")
    pos_count = 0
    for key in strategies.get("strategies", {}):
        strat_dir = POSITION_STATE_DIR / key
        if strat_dir.exists():
            for f in strat_dir.glob("dsl-*.json"):
                state = load_json(f)
                if state and state.get("active"):
                    pos_count += 1

    # Daily PnL
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_closes = [t for t in journal
                    if t.get("recordedAt", "").startswith(today) and t.get("action") == "CLOSE"]
    daily_pnl = sum(float(t.get("realizedPnl", 0)) for t in today_closes)
    daily_wins = sum(1 for t in today_closes if float(t.get("realizedPnl", 0)) > 0)
    daily_count = len(today_closes)
    wr = round(daily_wins / daily_count * 100, 1) if daily_count > 0 else 0

    mode = regime.get("riskMode", "UNKNOWN")
    mode_emoji = {"RISK_ON": "🟢", "BASELINE": "🟡", "RISK_OFF": "🔴"}.get(mode, "⚪")
    equity = arbiter.get("lastEquity", 0)
    peak = arbiter.get("peakEquity", 0)

    pnl_sign = "+" if daily_pnl >= 0 else ""
    text = (
        f"{mode_emoji} *Regime:* {mode}\n"
        f"📊 *Positions:* {pos_count} open | {len(pending)} pending\n"
        f"💰 *Daily PnL:* {pnl_sign}${daily_pnl:.2f} | {daily_count} trades | {wr}% WR\n"
        f"🏦 *Equity:* ${equity:,.2f} | Peak: ${peak:,.2f}\n"
        f"⏱ Last arbiter: {relative_time(arbiter.get('lastCheckAt', ''))}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                positions.append(state)

    if not positions:
        await update.message.reply_text("No open positions.")
        return

    lines = [f"*{len(positions)} open positions:*\n"]
    for p in positions:
        tier = (p.get("currentTierIndex", -1) or -1) + 1
        phase = "Phase 2" if tier > 0 else "Phase 1"
        direction = p.get("direction", "?")
        d_emoji = "🟢" if direction == "LONG" else "🔴"
        age = relative_time(p.get("createdAt", ""))
        lines.append(
            f"{d_emoji} *{direction} {p.get('asset', '?')}* ({p['_strategy']})\n"
            f"  Entry: ${p.get('entryPrice', 0):.4f} | Lev: {p.get('leverage', 0)}x\n"
            f"  {phase} T{tier} | HW: ${p.get('highWaterPrice', 0):.4f}\n"
            f"  Breaches: {p.get('currentBreachCount', 0)} | {age}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    recent = journal[-10:]
    if not recent:
        await update.message.reply_text("No trades recorded.")
        return

    lines = ["*Last 10 trades:*\n"]
    for t in reversed(recent):
        action = t.get("action", "?")
        asset = t.get("asset", "?")
        direction = t.get("direction", "?")
        age = relative_time(t.get("recordedAt", ""))
        if action == "CLOSE":
            pnl = float(t.get("realizedPnl", 0))
            sign = "+" if pnl >= 0 else ""
            emoji = "✅" if pnl >= 0 else "❌"
            reason = t.get("closeReason", "")
            lines.append(f"{emoji} CLOSE {direction} *{asset}* {sign}${pnl:.2f} ({reason}) — {age}")
        else:
            source = t.get("entrySource", t.get("entryMode", ""))
            lines.append(f"📥 OPEN {direction} *{asset}* via {source} — {age}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_equity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json")
    equity = arbiter.get("lastEquity", 0)
    peak = arbiter.get("peakEquity", 0)
    day_start = arbiter.get("dayStartEquity", 0)
    dd = (peak - equity) / peak * 100 if peak > 0 else 0
    daily_chg = equity - day_start if day_start > 0 else 0
    daily_pct = daily_chg / day_start * 100 if day_start > 0 else 0

    text = (
        f"🏦 *Equity Snapshot*\n\n"
        f"Current: *${equity:,.2f}*\n"
        f"Day start: ${day_start:,.2f} ({'+' if daily_chg >= 0 else ''}{daily_pct:.1f}%)\n"
        f"Peak: ${peak:,.2f}\n"
        f"Drawdown from peak: {dd:.1f}%\n"
        f"Last check: {relative_time(arbiter.get('lastCheckAt', ''))}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "UNKNOWN")
    mode_emoji = {"RISK_ON": "🟢", "BASELINE": "🟡", "RISK_OFF": "🔴"}.get(mode, "⚪")
    params = regime.get("regimes", {}).get(mode, {})
    text = (
        f"{mode_emoji} *Regime: {mode}*\n\n"
        f"Updated: {relative_time(regime.get('updatedAt', ''))}\n"
        f"By: {regime.get('updatedBy', '?')}\n"
        f"Reason: {regime.get('reason', '?')}\n\n"
        f"Max slots: {params.get('maxSlots', '?')}\n"
        f"Max leverage: {params.get('maxLeverageCrypto', '?')}x\n"
        f"Alloc/slot: {params.get('allocPctPerSlot', '?')}%\n"
        f"Entries: {'✅' if params.get('newEntriesAllowed') else '❌'}\n"
        f"Auto-entry: {'✅' if params.get('autoEntryEnabled') else '❌'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not pending:
        await update.message.reply_text("No pending signals.")
        return

    lines = [f"*{len(pending)} pending signals:*\n"]
    for p in pending[-10:]:
        mode = p.get("mode", p.get("signalType", "?"))
        entered = "✅ auto" if p.get("autoEntered") else "⏳ queued"
        scanner = p.get("scanner", p.get("source", "orca"))
        lines.append(
            f"• {mode} {p.get('direction', '?')} *{p.get('asset', '?')}* "
            f"score={p.get('score', '?')} [{scanner}] {entered}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Control Commands
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
    await update.message.reply_text("🟢 Regime set to *RISK\\_ON*. Max slots and leverage unlocked.", parse_mode="Markdown")


@authorized
async def cmd_risk_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_regime("RISK_OFF", "Manual /risk_off from Telegram")
    await update.message.reply_text(
        "🔴 Regime set to *RISK\\_OFF*. No new entries. Existing positions managed by DSL.",
        parse_mode="Markdown",
    )


@authorized
async def cmd_baseline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_regime("BASELINE", "Manual /baseline from Telegram")
    await update.message.reply_text("🟡 Regime set to *BASELINE*.", parse_mode="Markdown")


@authorized
async def cmd_flatten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 Setting RISK\\_OFF and dispatching Oz flatten...", parse_mode="Markdown")
    _set_regime("RISK_OFF", "Emergency flatten from Telegram")

    warp_key = os.environ.get("WARP_API_KEY", "")
    oz_env = os.environ.get("OZ_ENVIRONMENT_ID", "")
    if not warp_key:
        await update.message.reply_text(
            "⚠️ WARP\\_API\\_KEY not configured. RISK\\_OFF set locally — DSL + Risk Arbiter will manage exits.",
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
                await update.message.reply_text(f"✅ Oz flatten dispatched.\nRun ID: `{run_id}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Oz API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Oz dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Manual Trigger Commands
# ---------------------------------------------------------------------------

@authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🐋 Running ORCA scanner...")
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/orca-scanner-cron.py")])
    await update.message.reply_text(f"🐋 ORCA scan complete:\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_komodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🦎 Running KOMODO scanner...")
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/komodo-scanner-cron.py")])
    await update.message.reply_text(f"🦎 KOMODO scan complete:\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_arbiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 Running Risk Arbiter...")
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/risk-arbiter.py")])
    await update.message.reply_text(f"🚨 Arbiter complete:\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏥 Running health check + git sync...")
    output = await run_script_async(["bash", str(STATE_DIR / "scripts/vps/health-check-cron.sh")], timeout=90)
    await update.message.reply_text(f"🏥 Health check complete:\n```\n{output[:3000]}\n```", parse_mode="Markdown")


@authorized
async def cmd_arena(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Running arena monitor...")
    output = await run_script_async(["python3", str(STATE_DIR / "scripts/vps/arena-monitor.py")])
    await update.message.reply_text(f"📊 Arena monitor complete:\n```\n{output[:3000]}\n```", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Report Commands
# ---------------------------------------------------------------------------

@authorized
async def cmd_howl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    howl_files = sorted(MEMORY_DIR.glob("howl-*.md"), reverse=True)
    if not howl_files:
        await update.message.reply_text("No HOWL reports yet.")
        return
    content = howl_files[0].read_text()
    name = howl_files[0].name
    if len(content) > 3500:
        content = content[:3500] + f"\n\n_(truncated — full report in memory/{name})_"
    await update.message.reply_text(f"📜 *{name}:*\n\n{content}", parse_mode="Markdown")


@authorized
async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    if not journal:
        await update.message.reply_text("No trades recorded.")
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
        source_lines.append(f"  {src}: {data['count']} trades | ${data['pnl']:+.2f} | {src_wr}% WR")

    text = (
        f"📒 *Trade Journal Stats*\n\n"
        f"Total trades: {len(opens)} opens, {len(closes)} closes\n"
        f"Total PnL: *${total_pnl:+.2f}*\n"
        f"Win rate: {wr}% ({len(wins)}W / {len(losses)}L)\n"
        f"Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f}\n"
        f"Profit factor: {pf:.2f}\n\n"
        f"*By source:*\n" + "\n".join(source_lines) if source_lines else ""
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_arena_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arena = load_json(OUTPUTS_DIR / "arena-state.json")
    if not arena:
        await update.message.reply_text("No arena data yet. Run /arena first.")
        return

    insights = arena.get("insights", {})
    updated = relative_time(arena.get("updatedAt", ""))

    lb = arena.get("leaderboard", [])
    top_lines = []
    for i, entry in enumerate(lb[:5]):
        slug = entry.get("slug", entry.get("name", "?"))
        roi = float(entry.get("roi", entry.get("roiPct", 0)))
        trades = entry.get("totalTrades", entry.get("trades", "?"))
        top_lines.append(f"  {i+1}. {slug}: {roi:+.1f}% ROI ({trades} trades)")

    text = (
        f"📊 *Arena Insights* ({updated})\n\n"
        f"Best: *{insights.get('bestStrategy', '?')}* ({insights.get('bestRoi', 0):+.1f}% ROI)\n\n"
        f"*Top 5:*\n" + "\n".join(top_lines) + "\n\n"
        f"Winning traits: {', '.join(insights.get('winningTraits', []))}\n"
        f"Losing traits: {', '.join(insights.get('losingTraits', []))}\n\n"
        f"*Recommendations:*\n" +
        "\n".join(f"  • {r}" for r in insights.get("recommendations", []))
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Free text → Oz dispatch
# ---------------------------------------------------------------------------

@authorized
async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send non-command messages to Oz cloud agent."""
    message = update.message.text.strip()
    if not message:
        return

    warp_key = os.environ.get("WARP_API_KEY", "")
    if not warp_key:
        await update.message.reply_text("⚠️ WARP\\_API\\_KEY not configured. Cannot dispatch to Oz.", parse_mode="Markdown")
        return

    await update.message.reply_text("🧠 Sending to Oz...")

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
                await update.message.reply_text(f"✅ Oz dispatched.\nRun ID: `{run_id}`", parse_mode="Markdown")
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

    # Status & monitoring
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
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
    """Start the bot polling loop. Call from an async context."""
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)


async def stop_polling(app: Application):
    """Stop the bot gracefully."""
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
