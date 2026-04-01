"""
Senpi Telegram Bot — pure remote for the waifu-cli strategic suite.

Runs as an async background task inside the dashboard FastAPI app.
On startup, registers the command menu with BotFather automatically.

   Architecture:
   Railway runs the mechanical layer — scanners, DSL trailing stops,
   Risk Arbiter. No LLM, sub-2s execution.

   This bot exposes 11 strategic commands that delegate to waifu-cli.
   Free-text messages are dispatched to the Strategic Brain (Hermes Apollo)
   via the `hermes chat` subcommand.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("telegram_bot")

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"
USER_RULES_FILE = CONFIG_DIR / "user-rules.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHILD_ENV = {**os.environ, "SENPI_WAIFU_DIR": str(STATE_DIR)}

# Command descriptions — registered with BotFather and shown in /help.
# Each tuple: (command, short_desc_for_menu, detailed_desc_for_help)
COMMANDS = [
    (
        "status",
        "System snapshot",
        "Regime, open positions, daily PnL, equity, and arbiter status.",
    ),
    ("jido", "Autonomous executor", "Process high-conviction trades via brain policy."),
    ("evaluate", "Process signals", "HITL evaluation of queued scanner signals."),
    ("regime", "Market regime", "BTC/ETH macro classification and parameters."),
    (
        "review",
        "Portfolio review",
        "Equity, drawdown, daily PnL, dead-weight detection.",
    ),
    ("howl", "Nightly analysis", "10-pillar self-improvement analysis."),
    ("whale", "Copy-trade rebalance", "Mirror-trade portfolio management."),
    ("arena", "Predator leaderboard", "Top predator strategies and recommendations."),
    (
        "suguru",
        "Elite scanner",
        "Scan markets + AI deliberation → trade recommendation.",
    ),
    (
        "settings",
        "View all settings",
        "Unified view of rules, gates, and scanner scores.",
    ),
    ("set", "Change a setting", "Usage: /set <key> <value>"),
    ("flatten", "Close all trades", "Close all open positions across all strategies."),
    ("close", "Close a trade", "Select and close a specific open position."),
    ("emergency_stop", "Immediate RISK_OFF", "Block all entries and send alert."),
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


async def _safe_reply(update: Update, text: str, **kwargs):
    if update.message:
        try:
            return await update.message.reply_text(text, **kwargs)
        except Exception as e:
            logger.error("reply_text failed: %s", e)
    else:
        logger.warning("update.message is None — cannot reply")


async def _safe_edit(query, text: str, **kwargs) -> None:
    """Edit a callback-query message in-place. Catches BadRequest gracefully."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not changed" not in str(e).lower():
            logger.warning("edit_message_text failed: %s", e)
    except Exception as e:
        logger.error("edit_message_text failed: %s", e)


async def _progress_reply(update: Update, text: str = "⏳ Working..."):
    """Send a temporary progress message. Returns the Message for later edit."""
    if update.message:
        try:
            return await update.message.reply_text(text)
        except Exception as e:
            logger.error("progress reply failed: %s", e)
    return None


async def _answer_and_edit(query, text: str, reply_markup=None, **kwargs) -> None:
    """Standard callback pattern: answer the query, then edit the message."""
    await query.answer()
    await _safe_edit(query, text, reply_markup=reply_markup, **kwargs)


