import json
import numpy as np
from datetime import datetime, timezone

def calculate_atr(candles, period=14):
    """Calculate Average True Range for given candles"""
    if len(candles) < period + 1:
        return None
    
    true_ranges = []
    for i in range(1, len(candles)):
        high = float(candles[i]['h'])
        low = float(candles[i]['l'])
        prev_close = float(candles[i-1]['c'])
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr = max(tr1, tr2, tr3)
        true_ranges.append(tr)
    
    # Use the last 'period' values for ATR
    atr = np.mean(true_ranges[-period:])
    return atr

def calculate_slope(candles):
    """Calculate slope (percentage change) over the period"""
    if len(candles) < 2:
        return None
    
    # Use first and last close prices
    first_close = float(candles[0]['c'])
    last_close = float(candles[-1]['c'])
    
    # Calculate percentage change
    slope_pct = ((last_close - first_close) / first_close) * 100
    return slope_pct

def get_latest_funding_rate(funding_history):
    """Get the most recent funding rate"""
    if not funding_history or len(funding_history) == 0:
        return 0
    # Assuming funding history is sorted with most recent first
    latest = funding_history[0]
    return float(latest.get('r', 0))

def process_asset_data(data):
    """Process asset data to extract required metrics"""
    # Extract 1h candles (last 24)
    candles_1h = data['data']['candles']['1h'][-24:] if len(data['data']['candles']['1h']) >= 24 else data['data']['candles']['1h']
    
    # Extract 4h candles (last 6)
    candles_4h = data['data']['candles']['4h'][-6:] if len(data['data']['candles']['4h']) >= 6 else data['data']['candles']['4h']
    
    # Extract funding rate history
    funding_history = data['data'].get('fundingHistory', [])
    
    # Calculate metrics
    atr = calculate_atr(candles_1h)
    slope = calculate_slope(candles_4h)
    funding_rate = get_latest_funding_rate(funding_history)
    
    # Calculate ATR as percentage of price
    if atr is not None and len(candles_1h) > 0:
        avg_price = np.mean([float(c['c']) for c in candles_1h])
        atr_pct = (atr / avg_price) * 100
    else:
        atr_pct = None
    
    return {
        'atr': atr,
        'atr_pct': atr_pct,
        'slope': slope,
        'funding_rate': funding_rate,
        'price': float(candles_1h[-1]['c']) if len(candles_1h) > 0 else None
    }

# Load the data (in practice, we'd get this from the API responses)
# For now, we'll simulate having the data loaded
print("Processing market data...")

# Since we can't directly access the large JSON responses in this environment,
# we'll need to extract the key information manually or write a simpler approach.
# Let's instead read the raw data and process it step by step.

# Actually, let's try a different approach - we'll create a simplified processor
# that works with the data we can access.