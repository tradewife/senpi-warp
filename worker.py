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

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
SKILLS_DIR = Path(os.environ.get("SENPI_SKILLS_DIR", "/opt/senpi/senpi-skills"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "tradewife/senpi-waifu")
# Read Senpi auth token: prefer SENPIAUTHTOKEN, then SENPI_API_KEY, then SENPI_AUTH_TOKEN
SENPIAUTHTOKEN = os.environ.get("SENPIAUTHTOKEN", "").strip()
SENPI_API_KEY = os.environ.get("SENPI_API_KEY", "").strip()
SENPI_AUTH_TOKEN = os.environ.get("SENPI_AUTH_TOKEN", "").strip()
SENPI_TOKEN = SENPIAUTHTOKEN or SENPI_API_KEY or SENPI_AUTH_TOKEN

# Propagate key env vars to child processes — ensure both env var names are set
CHILD_ENV = {
    **os.environ,
    "SENPI_WAIFU_DIR": str(STATE_DIR),
    "SENPI_SKILLS_DIR": str(SKILLS_DIR),
    "SENPI_API_KEY": SENPI_API_KEY,
    "SENPI_AUTH_TOKEN": SENPI_API_KEY,
}


# ---------------------------------------------------------------------------
# Startup: git + mcporter
# ---------------------------------------------------------------------------

def setup_git():
    """Configure git for HTTPS push/pull using a GitHub token."""
    if not GITHUB_TOKEN:
        print("[startup] WARNING: GITHUB_TOKEN not set — git push/pull will fail")
        return
    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    subprocess.run(["git", "remote", "set-url", "origin", remote_url],
                   cwd=STATE_DIR, capture_output=True)
    subprocess.run(["git", "config", "user.email", "senpi-bot@railway"],
                   cwd=STATE_DIR, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Senpi Railway Bot"],
                   cwd=STATE_DIR, capture_output=True)
    print(f"[startup] git configured for {GITHUB_REPO}")


def setup_mcporter():
    """mcporter no longer used — direct HTTP calls to Senpi MCP instead."""
    if SENPI_TOKEN:
        print(f"[startup] Senpi auth token found — using direct MCP HTTP calls")
    else:
        print(\"[startup] WARNING: No Senpi auth token set — Senpi MCP calls will fail\")


def update_skills():
    """Pull latest senpi-skills (called periodically by health check)."""
    if SKILLS_DIR.exists():
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            cwd=SKILLS_DIR, capture_output=True, timeout=30,
        )


# ---------------------------------------------------------------------------
# Job runner helpers
# ---------------------------------------------------------------------------

def run_py(script: str):
    """Run a Python script from the repo, printing stderr."""
    result = subprocess.run(
        ["python3", str(STATE_DIR / script)],
        capture_output=True, text=True, env=CHILD_ENV,
    )
    if result.stderr.strip():
        # Print only non-empty stderr (scripts log to stderr)
        print(result.stderr.rstrip())


def run_sh(script: str):
    """Run a bash script from the repo."""
    result = subprocess.run(
        ["bash", str(STATE_DIR / script)],
        capture_output=True, text=True, env=CHILD_ENV,
    )
    if result.stderr.strip():
        print(result.stderr.rstrip())


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

def job_orca():
    run_py("scripts/vps/orca-scanner-cron.py")


def job_komodo():
    run_py("scripts/vps/komodo-scanner-cron.py")


def job_dsl():
    run_py("scripts/vps/dsl-runner.py")


def job_smflip():
    run_py("scripts/vps/sm-flip-cron.py")


def job_condor():
    run_py("scripts/vps/condor-scanner-cron.py")


def job_barracuda():
    run_py("scripts/vps/barracuda-scanner-cron.py")


def job_bison():
    run_py("scripts/vps/bison-scanner-cron.py")


def job_shark():
    run_py("scripts/vps/shark-scanner-cron.py")


def job_sentinel():
    run_py("scripts/vps/sentinel-scanner-cron.py")


def job_rhino():
    run_py("scripts/vps/rhino-scanner-cron.py")


def job_watchdog():
    run_py("scripts/vps/watchdog-cron.py")


def job_health():
    run_py("scripts/vps/health-check-cron.py")
    update_skills()


def job_arena():
    run_py("scripts/vps/arena-monitor.py")


def job_brain():
    run_py("scripts/vps/autonomous-brain.py")


def job_arbiter():
    run_py("scripts/vps/risk-arbiter.py")


def job_reconcile():
    run_py("scripts/vps/reconcile-closes.py")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Senpi Railway Worker starting ===")
    print(f"  STATE_DIR:  {STATE_DIR}")
    print(f"  SKILLS_DIR: {SKILLS_DIR}")
    print(f"  GITHUB_REPO: {GITHUB_REPO}")
    # Ensure required directories exist
    for subdir in ("outputs", "state", "memory"):
        (STATE_DIR / subdir).mkdir(parents=True, exist_ok=True)
    print(f"[startup] Ensured directories: outputs, state, memory under {STATE_DIR}")

    setup_git()
    setup_mcporter()

    # Startup regime bootstrap — run arena monitor once to initialize regime
    # before the scheduler fires any trading jobs
    print("[startup] Running initial regime classification...")
    try:
        run_py("scripts/vps/arena-monitor.py")
        run_py("scripts/vps/autonomous-brain.py")
        print("[startup] Regime bootstrap complete")
    except Exception as e:
        print(f"[startup] Regime bootstrap failed (non-fatal): {e}")

    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(8)},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )

    # ORCA Dual-Mode Scanner — every 60s
    scheduler.add_job(job_orca, "interval", seconds=60, id="orca")

    # KOMODO Momentum Scanner — every 5min (offset 1min to avoid pile-up)
    scheduler.add_job(job_komodo, "interval", minutes=5, id="komodo",
                      seconds=60)

    # DSL High Water Runner — every 3min
    scheduler.add_job(job_dsl, "interval", minutes=3, id="dsl")

    # CONDOR Multi-Asset Hunter — every 3min, offset 1min
    scheduler.add_job(job_condor, "interval", minutes=3, id="condor", seconds=60)

    # BARRACUDA Funding Decay Collector — every 15min
    scheduler.add_job(job_barracuda, "interval", minutes=15, id="barracuda", seconds=120)

    # BISON Conviction Trend Holder — every 30min
    scheduler.add_job(job_bison, "interval", minutes=30, id="bison", seconds=180)

    # SHARK Liquidation Cascade — every 2min, offset 30s
    scheduler.add_job(job_shark, "interval", minutes=2, id="shark", seconds=30)

    # SENTINEL Quality Trader Convergence — every 3min, offset 90s
    scheduler.add_job(job_sentinel, "interval", minutes=3, id="sentinel", seconds=90)

    # RHINO Momentum Pyramider — every 3min, offset 150s
    scheduler.add_job(job_rhino, "interval", minutes=3, id="rhino", seconds=150)

    # SM Flip Detector — every 5min
    scheduler.add_job(job_smflip, "interval", minutes=5, id="smflip")

    # Watchdog (margin/liq) — every 5min, offset 2min
    scheduler.add_job(job_watchdog, "interval", minutes=5, id="watchdog",
                      seconds=120)

    # Health Check + git sync — every 10min
    scheduler.add_job(job_health, "interval", minutes=10, id="health")

    # Arena Monitor — every 15min
    scheduler.add_job(job_arena, "interval", minutes=15, id="arena")

    # Autonomous Brain — every 5min, offset 210s
    scheduler.add_job(job_brain, "interval", minutes=5, id="brain", seconds=210)

    # Risk Arbiter (mechanical safety) — every 30s
    scheduler.add_job(job_arbiter, "interval", seconds=30, id="arbiter")

    # Reconcile closes — every 15min
    scheduler.add_job(job_reconcile, "interval", minutes=15, id="reconcile",
                      seconds=30)

    print("\nSchedule:")
    print("  🐋 ORCA Scanner:    every 60s")
    print("  🦎 KOMODO Scanner:  every 5min")
    print("  🦅 CONDOR Scanner:  every 3min")
    print("  🎣 BARRACUDA Scan:  every 15min")
    print("  🦬 BISON Scanner:   every 30min")
    print("  🦈 SHARK Scanner:   every 2min")
    print("  🛡 SENTINEL Scan:   every 3min")
    print("  🦏 RHINO Scan:      every 3min")
    print("  🔒 DSL HW Runner:   every 3min")
    print("  🔄 SM Flip:         every 5min")
    print("  👁  Watchdog:        every 5min")
    print("  🏥 Health Check:    every 10min")
    print("  📊 Arena Monitor:   every 15min")
    print("  🚨 Risk Arbiter:    every 30s")
    print("  🔃 Reconcile:       every 15min")
    print("\nWorker running. Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Worker stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[FATAL] Worker crashed: {e}")
        traceback.print_exc()
        raise