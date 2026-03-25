"""
runtime.py — Shared CLI runtime helpers.

Wraps senpi_common utilities for CLI command use.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import senpi_common as sc


def sync_before():
    """Pull latest state before running a command."""
    sc.git_pull()


def sync_after(message: str):
    """Commit and push after running a command."""
    sc.git_sync(message)


def acquire_command_lock(name: str) -> bool:
    """Acquire a lock for a CLI command."""
    return sc.acquire_lock(f"waifu-{name}")


def release_command_lock(name: str):
    """Release a CLI command lock."""
    sc.release_lock(f"waifu-{name}")
