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
    (
        "jido",
        "Run autonomous executor",
        "High-conviction trades via the in-container brain policy.",
    ),
    (
        "evaluate",
        "Process signals",
        "HITL evaluation of queued scanner signals.",
    ),
    (
        "rules",
        "View strategic ceiling",
        "ROI and safety rules for evaluate (Manual) and jido (Autonomous).",
    ),
    (
        "rules_set",
        "Update strategic rules",
        "Usage: /rules_set <key> <value>",
    ),
    (
        "regime",
        "BTC/ETH macro classification",
        "Active regime, parameters, guardrails, and reason.",
    ),
    (
        "review",
        "Portfolio health report",
        "Equity, drawdown, daily PnL, dead-weight detection, guardrail alerts.",
    ),
    (
        "howl",
        "Last nightly self-improvement",
        "HOWL analysis: win rates, scanner comparison, fee drag, arena benchmarking.",
    ),
    (
        "whale",
        "Mirror-trade rebalance",
        "Copy-trade portfolio status and rebalance actions.",
    ),
    (
        "arena",
        "Predator leaderboard",
        "Top predator strategies, winning/losing traits, recommendations.",
    ),
    (
        "emergency_stop",
        "Immediate RISK_OFF",
        "Block all entries and send Telegram alert.",
    ),
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


# ---------------------------------------------------------------------------
# /start — Onboarding
# ---------------------------------------------------------------------------


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (
        "🐺 *Senpi — Strategic Remote*\n\n"
        "This bot is a pure remote for the waifu-cli strategic suite.\n\n"
        "⚙️ *Mechanical Layer* (Railway)\n"
        "Scanners, DSL trailing stops, Risk Arbiter. No LLM.\n\n"
        "🧠 *Strategic Layer*\n"
        "Powered by local Hermes Apollo agent on Railway.\n\n"
        "_Send any non-command text to talk to the Strategic Brain._\n\n"
        "Use /help to see all commands."
    )
    await _safe_reply(update, text, parse_mode="Markdown")


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
    await _waifu_cli(update, "status")


@authorized
async def cmd_jido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "jido", timeout=120)


@authorized
async def cmd_evaluate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dry = "--dry-run" if context.args and "--dry-run" in context.args else ""
    await _waifu_cli(update, f"evaluate {dry}".strip(), timeout=120)


@authorized
async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "regime")


@authorized
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "review", timeout=120)


@authorized
async def cmd_howl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "howl", timeout=120)


@authorized
async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "whale", timeout=120)


@authorized
async def cmd_arena(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _waifu_cli(update, "arena", timeout=120)


@authorized
async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await _safe_reply(
        update, "🚨 *Triggering emergency stop...*", parse_mode="Markdown"
    )
    await _waifu_cli(update, "emergency-stop", timeout=120)


# ---------------------------------------------------------------------------
# /help — Full command reference
# ---------------------------------------------------------------------------


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    lines = ["🐺 *Senpi — Strategic Suite*\n"]
    for cmd_name, short_desc, detail in COMMANDS:
        lines.append(f"/{cmd_name} — {detail}")
    lines.append(
        "\n_Any non-command text is sent to the Strategic Brain (Hermes Apollo)._\n"
        "_The Brain can read state, modify user-rules.json, and push changes to GitHub._"
    )
    await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")


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

    key = args[0].lower()
    value = args[1]

    if key not in RULES_KEY_MAP:
        await _safe_reply(
            update,
            f"❌ Unknown key: `{key}`\n\nUse /rules\\_set without values to see all keys.",
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
        f"_Use /rules to verify._{git_sync_msg}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Free text → Strategic Brain (Hermes Apollo)
# ---------------------------------------------------------------------------


def _strip_tui_artifacts(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^[\u2500─━═]{3,}$", stripped):
            continue
        if re.match(r"^[\u2502│┃║┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬]", stripped):
            continue
        if re.match(r"^session_id:\s*", stripped):
            continue
        if re.match(r"^Hermes Agent\s+v?[\d.]+", stripped):
            continue
        if re.match(r"^Available Tools?:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Tools?:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Provider:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Model:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Loaded Skills?:\s*", stripped, re.IGNORECASE):
            continue
        if re.match(r"^Worktree:\s*", stripped, re.IGNORECASE):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    leading = 0
    for line in result.split("\n"):
        if line.strip() == "":
            leading += 1
        else:
            break
    if leading:
        result = (
            result.split("\n", leading)[-1]
            if leading < len(result.split("\n"))
            else result
        )
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


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
    await _safe_reply(update, "🧠 Thinking...")

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
        glm_key = (os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")).strip()
        glm_base = (os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")).strip()
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

    glm_key_env = (os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")).strip()
    glm_base_env = (os.environ.get("GLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")).strip()

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
            err_detail = (
                stderr_text[:2000] if stderr_text else f"exit code {returncode}"
            )
            logger.error("brain error: rc=%d stderr=%s", returncode, err_detail[:2000])
            await _safe_reply(
                update,
                f"❌ Brain Error (rc={returncode})\n\n{err_detail}",
            )
            return

        if not stdout_text:
            stderr_hint = f"\n\n_stderr: {stderr_text[:500]}_" if stderr_text else ""
            if stderr_text and not stdout_text:
                logger.error("brain empty output: stderr=%s", stderr_text[:300])
            await _safe_reply(
                update,
                f"⚠️ *Brain returned no output.*{stderr_hint}\n\n"
                f"_Check that `OPENAI_API_KEY` and `OPENAI_BASE_URL` "
                f"are set in Railway._",
                parse_mode="Markdown",
            )
            return

        output = _strip_tui_artifacts(stdout_text)
        if len(output) > 4000:
            output = output[:3900] + "\n\n_(truncated)_"

        if stderr_text and returncode == 0:
            logger.warning("brain stderr (rc=0): %s", stderr_text[:500])

        await _safe_reply(
            update,
            f"🧠 {output}",
        )

    except asyncio.TimeoutError:
        await _safe_reply(
            update,
            "⏱ Brain timed out (120s limit).\n_Try a shorter query or retry._",
        )
    except Exception as e:
        logger.error("brain exception: %s", e, exc_info=True)
        await _safe_reply(update, f"❌ Brain error: {e}")


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
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rules_set", cmd_rules_set))
    app.add_handler(CommandHandler("regime", cmd_regime))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("howl", cmd_howl))
    app.add_handler(CommandHandler("whale", cmd_whale))
    app.add_handler(CommandHandler("arena", cmd_arena))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))

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
