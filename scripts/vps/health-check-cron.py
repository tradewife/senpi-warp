#!/usr/bin/env python3
"""
Health Check — System health monitor. Runs every 10 minutes via APScheduler.

  1. Git pull (get config changes from Oz agents)
  2. Reconcile closed positions into trade journal
  3. Verify mcporter connectivity
  4. Validate config files
  5. Detect stale cron heartbeats
  6. Git sync any state changes

Native Python implementation using senpi_common.py.
No dependency on senpi-skills or OpenClaw.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    git_pull,
    git_sync,
    mcporter_call,
    send_telegram,
    check_stale_heartbeats,
    CONFIG_DIR,
    STRATEGIES_FILE,
    RISK_REGIME_FILE,
    SCANNER_CONFIG_FILE,
    OUTPUTS_DIR,
)

HEALTH_STATE_FILE = OUTPUTS_DIR / "health-state.json"
# SHARK_CONFIG_FILE removed — SHARK paused by Senpi (v1.0, -4.3% ROI)
SENTINEL_CONFIG_FILE = CONFIG_DIR / "sentinel-config.json"
RHINO_CONFIG_FILE = CONFIG_DIR / "rhino-config.json"
POLAR_CONFIG_FILE = CONFIG_DIR / "polar-config.json"
MANTIS_CONFIG_FILE = CONFIG_DIR / "mantis-config.json"
FOX_CONFIG_FILE = CONFIG_DIR / "fox-config.json"


def check_mcporter() -> bool:
    """Verify mcporter can reach Senpi."""
    result = mcporter_call("account_get_portfolio", {}, timeout=15)
    if "error" in result:
        log(f"Health: mcporter check failed: {result['error']}")
        return False
    return True


def check_config_files() -> list[str]:
    """Validate all config JSON files are parseable."""
    issues = []
    for config_file in [
        STRATEGIES_FILE,
        RISK_REGIME_FILE,
        SCANNER_CONFIG_FILE,
        SENTINEL_CONFIG_FILE,
        RHINO_CONFIG_FILE,
        POLAR_CONFIG_FILE,
        MANTIS_CONFIG_FILE,
        FOX_CONFIG_FILE,
    ]:
        if not config_file.exists():
            issues.append(f"Missing: {config_file.name}")
            continue
        data = load_json(config_file, default=None)
        if data is None:
            issues.append(f"Corrupt JSON: {config_file.name}")
    return issues


def run_reconcile():
    """Inline reconcile-closes logic to avoid subprocess overhead."""
    try:
        # Import the reconcile function directly
        reconcile_path = Path(__file__).resolve().parent / "reconcile-closes.py"
        if reconcile_path.exists():
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "reconcile", str(reconcile_path)
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.reconcile()
    except Exception as e:
        log(f"Health: reconcile error: {e}")


def main():
    if not acquire_lock("health-check"):
        return

    try:
        health = {
            "lastRunAt": now_iso(),
            "mcporterOk": False,
            "configIssues": [],
            "reconcileRan": False,
        }

        # 1. Git pull — get config changes from Oz agents
        git_pull()
        log("Health: git pull complete")

        # 2. Reconcile closes
        run_reconcile()
        health["reconcileRan"] = True
        log("Health: reconcile complete")

        # 3. Check mcporter connectivity
        health["mcporterOk"] = check_mcporter()
        if not health["mcporterOk"]:
            send_telegram(
                "⚠️ HEALTH: mcporter cannot reach Senpi API. Check SENPI_AUTH_TOKEN."
            )

        # 4. Validate config files
        health["configIssues"] = check_config_files()
        if health["configIssues"]:
            send_telegram(
                f"⚠️ HEALTH: Config issues detected:\n"
                + "\n".join(f"  • {i}" for i in health["configIssues"])
            )

        # 5. Check cron heartbeats for stale jobs
        stale_crons = check_stale_heartbeats()
        health["staleCrons"] = stale_crons
        if stale_crons:
            send_telegram(
                f"⚠️ HEALTH: Stale crons detected:\n"
                + "\n".join(f"  • {c}" for c in stale_crons)
            )
            log(f"Health: stale crons: {stale_crons}")

        # Save health state
        save_json(HEALTH_STATE_FILE, health)
        log(
            f"Health: mcporter={'OK' if health['mcporterOk'] else 'FAIL'} "
            f"config_issues={len(health['configIssues'])}"
        )

        # 6. Git sync any state changes
        git_sync("auto: health check")

    finally:
        release_lock("health-check")


if __name__ == "__main__":
    main()
