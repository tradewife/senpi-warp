#!/usr/bin/env bash
set -euo pipefail

APOLLO_DIR="/opt/hermes-apollo"
HERMES_HOME="${HERMES_HOME:-/root/.hermes}"

echo "[install-apollo] Installing Apollo distribution layer..."

mkdir -p "$HERMES_HOME" "$HERMES_HOME/skills" "$HERMES_HOME/bin"

if [ -d "$APOLLO_DIR/apollo" ]; then
    cp "$APOLLO_DIR/apollo/defaults/SOUL.md" "$HERMES_HOME/SOUL.md" 2>/dev/null || true
    cp -R "$APOLLO_DIR/apollo/skills/." "$HERMES_HOME/skills/" 2>/dev/null || true
    echo "[install-apollo] Copied Apollo SOUL.md and skills"
else
    echo "[install-apollo] WARNING: apollo/ directory not found in $APOLLO_DIR"
fi

if [ -f "$APOLLO_DIR/apollo/defaults/config-overrides.yaml" ]; then
    python3 - "$HERMES_HOME/config.yaml" "$APOLLO_DIR/apollo/defaults/config-overrides.yaml" <<'PY'
from __future__ import annotations
import sys
from pathlib import Path
import yaml

def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    return override

config_path = Path(sys.argv[1])
override_path = Path(sys.argv[2])

current = {}
if config_path.exists():
    current = yaml.safe_load(config_path.read_text()) or {}

overrides = yaml.safe_load(override_path.read_text()) or {}
merged = deep_merge(current, overrides)

config_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
PY
    echo "[install-apollo] Merged config overrides"
fi

echo "[install-apollo] Apollo installed into $HERMES_HOME"
