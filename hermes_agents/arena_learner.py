#!/usr/bin/env python3
"""
Hermes Arena Learner - Runs every 4 hours
Analyze Senpi Predators leaderboard to generate actionable recommendations.
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

def analyze_arena_data(arena_state):
    """Analyze arena state data and generate actionable recommendations"""
    leaderboard = arena_state.get("leaderboard", [])
    top_performers = arena_state.get("topPerformers", [])
    insights = arena_state.get("insights", {})
    
    # Extract key metrics
    if not leaderboard:
        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "ARENA_LEARNINGS",
            "recommendations": [],
            "note": "No leaderboard data available"
        }
    
    # Analyze top 3 performers
    top_3 = leaderboard[:3] if len(leaderboard) >= 3 else leaderboard
    
    # Calculate performance metrics
    winning_strategies = [s for s in leaderboard if s.get("totalPnl", 0) > 0]
    losing_strategies = [s for s in leaderboard if s.get("totalPnl", 0) <= 0]
    
    # Analyze volume vs performance correlation
    high_volume_strategies = sorted(leaderboard, key=lambda x: x.get("totalVolume", 0), reverse=True)[:5]
    low_volume_strategies = sorted(leaderboard, key=lambda x: x.get("totalVolume", 0))[:5]
    
    # Analyze trade frequency
    frequent_traders = sorted(leaderboard, key=lambda x: x.get("totalTrades", 0), reverse=True)[:5]
    infrequent_traders = sorted(leaderboard, key=lambda x: x.get("totalTrades", 0))[:5]
    
    # Generate recommendations based on analysis
    recommendations = []
    
    # 1. Best performing strategy analysis
    if winning_strategies:
        best = max(winning_strategies, key=lambda x: x.get("totalPnl", 0))
        roi_value = float(best.get("roi", "0%").replace("%", "")) if best.get("roi", "0%").replace("%", "") else 0
        recommendations.append({
            "scanner": best.get("name", "").upper().replace(" ", "_"),
            "action": "INCREASE_WEIGHT",
            "confidence": "HIGH" if roi_value > 20 else "MEDIUM",
            "reason": f"{best.get('name')} showing strongest performance with {best.get('totalPnl')} PnL ({best.get('roi')} ROI)"
        })
    
    # 2. Volume efficiency analysis
    if high_volume_strategies:
        # Check if high volume correlates with good performance
        high_volume_winners = [s for s in high_volume_strategies if s.get("totalPnl", 0) > 0]
        if len(high_volume_winners) >= 3:
            recommendations.append({
                "scanner": "VOLUME_ANALYSIS",
                "action": "MAINTAIN_WEIGHT",
                "confidence": "MEDIUM",
                "reason": "High volume strategies showing positive correlation with performance"
            })
        else:
            recommendations.append({
                "scanner": "VOLUME_ANALYSIS",
                "action": "DECREASE_WEIGHT",
                "confidence": "MEDIUM",
                "reason": "High volume strategies underperforming - consider favoring lower volume, higher conviction approaches"
            })
    
    # 3. Trade frequency analysis
    if frequent_traders:
        freq_winners = [s for s in frequent_traders if s.get("totalPnl", 0) > 0]
        if len(freq_winners) < 2:  # Less than 40% of frequent traders are winners
            recommendations.append({
                "scanner": "FREQUENCY_ANALYSIS",
                "action": "DECREASE_WEIGHT",
                "confidence": "HIGH",
                "reason": "Frequent trading showing negative correlation with performance - favor lower frequency, higher conviction strategies"
            })
        else:
            recommendations.append({
                "scanner": "FREQUENCY_ANALYSIS",
                "action": "MAINTAIN_WEIGHT",
                "confidence": "MEDIUM",
                "reason": "Frequent trading showing mixed results - maintain current approach"
            })
    
    # 4. Specific strategy insights from top performers
    orca_strategies = [s for s in leaderboard if "orca" in s.get("name", "").lower()]
    if orca_strategies:
        orca_performance = [s for s in orca_strategies if s.get("totalPnl", 0) > 0]
        if orca_performance:
            best_orca = max(orca_performance, key=lambda x: x.get("totalPnl", 0))
            recommendations.append({
                "scanner": "ORCA",
                "action": "INCREASE_WEIGHT",
                "confidence": "HIGH" if best_orca.get("totalPnl", 0) > 50 else "MEDIUM",
                "reason": f"Orca family showing strong performance - {best_orca.get('name')} with {best_orca.get('totalPnl')} PnL"
            })
        else:
            recommendations.append({
                "scanner": "ORCA",
                "action": "DECREASE_WEIGHT",
                "confidence": "LOW",
                "reason": "Orca strategies currently underperforming - reduce allocation pending improvement"
            })
    
    # 5. Polar strategy (current leader)
    polar = next((s for s in leaderboard if s.get("name", "").lower() == "polar"), None)
    if polar and polar.get("totalPnl", 0) > 200:  # Exceptional performance
        recommendations.append({
            "scanner": "POLAR",
            "action": "INCREASE_WEIGHT",
            "confidence": "HIGH",
            "reason": f"Polar strategy dominating with {polar.get('totalPnl')} PnL ({polar.get('roi')} ROI) - exceptional conviction approach"
        })
    elif polar and polar.get("totalPnl", 0) > 0:
        recommendations.append({
            "scanner": "POLAR",
            "action": "MAINTAIN_WEIGHT",
            "confidence": "MEDIUM",
            "reason": f"Polar showing positive performance - maintain current weight"
        })
    
    # 6. Losing strategies to avoid
    if losing_strategies:
        worst = min(losing_strategies, key=lambda x: x.get("totalPnl", 0))
        if worst.get("totalPnl", 0) < -50:  # Significantly losing
            recommendations.append({
                "scanner": worst.get("name", "").upper().replace(" ", "_"),
                "action": "DECREASE_WEIGHT",
                "confidence": "HIGH",
                "reason": f"{worst.get('name')} showing significant losses ({worst.get('totalPnl')} PnL) - reduce or eliminate exposure"
            })
    
    # 7. Insights-based recommendations
    winning_traits = insights.get("winningTraits", [])
    losing_traits = insights.get("losingTraits", [])
    
    if "higher conviction" in winning_traits:
        recommendations.append({
            "scanner": "CONVICTION_ANALYSIS",
            "action": "INCREASE_WEIGHT",
            "confidence": "MEDIUM",
            "reason": "Analysis shows higher conviction strategies winning - favor fewer, larger positions over diversification"
        })
    
    if "over-trading" in losing_traits:
        recommendations.append({
            "scanner": "TRADING_FREQUENCY",
            "action": "DECREASE_WEIGHT",
            "confidence": "MEDIUM",
            "reason": "Over-trading identified as losing trait - reduce frequency of position adjustments"
        })
    
    # Ensure we have at least some recommendations
    if not recommendations:
        recommendations = [
            {
                "scanner": "GENERAL",
                "action": "MAINTAIN_WEIGHT",
                "confidence": "LOW",
                "reason": "Insufficient data for strong recommendations - maintaining current weights"
            }
        ]
    
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "ARENA_LEARNINGS",
        "recommendations": recommendations,
        "note": f"Generated from analysis of {len(leaderboard)} strategies at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    }

def main():
    print(f"[Arena Learner] Starting at {datetime.utcnow().isoformat()}Z")
    
    # Step 1: Git pull
    git_pull()
    
    # Step 2: Load current arena state
    arena_file = PROJECT_ROOT / "outputs" / "arena-state.json"
    arena_state = load_json(arena_file, {"leaderboard": [], "insights": {}})
    
    print(f"[Arena Learner] Loaded arena state with {len(arena_state.get('leaderboard', []))} strategies")
    
    # Step 3: Generate learnings from actual data
    learnings = analyze_arena_data(arena_state)
    
    # Step 4: Save learnings
    learnings_file = PROJECT_ROOT / "outputs" / "arena-learnings.json"
    save_json(learnings_file, learnings)
    
    print(f"[Arena Learner] Generated {len(learnings.get('recommendations', []))} recommendations")
    
    # Step 5: Git commit and push
    os.system("git add outputs/arena-learnings.json > /dev/null 2>&1")
    os.system('git commit -m "arena learner: updated recommendations based on actual arena data" > /dev/null 2>&1')
    os.system("git push > /dev/null 2>&1")
    
    print("[Arena Learner] Completed")

if __name__ == "__main__":
    main()