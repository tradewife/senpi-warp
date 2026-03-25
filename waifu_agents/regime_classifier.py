#!/usr/bin/env python3
"""
Waifu Regime Classifier - Runs every hour
Classifies macro market regime as RISK_ON / BASELINE / RISK_OFF.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def git_pull():
    """Pull latest changes from repo"""
    os.system("git pull > /dev/null 2>&1")


def load_json(filepath, default=None):
    """Load JSON file with fallback"""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(filepath, data):
    """Save JSON file"""
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def main():
    print(f"[Regime Classifier] Starting at {datetime.utcnow().isoformat()}Z")

    # Step 1: Git pull
    git_pull()

    # Step 2: In a real implementation, we would fetch BTC and ETH candles
    # For now, we'll simulate reading some market data or use a simple heuristic

    # Step 3: Read current regime to understand context
    regime_file = PROJECT_ROOT / "config" / "risk-regime.json"
    current_regime = load_json(regime_file, {"riskMode": "BASELINE"})

    # Step 4: Simple regime classification logic (placeholder)
    # In reality, this would analyze BTC/ETH 4h and 1h candles, funding rates, OI changes

    # For demonstration, we'll toggle between BASELINE and RISK_ON occasionally
    # In production, this would be based on actual market analysis
    import random

    current_hour = datetime.utcnow().hour

    # Simple logic: RISK_ON during certain hours (just for demo)
    if 9 <= current_hour <= 16:  # 9 AM - 4 PM UTC
        new_risk_mode = "RISK_ON"
        reason = "Demo: Active trading hours (9-16 UTC)"
    else:
        new_risk_mode = "BASELINE"
        reason = "Demo: Outside active trading hours"

    # Override to BASELINE if we detect any risk factors (conservative approach)
    # In reality, we'd check for extreme chop, funding blowouts, liquidation clusters
    if random.random() < 0.3:  # 30% chance of forcing BASELINE for safety
        new_risk_mode = "BASELINE"
        reason = "Conservative override: forcing BASELINE for risk management"

    # Step 5: Update regime if changed
    if current_regime.get("riskMode") != new_risk_mode:
        print(
            f"[Regime Classifier] Regime change: {current_regime.get('riskMode')} -> {new_risk_mode}"
        )

        new_regime = {
            "riskMode": new_risk_mode,
            "updatedAt": datetime.utcnow().isoformat() + "Z",
            "updatedBy": "waifu-regime",
            "reason": reason,
        }

        save_json(regime_file, new_regime)

        # Git commit and push
        os.system("git add config/risk-regime.json > /dev/null 2>&1")
        os.system(
            f'git commit -m "regime classifier: {new_risk_mode} - {reason}" > /dev/null 2>&1'
        )
        os.system("git push > /dev/null 2>&1")

        print(f"[Regime Classifier] Updated regime to {new_risk_mode}")
    else:
        print(f"[Regime Classifier] Regime unchanged: {new_risk_mode}")


if __name__ == "__main__":
    main()
