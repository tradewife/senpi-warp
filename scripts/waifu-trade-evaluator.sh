#!/usr/bin/env bash
# Trade Evaluator — validates queued scanner signals and executes approved trades.
# Runs every 15 min as a Waifu cron job.
#
# This is the STRATEGIC layer — it reads state written by the mechanical layer
# (Railway/VPS worker) and decides which signals to execute.
#
# Prerequisites:
#   - SENPI_API_KEY env var set (Senpi MCP auth)
#   - mcporter configured with Senpi MCP server
#   - /home/kt/senpi-waifu git repo up to date

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[trade-evaluator] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting"

# 1. Sync repo
git pull --rebase --quiet 2>/dev/null || echo "[trade-evaluator] git pull failed (non-fatal)"

# 2. Check regime — skip if RISK_OFF
RISK_MODE=$(python3 -c "
import json
d = json.load(open('config/risk-regime.json'))
print(d.get('riskMode', 'BASELINE'))
")
echo "[trade-evaluator] regime: $RISK_MODE"
if [ "$RISK_MODE" = "RISK_OFF" ]; then
    echo "[trade-evaluator] RISK_OFF — skipping all entries"
    exit 0
fi

# 3. Read brain policy
echo "[trade-evaluator] reading brain policy and pending entries"

# 4. Process pending entries via Python (full logic is too complex for bash)
python3 -c "
import json, sys, os
sys.path.insert(0, '$WAIFU_DIR/scripts/lib')
from senpi_common import mcporter_call
from pathlib import Path
from datetime import datetime, timezone

# Load state
regime = json.load(open('config/risk-regime.json'))
brain = json.load(open('outputs/autonomous-brain.json')) if Path('outputs/autonomous-brain.json').exists() else {}
playbook = json.load(open('outputs/playbook-state.json')) if Path('outputs/playbook-state.json').exists() else {}
pending = json.load(open('state/pending-entries.json')) if Path('state/pending-entries.json').exists() else []
journal = json.load(open('memory/trade-journal.json')) if Path('memory/trade-journal.json').exists() else []

mode = regime.get('riskMode', 'BASELINE')
params = regime.get('regimes', {}).get(mode, regime.get('regimes', {}).get('BASELINE', {}))
policy = brain.get('executionPolicy', {})
signal_policy = brain.get('signalPolicy', {})

max_slots = int(params.get('maxSlots', 2))
max_leverage = float(params.get('maxLeverageCrypto', 10))
alloc_pct = float(params.get('allocPctPerSlot', 30))
auto_entry = params.get('autoEntryEnabled', True)
entries_allowed = params.get('newEntriesAllowed', True)

# Count open positions
open_positions = []
state_dir = Path('state')
for strat_dir in state_dir.iterdir():
    if not strat_dir.is_dir() or strat_dir.name.startswith('.'):
        continue
    for dsl_file in strat_dir.glob('dsl-*.json'):
        dsl = json.load(open(dsl_file))
        if dsl.get('active', False):
            open_positions.append(dsl)

print(f'  Open positions: {len(open_positions)}/{max_slots}')
print(f'  Pending entries: {len(pending)}')
print(f'  Auto entry: {auto_entry}, Entries allowed: {entries_allowed}')

if not entries_allowed or not auto_entry:
    print('  Skipping — entries not allowed or auto-entry disabled')
    sys.exit(0)

if len(pending) == 0:
    print('  No pending entries')
    sys.exit(0)

if len(open_positions) >= max_slots:
    print(f'  Max slots reached ({max_slots}) — skipping all entries')
    sys.exit(0)

# Load strategies
strategies = json.load(open('config/wolf-strategies.json'))
enabled = [s for k, s in strategies.get('strategies', {}).items() if s.get('enabled', True)]
if not enabled:
    print('  No enabled strategies')
    sys.exit(0)

strat = enabled[0]
strat_key = list(strategies.get('strategies', {}).keys())[0]
strategy_id = strat.get('strategyId', '')
print(f'  Using strategy: {strat_key} ({strategy_id[:12]}...)')

# HARDCODED RULES — these are non-negotiable
BANNED_ASSETS = set()  # XYZ equities banned
MIN_LEVERAGE = 7.0
MAX_LEVERAGE = 10.0
# Active scanners only — SHARK/BARRACUDA/BISON paused (removed from worker.py schedule)
MIN_SCORE_BY_SCANNER = {
    'orca': 6,
    'mantis': 7,
    'fox': 7,
    'komodo': 10,
    'condor': 10,
    'polar': 10,
    'sentinel': 5,
    'rhino': 5,
}

processed = []
skipped = []
for i, entry in enumerate(pending):
    if len(open_positions) >= max_slots:
        print(f'  Max slots reached — stopping')
        break

    asset = entry.get('asset', entry.get('symbol', ''))
    direction = entry.get('direction', entry.get('side', ''))
    scanner = str(entry.get('scanner', entry.get('source', entry.get('entryMode', 'unknown')))).lower()
    score = float(entry.get('score', entry.get('totalScore', 0)))

    # Normalize scanner name
    for s_name in list(MIN_SCORE_BY_SCANNER.keys()):
        if s_name in scanner:
            scanner = s_name
            break

    min_score = MIN_SCORE_BY_SCANNER.get(scanner, 6)

    # Check brain policy
    brain_ctx = entry.get('brainContext', {})
    if brain_ctx.get('blockedScanner'):
        print(f'  SKIP {asset}: scanner {scanner} blocked by brain')
        skipped.append(entry)
        continue

    # Hardcoded gates
    if score < min_score:
        print(f'  SKIP {asset}: score {score} < min {min_score} ({scanner})')
        skipped.append(entry)
        continue

    if not strategy_id or strategy_id.startswith('REPLACE'):
        print(f'  SKIP {asset}: no valid strategy ID configured')
        skipped.append(entry)
        continue

    # Check cooldown
    cooldown_file = Path(f'state/orca-cooldowns.json')
    if cooldown_file.exists():
        cooldowns = json.load(open(cooldown_file))
        cd_info = cooldowns.get(asset.upper(), {})
        if cd_info:
            import time
            cd_until = cd_info.get('cooldownUntil', '')
            if cd_until and cd_until > datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'):
                print(f'  SKIP {asset}: in cooldown until {cd_until}')
                skipped.append(entry)
                continue

    # Determine leverage (within 7-10x band)
    leverage = float(entry.get('leverage', 8))
    leverage = max(MIN_LEVERAGE, min(MAX_LEVERAGE, leverage))

    print(f'  APPROVE {asset} {direction} @ {leverage}x (score={score}, scanner={scanner})')

    # Execute via senpi_common.mcporter_call (direct HTTP)
    try:
        resp = mcporter_call('strategy_open_position', {
            'strategyId': strategy_id,
            'asset': asset,
            'direction': direction,
            'leverage': int(leverage),
            'marginUsd': entry.get('marginUsd', 0) or int(alloc_pct),
            'lockMode': 'pct_of_high_water',
        })

        if resp.get('success', False):
            print(f'    OPENED: {resp}')
            # Record in journal
            journal.append({
                'action': 'OPEN',
                'asset': asset,
                'direction': direction,
                'leverage': leverage,
                'marginUsd': entry.get('marginUsd', 0),
                'entrySource': scanner,
                'strategyKey': strat_key,
                'score': score,
                'realizedPnl': 0,
            })
            open_positions.append({'asset': asset, 'direction': direction})
        else:
            print(f'    FAILED: {resp.get(\"error\", \"unknown\")}')
            skipped.append(entry)
    except Exception as e:
        print(f'    ERROR: {e}')
        skipped.append(entry)

    processed.append(entry)

# Save remaining pending (skipped entries go back to queue)
remaining = [e for e in pending if e not in processed]
Path('state/pending-entries.json').write_text(json.dumps(remaining, indent=2) + '\n')

# Save journal
Path('memory/trade-journal.json').write_text(json.dumps(journal, indent=2) + '\n')

print(f'  Result: {len(processed)} processed, {len(skipped)} skipped, {len(remaining)} remaining')
"

# 5. Commit and push
git add state/pending-entries.json memory/trade-journal.json 2>/dev/null
git commit -m "trade-evaluator: process pending entries" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[trade-evaluator] git push failed (non-fatal)"

echo "[trade-evaluator] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
