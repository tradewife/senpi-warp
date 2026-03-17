"""
senpi_common.py — Shared utilities for all VPS cron scripts.

Handles: config loading, state read/write, mcporter calls, git sync,
Telegram alerts, and position management.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — resolved relative to SENPI_WAIFU_DIR env var or /opt/senpi/senpi-waifu
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/opt/senpi/senpi-waifu"))
CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"

SKILLS_DIR = Path(os.environ.get("SENPI_SKILLS_DIR", "/opt/senpi/senpi-skills"))

RISK_REGIME_FILE = CONFIG_DIR / "risk-regime.json"
SCANNER_CONFIG_FILE = CONFIG_DIR / "scanner-config.json"
STRATEGIES_FILE = CONFIG_DIR / "wolf-strategies.json"
PENDING_ENTRIES_FILE = POSITION_STATE_DIR / "pending-entries.json"
SCAN_HISTORY_FILE = POSITION_STATE_DIR / "scan-history.json"
TRADE_JOURNAL_FILE = MEMORY_DIR / "trade-journal.json"

LOCKFILE_DIR = Path("/tmp/senpi-locks")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    """Load a JSON file, returning `default` if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: Path, data, *, indent=2):
    """Atomically write JSON (write to .tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, default=str)
        f.write("\n")
    tmp.rename(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Risk regime
# ---------------------------------------------------------------------------

def load_regime() -> dict:
    """Return the full regime config."""
    return load_json(RISK_REGIME_FILE)


def current_regime_params() -> dict:
    """Return the active regime's parameter block."""
    regime = load_regime()
    mode = regime.get("riskMode", "BASELINE")
    return regime.get("regimes", {}).get(mode, regime["regimes"]["BASELINE"])


def is_entries_allowed() -> bool:
    params = current_regime_params()
    return params.get("newEntriesAllowed", False)


def is_auto_entry_enabled() -> bool:
    params = current_regime_params()
    return params.get("autoEntryEnabled", False)


def set_risk_mode(mode: str, reason: str, updated_by: str = "vps-script"):
    """Update the risk regime mode. Only the Risk Arbiter or Oz should call this."""
    regime = load_regime()
    regime["riskMode"] = mode
    regime["updatedAt"] = now_iso()
    regime["updatedBy"] = updated_by
    regime["reason"] = reason
    save_json(RISK_REGIME_FILE, regime)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

def load_strategies() -> dict:
    return load_json(STRATEGIES_FILE)


def get_enabled_strategies() -> list[dict]:
    """Return list of enabled strategy dicts, each with its key injected."""
    data = load_strategies()
    result = []
    for key, strat in data.get("strategies", {}).items():
        if strat.get("enabled", True):
            strat["_key"] = key
            result.append(strat)
    return result


def get_strategy_state_dir(strategy_key: str) -> Path:
    d = POSITION_STATE_DIR / strategy_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_open_positions(strategy_key: str) -> list[dict]:
    """Return list of active DSL state dicts for a strategy."""
    d = get_strategy_state_dir(strategy_key)
    positions = []
    for f in d.glob("dsl-*.json"):
        state = load_json(f)
        if state and state.get("active", False):
            state["_file"] = str(f)
            positions.append(state)
    return positions


def count_open_slots(strategy: dict) -> int:
    """How many slots are free in this strategy. Respects gate state."""
    if strategy.get("gateState", "OPEN") != "OPEN":
        return 0
    max_slots = strategy.get("maxSlots", 2)
    open_count = len(get_open_positions(strategy["_key"]))
    return max(0, max_slots - open_count)


# ---------------------------------------------------------------------------
# Pending entries queue
# ---------------------------------------------------------------------------

def load_pending_entries() -> list[dict]:
    return load_json(PENDING_ENTRIES_FILE, default=[])


def save_pending_entries(entries: list[dict]):
    save_json(PENDING_ENTRIES_FILE, entries)


def add_pending_entry(entry: dict):
    """Append an entry to the pending queue."""
    entries = load_pending_entries()
    entry["queuedAt"] = now_iso()
    entries.append(entry)
    save_pending_entries(entries)


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------

def load_trade_journal() -> list[dict]:
    return load_json(TRADE_JOURNAL_FILE, default=[])


def record_trade(trade: dict):
    """Append a trade to the journal."""
    journal = load_trade_journal()
    trade["recordedAt"] = now_iso()
    journal.append(trade)
    save_json(TRADE_JOURNAL_FILE, journal)


# ---------------------------------------------------------------------------
# mcporter execution
# ---------------------------------------------------------------------------

def mcporter_call(tool: str, args: dict, *, timeout: int = 30) -> dict:
    """
    Call a Senpi MCP tool via mcporter.
    Returns parsed JSON response or raises on failure.
    """
    cmd = ["mcporter", "call", "senpi", tool, "--json", json.dumps(args)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            log(f"mcporter error ({tool}): {result.stderr.strip()}")
            return {"error": result.stderr.strip()}
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except subprocess.TimeoutExpired:
        log(f"mcporter timeout ({tool})")
        return {"error": "timeout"}
    except json.JSONDecodeError:
        log(f"mcporter bad JSON ({tool}): {result.stdout[:200]}")
        return {"error": "bad_json", "raw": result.stdout[:500]}


# ---------------------------------------------------------------------------
# Locking (prevent cron overlap)
# ---------------------------------------------------------------------------

def acquire_lock(name: str) -> bool:
    """Simple file-based lock. Returns True if acquired."""
    LOCKFILE_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    if lockfile.exists():
        # Check if stale (>5 min old)
        age = time.time() - lockfile.stat().st_mtime
        if age < 60:
            return False
        log(f"Stale lock for {name} ({age:.0f}s old), removing")
    lockfile.write_text(str(os.getpid()))
    return True


def release_lock(name: str):
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    lockfile.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Git sync
# ---------------------------------------------------------------------------

def git_sync(message: str = "auto: state update"):
    """Stage all changes in STATE_DIR and push. Uses a global lock to prevent concurrent pushes."""
    if not acquire_lock("git-sync"):
        log("git sync: another sync in progress — skipping")
        return
    try:
        subprocess.run(["git", "add", "-A"], cwd=STATE_DIR, capture_output=True, timeout=10)
        # Only commit if there are changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=STATE_DIR, capture_output=True, timeout=10,
        )
        if result.returncode != 0:  # There are staged changes
            subprocess.run(
                ["git", "commit", "-m", message, "--no-verify"],
                cwd=STATE_DIR, capture_output=True, timeout=15,
            )
            subprocess.run(
                ["git", "push", "--quiet"],
                cwd=STATE_DIR, capture_output=True, timeout=30,
            )
    except subprocess.TimeoutExpired:
        log("git sync timeout — will retry next cycle")
    finally:
        release_lock("git-sync")


def git_pull():
    """Pull latest state (Oz agents may have pushed config changes)."""
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            cwd=STATE_DIR, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        log("git pull timeout")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    """Send a Telegram alert. Reads token and chat_id from env."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)
