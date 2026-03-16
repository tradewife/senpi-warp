#!/usr/bin/env bash
# Job 4: Watchdog — every 5 minutes (offset by 2min from SM flip)
set -euo pipefail
export SENPI_STATE_DIR="${SENPI_STATE_DIR:-/opt/senpi/senpi-waifu}"
SKILLS="${SENPI_SKILLS_DIR:-/opt/senpi/senpi-skills}"
SCRIPT="$SKILLS/wolf-strategy/scripts/wolf-monitor.py"
[ -f "$SCRIPT" ] && python3 "$SCRIPT" || echo "$(date -u +%H:%M:%S) wolf-monitor.py not found" >&2
