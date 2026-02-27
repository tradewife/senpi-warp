#!/usr/bin/env bash
# Job 3: SM Flip Detector — every 5 minutes
set -euo pipefail
export SENPI_STATE_DIR="${SENPI_STATE_DIR:-/opt/senpi/senpi-state}"
SKILLS="${SENPI_SKILLS_DIR:-/opt/senpi/senpi-skills}"
SCRIPT="$SKILLS/wolf-strategy/scripts/sm-flip-check.py"
[ -f "$SCRIPT" ] && python3 "$SCRIPT" || echo "$(date -u +%H:%M:%S) sm-flip-check.py not found" >&2
