#!/usr/bin/env python3
"""
Hermes Trade Evaluator - Runs every 15 minutes
Validates queued scanner signals and executes approved trades.
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
        with open(filepath, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(filepath, data):
    """Save JSON file"""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

def main():
    print(f"[Trade Evaluator] Starting at {datetime.utcnow().isoformat()}Z")
    
    # Step 1: Git pull
    git_pull()
    
    # Step 2: Read pending entries
    pending_file = PROJECT_ROOT / "state" / "pending-entries.json"
    pending_entries = load_json(pending_file, [])
    
    if not pending_entries:
        print("[Trade Evaluator] No pending entries")
        return
    
    print(f"[Trade Evaluator] Found {len(pending_entries)} pending entries")
    
    # Step 3: Read brain policy
    brain_file = PROJECT_ROOT / "outputs" / "autonomous-brain.json"
    brain = load_json(brain_file, {})
    
    # Step 4: Read risk regime
    regime_file = PROJECT_ROOT / "config" / "risk-regime.json"
    regime = load_json(regime_file, {"riskMode": "RISK_OFF"})
    
    if regime.get("riskMode") == "RISK_OFF":
        print("[Trade Evaluator] RISK_OFF - skipping all entries")
        # Clear pending entries anyway
        save_json(pending_file, [])
        return
    
    # Step 5: Process each entry
    processed = []
    remaining = []
    
    for entry in pending_entries:
        # Basic validation - in real implementation, this would be more complex
        scanner = entry.get("scanner", "unknown")
        asset = entry.get("asset", "")
        signal_score = entry.get("signalScore", "PASS")
        
        # Check if scanner is enabled per brain policy
        scanner_policy = brain.get("scannerPriorities", {}).get(scanner, {})
        if scanner_policy.get("blocked", False):
            print(f"[Trade Evaluator] Blocking {scanner} signal for {asset} per brain policy")
            remaining.append(entry)
            continue
            
        # Apply HARD constraints (these would be checked by mechanical layer)
        # For now, we just log and assume mechanical layer enforces them
        
        # Simulate trade execution - in reality this would call mcporter
        print(f"[Trade Evaluator] Processing {scanner} signal for {asset} (score: {signal_score})")
        
        # Record trade (simulated)
        trade_record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "action": "OPEN",
            "asset": asset,
            "scanner": scanner,
            "signalScore": signal_score,
            "status": "SIMULATED"
        }
        processed.append(trade_record)
    
    # Step 6: Update trade journal
    journal_file = PROJECT_ROOT / "memory" / "trade-journal.json"
    journal = load_json(journal_file, [])
    journal.extend(processed)
    save_json(journal_file, journal)
    
    # Step 7: Clear processed entries, keep remaining
    save_json(pending_file, remaining)
    
    # Step 8: Git commit and push
    os.system("git add . > /dev/null 2>&1")
    os.system('git commit -m "trade evaluator: processed signals" > /dev/null 2>&1')
    os.system("git push > /dev/null 2>&1")
    
    print(f"[Trade Evaluator] Processed {len(processed)} entries, {len(remaining)} remaining")

if __name__ == "__main__":
    main()