#!/usr/bin/env bash
# Arena Strategy Learner — studies Senpi Predators leaderboard for intelligence.
# Runs every 4 hours as a Hermes cron job.

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[arena-learner] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting"
git pull --rebase --quiet 2>/dev/null || true

python3 -c "
import json, sys
sys.path.insert(0, '$WAIFU_DIR/scripts/lib')
from senpi_common import mcporter_call
from pathlib import Path
from datetime import datetime, timezone

# 1. Fetch leaderboard
leaderboard = mcporter_call('discovery_get_top_traders', {'limit': 50, 'timeframe': '30d'})

# 2. Read our journal
journal = json.load(open('memory/trade-journal.json')) if Path('memory/trade-journal.json').exists() else []

# 3. Compute our stats
our_closes = [t for t in journal if t.get('action') == 'CLOSE']
our_wins = [t for t in our_closes if float(t.get('realizedPnl', 0)) > 0]
our_wr = len(our_wins) / len(our_closes) * 100 if our_closes else 0
our_pnl = sum(float(t.get('realizedPnl', 0)) for t in our_closes)

print(f'  Our stats: {len(our_closes)} closes, {our_wr:.1f}% WR, \${our_pnl:,.2f} PnL')

# 4. Analyze leaderboard
recommendations = []
top_traders = leaderboard.get('data', leaderboard.get('traders', []))

if not top_traders:
    print('  No leaderboard data available')
    Path('outputs/arena-learnings.json').write_text(json.dumps({
        'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'recommendations': [],
        'note': 'No leaderboard data available'
    }, indent=2) + '\n')
    exit(0)

# Top 5 stats
top5 = top_traders[:5]
avg_top5_wr = sum(float(t.get('winRate', 0)) for t in top5) / len(top5) if top5 else 0

print(f'  Top 5 avg WR: {avg_top5_wr:.1f}%')
for t in top5[:3]:
    print(f'    {t.get(\"user\", t.get(\"address\", \"?\"))[:16]}... WR={t.get(\"winRate\", 0)}% PnL={t.get(\"totalPnl\", 0)}')

# 5. Generate recommendations based on comparison
if our_wr < 40 and len(our_closes) >= 10:
    recommendations.append({
        'action': 'tighten_scores',
        'confidence': 'high',
        'reason': f'Win rate {our_wr:.0f}% < 40% across {len(our_closes)} trades. Tighten entry scores to improve selectivity.',
        'risk': 'reducing'
    })

if our_wr > 55 and len(our_closes) >= 10:
    recommendations.append({
        'action': 'slightly_loosen',
        'confidence': 'medium',
        'reason': f'Win rate {our_wr:.0f}% is strong. Could capture more edge with slightly lower scores (requires manual approval).',
        'risk': 'increasing'
    })

if avg_top5_wr > our_wr + 15:
    recommendations.append({
        'action': 'study_top_strategies',
        'confidence': 'medium',
        'reason': f'Arena top 5 avg WR ({avg_top5_wr:.0f}%) significantly exceeds ours ({our_wr:.0f}%). Study their holding patterns.',
        'risk': 'neutral'
    })

if len(our_closes) > 50 and our_pnl < 0:
    recommendations.append({
        'action': 'reduce_frequency',
        'confidence': 'high',
        'reason': f'{len(our_closes)} trades with negative total PnL. Over-trading detected — reduce scan frequency.',
        'risk': 'reducing'
    })

# Hard constraint reminders
recommendations.append({
    'action': 'reminder_max_leverage',
    'confidence': 'absolute',
    'reason': 'Max leverage is 10x. Never increase. This is proven across 22 agents.',
    'risk': 'rule'
})

# 6. Save learnings
learnings = {
    'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'ourStats': {
        'closes': len(our_closes),
        'winRate': round(our_wr, 1),
        'totalPnl': round(our_pnl, 2),
    },
    'arenaTop5AvgWinRate': round(avg_top5_wr, 1),
    'recommendations': recommendations,
    'appliedChanges': [],
}

Path('outputs/arena-learnings.json').write_text(json.dumps(learnings, indent=2) + '\n')
print(f'  Recommendations: {len(recommendations)}')
for r in recommendations:
    print(f'    [{r[\"confidence\"]}] {r[\"action\"]}: {r[\"reason\"][:80]}')
"

git add outputs/arena-learnings.json 2>/dev/null
git commit -m "arena-learner: update learnings" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[arena-learner] git push failed (non-fatal)"

echo "[arena-learner] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
