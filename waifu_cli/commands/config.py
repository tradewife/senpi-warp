"""
config.py — Environment configuration management.

Usage:
    waifu config show              Show all config (secrets masked)
    waifu config get <key>         Get specific value
    waifu config set <key> <val>   Set value (writes to .env)
    waifu config validate          Check required vars are set
    waifu config export            Export as .env format for Railway
"""

import os
import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

REQUIRED_VARS = [
    ("SENPI_AUTH_TOKEN", "Senpi MCP authentication token"),
    ("GITHUB_TOKEN", "GitHub token for git push/pull"),
]

OPTIONAL_VARS = [
    ("SENPI_WAIFU_DIR", str(PROJECT_ROOT), "Project root directory"),
    ("GITHUB_REPO", "tradewife/senpi-waifu", "GitHub repo for state sync"),
    ("TELEGRAM_BOT_TOKEN", "", "Telegram bot token for alerts"),
    ("TELEGRAM_CHAT_ID", "", "Telegram chat ID for alerts"),
    ("RAILWAY_TOKEN", "", "Railway CLI token"),
    ("MCPORTER_CMD", "mcporter", "mcporter binary path"),
]

SENSITIVE_VARS = {
    "SENPI_AUTH_TOKEN",
    "GITHUB_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "RAILWAY_TOKEN",
}


def _load_env_file() -> dict:
    """Load values from .env file."""
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _save_env_file(values: dict):
    """Save values to .env file, preserving comments and order."""
    existing = {}
    lines = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            if "=" in stripped:
                key, _, _ = stripped.partition("=")
                existing[key.strip()] = line
    for key, val in values.items():
        if val is not None:
            existing[key] = f'{key}="{val}"'
    seen = set()
    out_lines = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            if "=" in stripped:
                key, _, _ = stripped.partition("=")
                key = key.strip()
                if key in existing and key not in seen:
                    out_lines.append(existing[key])
                    seen.add(key)
                elif key not in seen:
                    out_lines.append(line)
                    seen.add(key)
    for key, val in values.items():
        if key not in seen and val is not None:
            out_lines.append(f'{key}="{val}"')
            seen.add(key)
    ENV_FILE.write_text("\n".join(out_lines) + "\n")


def _get_value(key: str) -> str | None:
    """Get value from .env file, then environment, then default."""
    env_file = _load_env_file()
    if key in env_file and env_file[key]:
        return env_file[key]
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    for opt_key, default, _ in OPTIONAL_VARS:
        if opt_key == key:
            return default
    return None


def _mask_value(key: str, value: str) -> str:
    """Mask sensitive values for display."""
    if key in SENSITIVE_VARS and value:
        if len(value) <= 8:
            return "*" * len(value)
        return value[:4] + "*" * 8 + value[-4:]
    return value


@click.group()
def config():
    """Manage environment configuration."""


@config.command("show")
def show_config():
    """Show all configuration values (secrets masked)."""
    env_file = _load_env_file()
    click.echo(f"\n{'=' * 55}")
    click.echo("  WAIFU CONFIGURATION")
    click.echo(f"  Config file: {ENV_FILE}")
    click.echo(f"{'=' * 55}")
    click.echo("\n📋 Required:")
    missing = []
    for key, desc in REQUIRED_VARS:
        val = _get_value(key)
        source = (
            ".env"
            if key in env_file and env_file[key]
            else ("env" if os.environ.get(key) else "missing")
        )
        if val:
            display = _mask_value(key, val)
            click.echo(f"   {key}: {display}")
            click.echo(f"      ({source})")
        else:
            click.echo(f"   {key}: <not set>")
            click.echo(f"      ({desc})")
            missing.append(key)
    click.echo("\n📦 Optional:")
    for key, default, desc in OPTIONAL_VARS:
        val = _get_value(key)
        source = (
            ".env"
            if key in env_file and env_file[key]
            else ("env" if os.environ.get(key) else "default")
        )
        display = _mask_value(key, val) if val else "<not set>"
        click.echo(f"   {key}: {display}")
        if val != default:
            click.echo(f"      ({source}, default: {default})")
    if missing:
        click.echo(f"\n⚠️  Missing required: {', '.join(missing)}")
        click.echo("   Run: waifu config set <key> <value>")
    else:
        click.echo("\n✅ All required variables set")
    click.echo(f"\n{'=' * 55}\n")


@config.command("get")
@click.argument("key")
def get_value(key):
    """Get a specific configuration value."""
    val = _get_value(key)
    if val is None:
        click.echo(f"{key}: <not set>")
        sys.exit(1)
    if key in SENSITIVE_VARS:
        click.echo(f"{key}={_mask_value(key, val)}")
        click.echo("(masked — use waifu config show to see source)")
    else:
        click.echo(f"{key}={val}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def set_value(key, value):
    """Set a configuration value (writes to .env file)."""
    env_file = _load_env_file()
    env_file[key] = value
    _save_env_file(env_file)
    display = _mask_value(key, value)
    click.echo(f"✅ Set {key}={display}")
    click.echo(f"   Written to {ENV_FILE}")
    if key in SENSITIVE_VARS:
        click.echo("   ⚠️  This is a sensitive value — .env is gitignored")


@config.command("validate")
def validate_config():
    """Validate that all required variables are set."""
    env_file = _load_env_file()
    errors = []
    warnings = []
    for key, desc in REQUIRED_VARS:
        val = _get_value(key)
        if not val:
            errors.append(f"{key}: {desc}")
    if not _get_value("TELEGRAM_BOT_TOKEN") or not _get_value("TELEGRAM_CHAT_ID"):
        warnings.append(
            "Telegram alerts disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)"
        )
    if not _get_value("RAILWAY_TOKEN"):
        warnings.append("Railway CLI may require login (RAILWAY_TOKEN not set)")
    click.echo("\n📋 Configuration Validation\n")
    if errors:
        click.echo("❌ Missing required variables:")
        for err in errors:
            click.echo(f"   - {err}")
        click.echo("\n   Fix with: waifu config set <key> <value>")
    else:
        click.echo("✅ All required variables are set")
    if warnings:
        click.echo("\n⚠️  Warnings:")
        for warn in warnings:
            click.echo(f"   - {warn}")
    click.echo()
    sys.exit(1 if errors else 0)


@config.command("export")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["env", "json"]),
    default="env",
    help="Output format",
)
def export_config(fmt):
    """Export configuration for Railway or other platforms."""
    env_file = _load_env_file()
    values = {}
    for key, _ in REQUIRED_VARS:
        val = _get_value(key)
        if val:
            values[key] = val
    for key, _, _ in OPTIONAL_VARS:
        val = _get_value(key)
        if val:
            values[key] = val
    if fmt == "json":
        import json

        click.echo(json.dumps(values, indent=2))
    else:
        for key, val in values.items():
            click.echo(f'{key}="{val}"')
    click.echo("\n# Copy above to Railway dashboard > Variables", err=True)
