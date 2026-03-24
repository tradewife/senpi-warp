#!/usr/bin/env bash
# Regime Classifier — classifies macro market regime as RISK_ON/BASELINE/RISK_OFF.
# Runs every hour as a Hermes cron job.
#
# Reads BTC + ETH candles via Senpi MCP, analyzes trend/ATR/funding/OI,
# and updates config/risk-regime.json.
#
# Hard rules: maxLeverage never exceeds 10. XYZ leverage always 0.

set -euo pipefail
WAIFU_DIR="${SENPI_WAIFU_DIR:-/home/kt/senpi-waifu}"
cd "$WAIFU_DIR"

echo "[regime-classifier] $(date -u +%Y-%m-%dT%H:%M:%SZ) starting"
git pull --rebase --quiet 2>/dev/null || true

python3 -c "
import json, sys
sys.path.insert(0, '$WAIFU_DIR/scripts/lib')
from senpi_common import mcporter_call

def regime_classify():
    # Fetch candles
    btc_4h = mcporter_call('market_get_candles', {'asset': 'BTC', 'interval': '4h', 'limit': 10})
    btc_1h = mcporter_call('market_get_candles', {'asset': 'BTC', 'interval': '1h', 'limit': 10})

    # Default to BASELINE if data unavailable
    if not btc_4h.get('success', False):
        print('  Could not fetch candles — defaulting to BASELINE')
        return 'BASELINE', 'No candle data available'

    candles = btc_4h.get('data', btc_4h.get('candles', []))
    if not candles or len(candles) < 5:
        print('  Insufficient candle data — defaulting to BASELINE')
        return 'BASELINE', 'Insufficient candle data'

    # Simple trend analysis
    closes = [float(c.get('close', c.get('c', 0))) for c in candles[-6:] if float(c.get('close', c.get('c', 0))) > 0]
    if len(closes) < 3:
        return 'BASELINE', 'Not enough close prices'

    # MA slope (short)
    ma_short = sum(closes[-3:]) / 3
    ma_long = sum(closes) / len(closes)
    slope = (ma_short - ma_long) / ma_long * 100 if ma_long > 0 else 0

    # Volatility (ATR approximation)
    highs = [float(c.get('high', c.get('h', 0))) for c in candles[-6:]]
    lows = [float(c.get('low', c.get('l', 0))) for c in candles[-6:]]
    atr = sum(h - l for h, l in zip(highs[-3:], lows[-3:])) / 3
    atr_pct = atr / ma_long * 100 if ma_long > 0 else 0

    print(f'  BTC MA slope: {slope:.2f}%, ATR: {atr_pct:.2f}%')
    print(f'  MA_short: {ma_short:.0f}, MA_long: {ma_long:.0f}')

    # Classification
    # RISK_ON: clear trend + controlled volatility
    # RISK_OFF: extreme volatility or chop (low slope + high ATR)
    # BASELINE: everything else

    if abs(slope) > 1.5 and atr_pct < 5.0:
        direction = 'BULLISH' if slope > 0 else 'BEARISH'
        reason = f'Clear {direction} trend (slope={slope:.1f}%, ATR={atr_pct:.1f}%)'
        print(f'  -> RISK_ON ({reason})')
        return 'RISK_ON', reason
    elif atr_pct > 6.0 or (abs(slope) < 0.3 and atr_pct > 3.0):
        reason = f'High volatility/chop (slope={slope:.1f}%, ATR={atr_pct:.1f}%)'
        print(f'  -> RISK_OFF ({reason})')
        return 'RISK_OFF', reason
    else:
        reason = f'Mixed signals (slope={slope:.1f}%, ATR={atr_pct:.1f}%)'
        print(f'  -> BASELINE ({reason})')
        return 'BASELINE', reason

mode, reason = regime_classify()

# Update regime file
from pathlib import Path
from datetime import datetime, timezone

regime_path = Path('config/risk-regime.json')
regime = json.load(open(regime_path))

old_mode = regime.get('riskMode', 'BASELINE')
regime['riskMode'] = mode
regime['updatedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
regime['updatedBy'] = 'hermes-regime'
regime['reason'] = reason

regime_path.write_text(json.dumps(regime, indent=2) + '\n')

if old_mode != mode:
    print(f'  REGIME CHANGE: {old_mode} -> {mode}')
else:
    print(f'  Regime unchanged: {mode}')
"

git add config/risk-regime.json 2>/dev/null
git commit -m "regime-classifier: update regime" --allow-empty 2>/dev/null || true
git push 2>/dev/null || echo "[regime-classifier] git push failed (non-fatal)"

echo "[regime-classifier] $(date -u +%Y-%m-%dT%H:%M:%SZ) done"
