#!/usr/bin/env python3
"""
Senpi Railway Worker — replaces the Linux crontab for Railway deployment.

Runs all VPS cron jobs via APScheduler. On startup:
  1. Configures git HTTPS credentials (GITHUB_TOKEN env var)
  2. Configures mcporter with Senpi MCP server (SENPI_API_KEY env var)
  3. Schedules all jobs at their original intervals

Environment variables (set in Railway dashboard):
  SENPI_API_KEY        — Senpi MCP authentication token
  GITHUB_TOKEN         — GitHub personal access token (repo read/write)
  GITHUB_REPO          — e.g. tradewife/senpi-waifu
  TELEGRAM_BOT_TOKEN   — optional, for trade alerts
  TELEGRAM_CHAT_ID     — optional
  SENPI_STATE_DIR      — defaults to /app
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

STATE_DIR = Path(os.environ.get("SENPI_STATE_DIR", "/app"))
SKILLS_DIR = Path(os.environ.get("SENPI_SKILLS_DIR", "/opt/senpi/senpi-skills"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "tradewife/senpi-waifu")
SENPI_API_KEY = os.environ.get("SENPI_API_KEY", "")

# Propagate key env vars to child processes
CHILD_ENV = {
    **os.environ,
    "SENPI_STATE_DIR": str(STATE_DIR),
    "SENPI_SKILLS_DIR": str(SKILLS_DIR),
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
    """Configure mcporter with Senpi MCP server."""
    if not SENPI_API_KEY:
        print("[startup] WARNING: SENPI_API_KEY not set — mcporter will not work")
        return
    result = subprocess.run(
        [
            "mcporter", "config", "add", "senpi",
            "--command", "npx",
            "--env", f"SENPI_AUTH_TOKEN={SENPI_API_KEY}",
            "--", "mcp-remote", "https://mcp.prod.senpi.ai/mcp",
            "--header", f"Authorization: Bearer {SENPI_API_KEY}",
        ],
        capture_output=True, text=True, env=CHILD_ENV,
    )
    if result.returncode == 0:
        print("[startup] mcporter configured with Senpi MCP")
    else:
        print(f"[startup] mcporter config warning: {result.stderr.strip()[:200]}")


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
    run_sh("scripts/vps/dsl-combined-cron.sh")


def job_smflip():
    run_sh("scripts/vps/sm-flip-cron.sh")


def job_watchdog():
    run_sh("scripts/vps/watchdog-cron.sh")


def job_health():
    run_sh("scripts/vps/health-check-cron.sh")
    update_skills()


def job_arena():
    run_py("scripts/vps/arena-monitor.py")


def job_arbiter():
    run_py("scripts/vps/risk-arbiter.py")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Senpi Railway Worker starting ===")
    print(f"  STATE_DIR:  {STATE_DIR}")
    print(f"  SKILLS_DIR: {SKILLS_DIR}")
    print(f"  GITHUB_REPO: {GITHUB_REPO}")

    setup_git()
    setup_mcporter()

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

    # SM Flip Detector — every 5min
    scheduler.add_job(job_smflip, "interval", minutes=5, id="smflip")

    # Watchdog (margin/liq) — every 5min, offset 2min
    scheduler.add_job(job_watchdog, "interval", minutes=5, id="watchdog",
                      seconds=120)

    # Health Check + git sync — every 10min
    scheduler.add_job(job_health, "interval", minutes=10, id="health")

    # Arena Monitor — every 15min
    scheduler.add_job(job_arena, "interval", minutes=15, id="arena")

    # Risk Arbiter (mechanical safety) — every 30s
    scheduler.add_job(job_arbiter, "interval", seconds=30, id="arbiter")

    print("\nSchedule:")
    print("  🐋 ORCA Scanner:    every 60s")
    print("  🦎 KOMODO Scanner:  every 5min")
    print("  🔒 DSL HW Runner:   every 3min")
    print("  🔄 SM Flip:         every 5min")
    print("  👁  Watchdog:        every 5min")
    print("  🏥 Health Check:    every 10min")
    print("  📊 Arena Monitor:   every 15min")
    print("  🚨 Risk Arbiter:    every 30s")
    print("\nWorker running. Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Worker stopped.")


if __name__ == "__main__":
    main()
