"""
debug.py — Railway CLI wrappers for production observability.

Usage:
    waifu debug logs [--lines N] [--follow] [--filter TEXT]
    waifu debug status
    waifu debug deploy [--trigger]
    waifu debug tail <scanner>
"""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

import click

PROJECT_ROOT = Path(os.environ.get("SENPI_WAIFU_DIR", Path(__file__).parent.parent.parent))

sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "lib"))

import senpi_common as sc

RAILWAY_BIN = "/home/kt/.nvm/versions/node/v24.10.0/bin/railway"

VALID_SCANNERS = [
    "orca", "mantis", "fox", "komodo", "condor", "polar",
    "sentinel", "rhino", "dsl", "arbiter", "brain",
    "watchdog", "health", "arena", "elite",
]


def _find_railway() -> str:
    """Return path to railway CLI or exit with helpful error."""
    if os.path.isfile(RAILWAY_BIN) and os.access(RAILWAY_BIN, os.X_OK):
        return RAILWAY_BIN
    found = shutil.which("railway")
    if found:
        return found
    click.echo("❌ railway CLI not found.")
    click.echo("   Install: npm i -g @railway/cli")
    click.echo(f"   Expected at: {RAILWAY_BIN}")
    sys.exit(1)


@click.group()
def debug():
    """Debug tools — Railway logs, deployment status, live monitoring."""


@debug.command()
@click.option("--lines", "-n", default=50, show_default=True, help="Number of lines to show.")
@click.option("--follow", "-f", is_flag=True, help="Follow mode (tail -f).")
@click.option("--filter", "grep_filter", default=None, help="Filter output (e.g. 'ORCA', 'error').")
def logs(lines, follow, grep_filter):
    """Stream Railway deployment logs."""
    railway = _find_railway()
    cmd = [railway, "logs", "--lines", str(lines)]

    if follow:
        cmd.append("--follow")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                if grep_filter is None or grep_filter.lower() in line.lower():
                    click.echo(line, nl=False)
        except KeyboardInterrupt:
            click.echo("\n🛑 Stopped following logs.")
        finally:
            if proc.poll() is None:
                proc.terminate()
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout or result.stderr
        if grep_filter:
            for line in output.splitlines():
                if grep_filter.lower() in line.lower():
                    click.echo(line)
        else:
            click.echo(output)


@debug.command()
def status():
    """Railway deployment status + local system health."""
    railway = _find_railway()

    click.echo(f"{'=' * 55}")
    click.echo("  RAILWAY DEPLOYMENT STATUS")
    click.echo(f"{'=' * 55}")

    result = subprocess.run([railway, "status"], capture_output=True, text=True)
    click.echo(result.stdout or result.stderr)

    click.echo(f"{'=' * 55}")
    click.echo("  LOCAL SYSTEM HEALTH")
    click.echo(f"{'=' * 55}")

    health = sc.load_json(sc.OUTPUTS_DIR / "health-state.json", default={})
    if health:
        click.echo(f"\n📊 Health state:")
        for key, val in health.items():
            click.echo(f"   {key}: {val}")
    else:
        click.echo("\n📊 Health state: no data")

    heartbeats = sc.load_json(sc.OUTPUTS_DIR / "cron-heartbeats.json", default={})
    if heartbeats:
        click.echo(f"\n💓 Heartbeats ({len(heartbeats)} crons):")
        for name, ts in sorted(heartbeats.items()):
            click.echo(f"   {name:20s} {ts}")
    else:
        click.echo("\n💓 Heartbeats: no data")

    stale = sc.check_stale_heartbeats()
    if stale:
        click.echo(f"\n⚠️  Stale crons: {', '.join(stale)}")
    else:
        click.echo("\n✅ All mechanical crons healthy")

    click.echo(f"\n{'=' * 55}")


@debug.command()
@click.option("--trigger", is_flag=True, help="Trigger a redeploy with 'railway up -d'.")
def deploy(trigger):
    """Show deployment state or trigger a redeploy."""
    railway = _find_railway()

    if trigger:
        click.echo("🚀 Triggering redeploy...")
        result = subprocess.run(
            [railway, "up", "-d"],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT),
        )
        click.echo(result.stdout or result.stderr)
    else:
        result = subprocess.run([railway, "status"], capture_output=True, text=True)
        click.echo(result.stdout or result.stderr)


@debug.command()
@click.argument("scanner", type=click.Choice(VALID_SCANNERS, case_sensitive=False))
def tail(scanner):
    """Tail logs filtered to a specific scanner or subsystem."""
    railway = _find_railway()
    scanner_upper = scanner.upper()

    click.echo(f"📡 Tailing {scanner_upper} logs (Ctrl+C to stop)...")

    cmd = [railway, "logs", "--follow"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            if scanner_upper in line.upper():
                click.echo(line, nl=False)
    except KeyboardInterrupt:
        click.echo(f"\n🛑 Stopped tailing {scanner_upper}.")
    finally:
        if proc.poll() is None:
            proc.terminate()
