import json
import statistics
import math
from datetime import datetime, timezone

# Load the saved data
with open("btc_data.json", "r") as f:
    btc_data = json.load(f)
with open("eth_data.json", "r") as f:
    eth_data = json.load(f)

print("Data loaded successfully")
print(f"BTC 1h candles: {len(btc_data['data']['candles']['1h'])}")
print(f"ETH 1h candles: {len(eth_data['data']['candles']['1h'])}")
print(f"BTC 4h candles: {len(btc_data['data']['candles']['4h'])}")
print(f"ETH 4h candles: {len(eth_data['data']['candles']['4h'])}")


# Helper functions
def true_range(high, low, prev_close):
    """Calculate True Range"""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles, period):
    """Calculate ATR for given period"""
    if len(candles) < period + 1:
        return None

    tr_values = []
    for i in range(1, len(candles)):
        high = float(candles[i]["h"])
        low = float(candles[i]["l"])
        prev_close = float(candles[i - 1]["c"])
        tr = true_range(high, low, prev_close)
        tr_values.append(tr)

    # Use the last 'period' values
    if len(tr_values) >= period:
        return statistics.mean(tr_values[-period:])
    else:
        return statistics.mean(tr_values) if tr_values else None


def ma_slope(candles, period):
    """Calculate MA slope as percentage change over period"""
    if len(candles) < period:
        return None

    # Get closing prices for the last 'period' candles
    closes = [float(candle["c"]) for candle in candles[-period:]]

    # Calculate linear regression slope
    n = len(closes)
    x_vals = list(range(n))
    x_mean = sum(x_vals) / n
    y_mean = sum(closes) / n

    numerator = sum((x_vals[i] - x_mean) * (closes[i] - y_mean) for i in range(n))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if denominator == 0:
        return 0

    slope = numerator / denominator
    # Convert to percentage change over the period
    # Slope is change per candle, multiply by period to get total change
    total_change_pct = (slope * period) / y_mean * 100
    return total_change_pct


# Process BTC data
btc_1h_candles = btc_data["data"]["candles"]["1h"]
btc_4h_candles = btc_data["data"]["candles"]["4h"]

# Process ETH data
eth_1h_candles = eth_data["data"]["candles"]["1h"]
eth_4h_candles = eth_data["data"]["candles"]["4h"]

# Calculate ATR for last 24 1h candles
btc_atr = atr(btc_1h_candles, 24)
eth_atr = atr(eth_1h_candles, 24)

# Calculate MA slope for last 6 4h candles
btc_slope = ma_slope(btc_4h_candles, 6)
eth_slope = ma_slope(eth_4h_candles, 6)

# Get current prices
btc_current_price = float(btc_1h_candles[-1]["c"])
eth_current_price = float(eth_1h_candles[-1]["c"])

# Calculate ATR as percentage of price
btc_atr_pct = (btc_atr / btc_current_price) * 100 if btc_atr else 0
eth_atr_pct = (eth_atr / eth_current_price) * 100 if eth_atr else 0

print("\n=== CALCULATION RESULTS ===")
print(f"BTC Current Price: ${btc_current_price:,.2f}")
print(f"ETH Current Price: ${eth_current_price:,.2f}")
print(f"BTC ATR (24-period): {btc_atr:.2f} -> {btc_atr_pct:.2f}% of price")
print(f"ETH ATR (24-period): {eth_atr:.2f} -> {eth_atr_pct:.2f}% of price")
print(f"BTC 4h MA slope (6-period): {btc_slope:.2f}%")
print(f"ETH 4h MA slope (6-period): {eth_slope:.2f}%")


# Determine regime for each asset based on rules
def classify_regime(slope_pct, atr_pct):
    """Classify regime based on slope and ATR"""
    # RISK_ON: clear directional trend (slope > 1.5%) with controlled volatility (ATR < 5%)
    if slope_pct > 1.5 and atr_pct < 5.0:
        return "RISK_ON"
    # RISK_OFF: extreme volatility/chop (ATR > 6%) or very low slope (< 0.3%) with elevated volatility
    elif atr_pct > 6.0 or (
        slope_pct < 0.3 and atr_pct > 5.0
    ):  # elevated volatility defined as ATR > 5%
        return "RISK_OFF"
    else:
        return "BASELINE"


btc_regime = classify_regime(btc_slope, btc_atr_pct)
eth_regime = classify_regime(eth_slope, eth_atr_pct)

print(f"\nBTC Regime Classification: {btc_regime}")
print(f"ETH Regime Classification: {eth_regime}")

# Overall regime logic
if btc_regime == "RISK_ON" and eth_regime == "RISK_ON":
    overall_regime = "RISK_ON"
    reason = "Both BTC and ETH show clear directional trend (>1.5% slope) with controlled volatility (<5% ATR)"
elif btc_regime == "RISK_OFF" or eth_regime == "RISK_OFF":
    overall_regime = "RISK_OFF"
    if btc_atr_pct > 6.0 or eth_atr_pct > 6.0:
        reason = "Extreme volatility/chop detected (ATR > 6%)"
    else:
        reason = "Very low slope (<0.3%) with elevated volatility"
else:
    overall_regime = "BASELINE"
    reason = "Mixed signals or insufficient trend/volatility for clear regime"

print(f"\nOverall Market Regime: {overall_regime}")
print(f"Reason: {reason}")

# Update config/risk-regime.json
config_path = "config/risk-regime.json"
try:
    with open(config_path, "r") as f:
        config = json.load(f)
except Exception as e:
    print(f"Error reading config: {e}")
    config = {"regimes": {}, "globalGuardrails": {}}

# Update the config
config["riskMode"] = overall_regime
config["updatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
config["updatedBy"] = "waifu-regime"
config["reason"] = reason

# Write back to file
with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print(f"\nUpdated {config_path}")
print("Done!")
