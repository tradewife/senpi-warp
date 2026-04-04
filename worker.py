#!/usr/bin/env python3
"""
Senpi Railway Worker — replaces the Linux crontab for Railway deployment.

Runs all VPS cron jobs via APScheduler. On startup:
  1. Configures git HTTPS credentials (GITHUB_TOKEN env var)
  2. Configures mcporter with Senpi MCP server (SENPI_API_KEY env var)
  3. Schedules all jobs at their original intervals

Environment variables (set in Railway dashboard):
  SENPIAUTHTOKEN       — Senpi MCP authentication token (preferred)
  SENPI_API_KEY        — Senpi MCP authentication token (fallback)
  GITHUB_TOKEN         — GitHub personal access token (repo read/write)
  GITHUB_REPO          — e.g. tradewife/senpi-waifu
  TELEGRAM_BOT_TOKEN   — optional, for trade alerts
  TELEGRAM_CHAT_ID     — optional
  SENPI_WAIFU_DIR      — defaults to /app
  SENPI_SKILLS_DIR     — defaults to /opt/senpi/senpi-skills
"""

import os
import subprocess
import sys
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from typing import Optional

# Force line-buffered output for Railway log capture (belt-and-suspenders with PYTHONUNBUFFERED)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
SKILLS_DIR = Path(os.environ.get("SENPI_SKILLS_DIR", "/opt/senpi/senpi-skills"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "tradewife/senpi-waifu")
# Read Senpi auth token: prefer SENPI_AUTH_TOKEN (official), then SENPI_API_KEY, then SENPIAUTHTOKEN (legacy)
SENPI_AUTH_TOKEN = os.environ.get("SENPI_AUTH_TOKEN", "").strip()
SENPI_API_KEY = os.environ.get("SENPI_API_KEY", "").strip()
SENPIAUTHTOKEN = os.environ.get("SENPIAUTHTOKEN", "").strip()
SENPI_TOKEN = SENPI_AUTH_TOKEN or SENPI_API_KEY or SENPIAUTHTOKEN

# Propagate key env vars to child processes — ensure all token env var names are set
CHILD_ENV = {
    **os.environ,
    "SENPI_WAIFU_DIR": str(STATE_DIR),
    "SENPI_SKILLS_DIR": str(SKILLS_DIR),
}
if SENPI_TOKEN:
    CHILD_ENV.update(
        {
            "SENPIAUTHTOKEN": SENPI_TOKEN,
            "SENPI_API_KEY": SENPI_TOKEN,
            "SENPI_AUTH_TOKEN": SENPI_TOKEN,
        }
    )


# ---------------------------------------------------------------------------
# Startup: git + mcporter
# ---------------------------------------------------------------------------


def setup_git():
    """Configure git for HTTPS push/pull using a GitHub token."""
    if not GITHUB_TOKEN:
        print("[startup] WARNING: GITHUB_TOKEN not set — git push/pull will fail", flush=True)
        return
    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", remote_url],
        cwd=STATE_DIR,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "senpi-bot@railway"],
        cwd=STATE_DIR,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Senpi Railway Bot"],
        cwd=STATE_DIR,
        capture_output=True,
    )
    print(f"[startup] git configured for {GITHUB_REPO}", flush=True)


def setup_mcporter():
    """mcporter no longer used — direct HTTP calls to Senpi MCP instead."""
    if SENPI_TOKEN:
        print("[startup] Senpi auth token found — using direct MCP HTTP calls", flush=True)
    else:
        print("[startup] WARNING: No Senpi auth token set — Senpi MCP calls will fail", flush=True)


