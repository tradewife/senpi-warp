#!/usr/bin/env bash
# Job 5: Health Check + git sync — every 10 minutes
set -euo pipefail
export SENPI_STATE_DIR="${SENPI_STATE_DIR:-/opt/senpi/senpi-state}"
SKILLS="${SENPI_SKILLS_DIR:-/opt/senpi/senpi-skills}"

# Pull any config changes from Oz agents
git -C "$SENPI_STATE_DIR" pull --rebase --quiet 2>/dev/null || true

# Reconcile closed positions into trade journal
python3 "$SENPI_STATE_DIR/scripts/vps/reconcile-closes.py" 2>/dev/null || true

# Run health check if available
SCRIPT="$SKILLS/wolf-strategy/scripts/job-health-check.py"
[ -f "$SCRIPT" ] && python3 "$SCRIPT"

# Commit and push any state changes
cd "$SENPI_STATE_DIR"
git add -A
git diff --cached --quiet || git commit -m "auto: health check sync" --no-verify
git push --quiet 2>/dev/null || true
