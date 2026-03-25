"""
main.py — Click CLI group and command registration.

Usage:
    waifu evaluate       Process pending scanner signals and execute approved trades
    waifu regime         Classify macro market regime (RISK_ON / BASELINE / RISK_OFF)
    waifu review         Portfolio review — check risk rails, write report
    waifu howl           Nightly 10-pillar self-improvement analysis
    waifu whale          Daily copy-trade portfolio review and rebalance
    waifu arena          Study Senpi Predators leaderboard for intelligence
    waifu status         Show current system state (read-only)
    waifu emergency-stop Set RISK_OFF immediately
"""

import click

from waifu_cli.commands.evaluate import evaluate
from waifu_cli.commands.regime import regime
from waifu_cli.commands.review import review
from waifu_cli.commands.howl import howl
from waifu_cli.commands.whale import whale
from waifu_cli.commands.arena import arena
from waifu_cli.commands.status import status
from waifu_cli.commands.emergency_stop import emergency_stop
from waifu_cli.commands.debug import debug
from waifu_cli.commands.dev import dev
from waifu_cli.commands.config import config


@click.group()
@click.version_option(version="1.0.0", prog_name="waifu")
def cli():
    """Waifu — strategic supervisor for the senpi-waifu trading system."""


cli.add_command(evaluate)
cli.add_command(regime)
cli.add_command(review)
cli.add_command(howl)
cli.add_command(whale)
cli.add_command(arena)
cli.add_command(status)
cli.add_command(emergency_stop)
cli.add_command(debug)
cli.add_command(dev)
cli.add_command(config)