def run_py(script: str, args: Optional[list] = None, timeout: int = 120):
    """Run a Python script from the repo, printing output."""
    cmd = ["python3", str(STATE_DIR / script)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=CHILD_ENV,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        output = f"[TIMEOUT] {script} killed after {timeout}s"
        if e.stdout:
            output += "\n" + e.stdout.decode(errors="replace")[-500:]
        if e.stderr:
            output += "\n" + e.stderr.decode(errors="replace")[-500:]
        print(output, flush=True)
        return
    except Exception as e:
        print(f"[ERROR] {script} raised {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(line, flush=True)
    elif result.returncode != 0:
        print(f"[EXIT {result.returncode}] {script} (no output)", flush=True)


def run_sh(script: str):
    """Run a bash script from the repo."""
    try:
        result = subprocess.run(
            ["bash", str(STATE_DIR / script)],
            capture_output=True,
            text=True,
            env=CHILD_ENV,
        )
    except Exception as e:
        print(f"[ERROR] {script} raised {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        print(output, flush=True)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def job_orca():
    run_py("scripts/vps/orca-scanner-cron.py")


def job_komodo():
    run_py("scripts/vps/komodo-scanner-cron.py")


def job_dsl():
    run_py("scripts/vps/dsl-runner.py")


def job_polar():
    run_py("scripts/vps/polar-scanner-cron.py")


def job_mantis():
    run_py("scripts/vps/mantis-scanner-cron.py")


def job_fox():
    run_py("scripts/vps/fox-scanner-cron.py")


def job_smflip():
    run_py("scripts/vps/sm-flip-cron.py")


def job_condor():
    run_py("scripts/vps/condor-scanner-cron.py")


def job_roach():
    run_py("scripts/vps/roach-scanner-cron.py")


# PAUSED: job_barracuda — BARRACUDA removed per user request (check if Senpi-paused)
# PAUSED: job_bison     — BISON removed per user request (check if Senpi-paused)
# PAUSED: job_shark     — SHARK paused by Senpi (v1.0, -4.3% ROI)


def job_sentinel():
    run_py("scripts/vps/sentinel-scanner-cron.py")


def job_rhino():
    run_py("scripts/vps/rhino-scanner-cron.py")


def job_watchdog():
    run_py("scripts/vps/watchdog-cron.py")


def job_health():
    run_py("scripts/vps/health-check-cron.py")
    update_skills()


def update_skills():
    """Pull latest senpi-skills (called periodically by health check)."""
    if SKILLS_DIR.exists():
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            cwd=SKILLS_DIR,
            capture_output=True,
            timeout=30,
        )


def job_arena():
    run_py("scripts/vps/arena-monitor.py")


def job_brain():
    run_py("scripts/vps/autonomous-brain.py")


def job_regime():
    """Regime classifier — runs via waifu CLI."""
    try:
        result = subprocess.run(
            ["python3", "-m", "waifu_cli", "regime"],
            capture_output=True,
            text=True,
            env=CHILD_ENV,
            timeout=120,
        )
    except Exception as e:
        print(f"[ERROR] job_regime: {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(line, flush=True)
    elif result.returncode != 0:
        print(f"[EXIT {result.returncode}] job_regime (no output)", flush=True)


def job_arbiter():
    run_py("scripts/vps/risk-arbiter.py")


def job_reconcile():
    run_py("scripts/vps/reconcile-closes.py")


def job_suguru():
    """Suguru scan + hermes deliberation — writes recommendation for user approval."""
    print("[suguru] Step 1/2: scanning...", flush=True)
    run_py("scripts/vps/suguru.py", ["--scan-only"])
    print("[suguru] Step 2/2: hermes deliberating...", flush=True)
    run_py("scripts/vps/suguru_decide.py")


def job_suguru_stale():
    run_py("scripts/vps/suguru.py", ["--stale"])


def job_jido():
    """Autonomous trade executor with tiered governance (replaces evaluate)."""
    try:
        result = subprocess.run(
            ["python3", "-m", "waifu_cli", "jido"],
            capture_output=True,
            text=True,
            env=CHILD_ENV,
            timeout=120,
        )
    except Exception as e:
        print(f"[ERROR] job_jido: {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(line, flush=True)
    elif result.returncode != 0:
        print(f"[EXIT {result.returncode}] job_jido (no output)", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Senpi Railway Worker starting ===", flush=True)
    print(f"  STATE_DIR:  {STATE_DIR}", flush=True)
    print(f"  SKILLS_DIR: {SKILLS_DIR}", flush=True)
    print(f"  GITHUB_REPO: {GITHUB_REPO}", flush=True)
    # Ensure required directories exist
    for subdir in ("outputs", "state", "memory"):
        (STATE_DIR / subdir).mkdir(parents=True, exist_ok=True)
    print(f"[startup] Ensured directories: outputs, state, memory under {STATE_DIR}", flush=True)

    setup_git()
    setup_mcporter()

    # Startup regime bootstrap — run arena monitor once to initialize regime
    # before the scheduler fires any trading jobs
    print("[startup] Running initial regime classification...", flush=True)
    try:
        run_py("scripts/vps/arena-monitor.py")
        run_py("scripts/vps/autonomous-brain.py")
        print("[startup] Regime bootstrap complete", flush=True)
    except Exception as e:
        print(f"[startup] Regime bootstrap failed (non-fatal): {e}", flush=True)

    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(8)},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )

    # ORCA Dual-Mode Scanner — v1.3: every 3min (was 60s, reduced to prevent fee bleed)
    scheduler.add_job(job_orca, "interval", minutes=3, id="orca")

    # MANTIS Dual-Mode Scanner — every 90s
    scheduler.add_job(job_mantis, "interval", seconds=90, id="mantis")

    # FOX Dual-Mode Scanner — every 90s
    scheduler.add_job(job_fox, "interval", seconds=90, id="fox")

    # ROACH Striker-Only Scanner — every 90s (NEW: v1.0, Stalker disabled)
    scheduler.add_job(job_roach, "interval", seconds=90, id="roach")

    # KOMODO Momentum Scanner — every 5min (offset 1min to avoid pile-up)
    scheduler.add_job(job_komodo, "interval", minutes=5, id="komodo", seconds=60)

    # DSL High Water Runner — every 3min
    scheduler.add_job(job_dsl, "interval", minutes=3, id="dsl")

    # CONDOR Multi-Asset Hunter — every 3min, offset 1min
    scheduler.add_job(job_condor, "interval", minutes=3, id="condor", seconds=60)

    # POLAR ETH Alpha Hunter — every 3min, offset 45s
    scheduler.add_job(job_polar, "interval", minutes=3, id="polar", seconds=45)

    # PAUSED: BARRACUDA — removed (check vs Senpi paused list)
    # PAUSED: BISON      — removed (check vs Senpi paused list)
    # PAUSED: SHARK      — removed (Senpi paused, v1.0, -4.3% ROI)

    # SENTINEL Quality Trader Convergence — every 3min, offset 90s
    scheduler.add_job(job_sentinel, "interval", minutes=3, id="sentinel", seconds=90)

    # RHINO Momentum Pyramider — every 3min, offset 150s
    scheduler.add_job(job_rhino, "interval", minutes=3, id="rhino", seconds=150)

    # SM Flip Detector — every 5min
    scheduler.add_job(job_smflip, "interval", minutes=5, id="smflip")

    # Watchdog (margin/liq) — every 5min, offset 2min
    scheduler.add_job(job_watchdog, "interval", minutes=5, id="watchdog", seconds=120)

    # Health Check + git sync — every 10min
    scheduler.add_job(job_health, "interval", minutes=10, id="health")

    # Arena Monitor — every 15min
    scheduler.add_job(job_arena, "interval", minutes=15, id="arena")

    # Autonomous Brain — every 5min, offset 210s
    scheduler.add_job(job_brain, "interval", minutes=5, id="brain", seconds=210)

    # Regime Classifier — every 15min, offset 5min
    scheduler.add_job(job_regime, "interval", minutes=15, id="regime", seconds=300)

    # Risk Arbiter (mechanical safety) — every 30s
    scheduler.add_job(job_arbiter, "interval", seconds=30, id="arbiter")

    # Reconcile closes — every 15min
    scheduler.add_job(job_reconcile, "interval", minutes=15, id="reconcile", seconds=30)

    # SUGURU (scan → hermes decide → execute) — every 30min, offset 7min
    scheduler.add_job(job_suguru, "interval", minutes=30, id="suguru", seconds=420)

    # SUGURU Stale Order Check — every 5min, offset 3min
    scheduler.add_job(
        job_suguru_stale, "interval", minutes=5, id="suguru_stale", seconds=180
    )

    # JIDO Autonomous Trade Executor — every 5min, offset 90s
    scheduler.add_job(job_jido, "interval", minutes=5, id="jido", seconds=90)

    print("\nSchedule:", flush=True)
    print("  🐋 ORCA Scanner:    every 3min (v1.3)", flush=True)
    print("  🦗 MANTIS Scanner:  every 90s", flush=True)
    print("  🦊 FOX Scanner:     every 90s", flush=True)
    print("  🪳 ROACH Scanner:   every 90s (NEW: striker-only)", flush=True)
    print("  🦎 KOMODO Scanner:  every 5min", flush=True)
    print("  🦅 CONDOR Scanner:  every 3min", flush=True)
    print("  🐻‍❄️ POLAR Scanner:   every 3min", flush=True)
    print("  🛡 SENTINEL Scan:   every 3min", flush=True)
    print("  🦏 RHINO Scan:      every 3min", flush=True)
    print("  🔒 DSL HW Runner:   every 3min", flush=True)
    print("  🔄 SM Flip:         every 5min", flush=True)
    print("  👁  Watchdog:        every 5min", flush=True)
    print("  🏥 Health Check:    every 10min", flush=True)
    print("  📊 Arena Monitor:   every 15min", flush=True)
    print("  🌡  Regime Class:    every 15min", flush=True)
    print("  🚨 Risk Arbiter:    every 30s", flush=True)
    print("  🔃 Reconcile:       every 15min", flush=True)
    print("  ⚡ SUGURU Pipeline: every 30min (scan→hermes→execute)", flush=True)
    print("  ⏰ SUGURU Stale:   every 5min", flush=True)
    print("  ⚡ JIDO Executor:   every 5min", flush=True)
    print("  [PAUSED] 🦈 SHARK / 🎣 BARRACUDA / 🦬 BISON — removed from schedule", flush=True)
    print(f"\nWorker running — {len(scheduler.get_jobs())} jobs scheduled.\n", flush=True)
    sys.stdout.flush()

    # --- APScheduler error listener ---
    def _on_job_error(event):
        print(f"[ALERT] job {event.job_id} failed: {event.exception}", flush=True)

    def _on_job_missed(event):
        print(f"[ALERT] job {event.job_id} missed its scheduled time", flush=True)

    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)

    # --- Heartbeat (1 min for faster observability) ---
    import datetime as _dt

    _hb_count = [0]

    def _heartbeat():
        _hb_count[0] += 1
        ts = _dt.datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{ts}] heartbeat #{_hb_count[0]} — scheduler alive", flush=True)

    scheduler.add_job(
        _heartbeat,
        "interval",
        minutes=1,
        id="heartbeat",
        next_run_time=_dt.datetime.utcnow(),
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Worker stopped.", flush=True)
    finally:
        print("[shutdown] Scheduler terminated — container exiting.", flush=True)


def start_telegram_bot():
    """Start Telegram bot polling in a daemon thread alongside the scheduler."""
    import threading

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[startup] TELEGRAM_BOT_TOKEN not set — Telegram bot disabled", flush=True)
        return

    try:
        import asyncio
        from dashboard.telegram_bot import create_bot_application, start_polling
    except ImportError:
        print("[startup] dashboard.telegram_bot import failed — Telegram bot disabled", flush=True)
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = create_bot_application()
        if not app:
            print("[startup] Telegram bot creation returned None", flush=True)
            return
        print("[startup] Telegram bot starting (polling)...", flush=True)
        loop.run_until_complete(start_polling(app))
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[startup] Telegram bot thread launched", flush=True)


if __name__ == "__main__":
    try:
        start_telegram_bot()
        main()
    except Exception as e:
        import traceback

        print(f"[FATAL] Worker crashed: {e}", flush=True)
        traceback.print_exc()
        raise
