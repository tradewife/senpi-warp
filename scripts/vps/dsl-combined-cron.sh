#!/usr/bin/env bash
# Job 2: DSL Combined Runner — every 3 minutes
# Delegates to the actual senpi-skills dsl-combined.py script
# which iterates all active DSL state files across all strategies.
set -euo pipefail

export SENPI_STATE_DIR="${SENPI_STATE_DIR:-/opt/senpi/senpi-state}"
SKILLS="${SENPI_SKILLS_DIR:-/opt/senpi/senpi-skills}"
SCRIPT="$SKILLS/wolf-strategy/scripts/dsl-combined.py"

if [ -f "$SCRIPT" ]; then
    python3 "$SCRIPT"
else
    echo "$(date -u +%H:%M:%S) dsl-combined.py not found at $SCRIPT" >&2
fi
