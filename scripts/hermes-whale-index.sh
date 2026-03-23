#!/usr/bin/env bash
# Whale Index Manager — daily copy-trade portfolio review and rebalance.
# Runs daily at 01:00 as a Hermes cron job.
# Reads memory/whale-index-prompt.md and follows it.

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[whale-index] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting"
git pull --rebase --quiet 2>/dev/null || true

python3 -c "
import json, subprocess
from pathlib import Path
from datetime import datetime, timezone

def mcporter_call(tool, args={}):
    cmd = ['mcporter', 'call', 'senpi', tool]
    if args:
        cmd += ['--json', json.dumps(args)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout) if r.stdout else {}

# Load state
state_path = Path('outputs/whale-index-state.json')
state = json.load(open(state_path)) if state_path.exists() else {
    'slots': [], 'watchlist': {}, 'notes': [],
    'budget': 1000, 'riskTolerance': 'conservative', 'targetSlots': 2
}

state['updatedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# Discover top traders
traders = mcporter_call('discovery_get_top_traders', {'limit': 50, 'timeframe': '30d'})
top = traders.get('data', traders.get('traders', []))

if not top:
    print('  No trader data available — skipping')
    state['notes'].append(f'{datetime.now(timezone.utc).isoformat()}: No discovery data available')
    state_path.write_text(json.dumps(state, indent=2) + '\n')
    exit(0)

print(f'  Discovery returned {len(top)} traders')

# Apply filters based on risk tolerance
risk = state.get('riskTolerance', 'conservative')
allowed_labels = {
    'conservative': ['ELITE'],
    'moderate': ['ELITE', 'RELIABLE'],
    'aggressive': ['ELITE', 'RELIABLE', 'BALANCED'],
}.get(risk, ['ELITE'])

max_leverage_map = {
    'conservative': 10,
    'moderate': 15,
    'aggressive': 25,
}

max_leverage = max_leverage_map.get(risk, 10)
allowed = [t for t in top if t.get('consistencyLabel', t.get('label', '')) in allowed_labels]
print(f'  After risk filter ({risk}): {len(allowed)} candidates')

# Score candidates
def score_trader(t):
    pnl_rank = 0
    wr = float(t.get('winRate', 0))
    consistency = float(t.get('consistency', 0))
    hold_time = float(t.get('avgHoldTime', 0))
    drawdown = float(t.get('maxDrawdown', 100))
    return 0.35 * 50 + 0.25 * wr + 0.20 * consistency + 0.10 * min(hold_time, 100) + 0.10 * (100 - drawdown)

scored = [(score_trader(t), t) for t in allowed]
scored.sort(key=lambda x: x[0], reverse=True)

# Exclude already-active traders
active_addresses = set(s.get('traderAddress', '') for s in state.get('slots', []))
new_candidates = [(s, t) for s, t in scored if t.get('address', t.get('traderAddress', '')) not in active_addresses]

# Monitor existing slots
for slot in state.get('slots', []):
    addr = slot.get('traderAddress', '')
    trader = next((t for t in top if t.get('address', '') == addr), None)
    if not trader:
        slot['status'] = 'WATCH'
        slot['watchCount'] = slot.get('watchCount', 0) + 1
        print(f'  SLOT {slot.get(\"asset\",\"?\")}: WATCH (trader not found)')
        continue

    rank = next((i+1 for i, t in enumerate(top) if t.get('address', '') == addr), 99)
    slot['lastRank'] = rank
    slot['lastCheckedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if rank <= 50:
        slot['status'] = 'HOLD'
        slot['watchCount'] = 0
        print(f'  SLOT {slot.get(\"asset\",\"?\")}: HOLD (rank {rank})')
    elif rank <= 75:
        slot['watchCount'] = slot.get('watchCount', 0) + 1
        slot['status'] = 'WATCH' if slot['watchCount'] >= 2 else 'HOLD'
        print(f'  SLOT {slot.get(\"asset\",\"?\")}: {slot[\"status\"]} (rank {rank}, watch {slot[\"watchCount\"]})')
    else:
        slot['watchCount'] = slot.get('watchCount', 0) + 1
        slot['status'] = 'WATCH'
        print(f'  SLOT {slot.get(\"asset\",\"?\")}: WATCH (rank {rank}, watch {slot[\"watchCount\"]})')

# Fill empty slots
target_slots = state.get('targetSlots', 2)
active_count = sum(1 for s in state.get('slots', []) if s.get('status') in ('HOLD', 'WATCH'))
empty_slots = target_slots - active_count

if empty_slots > 0 and new_candidates:
    print(f'  Filling {min(empty_slots, len(new_candidates))} empty slot(s)')
    for score, trader in new_candidates[:empty_slots]:
        addr = trader.get('address', trader.get('traderAddress', ''))
        # Note: actual mirror strategy creation requires mcporter call
        # For paper trading, just track the slot
        new_slot = {
            'traderAddress': addr,
            'traderLabel': trader.get('consistencyLabel', trader.get('label', 'UNKNOWN')),
            'status': 'HOLD',
            'watchCount': 0,
            'createdAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'lastCheckedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'lastRank': next((i+1 for i, t in enumerate(top) if t.get('address', '') == addr), 99),
            'score': round(score, 1),
            'winRate': trader.get('winRate', 0),
            'totalPnl': trader.get('totalPnl', 0),
        }
        state['slots'].append(new_slot)
        print(f'    Added: {addr[:12]}... (score={score:.1f})')

state_path.write_text(json.dumps(state, indent=2) + '\n')
print(f'  Portfolio: {len(state[\"slots\"])} slots')
"

git add outputs/whale-index-state.json 2>/dev/null
git commit -m "whale-index: daily rebalance" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[whale-index] git push failed (non-fatal)"

echo "[whale-index] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