def _build_status_keyboard() -> InlineKeyboardMarkup:
    """Build inline action buttons for /status output."""
    _, positions = _count_open_positions()
    buttons = []
    if positions:
        buttons.append(
            [
                InlineKeyboardButton("🔄 Jido", callback_data="act:jido_prompt"),
                InlineKeyboardButton(
                    "⚡ Evaluate", callback_data="act:evaluate_prompt"
                ),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("🔴 Flatten", callback_data="act:flatten_prompt"),
                InlineKeyboardButton("✂️ Close", callback_data="act:close_prompt"),
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton("🔃 Refresh", callback_data="act:status_refresh"),
            InlineKeyboardButton("📊 Review", callback_data="act:review_run"),
            InlineKeyboardButton("🛡 Gates", callback_data="act:gates_view"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def is_authorized(update: Update) -> bool:
    chat_id = (
        getattr(update.effective_chat, "id", None) if update.effective_chat else None
    )
    logger.info(
        "incoming chat_id=%s configured TELEGRAM_CHAT_ID=%s", chat_id, TELEGRAM_CHAT_ID
    )

    if not TELEGRAM_CHAT_ID:
        return True
    if chat_id is None:
        return False
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            await _safe_reply(
                update,
                "⛔ Unauthorized. This bot only responds to its configured owner.",
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


def _deactivate_dsl_state(pos: dict, reason: str) -> None:
    """Mark a DSL state file as closed after user-initiated close."""
    strat_key = pos.get("_key", "")
    asset = pos.get("asset", "")
    strat_dir = POSITION_STATE_DIR / strat_key
    if not strat_dir.exists():
        return
    for f in strat_dir.glob("dsl-*.json"):
        state = load_json(f)
        if state and state.get("active") and state.get("asset") == asset:
            state["active"] = False
            state["closedAt"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            state["closeReason"] = reason
            with open(f, "w") as fh:
                json.dump(state, fh, indent=2)
                fh.write("\n")
            break


def _daily_stats(journal: list[dict]) -> dict:
    """Compute today's trading stats from the journal."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    closes = [
        t
        for t in journal
        if t.get("recordedAt", "").startswith(today) and t.get("action") == "CLOSE"
    ]
    pnl = sum(float(t.get("realizedPnl", 0)) for t in closes)
    wins = sum(1 for t in closes if float(t.get("realizedPnl", 0)) > 0)
    count = len(closes)
    return {
        "pnl": pnl,
        "wins": wins,
        "count": count,
        "wr": round(wins / count * 100, 1) if count > 0 else 0,
    }


def _regime_header() -> str:
    """One-line context header: regime, open positions, pending signals."""
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "UNKNOWN")
    _, positions = _count_open_positions()
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not isinstance(pending, list):
        pending = []
    parts = [f"📊 {mode}", f"{len(positions)} open", f"{len(pending)} pending"]
    return " • ".join(parts)


def _check_stale_crons(heartbeats: dict) -> list[str]:
    """Check for stale cron heartbeats. Returns list of stale cron names."""
    stale_limits = {
        "orca": 10,
        "mantis": 4,
        "fox": 4,
        "roach": 4,
        "komodo": 12,
        "condor": 8,
        "polar": 8,
        "rhino": 8,
        "sentinel": 8,
        "dsl-runner": 8,
        "risk-arbiter": 3,
        "brain": 12,
    }
    now = datetime.now(timezone.utc)
    stale = []
    for name, max_min in stale_limits.items():
        last = heartbeats.get(name)
        if not last:
            continue
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (now - dt).total_seconds() > max_min * 60:
                stale.append(name)
        except (ValueError, TypeError):
            continue
    return stale


# ---------------------------------------------------------------------------
# /start — Onboarding
# ---------------------------------------------------------------------------


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "UNKNOWN")
    _, positions = _count_open_positions()
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not isinstance(pending, list):
        pending = []

    # Build compact status header
    lines = ["🐺 *Senpi Control Panel*\n"]
    lines.append(f"📊 *{mode}*  •  {len(positions)} open  •  {len(pending)} pending")

    if positions:
        for pos in positions[:3]:
            asset = pos.get("asset", "?")
            direction = pos.get("direction", "?")
            roe = float(pos.get("currentRoe", 0) or 0)
            lines.append(f"   {asset} {direction} {roe:+.1f}%")

    # Cron health
    heartbeat_file = OUTPUTS_DIR / "cron-heartbeats.json"
    heartbeats = load_json(heartbeat_file, default={})
    stale_crons = _check_stale_crons(heartbeats)
    if stale_crons:
        lines.append(f"⚠️ Stale: {', '.join(stale_crons)}")
    else:
        lines.append("✅ All crons healthy")

    lines.append("\n_Any message → Strategic Brain_")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Status", callback_data="act:status_run"),
                InlineKeyboardButton("🌐 Regime", callback_data="act:regime_run"),
                InlineKeyboardButton("📋 Review", callback_data="act:review_run"),
            ],
            [
                InlineKeyboardButton("⚡ Jido", callback_data="act:jido_prompt"),
                InlineKeyboardButton("⚡ Suguru", callback_data="act:suguru_scan_menu"),
                InlineKeyboardButton("🐋 Whale", callback_data="act:whale_run"),
            ],
            [
                InlineKeyboardButton("🔴 Flatten", callback_data="act:flatten_prompt"),
                InlineKeyboardButton("✂️ Close", callback_data="act:close_prompt"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="act:settings_view"),
                InlineKeyboardButton(
                    "🚨 Emergency Stop", callback_data="act:emergency_prompt"
                ),
            ],
        ]
    )

    await _safe_reply(
        update, "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
    )


# ---------------------------------------------------------------------------
# Waifu-CLI command delegates
# ---------------------------------------------------------------------------


async def _waifu_cli(update: Update, cmd: str, timeout: int = 90) -> None:
    """Run a waifu-cli command and reply with the output."""
    if not update.message:
        return
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        await _safe_reply(update, "❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, cmd], timeout=timeout)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    await _safe_reply(update, f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "⏳ Loading status...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "status"], timeout=60)
    if len(output) > 3800:
        output = output[:3700] + "\n\n_(truncated)_"
    keyboard = _build_status_keyboard()
    if msg:
        await msg.edit_text(
            f"```\n{output}\n```",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


@authorized
async def cmd_jido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "BASELINE")
    rules = load_json(USER_RULES_FILE, default={})
    jido_rules = rules.get("jido", {})
    auto = "ON" if jido_rules.get("autoExecuteEnabled", True) else "OFF"
    roi = float(jido_rules.get("roi_threshold_auto", 0.15))
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not isinstance(pending, list):
        pending = []

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Run", callback_data="act:jido_confirm"),
                InlineKeyboardButton("🔍 Dry Run", callback_data="act:jido_dry"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="act:jido_cancel")],
        ]
    )
    await update.message.reply_text(
        f"🔮 *Jido — Autonomous Executor*\n\n"
        f"Regime: *{mode}*  •  Auto-execute: *{auto}*\n"
        f"ROI threshold: *{roi:.0%}*  •  {len(pending)} pending signals\n\n"
        f"▶️ *Run* — live execution\n"
        f"🔍 *Dry Run* — preview only",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@authorized
async def cmd_evaluate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "BASELINE")
    pending = load_json(POSITION_STATE_DIR / "pending-entries.json", default=[])
    if not isinstance(pending, list):
        pending = []
    _, positions = _count_open_positions()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Execute", callback_data="act:evaluate_confirm"),
                InlineKeyboardButton("🔍 Dry Run", callback_data="act:evaluate_dry"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="act:evaluate_cancel")],
        ]
    )
    await update.message.reply_text(
        f"⚡ *Evaluate — Signal Processor*\n\n"
        f"Regime: *{mode}*  •  {len(positions)} open  •  {len(pending)} pending\n\n"
        f"▶️ *Execute* — process signals and place trades\n"
        f"🔍 *Dry Run* — preview approvals/rejections only",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@authorized
async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "⏳ Classifying market regime...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "regime"], timeout=60)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    if msg:
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "📊 Generating portfolio report...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "review"], timeout=120)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    if msg:
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_howl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "🐺 Loading HOWL analysis...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "howl"], timeout=120)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    if msg:
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "🐋 Running whale rebalance analysis...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "whale"], timeout=120)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    if msg:
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_arena(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    msg = await _progress_reply(update, "🏟 Loading arena leaderboard...")
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        if msg:
            await msg.edit_text("❌ waifu-cli not found in PATH.")
        return
    output = await run_script_async([waifu_bin, "arena"], timeout=120)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    if msg:
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")


@authorized
async def cmd_suguru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suguru — Scan + Hermes decision layer."""
    if not update.message:
        return
    regime = load_json(CONFIG_DIR / "risk-regime.json")
    mode = regime.get("riskMode", "UNKNOWN")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔍 Scan Only", callback_data="act:suguru_scan_only"
                ),
                InlineKeyboardButton(
                    "🧠 Hermes Scan", callback_data="act:suguru_hermes_scan"
                ),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="act:suguru_cancel")],
        ]
    )
    await update.message.reply_text(
        f"⚡ *Suguru — Elite Scanner*\n\n"
        f"Regime: *{mode}*\n\n"
        f"🔍 *Scan Only* — show scored candidates\n"
        f"🧠 *Hermes Scan* — scan + AI deliberation → trade recommendation",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@authorized
async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚨 CONFIRM", callback_data="act:emergency_stop_confirm"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data="act:emergency_stop_cancel"
                ),
            ],
        ]
    )
    await update.message.reply_text(
        "🚨 *Emergency Stop Confirmation*\n\n"
        "This will:\n"
        "• Set regime to RISK\\_OFF\n"
        "• Block all new entries\n"
        "• Send Telegram alert\n"
        "• Existing positions stay open (managed by DSL)\n\n"
        "_Are you sure?_",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@authorized
async def cmd_flatten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    _, positions = _count_open_positions()
    if not positions:
        await _safe_reply(update, "ℹ️ No open positions to close.")
        return
    pos_lines = []
    for pos in positions:
        asset = pos.get("asset", "?")
        direction = pos.get("direction", "?")
        roe = float(pos.get("currentRoe", 0) or 0)
        pos_lines.append(f"  • {asset} {direction} ({roe:+.1f}%)")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔴 CLOSE ALL", callback_data="act:flatten_confirm"
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="act:flatten_cancel"),
            ],
        ]
    )
    await _safe_reply(
        update,
        f"🔴 *Flatten — Close All Positions*\n\n"
        f"{len(positions)} open position(s):\n" + "\n".join(pos_lines) + "\n\n"
        f"_This will close ALL positions immediately._\n"
        f"_Are you sure?_",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@authorized
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    _, positions = _count_open_positions()
    if not positions:
        await _safe_reply(update, "ℹ️ No open positions to close.")
        return
    buttons = []
    for pos in positions:
        asset = pos.get("asset", "?")
        direction = pos.get("direction", "?")
        roe = float(pos.get("currentRoe", 0) or 0)
        strat_key = pos.get("_key", "")
        label = f"{asset} {direction} ({roe:+.1f}%)"
        callback = f"act:close_single:{strat_key}:{asset}"
        buttons.append([InlineKeyboardButton(f"🔴 {label}", callback_data=callback)])
    buttons.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="act:close_cancel")]
    )
    keyboard = InlineKeyboardMarkup(buttons)
    await _safe_reply(
        update,
        "🔴 *Close Trade — Select Position*\n\nChoose which position to close:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# /help — Full command reference
# ---------------------------------------------------------------------------


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (
        "🐺 *Senpi — Command Reference*\n\n"
        "*Monitor*\n"
        "/status — System snapshot\n"
        "/regime — Market regime classification\n"
        "/review — Portfolio health report\n"
        "/howl — Nightly self-improvement analysis\n"
        "/arena — Predator leaderboard\n\n"
        "*Execute*\n"
        "/jido — Autonomous executor\n"
        "/evaluate — Process scanner signals\n"
        "/whale — Copy-trade rebalance\n\n"
        "*Settings*\n"
        "/settings — View all rules, gates, and scores\n"
        "/set — Change a setting (usage: /set <key> <value>)\n\n"
        "*Safety*\n"
        "/flatten — Close all open positions\n"
        "/close — Close a specific position\n"
        "/emergency\\_stop — Immediate RISK\\_OFF\n\n"
        "_Any non-command text → Strategic Brain_"
    )
    await _safe_reply(update, text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /rules + /rules_set — User Sovereignty
# ---------------------------------------------------------------------------


@authorized
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    rules = load_json(USER_RULES_FILE, default={})
    if not rules:
        await _safe_reply(
            update,
            "⚠️ No user rules found.\n\n_Default rules will be created on next config load._",
        )
        return

    evaluate = rules.get("evaluate", {})
    jido = rules.get("jido", {})

    lines = [
        "📋 *User Rules*\n",
        "*Evaluate (Manual):*",
        f"  minScore: {evaluate.get('minScore', '?')}",
        f"  maxLeverage: {evaluate.get('maxLeverage', '?')}x",
        f"  maxPositions: {evaluate.get('maxPositions', '?')}",
        f"  cooldown: {evaluate.get('cooldownMinutes', '?')}min",
        "",
        "*Jido (Autonomous):*",
        f"  roi_threshold: {jido.get('roi_threshold_auto', '?')}",
        f"  minScore: {jido.get('minScore', '?')}",
        f"  autoExecute: {jido.get('autoExecuteEnabled', '?')}",
    ]

    fixed_tp = rules.get("fixed_tp_roe", {})
    tp_on = fixed_tp.get("enabled", False)
    lines.append("")
    lines.append(f"*Fixed TP ROE:* {'ON' if tp_on else 'OFF'}")
    if tp_on:
        lines.append(f"  tpRoePct: {fixed_tp.get('tpRoePct', '?')}%")

    fixed_sl = rules.get("fixed_sl_roe", {})
    sl_on = fixed_sl.get("enabled", False)
    lines.append(f"*Fixed SL ROE:* {'ON' if sl_on else 'OFF'}")
    if sl_on:
        lines.append(f"  slRoePct: {fixed_sl.get('slRoePct', '?')}%")

    partial_tp = rules.get("partial_tp", {})
    ptp_on = partial_tp.get("enabled", False)
    lines.append(f"*Partial TP:* {'ON' if ptp_on else 'OFF'}")
    if ptp_on:
        lines.append(
            f"  TP1: {partial_tp.get('tp1RoePct', '?')}% / close {partial_tp.get('tp1ClosePct', '?')}%\n"
            f"  TP2: {partial_tp.get('tp2RoePct', '?')}% / close {partial_tp.get('tp2ClosePct', '?')}%"
        )

    partial_sl = rules.get("partial_sl", {})
    psl_on = partial_sl.get("enabled", False)
    lines.append(f"*Partial SL:* {'ON' if psl_on else 'OFF'}")
    if psl_on:
        lines.append(
            f"  SL1: {partial_sl.get('sl1RoePct', '?')}% / close {partial_sl.get('sl1ClosePct', '?')}%\n"
            f"  SL2: {partial_sl.get('sl2RoePct', '?')}% / close {partial_sl.get('sl2ClosePct', '?')}%"
        )

    dsl = rules.get("dsl_override", {})
    dsl_on = dsl.get("enabled", False)
    lines.append(f"*DSL Override:* {'ON' if dsl_on else 'OFF'}")

    lines.append("")
    lines.append(
        f"Updated: {rules.get('updatedAt', '?')} by {rules.get('updatedBy', '?')}"
    )
    lines.append("")
    lines.append("_Use /rules\\_set <key> <value> to change._")

    await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")


RULES_KEY_MAP = {
    "jido_roi": ("jido", "roi_threshold_auto", float),
    "jido_minscore": ("jido", "minScore", int),
    "jido_auto": (
        "jido",
        "autoExecuteEnabled",
        lambda v: v.lower() in ("true", "1", "on"),
    ),
    "suguru_enabled": (
        "jido",
        "suguru_enabled",
        lambda v: v.lower() in ("true", "1", "on"),
    ),
    "suguru_maxlev": ("jido", "suguru_max_leverage", int),
    "suguru_maxmargin": ("jido", "suguru_max_margin_pct", float),
    "suguru_minconf": ("jido", "suguru_min_confidence", float),
    "eval_minscore": ("evaluate", "minScore", int),
    "eval_maxlev": ("evaluate", "maxLeverage", int),
    "eval_maxpos": ("evaluate", "maxPositions", int),
    "eval_cooldown": ("evaluate", "cooldownMinutes", int),
    "fixed_tp": ("fixed_tp_roe", "tpRoePct", float),
    "fixed_sl": ("fixed_sl_roe", "slRoePct", float),
    "partial_tp1": ("partial_tp", "tp1RoePct", float),
    "partial_tp1_pct": ("partial_tp", "tp1ClosePct", float),
    "partial_tp2": ("partial_tp", "tp2RoePct", float),
    "partial_tp2_pct": ("partial_tp", "tp2ClosePct", float),
    "partial_sl1": ("partial_sl", "sl1RoePct", float),
    "partial_sl1_pct": ("partial_sl", "sl1ClosePct", float),
    "partial_sl2": ("partial_sl", "sl2RoePct", float),
    "partial_sl2_pct": ("partial_sl", "sl2ClosePct", float),
}

RULES_CONFIRMATIONS = {
    "jido_roi": lambda v: f"Jido will now require {float(v):.0%} ROI before auto-executing.",
    "jido_minscore": lambda v: f"Jido minimum score set to {v}.",
    "jido_auto": lambda v: f"Jido auto-execute {'enabled' if v.lower() in ('true', '1', 'on') else 'disabled'}.",
    "suguru_enabled": lambda v: f"Suguru in Jido {'enabled' if v.lower() in ('true', '1', 'on') else 'disabled'}.",
    "suguru_maxlev": lambda v: f"Suguru max leverage set to {v}x.",
    "suguru_maxmargin": lambda v: f"Suguru max margin set to {float(v):.0f}%.",
    "suguru_minconf": lambda v: f"Suguru min confidence set to {float(v):.0%}.",
    "eval_minscore": lambda v: f"Manual evaluate minimum score set to {v}.",
    "eval_maxlev": lambda v: f"Manual evaluate max leverage set to {v}x (hardcoded 7-10x band still applies).",
    "eval_maxpos": lambda v: f"Manual evaluate max positions set to {v} (hardcoded 3-position cap still applies).",
    "eval_cooldown": lambda v: f"Manual evaluate cooldown set to {v} minutes.",
    "fixed_tp": lambda v: f"Fixed TP ROE set to {float(v):.1f}%.",
    "fixed_sl": lambda v: f"Fixed SL ROE set to {float(v):.1f}%.",
    "partial_tp1": lambda v: f"Partial TP1 at {float(v):.1f}% ROE.",
    "partial_tp1_pct": lambda v: f"Partial TP1 close amount set to {float(v):.0f}% of position.",
    "partial_tp2": lambda v: f"Partial TP2 at {float(v):.1f}% ROE.",
    "partial_tp2_pct": lambda v: f"Partial TP2 close amount set to {float(v):.0f}% of position.",
    "partial_sl1": lambda v: f"Partial SL1 at {float(v):.1f}% ROE.",
    "partial_sl1_pct": lambda v: f"Partial SL1 close amount set to {float(v):.0f}% of position.",
    "partial_sl2": lambda v: f"Partial SL2 at {float(v):.1f}% ROE.",
    "partial_sl2_pct": lambda v: f"Partial SL2 close amount set to {float(v):.0f}% of position.",
}

ENABLE_SECTIONS = {
    "fixed_tp_roe": "fixed_tp",
    "fixed_sl_roe": "fixed_sl",
    "partial_tp": "partial_tp",
    "partial_sl": "partial_sl",
}


async def _handle_rules_set(update: Update, key: str, value: str):
    """Core logic for setting a rules key. Used by both /rules_set and /set."""
    if key not in RULES_KEY_MAP:
        await _safe_reply(
            update,
            f"❌ Unknown key: `{key}`\n\nUse /set to see all keys.",
            parse_mode="Markdown",
        )
        return

    section, field, converter = RULES_KEY_MAP[key]
    try:
        converted = converter(value)
    except (ValueError, TypeError):
        await _safe_reply(
            update,
            f"❌ Invalid value: `{value}` for key `{key}`",
            parse_mode="Markdown",
        )
        return

    rules = load_json(USER_RULES_FILE, default={})
    if section not in rules:
        rules[section] = {}

    if section in ENABLE_SECTIONS:
        rules[section]["enabled"] = True

    rules[section][field] = converted
    rules["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rules["updatedBy"] = "telegram-bot"

    tmp = USER_RULES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(rules, f, indent=2)
        f.write("\n")
    tmp.rename(USER_RULES_FILE)

    git_sync_msg = ""
    try:
        sync_proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            "import sys; sys.path.insert(0,'scripts/lib'); "
            "import senpi_common as sc; "
            "sc.git_sync('strat: rule update via telegram')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=CHILD_ENV,
            cwd=str(STATE_DIR),
        )
        _, sync_err = await asyncio.wait_for(sync_proc.communicate(), timeout=30)
        if sync_err:
            git_sync_msg = f"\n\n⚠️ Git sync warning: {sync_err.decode().strip()[:200]}"
        else:
            git_sync_msg = "\n\n✅ Synced to GitHub."
    except Exception:
        git_sync_msg = "\n\n⚠️ Git sync failed (non-fatal)."

    confirmation_fn = RULES_CONFIRMATIONS.get(key)
    confirmation = confirmation_fn(value) if confirmation_fn else f"`{key}` → `{value}`"

    await _safe_reply(
        update,
        f"✅ {confirmation}\n\n"
        f"_Changes take effect on next Jido run (within 5 min)._\n"
        f"_Use /settings to verify._{git_sync_msg}",
        parse_mode="Markdown",
    )


@authorized
async def cmd_rules_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    args = context.args

    if not args or len(args) < 2:
        key_groups = [
            (
                "Evaluate (Manual)",
                [
                    ("eval_minscore", "int", "Minimum signal score"),
                    ("eval_maxlev", "int", "Max leverage (7-10x band hardcoded)"),
                    ("eval_maxpos", "int", "Max positions (3 hardcoded cap)"),
                    ("eval_cooldown", "int", "Cooldown minutes"),
                ],
            ),
            (
                "Jido (Autonomous)",
                [
                    ("jido_roi", "float", "Auto-execute ROI threshold (e.g. 0.20)"),
                    ("jido_minscore", "int", "Minimum signal score"),
                    ("jido_auto", "true/false", "Enable auto-execute"),
                ],
            ),
            (
                "Strategic Overrides",
                [
                    ("fixed_tp", "float", "Fixed TP ROE% (e.g. 20)"),
                    ("fixed_sl", "float", "Fixed SL ROE% (e.g. -15)"),
                    ("partial_tp1", "float", "Partial TP1 ROE%"),
                    ("partial_tp1_pct", "float", "Partial TP1 close %"),
                    ("partial_tp2", "float", "Partial TP2 ROE%"),
                    ("partial_tp2_pct", "float", "Partial TP2 close %"),
                    ("partial_sl1", "float", "Partial SL1 ROE%"),
                    ("partial_sl1_pct", "float", "Partial SL1 close %"),
                    ("partial_sl2", "float", "Partial SL2 ROE%"),
                    ("partial_sl2_pct", "float", "Partial SL2 close %"),
                ],
            ),
        ]
        lines = ["Usage: `/rules_set <key> <value>`\n"]
        for group_name, keys in key_groups:
            lines.append(f"*{group_name}:*")
            for k, t, d in keys:
                lines.append(f"  `{k}` ({t}) — {d}")
            lines.append("")
        await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")
        return

    await _handle_rules_set(update, args[0].lower(), args[1])


# ---------------------------------------------------------------------------
# /gates + /gates_set + /gates_reset — User-Configurable Safety Gates
# ---------------------------------------------------------------------------

GATES_KEY_MAP = {
    "max_positions": ("safety_gates", "maxPositionsTotal", int),
    "cooldown": ("safety_gates", "perAssetCooldownMinutes", int),
    "dir_cap": ("safety_gates", "directionalCapPct", int),
    "min_lev": ("safety_gates", "minLeverage", int),
    "max_lev": ("safety_gates", "maxLeverage", int),
    "banned_prefix": (
        "safety_gates",
        "bannedAssetPrefixes",
        lambda v: [p.strip() for p in v.split(",") if p.strip()],
    ),
    "score_orca": ("safety_gates:minScores", "orca", int),
    "score_mantis": ("safety_gates:minScores", "mantis", int),
    "score_fox": ("safety_gates:minScores", "fox", int),
    "score_komodo": ("safety_gates:minScores", "komodo", int),
    "score_condor": ("safety_gates:minScores", "condor", int),
    "score_polar": ("safety_gates:minScores", "polar", int),
    "score_sentinel": ("safety_gates:minScores", "sentinel", int),
    "score_rhino": ("safety_gates:minScores", "rhino", int),
}

GATES_BOUNDS = {
    "maxPositionsTotal": (1, 10, "1-10 positions"),
    "perAssetCooldownMinutes": (0, 1440, "0-1440 min (0=disabled)"),
    "directionalCapPct": (50, 100, "50-100%"),
    "minLeverage": (1, 50, "1-50x"),
    "maxLeverage": (1, 50, "1-50x"),
}

DEFAULT_MIN_SCORES = {
    "orca": 6,
    "mantis": 7,
    "fox": 7,
    "komodo": 10,
    "condor": 10,
    "polar": 10,
    "sentinel": 5,
    "rhino": 5,
}

DEFAULT_GUARDRAILS = {
    "dailyLossLimitPct": 10,
    "catastrophicDrawdownPct": 20,
    "maxConsecutiveStopOuts": 4,
    "directionalCapPct": 70,
    "minLeverage": 7,
    "maxLeverage": 10,
    "maxPositionsTotal": 3,
    "perAssetCooldownMinutes": 120,
    "bannedAssetPrefixes": ["xyz:"],
}


def _get_current_gates() -> dict:
    """Read effective gate values (defaults + user overrides)."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
    from senpi_common import load_global_guardrails, load_user_min_scores

    guardrails = load_global_guardrails()
    base_scores = dict(DEFAULT_MIN_SCORES)
    user_scores = load_user_min_scores()
    if user_scores:
        base_scores.update(user_scores)
    return {**guardrails, "minScores": base_scores}


def _get_user_overrides() -> dict:
    """Read only the user-overridden gate values from user-rules.json."""
    rules = load_json(USER_RULES_FILE, default={})
    return rules.get("safety_gates", {})


def _validate_gate(key: str, value) -> tuple[bool, str]:
    """Validate a gate value against bounds. Returns (ok, error_msg)."""
    bounds = GATES_BOUNDS.get(key)
    if bounds:
        lo, hi, _ = bounds
        if not isinstance(value, (int, float)):
            return False, "Must be a number"
        if not (lo <= value <= hi):
            return False, f"Out of range ({bounds[2]})"

    # Cross-field: min <= max leverage
    if key == "minLeverage":
        current = _get_current_gates()
        if value > current.get("maxLeverage", 50):
            return (
                False,
                f"min ({value}) > current max ({current['maxLeverage']}). Set max_lev first.",
            )
    if key == "maxLeverage":
        current = _get_current_gates()
        if value < current.get("minLeverage", 1):
            return (
                False,
                f"max ({value}) < current min ({current['minLeverage']}). Set min_lev first.",
            )

    return True, "ok"


@authorized
async def cmd_gates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    current = _get_current_gates()
    overrides = _get_user_overrides()
    user_scores = overrides.get("minScores", {})

    def _src(field: str, val) -> str:
        """Show if value differs from default. Escapes Telegram Markdown specials."""
        default_val = DEFAULT_GUARDRAILS.get(field)
        if default_val is not None and val != default_val:
            raw = f"{val}  ✏️ (default: {default_val})"
        else:
            raw = str(val)
        # Escape Telegram Markdown special chars in dynamic values
        return (
            raw.replace("_", "\\_")
            .replace("*", "\\*")
            .replace("[", "\\[")
            .replace("`", "\\`")
        )

    def _src_score(scanner: str, val) -> str:
        default_val = DEFAULT_MIN_SCORES.get(scanner)
        if default_val is not None and val != default_val:
            return f"{val}  ✏️ (default: {default_val})"
        return str(val)

    lines = [
        "🛡 *Safety Gates*\n",
        "*Gate 1: Entries Allowed*",
        "  REGIME-GATED  (automatic)\n",
        "*Gate 2: Auto-Entry*",
        "  REGIME + BRAIN  (automatic)\n",
        "*Gate 3: Valid Strategy*",
        "  REQUIRED  (automatic)\n",
        f"*Gate 4: Max Positions*",
        f"  {_src('maxPositionsTotal', current.get('maxPositionsTotal', 3))}\n",
        "*Gate 5: Scanner Blocked*",
        "  BRAIN POLICY  (automatic)\n",
        "*Gate 6: Min Score Thresholds*",
    ]
    for scanner in DEFAULT_MIN_SCORES:
        val = current.get("minScores", {}).get(scanner, DEFAULT_MIN_SCORES[scanner])
        lines.append(f"  {scanner}: {_src_score(scanner, val)}")
    lines.append("")

    banned = current.get("bannedAssetPrefixes", ["xyz:"])
    lines.append(f"*Gate 7: Banned Prefixes*")
    lines.append(f"  {', '.join(banned)}\n")

    lines.append(f"*Gate 8: Cooldown*")
    lines.append(
        f"  {_src('perAssetCooldownMinutes', current.get('perAssetCooldownMinutes', 120))} min\n"
    )

    lines.append(f"*Gate 9: Directional Cap*")
    lines.append(
        f"  {_src('directionalCapPct', current.get('directionalCapPct', 70))}%\n"
    )

    min_lev = current.get("minLeverage", 7)
    max_lev = current.get("maxLeverage", 10)
    lines.append(f"*Gate 10: Leverage Band*")
    lines.append(f"  {_src('minLeverage', min_lev)}-{_src('maxLeverage', max_lev)}x\n")

    lines.append("✏️ = user override (differs from default)")
    lines.append("Gates 1-3, 5 are automatic — controlled by regime/brain.")
    lines.append("\n_Use /gates\\_set <key> <value> to modify._")
    lines.append("_Use /gates\\_reset to restore all defaults._")

    await _safe_reply(update, "\n".join(lines))


async def _handle_gates_set(update: Update, key: str, value_str: str):
    """Core logic for setting a gates key. Used by both /gates_set and /set."""
    if key not in GATES_KEY_MAP:
        await _safe_reply(
            update,
            f"❌ Unknown key: `{key}`\n\nUse /set to see all keys.",
            parse_mode="Markdown",
        )
        return

    section_path, field, converter = GATES_KEY_MAP[key]

    try:
        converted = converter(value_str)
    except (ValueError, TypeError) as e:
        await _safe_reply(
            update,
            f"❌ Invalid value: `{value_str}` for key `{key}` ({e})",
            parse_mode="Markdown",
        )
        return

    # Validate bounds for top-level guardrails
    validate_field = field if section_path == "safety_gates" else None
    if validate_field:
        ok, err = _validate_gate(validate_field, converted)
        if not ok:
            await _safe_reply(
                update,
                f"❌ Invalid: {err}",
                parse_mode="Markdown",
            )
            return
    # Validate score bounds
    if section_path == "safety_gates:minScores":
        if not (1 <= converted <= 20):
            await _safe_reply(
                update,
                f"❌ Score must be 1-20, got {converted}",
                parse_mode="Markdown",
            )
            return

    # Write to user-rules.json
    rules = load_json(USER_RULES_FILE, default={})

    if ":" in section_path:
        # Nested path like safety_gates:minScores
        parts = section_path.split(":")
        target = rules
        for part in parts:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[field] = converted
    else:
        if section_path not in rules or not isinstance(rules.get(section_path), dict):
            rules[section_path] = {}
        rules[section_path][field] = converted

    rules["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rules["updatedBy"] = "telegram-gates"

    tmp = USER_RULES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(rules, f, indent=2)
        f.write("\n")
    tmp.rename(USER_RULES_FILE)

    # Git sync
    git_sync_msg = ""
    try:
        sync_proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            "import sys; sys.path.insert(0,'scripts/lib'); "
            "import senpi_common as sc; "
            "sc.git_sync('strat: gate update via telegram')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=CHILD_ENV,
            cwd=str(STATE_DIR),
        )
        _, sync_err = await asyncio.wait_for(sync_proc.communicate(), timeout=30)
        if sync_err:
            git_sync_msg = f"\n\n⚠️ Git sync warning: {sync_err.decode().strip()[:200]}"
        else:
            git_sync_msg = "\n\n✅ Synced to GitHub."
    except Exception:
        git_sync_msg = "\n\n⚠️ Git sync failed (non-fatal)."

    # Human-readable confirmation
    gate_name = key.replace("_", " ").title()
    await _safe_reply(
        update,
        f"✅ Gate updated: {gate_name} → {converted}\n\n"
        f"_Takes effect on next evaluate/jido run._\n"
        f"_Use /settings to verify._{git_sync_msg}",
        parse_mode="Markdown",
    )


@authorized
async def cmd_gates_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    args = context.args

    if not args or len(args) < 2:
        key_groups = [
            (
                "Positions & Exposure",
                [
                    ("max_positions", "int", "Max concurrent positions (1-10)"),
                    ("dir_cap", "int", "Directional exposure cap % (50-100)"),
                ],
            ),
            (
                "Leverage Band",
                [
                    ("min_lev", "int", "Minimum leverage (1-50x)"),
                    ("max_lev", "int", "Maximum leverage (1-50x)"),
                ],
            ),
            (
                "Timing & Bans",
                [
                    ("cooldown", "int", "Per-asset cooldown minutes (0=off, max 1440)"),
                    ("banned_prefix", "csv", "Banned asset prefixes (e.g. xyz:,test:)"),
                ],
            ),
            (
                "Per-Scanner Min Scores (1-20)",
                [
                    ("score_orca", "int", "ORCA minimum signal score"),
                    ("score_mantis", "int", "MANTIS minimum signal score"),
                    ("score_fox", "int", "FOX minimum signal score"),
                    ("score_komodo", "int", "KOMODO minimum signal score"),
                    ("score_condor", "int", "CONDOR minimum signal score"),
                    ("score_polar", "int", "POLAR minimum signal score"),
                    ("score_sentinel", "int", "SENTINEL minimum signal score"),
                    ("score_rhino", "int", "RHINO minimum signal score"),
                ],
            ),
        ]
        lines = ["Usage: `/gates_set <key> <value>`\n"]
        for group_name, keys in key_groups:
            lines.append(f"*{group_name}:*")
            for k, t, d in keys:
                lines.append(f"  `{k}` ({t}) — {d}")
            lines.append("")
        await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")
        return

    await _handle_gates_set(update, args[0].lower(), " ".join(args[1:]))


@authorized
async def cmd_gates_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    rules = load_json(USER_RULES_FILE, default={})
    had_overrides = "safety_gates" in rules and rules["safety_gates"]

    if not had_overrides:
        await _safe_reply(
            update,
            "ℹ️ No user gate overrides found — already at defaults.",
        )
        return

    rules.pop("safety_gates", None)
    rules["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rules["updatedBy"] = "telegram-gates-reset"

    tmp = USER_RULES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(rules, f, indent=2)
        f.write("\n")
    tmp.rename(USER_RULES_FILE)

    # Git sync
    git_sync_msg = ""
    try:
        sync_proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            "import sys; sys.path.insert(0,'scripts/lib'); "
            "import senpi_common as sc; "
            "sc.git_sync('strat: gates reset via telegram')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=CHILD_ENV,
            cwd=str(STATE_DIR),
        )
        _, sync_err = await asyncio.wait_for(sync_proc.communicate(), timeout=30)
        if sync_err:
            git_sync_msg = f"\n\n⚠️ Git sync warning: {sync_err.decode().strip()[:200]}"
        else:
            git_sync_msg = "\n\n✅ Synced to GitHub."
    except Exception:
        git_sync_msg = "\n\n⚠️ Git sync failed (non-fatal)."

    await _safe_reply(
        update,
        "🔄 All gate overrides removed — defaults restored.\n\n"
        "  Max positions: 3\n"
        "  Cooldown: 120 min\n"
        "  Directional cap: 70%\n"
        "  Leverage: 7-10x\n"
        "  Banned: xyz:*\n"
        f"_Use /gates to verify._{git_sync_msg}",
    )


# ---------------------------------------------------------------------------
# /settings + /set — Unified Settings View
# ---------------------------------------------------------------------------


@authorized
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = _build_settings_text()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Execution", callback_data="act:settings_help_execution"
                ),
                InlineKeyboardButton(
                    "✏️ Position Mgmt", callback_data="act:settings_help_position"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Gates", callback_data="act:settings_help_gates"
                ),
                InlineKeyboardButton(
                    "✏️ Scores", callback_data="act:settings_help_scores"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Reset Gates", callback_data="act:gates_reset_prompt"
                )
            ],
        ]
    )
    await _safe_reply(update, text, parse_mode="Markdown", reply_markup=keyboard)


def _build_settings_text() -> str:
    """Build unified settings view combining rules + gates + scores."""
    rules = load_json(USER_RULES_FILE, default={})
    ev = rules.get("evaluate", {})
    jido = rules.get("jido", {})

    lines = [f"⚙️ *Settings*\n"]

    # Execution
    lines.append("*EXECUTION*")
    auto = "ON" if jido.get("autoExecuteEnabled", True) else "OFF"
    lines.append(
        f"  Evaluate: minScore {ev.get('minScore', '?')} • maxLev {ev.get('maxLeverage', '?')}x • maxPos {ev.get('maxPositions', '?')} • cooldown {ev.get('cooldownMinutes', '?')}min"
    )
    roi_val = jido.get("roi_threshold_auto", "?")
    if isinstance(roi_val, (int, float)):
        roi_display = f"{roi_val:.0%}"
    else:
        roi_display = str(roi_val)
    lines.append(
        f"  Jido: ROI threshold {roi_display} • minScore {jido.get('minScore', '?')} • auto {auto}"
    )
    lines.append("")

    # Position management
    lines.append("*POSITION MANAGEMENT*")
    tp = rules.get("fixed_tp_roe", {})
    sl = rules.get("fixed_sl_roe", {})
    ptp = rules.get("partial_tp", {})
    psl = rules.get("partial_sl", {})
    dsl = rules.get("dsl_override", {})
    tp_str = (
        f"{tp.get('tpRoePct')}%" if tp.get("enabled") and tp.get("tpRoePct") else "OFF"
    )
    sl_str = (
        f"{sl.get('slRoePct')}%" if sl.get("enabled") and sl.get("slRoePct") else "OFF"
    )
    lines.append(f"  Fixed TP: {tp_str}  •  Fixed SL: {sl_str}")
    lines.append(
        f"  Partial TP: {'ON' if ptp.get('enabled') else 'OFF'}  •  Partial SL: {'ON' if psl.get('enabled') else 'OFF'}"
    )
    lines.append(f"  DSL Override: {'ON' if dsl.get('enabled') else 'OFF'}")
    lines.append("")

    # Safety gates
    current = _get_current_gates()
    lines.append("*SAFETY GATES*")
    lines.append(
        f"  Max positions: {current.get('maxPositionsTotal', 3)}  •  Cooldown: {current.get('perAssetCooldownMinutes', 120)}min"
    )
    lines.append(
        f"  Directional cap: {current.get('directionalCapPct', 70)}%  •  Leverage: {current.get('minLeverage', 7)}-{current.get('maxLeverage', 10)}x"
    )
    banned = current.get("bannedAssetPrefixes", ["xyz:"])
    lines.append(f"  Banned: {', '.join(banned)}")
    lines.append("")

    # Scanner scores
    scores = current.get("minScores", DEFAULT_MIN_SCORES)
    lines.append("*SCANNER SCORES*")
    score_parts = [f"{s}: {scores.get(s, v)}" for s, v in DEFAULT_MIN_SCORES.items()]
    # Split into two rows
    mid = len(score_parts) // 2
    lines.append(f"  {' • '.join(score_parts[:mid])}")
    lines.append(f"  {' • '.join(score_parts[mid:])}")
    lines.append("")

    updated = rules.get("updatedAt", "?")
    by = rules.get("updatedBy", "?")
    lines.append(f"_Updated: {updated} by {by}_")
    lines.append("\n_Use /set <key> <value> to change_")

    return "\n".join(lines)


@authorized
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified /set command — routes to rules or gates handler."""
    if not update.message:
        return
    args = context.args

    if not args or len(args) < 2:
        # Show all available keys grouped
        text = _build_set_help_text()
        await _safe_reply(update, text, parse_mode="Markdown")
        return

    key = args[0].lower()
    value = args[1]

    # Check rules keys first, then gates keys
    if key in RULES_KEY_MAP:
        await _handle_rules_set(update, key, value)
    elif key in GATES_KEY_MAP:
        await _handle_gates_set(update, key, " ".join(args[1:]))
    else:
        await _safe_reply(
            update,
            f"❌ Unknown key: `{key}`\n\nUse /set to see all available keys.",
            parse_mode="Markdown",
        )


def _build_set_help_text() -> str:
    """Build help text for /set showing all available keys."""
    return (
        "Usage: `/set <key> <value>`\n\n"
        "*Execution*\n"
        "  `eval_minscore` (int) — Evaluate min score\n"
        "  `eval_maxlev` (int) — Max leverage (7-10x band)\n"
        "  `eval_maxpos` (int) — Max positions (3 cap)\n"
        "  `eval_cooldown` (int) — Cooldown minutes\n"
        "  `jido_roi` (float) — Auto-execute ROI threshold\n"
        "  `jido_minscore` (int) — Jido min score\n"
        "  `jido_auto` (true/false) — Enable auto-execute\n\n"
        "*Suguru (in Jido)*\n"
        "  `suguru_enabled` (true/false) — Enable suguru in Jido\n"
        "  `suguru_maxlev` (int) — Max leverage\n"
        "  `suguru_maxmargin` (float) — Max margin %\n"
        "  `suguru_minconf` (float) — Min hermes confidence\n\n"
        "*Position Management*\n"
        "  `fixed_tp` (float) — Fixed TP ROE%\n"
        "  `fixed_sl` (float) — Fixed SL ROE%\n"
        "  `partial_tp1` (float) — Partial TP1 ROE%\n"
        "  `partial_tp1_pct` (float) — TP1 close %\n"
        "  `partial_tp2` (float) — Partial TP2 ROE%\n"
        "  `partial_tp2_pct` (float) — TP2 close %\n"
        "  `partial_sl1` (float) — Partial SL1 ROE%\n"
        "  `partial_sl1_pct` (float) — SL1 close %\n"
        "  `partial_sl2` (float) — Partial SL2 ROE%\n"
        "  `partial_sl2_pct` (float) — SL2 close %\n\n"
        "*Safety Gates*\n"
        "  `max_positions` (int) — Max concurrent positions\n"
        "  `dir_cap` (int) — Directional cap %\n"
        "  `min_lev` (int) — Min leverage\n"
        "  `max_lev` (int) — Max leverage\n"
        "  `cooldown` (int) — Per-asset cooldown min\n"
        "  `banned_prefix` (csv) — Banned asset prefixes\n\n"
        "*Scanner Scores*\n"
        "  `score_orca` (int) — ORCA min score\n"
        "  `score_mantis` (int) — MANTIS min score\n"
        "  `score_fox` (int) — FOX min score\n"
        "  `score_komodo` (int) — KOMODO min score\n"
        "  `score_condor` (int) — CONDOR min score\n"
        "  `score_polar` (int) — POLAR min score\n"
        "  `score_sentinel` (int) — SENTINEL min score\n"
        "  `score_rhino` (int) — RHINO min score"
    )


# ---------------------------------------------------------------------------
# Free text → Strategic Brain (Hermes Apollo)
# ---------------------------------------------------------------------------


def _strip_tui_artifacts(text: str) -> str:
    # Strip ANSI escape codes
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    lines = text.split("\n")
    cleaned = []
    in_tool_block = False
    for line in lines:
        stripped = line.strip()
        # Box-drawing borders (╭╮╰╯│┃║ etc.)
        if re.match(r"^[╭╮╰╯┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬│┃║┊┆¦\u2500─━═]", stripped):
            continue
        # Horizontal rules
        if re.match(r"^[\u2500─━═]{3,}$", stripped):
            continue
        # Tool execution lines from Hermes
        if re.match(r"^\[tool\]", stripped):
            in_tool_block = True
            continue
        if re.match(r"^\[done\]", stripped):
            in_tool_block = False
            continue
        if in_tool_block and re.match(r"^[┊┆¦]", stripped):
            continue
        if in_tool_block and stripped == "":
            in_tool_block = False
            continue
        in_tool_block = False
        # Hermes TUI metadata lines
        if re.match(r"^session_id:\s*", stripped):
            continue
        if re.match(r"^Hermes Agent\s+v?[\d.]+", stripped):
            continue
        if re.match(r"^(Available\s+)?Tools?:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Provider:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Model:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Loaded Skills?:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Worktree:\s*", stripped, re.IGNORECASE):
            continue
        # Kaomoji tool status lines (e.g. "(◕ᴗ◕✿) 💻 ...")
        if re.match(r"^\(.*[◕◡≧★ω٩۶].*\)", stripped):
            continue
        # Standalone emoji tool indicator lines
        if re.match(r"^[┊┆¦]\s*(📖|💻|⚡)", stripped):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    # Collapse excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


async def _call_hermes(message: str, timeout: int = 120) -> str:
    """Call hermes binary and return the response text."""
    hermes_bin = os.environ.get("HERMES_BIN_PATH", "/usr/local/bin/hermes")
    if not os.path.isfile(hermes_bin):
        hermes_bin = shutil.which("hermes") or ""
    if not hermes_bin:
        return "⚠️ Hermes binary not found."

    hermes_home = os.environ.get("HERMES_HOME", "/root/.hermes")
    hermes_model = os.environ.get("HERMES_MODEL", "").strip()
    hermes_provider = os.environ.get("HERMES_INFERENCE_PROVIDER", "zai").strip()

    glm_key = (
        os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    ).strip()
    glm_base = (
        os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    ).strip()

    env = {
        **CHILD_ENV,
        "HERMES_HOME": hermes_home,
        "HERMES_INFERENCE_PROVIDER": hermes_provider,
        "HERMES_MODEL": hermes_model,
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    if glm_key:
        env["GLM_API_KEY"] = glm_key
    if glm_base:
        env["GLM_BASE_URL"] = glm_base

    soul_path = CONFIG_DIR / "hermes-soul.md"
    if soul_path.exists():
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = soul_path.read_text()

    cmd_args = [hermes_bin, "chat", "-Q", "-q", message]
    if hermes_model:
        cmd_args += ["-m", hermes_model]
    if hermes_provider:
        cmd_args += ["--provider", hermes_provider]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(STATE_DIR),
        )
        stdout_raw, stderr_raw = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout_text = stdout_raw.decode().strip()
        stderr_text = stderr_raw.decode().strip()

        if proc.returncode != 0:
            return f"❌ Hermes error (rc={proc.returncode}): {stderr_text[:200]}"

        return stdout_text or stderr_text
    except asyncio.TimeoutError:
        return "⏱ Hermes timed out."
    except Exception as e:
        return f"❌ Hermes exception: {e}"


@authorized
async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        logger.warning("handle_free_text: update.message is None")
        return

    message = update.message.text.strip()
    if not message:
        return

    hermes_bin = os.environ.get("HERMES_BIN_PATH", "/usr/local/bin/hermes")
    if not os.path.isfile(hermes_bin):
        hermes_bin = shutil.which("hermes") or ""

    if not hermes_bin:
        await _safe_reply(
            update,
            "⚠️ *Brain not available*\n\n"
            "Hermes binary not found.\n"
            "Set `OPENAI\\_API\\_KEY` and `OPENAI\\_BASE\\_URL` for Hermes.",
            parse_mode="Markdown",
        )
        return

    logger.info("brain dispatch: hermes=%s query=%r", hermes_bin, message[:80])
    progress_msg = await _progress_reply(update, "🤖 Thinking...")

    hermes_home = os.environ.get("HERMES_HOME", "/root/.hermes")
    hermes_model = os.environ.get("HERMES_MODEL", "").strip()
    hermes_provider = os.environ.get("HERMES_INFERENCE_PROVIDER", "zai").strip()

    hermes_env_path = Path(hermes_home) / ".env"
    try:
        hermes_env_path.parent.mkdir(parents=True, exist_ok=True)
        env_lines = []
        if hermes_env_path.exists():
            for raw_line in hermes_env_path.read_text().splitlines():
                if not raw_line.startswith("GLM_"):
                    env_lines.append(raw_line)
        glm_key = (
            os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        glm_base = (
            os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
        ).strip()
        if glm_key:
            env_lines.append(f"GLM_API_KEY={glm_key}")
        if glm_base:
            env_lines.append(f"GLM_BASE_URL={glm_base}")
        hermes_env_path.write_text("\n".join(env_lines) + "\n")

        # Merge provider/model into config.yaml without clobbering Apollo overrides
        config_yaml_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml

            existing = {}
            if config_yaml_path.exists():
                existing = yaml.safe_load(config_yaml_path.read_text()) or {}
            model_block = existing.get("model", {})
            if not isinstance(model_block, dict):
                model_block = {}
            model_block["provider"] = hermes_provider
            if hermes_model:
                model_block["default"] = hermes_model
            if glm_base:
                model_block["base_url"] = glm_base
            existing["model"] = model_block
            config_yaml_path.write_text(yaml.safe_dump(existing, sort_keys=False))
        except Exception as yaml_err:
            logger.warning("Failed to update hermes config.yaml: %s", yaml_err)
    except Exception as e:
        logger.warning("Failed to sync GLM keys to hermes .env: %s", e)

    glm_key_env = (
        os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    ).strip()
    glm_base_env = (
        os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    ).strip()

    env = {
        **CHILD_ENV,
        "HERMES_HOME": hermes_home,
        "HERMES_INFERENCE_PROVIDER": hermes_provider,
        "HERMES_MODEL": hermes_model,
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    if glm_key_env:
        env["GLM_API_KEY"] = glm_key_env
    if glm_base_env:
        env["GLM_BASE_URL"] = glm_base_env

    soul_path = CONFIG_DIR / "hermes-soul.md"
    if soul_path.exists():
        env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = soul_path.read_text()

    cmd_args = [hermes_bin, "chat", "-Q", "-q", message]
    if hermes_model:
        cmd_args += ["-m", hermes_model]
    if hermes_provider:
        cmd_args += ["--provider", hermes_provider]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(STATE_DIR),
        )
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=120)
        stdout_text = stdout_raw.decode().strip()
        stderr_text = stderr_raw.decode().strip()
        returncode = proc.returncode

        logger.info(
            "brain result: rc=%d stdout_len=%d stderr_len=%d",
            returncode,
            len(stdout_text),
            len(stderr_text),
        )

        if returncode != 0:
            err_lines = stderr_text.splitlines() if stderr_text else []
            err_tail = (
                "\n".join(err_lines[-30:]) if err_lines else f"exit code {returncode}"
            )
            logger.error(
                "brain error: rc=%d stderr_tail=%s", returncode, err_tail[:2000]
            )
            reply_text = f"❌ Brain Error (rc={returncode})\n\n{err_tail[:3500]}"
            if progress_msg:
                try:
                    await progress_msg.edit_text(reply_text)
                except Exception:
                    await _safe_reply(update, reply_text)
            else:
                await _safe_reply(update, reply_text)
            return

        if not stdout_text:
            stderr_hint = f"\n\n_stderr: {stderr_text[:500]}_" if stderr_text else ""
            if stderr_text and not stdout_text:
                logger.error("brain empty output: stderr=%s", stderr_text[:300])
            reply_text = (
                f"⚠️ *Brain returned no output.*{stderr_hint}\n\n"
                f"_Check that `OPENAI_API_KEY` and `OPENAI_BASE_URL` "
                f"are set in Railway._"
            )
            if progress_msg:
                try:
                    await progress_msg.edit_text(reply_text, parse_mode="Markdown")
                except Exception:
                    await _safe_reply(update, reply_text, parse_mode="Markdown")
            else:
                await _safe_reply(update, reply_text, parse_mode="Markdown")
            return

        # Strip TUI artifacts from both streams
        stdout_clean = _strip_tui_artifacts(stdout_text)
        stderr_clean = _strip_tui_artifacts(stderr_text) if stderr_text else ""

        # Dedup step 1: hermes echoes output via both stdout and stderr.
        # If one stream's content is entirely contained in the other, use the longer one.
        if stdout_clean and stderr_clean:
            if stdout_clean in stderr_clean:
                output = stderr_clean
            elif stderr_clean in stdout_clean:
                output = stdout_clean
            else:
                output = stdout_clean
        else:
            output = stdout_clean or stderr_clean

        # Dedup step 2: hermes may echo the entire response twice in the same stream.
        # Check if the second half of the text is a near-exact repeat of the first half.
        text = output.strip()
        n = len(text)
        if n > 100:
            # Normalize for comparison: collapse all whitespace runs to single spaces
            import re as _re

            def _norm(s):
                return _re.sub(r"\s+", " ", s).strip().lower()

            # Check if the full text appears again starting from the midpoint region
            best_split = None
            for start_pct in range(30, 71, 5):
                check_from = n * start_pct // 100
                probe = text[: n // 2]
                probe_norm = _norm(probe)
                search = text[check_from:]
                search_norm = _norm(search)
                idx = search_norm.find(probe_norm)
                if idx >= 0:
                    # Found it — confirm the remainder matches
                    candidate = search[: len(probe) + 50]
                    if _norm(candidate).startswith(
                        probe_norm[: max(len(probe_norm) - 20, 50)]
                    ):
                        best_split = check_from
                        break
            if best_split:
                output = text[:best_split].strip()
            else:
                # Fallback: check if first half equals second half (exact repeat)
                mid = n // 2
                if n >= 200 and _norm(text[:mid]) == _norm(text[mid:]):
                    output = text[:mid].strip()

        # Dedup step 3: remove duplicate first non-empty lines
        lines = output.split("\n")
        non_empty = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]
        if len(non_empty) >= 2 and non_empty[0][1] == non_empty[1][1]:
            lines.pop(non_empty[1][0])
            output = "\n".join(lines)

        if len(output) > 4000:
            output = output[:3900] + "\n\n_(truncated)_"

        if stderr_text and returncode == 0:
            logger.warning("brain stderr (rc=0): %s", stderr_text[:500])

        reply_text = f"🤖 {output}"
        if progress_msg:
            try:
                await progress_msg.edit_text(reply_text)
            except Exception:
                await _safe_reply(update, reply_text)
        else:
            await _safe_reply(update, reply_text)

    except asyncio.TimeoutError:
        reply_text = "⏱ Brain timed out (120s limit).\n_Try a shorter query or retry._"
        if progress_msg:
            try:
                await progress_msg.edit_text(reply_text)
            except Exception:
                await _safe_reply(update, reply_text)
        else:
            await _safe_reply(update, reply_text)
    except Exception as e:
        logger.error("brain exception: %s", e, exc_info=True)
        reply_text = f"❌ Brain error: {e}"
        if progress_msg:
            try:
                await progress_msg.edit_text(reply_text)
            except Exception:
                await _safe_reply(update, reply_text)
        else:
            await _safe_reply(update, reply_text)


# ---------------------------------------------------------------------------
# Callback query handler — central router for all inline buttons
# ---------------------------------------------------------------------------


async def _run_waifu_and_edit(query, cmd: str, timeout: int = 120) -> None:
    """Run a waifu CLI command and edit the callback message with the result."""
    waifu_bin = shutil.which("waifu")
    if not waifu_bin:
        await _answer_and_edit(query, "❌ waifu-cli not found in PATH.")
        return
    await query.answer()
    await _safe_edit(query, f"⏳ Running `{cmd}`...", parse_mode="Markdown")
    output = await run_script_async([waifu_bin] + cmd.split(), timeout=timeout)
    if len(output) > 4000:
        output = output[:3900] + "\n\n_(truncated)_"
    await _safe_edit(query, f"```\n{output}\n```", parse_mode="Markdown")


async def _handle_action_callback(query, action: str) -> None:
    """Route act:* callbacks to their implementations."""
    if action == "emergency_stop_confirm":
        await _run_waifu_and_edit(query, "emergency-stop", timeout=120)

    elif action == "emergency_stop_cancel":
        await _answer_and_edit(query, "✅ Emergency stop cancelled.")

    # --- SUGURU ---
    elif action == "suguru_scan_menu":
        regime = load_json(CONFIG_DIR / "risk-regime.json")
        mode = regime.get("riskMode", "UNKNOWN")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔍 Scan Only", callback_data="act:suguru_scan_only"
                    ),
                    InlineKeyboardButton(
                        "🧠 Hermes Scan", callback_data="act:suguru_hermes_scan"
                    ),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="act:suguru_cancel")],
            ]
        )
        await _answer_and_edit(
            query,
            f"⚡ *Suguru — Elite Scanner*\n\n"
            f"Regime: *{mode}*\n\n"
            f"🔍 *Scan Only* — show scored candidates\n"
            f"🧠 *Hermes Scan* — scan + AI deliberation → trade recommendation",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    elif action == "suguru_scan_only":
        await _safe_edit(query, "🔍 Scanning markets...")
        await run_script_async(
            ["python3", str(STATE_DIR / "scripts/vps/suguru.py"), "--scan-only"],
            timeout=120,
        )
        scan = load_json(OUTPUTS_DIR / "suguru-candidates.json", default={})
        cands = scan.get("candidates", [])
        if not cands:
            await _safe_edit(
                query, "🔍 *Suguru Scan*\n\nNo candidates found.", parse_mode="Markdown"
            )
            return
        lines = [f"🔍 *Suguru Scan — {len(cands)} candidates*\n"]
        for i, c in enumerate(cands[:5]):
            scores = c.get("sub_scores", {})
            lines.append(
                f"{i + 1}. *{c['direction']} {c['asset']}* GSS={c['gss']:.2f}\n"
                f"   px={c['entry_price']} lev={c['leverage']}x "
                f"netRR={c['net_rr']:.2f} risk={c['risk_pct']:.1f}%\n"
                f"   confluence={scores.get('scanner_confluence', 0):.2f} "
                f"whale={scores.get('SM_whale_bias', 0):.2f}"
            )
        await _safe_edit(query, "\n".join(lines), parse_mode="Markdown")

    elif action == "suguru_hermes_scan":
        await _safe_edit(query, "🔍 Scanning...")
        await run_script_async(
            ["python3", str(STATE_DIR / "scripts/vps/suguru.py"), "--scan-only"],
            timeout=120,
        )
        scan = load_json(OUTPUTS_DIR / "suguru-candidates.json", default={})
        cands = scan.get("candidates", [])
        if not cands:
            await _safe_edit(
                query, "🧠 *Suguru Scan*\n\nNo candidates found.", parse_mode="Markdown"
            )
            return

        # Build prompt for waifu to choose best candidate
        cand_lines = []
        for i, c in enumerate(cands, 1):
            cand_lines.append(
                f"{i}. {c.get('direction', '?')} {c.get('asset', '?')} "
                f"@ ${c.get('price', 0):.2f} lev={c.get('leverage', 0)}x "
                f"GSS={c.get('gss', 0):.2f} netRR={c.get('netRr', 0):.2f}"
            )
        candidates_text = "\n".join(cand_lines)

        prompt = (
            f"You are a trading strategy advisor. Analyze these {len(cands)} candidate trades "
            f"and recommend the BEST one to execute. Consider GSS score, netRR, regime alignment, "
            f"and risk/reward.\n\n"
            f"Candidates:\n{candidates_text}\n\n"
            f"Respond with exactly this format (no other text):\n"
            f"RECOMMEND: [BUY/SELL] [ASSET] @ [PRICE] [LEVERAGE]x\n"
            f"REASON: [2-3 sentence explanation]\n"
            f"CONFIDENCE: [0-100%]"
        )

        await _safe_edit(query, "🧠 Waifu deciding...")
        output = await run_script_async(
            ["python3", "-m", "waifu_cli", "regime"],
            timeout=30,
        )
        regime = load_json(CONFIG_DIR / "risk-regime.json", default={})
        mode = regime.get("riskMode", "BASELINE")

        # Use hermes to decide - build a message for handle_free_text style flow
        import json as _json

        suggestion = f"{prompt}\n\nCurrent regime: {mode}"
        output = await _call_hermes(suggestion)
        output_clean = _strip_tui_artifacts(output) if output else ""

        # Parse hermes response for RECOMMEND line
        recommended = None
        reasoning = "No clear signal"
        confidence = 30

        if output_clean:
            for line in output_clean.split("\n"):
                line = line.strip()
                if line.upper().startswith("RECOMMEND:"):
                    try:
                        parts = line[10:].strip().split()
                        if len(parts) >= 3:
                            direction = parts[0].upper()
                            asset = parts[1].upper()
                            # Find matching candidate
                            for c in cands:
                                if c.get("asset", "").upper() == asset:
                                    recommended = c
                                    break
                            if not recommended:
                                recommended = cands[0]
                    except:
                        recommended = cands[0] if cands else None
                elif line.upper().startswith("REASON:"):
                    reasoning = line[7:].strip()
                elif line.upper().startswith("CONFIDENCE:"):
                    try:
                        conf_str = line[11:].strip().replace("%", "")
                        confidence = int(conf_str)
                    except:
                        pass

        if not recommended and cands:
            recommended = cands[0]
            reasoning = "No clear signal from waifu - defaulting to top candidate"
            confidence = 30

        if recommended:
            rec = recommended
            text = (
                f"🧠 *Waifu Recommends*\n\n"
                f"*{rec.get('direction', 'LONG')} {rec.get('asset', '?')}*\n"
                f"Confidence: {confidence}%\n\n"
                f"Entry: ${rec.get('price', 0):.2f} | "
                f"Leverage: {rec.get('leverage', 0)}x | "
                f"NetRR: {rec.get('netRr', 0):.2f}\n\n"
                f"_{reasoning}_"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Approve", callback_data="act:suguru_approve"
                        ),
                        InlineKeyboardButton(
                            "❌ Reject", callback_data="act:suguru_reject"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "💬 Chat to customize", callback_data="act:suguru_chat"
                        )
                    ],
                ]
            )
            await _safe_edit(query, text, reply_markup=keyboard)
        else:
            await _safe_edit(
                query,
                f"🧠 *No Trade*\n\n_Waifu found no strong signals._",
                parse_mode="Markdown",
            )

    elif action == "suguru_approve":
        await _safe_edit(query, "⚡ Executing approved trade...")
        output = await run_script_async(
            ["python3", str(STATE_DIR / "scripts/vps/suguru.py"), "--execute-approved"],
            timeout=120,
        )
        await _safe_edit(
            query,
            f"✅ *Suguru trade executed*\n\n```\n{output[:3000]}\n```",
            parse_mode="Markdown",
        )

    elif action == "suguru_reject":
        await _safe_edit(query, "❌ Trade rejected.")

    elif action == "suguru_chat":
        await _safe_edit(
            query,
            "💬 *Chat with Suguru*\n\n"
            "Type your next message to customize the trade.\n"
            "_Your next message goes to the Strategic Brain._",
            parse_mode="Markdown",
        )

    elif action == "suguru_cancel":
        await _safe_edit(query, "✅ Suguru cancelled.")

    # --- JIDO (unchanged) ---
    elif action == "jido_confirm":
        await _run_waifu_and_edit(query, "jido", timeout=120)

    elif action == "jido_dry":
        await _run_waifu_and_edit(query, "jido --dry-run", timeout=120)

    elif action == "jido_cancel":
        await _answer_and_edit(query, "✅ Jido cancelled.")

    elif action == "jido_prompt":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ Run Jido", callback_data="act:jido_confirm"
                    ),
                    InlineKeyboardButton("🔍 Dry Run", callback_data="act:jido_dry"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="act:jido_cancel")],
            ]
        )
        await _answer_and_edit(
            query,
            "🔮 *Jido — Autonomous Executor*\n\nChoose execution mode:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "evaluate_confirm":
        await _run_waifu_and_edit(query, "evaluate", timeout=120)

    elif action == "evaluate_dry":
        await _run_waifu_and_edit(query, "evaluate --dry-run", timeout=120)

    elif action == "evaluate_cancel":
        await _answer_and_edit(query, "✅ Evaluate cancelled.")

    elif action == "evaluate_prompt":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ Execute", callback_data="act:evaluate_confirm"
                    ),
                    InlineKeyboardButton(
                        "🔍 Dry Run", callback_data="act:evaluate_dry"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data="act:evaluate_cancel"
                    )
                ],
            ]
        )
        await _answer_and_edit(
            query,
            "⚡ *Evaluate — Signal Processor*\n\nChoose execution mode:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "status_refresh":
        waifu_bin = shutil.which("waifu")
        if not waifu_bin:
            await _answer_and_edit(query, "❌ waifu-cli not found in PATH.")
            return
        await query.answer()
        await _safe_edit(query, "⏳ Refreshing status...", parse_mode="Markdown")
        output = await run_script_async([waifu_bin, "status"], timeout=60)
        if len(output) > 3800:
            output = output[:3700] + "\n\n_(truncated)_"
        keyboard = _build_status_keyboard()
        await _safe_edit(
            query,
            f"```\n{output}\n```",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    elif action == "review_run":
        await _run_waifu_and_edit(query, "review", timeout=120)

    elif action == "gates_view":
        waifu_bin = shutil.which("waifu")
        if not waifu_bin:
            await _answer_and_edit(query, "❌ waifu-cli not found in PATH.")
            return
        await query.answer()
        await _safe_edit(query, "⏳ Loading gates...", parse_mode="Markdown")
        output = await run_script_async([waifu_bin, "gates"], timeout=60)
        if len(output) > 4000:
            output = output[:3900] + "\n\n_(truncated)_"
        await _safe_edit(query, f"```\n{output}\n```", parse_mode="Markdown")

    elif action == "settings_view":
        text = _build_settings_text()
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Execution", callback_data="act:settings_help_execution"
                    ),
                    InlineKeyboardButton(
                        "✏️ Position Mgmt", callback_data="act:settings_help_position"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Gates", callback_data="act:settings_help_gates"
                    ),
                    InlineKeyboardButton(
                        "✏️ Scores", callback_data="act:settings_help_scores"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Reset Gates", callback_data="act:gates_reset_prompt"
                    )
                ],
            ]
        )
        await _answer_and_edit(
            query, text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif action == "settings_help_execution":
        text = (
            "✏️ *Execution Keys*\n\n"
            "`/set eval_minscore <int>` — Evaluate min score\n"
            "`/set eval_maxlev <int>` — Max leverage\n"
            "`/set eval_maxpos <int>` — Max positions\n"
            "`/set eval_cooldown <int>` — Cooldown minutes\n"
            "`/set jido_roi <float>` — ROI threshold\n"
            "`/set jido_minscore <int>` — Jido min score\n"
            "`/set jido_auto <true/false>` — Auto-execute"
        )
        await _answer_and_edit(query, text, parse_mode="Markdown")

    elif action == "settings_help_position":
        text = (
            "✏️ *Position Management Keys*\n\n"
            "`/set fixed_tp <float>` — Fixed TP ROE%\n"
            "`/set fixed_sl <float>` — Fixed SL ROE%\n"
            "`/set partial_tp1 <float>` — Partial TP1 ROE%\n"
            "`/set partial_tp1_pct <float>` — TP1 close %\n"
            "`/set partial_tp2 <float>` — Partial TP2 ROE%\n"
            "`/set partial_tp2_pct <float>` — TP2 close %\n"
            "`/set partial_sl1 <float>` — Partial SL1 ROE%\n"
            "`/set partial_sl1_pct <float>` — SL1 close %\n"
            "`/set partial_sl2 <float>` — Partial SL2 ROE%\n"
            "`/set partial_sl2_pct <float>` — SL2 close %"
        )
        await _answer_and_edit(query, text, parse_mode="Markdown")

    elif action == "settings_help_gates":
        text = (
            "✏️ *Safety Gate Keys*\n\n"
            "`/set max_positions <int>` — Max concurrent positions\n"
            "`/set dir_cap <int>` — Directional cap %\n"
            "`/set min_lev <int>` — Min leverage\n"
            "`/set max_lev <int>` — Max leverage\n"
            "`/set cooldown <int>` — Per-asset cooldown min\n"
            "`/set banned_prefix <csv>` — Banned asset prefixes"
        )
        await _answer_and_edit(query, text, parse_mode="Markdown")

    elif action == "settings_help_scores":
        text = (
            "✏️ *Scanner Score Keys*\n\n"
            "`/set score_orca <int>` — ORCA min score\n"
            "`/set score_mantis <int>` — MANTIS min score\n"
            "`/set score_fox <int>` — FOX min score\n"
            "`/set score_komodo <int>` — KOMODO min score\n"
            "`/set score_condor <int>` — CONDOR min score\n"
            "`/set score_polar <int>` — POLAR min score\n"
            "`/set score_sentinel <int>` — SENTINEL min score\n"
            "`/set score_rhino <int>` — RHINO min score"
        )
        await _answer_and_edit(query, text, parse_mode="Markdown")

    elif action == "gates_reset_prompt":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Confirm Reset", callback_data="act:gates_reset_confirm"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data="act:settings_view"
                    ),
                ],
            ]
        )
        await _answer_and_edit(
            query,
            "🔄 *Reset all gate overrides to defaults?*\n\n"
            "This will restore:\n"
            "  Max positions: 3\n"
            "  Cooldown: 120 min\n"
            "  Directional cap: 70%\n"
            "  Leverage: 7-10x\n"
            "  Banned: xyz:\\*",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "gates_reset_confirm":
        rules = load_json(USER_RULES_FILE, default={})
        had_overrides = "safety_gates" in rules and rules["safety_gates"]
        if not had_overrides:
            await _answer_and_edit(
                query, "ℹ️ No user gate overrides found — already at defaults."
            )
            return
        rules.pop("safety_gates", None)
        rules["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rules["updatedBy"] = "telegram-gates-reset"
        tmp = USER_RULES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(rules, f, indent=2)
            f.write("\n")
        tmp.rename(USER_RULES_FILE)
        await _answer_and_edit(
            query, "✅ All gate overrides removed — defaults restored."
        )

    elif action == "status_run":
        waifu_bin = shutil.which("waifu")
        if not waifu_bin:
            await _answer_and_edit(query, "❌ waifu-cli not found in PATH.")
            return
        await query.answer()
        await _safe_edit(query, "⏳ Loading status...", parse_mode="Markdown")
        output = await run_script_async([waifu_bin, "status"], timeout=60)
        if len(output) > 3800:
            output = output[:3700] + "\n\n_(truncated)_"
        keyboard = _build_status_keyboard()
        await _safe_edit(
            query,
            f"```\n{output}\n```",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    elif action == "regime_run":
        await _run_waifu_and_edit(query, "regime", timeout=60)

    elif action == "whale_run":
        await _run_waifu_and_edit(query, "whale", timeout=120)

    elif action == "emergency_prompt":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚨 CONFIRM", callback_data="act:emergency_stop_confirm"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data="act:emergency_stop_cancel"
                    ),
                ],
            ]
        )
        await _answer_and_edit(
            query,
            "🚨 *Emergency Stop Confirmation*\n\n"
            "This will:\n"
            "• Set regime to RISK\\_OFF\n"
            "• Block all new entries\n"
            "• Send Telegram alert\n"
            "• Existing positions stay open (managed by DSL)\n\n"
            "_Are you sure?_",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "flatten_confirm":
        await query.answer()
        await _safe_edit(query, "⏳ Closing all positions...")
        _, positions = _count_open_positions()
        if not positions:
            await _safe_edit(query, "ℹ️ No open positions found.")
            return
        results = []
        for pos in positions:
            asset = pos.get("asset", "?")
            strat_key = pos.get("_key", "")
            strategy_id = pos.get("strategyId", "")
            close_script = (
                "import sys; sys.path.insert(0,'scripts/lib'); "
                "import senpi_common as sc; "
                f"r = sc.mcporter_call('strategy_close_position', "
                f"{{'strategyId': '{strategy_id}', 'asset': '{asset}'}}, timeout=15); "
                "print('OK' if 'error' not in r else f\"FAIL:{r.get('error')}\")"
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3",
                    "-c",
                    close_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=CHILD_ENV,
                    cwd=str(STATE_DIR),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                result = stdout.decode().strip()
                if result.startswith("OK"):
                    results.append(f"✅ {asset} — closed")
                    _deactivate_dsl_state(pos, "user_flatten")
                else:
                    results.append(f"❌ {asset} — {result}")
            except asyncio.TimeoutError:
                results.append(f"❌ {asset} — timeout")
            except Exception as e:
                results.append(f"❌ {asset} — {e}")
        await _safe_edit(
            query,
            "🔴 *Flatten Results*\n\n" + "\n".join(results),
            parse_mode="Markdown",
        )

    elif action == "flatten_cancel":
        await _answer_and_edit(query, "✅ Flatten cancelled.")

    elif action == "close_cancel":
        await _answer_and_edit(query, "✅ Close cancelled.")

    elif action.startswith("close_single_confirm:"):
        parts = action.split(":", 2)
        if len(parts) < 3:
            await _answer_and_edit(query, "❌ Invalid close action.")
            return
        strat_key, asset = parts[1], parts[2]
        await query.answer()
        await _safe_edit(query, f"⏳ Closing {asset}...")
        _, positions = _count_open_positions()
        target = None
        for pos in positions:
            if pos.get("_key") == strat_key and pos.get("asset") == asset:
                target = pos
                break
        if not target:
            await _safe_edit(
                query, f"ℹ️ Position {asset} not found (may already be closed)."
            )
            return
        strategy_id = target.get("strategyId", "")
        close_script = (
            "import sys; sys.path.insert(0,'scripts/lib'); "
            "import senpi_common as sc; "
            f"r = sc.mcporter_call('strategy_close_position', "
            f"{{'strategyId': '{strategy_id}', 'asset': '{asset}'}}, timeout=15); "
            "print('OK' if 'error' not in r else f\"FAIL:{r.get('error')}\")"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "-c",
                close_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=CHILD_ENV,
                cwd=str(STATE_DIR),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            result = stdout.decode().strip()
            if result.startswith("OK"):
                _deactivate_dsl_state(target, "user_close")
                await _safe_edit(query, f"✅ Closed {asset}")
            else:
                await _safe_edit(query, f"❌ Failed to close {asset}\n{result}")
        except asyncio.TimeoutError:
            await _safe_edit(query, f"❌ Close {asset} timed out")
        except Exception as e:
            await _safe_edit(query, f"❌ Close {asset} error: {e}")

    elif action.startswith("close_single:"):
        parts = action.split(":", 2)
        if len(parts) < 3:
            await _answer_and_edit(query, "❌ Invalid close action.")
            return
        strat_key, asset = parts[1], parts[2]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔴 CONFIRM",
                        callback_data=f"act:close_single_confirm:{strat_key}:{asset}",
                    ),
                    InlineKeyboardButton("❌ Cancel", callback_data="act:close_cancel"),
                ],
            ]
        )
        await _answer_and_edit(
            query,
            f"🔴 *Close {asset}?*\n\n_This will close the position immediately._",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "flatten_prompt":
        _, positions = _count_open_positions()
        if not positions:
            await _answer_and_edit(query, "ℹ️ No open positions to close.")
            return
        pos_lines = []
        for pos in positions:
            asset = pos.get("asset", "?")
            direction = pos.get("direction", "?")
            roe = float(pos.get("currentRoe", 0) or 0)
            pos_lines.append(f"  • {asset} {direction} ({roe:+.1f}%)")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔴 CLOSE ALL", callback_data="act:flatten_confirm"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data="act:flatten_cancel"
                    ),
                ],
            ]
        )
        await _answer_and_edit(
            query,
            f"🔴 *Flatten — Close All Positions*\n\n"
            f"{len(positions)} open position(s):\n" + "\n".join(pos_lines) + "\n\n"
            f"_This will close ALL positions immediately._",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "close_prompt":
        _, positions = _count_open_positions()
        if not positions:
            await _answer_and_edit(query, "ℹ️ No open positions to close.")
            return
        buttons = []
        for pos in positions:
            asset = pos.get("asset", "?")
            direction = pos.get("direction", "?")
            roe = float(pos.get("currentRoe", 0) or 0)
            strat_key = pos.get("_key", "")
            label = f"{asset} {direction} ({roe:+.1f}%)"
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔴 {label}",
                        callback_data=f"act:close_single:{strat_key}:{asset}",
                    )
                ]
            )
        buttons.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="act:close_cancel")]
        )
        keyboard = InlineKeyboardMarkup(buttons)
        await _answer_and_edit(
            query,
            "🔴 *Close Trade — Select Position*\n\nChoose which position to close:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    else:
        await _answer_and_edit(query, f"⚠️ Unknown action: {action}")


@authorized
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Central callback router — dispatches by prefix."""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""

    if data == "noop":
        await query.answer()
        return

    if data.startswith("act:"):
        action = data[4:]
        await _handle_action_callback(query, action)
    else:
        await query.answer()
        logger.warning("unhandled callback_data: %s", data)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------


def create_bot_application() -> Optional[Application]:
    """Create and configure the Telegram bot. Returns None if token not set."""
    if not TELEGRAM_BOT_TOKEN:
        return None

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("jido", cmd_jido))
    app.add_handler(CommandHandler("evaluate", cmd_evaluate))
    app.add_handler(CommandHandler("regime", cmd_regime))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("howl", cmd_howl))
    app.add_handler(CommandHandler("whale", cmd_whale))
    app.add_handler(CommandHandler("arena", cmd_arena))
    app.add_handler(CommandHandler("suguru", cmd_suguru))
    app.add_handler(CommandHandler("flatten", cmd_flatten))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    # Backward-compatible aliases
    app.add_handler(CommandHandler("gates", cmd_gates))
    app.add_handler(CommandHandler("gates_set", cmd_gates_set))
    app.add_handler(CommandHandler("gates_reset", cmd_gates_reset))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rules_set", cmd_rules_set))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))

    # Callback query handler — must be AFTER all CommandHandlers
    app.add_handler(CallbackQueryHandler(handle_callback))

    return app


async def start_polling(app: Application):
    """Start the bot polling loop and register command menu with BotFather."""
    await app.initialize()
    await app.start()

    # Register command menu — appears when user types "/" in the chat
    bot_commands = [BotCommand(cmd, short) for cmd, short, _ in COMMANDS]
    try:
        await app.bot.set_my_commands(bot_commands)
        cmd_names = ", ".join(cmd for cmd, _, _ in COMMANDS)
        logger.info("Telegram commands registered: %s", cmd_names)
    except Exception as e:
        logger.error("Failed to register Telegram commands: %s", e)

    await app.updater.start_polling(drop_pending_updates=True)


async def stop_polling(app: Application):
    """Stop the bot gracefully."""
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
