#!/usr/bin/env python3
"""
reconcile-closes.py — Detects DSL positions that were closed (by dsl-combined.py
or risk arbiter) and records CLOSE entries in the trade journal.

Runs as part of health-check-cron.sh every 10 minutes. Scans all DSL state files
for positions marked active=False that don't yet have a CLOSE journal entry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    log, load_json, POSITION_STATE_DIR, MEMORY_DIR,
    load_trade_journal, record_trade,
    get_enabled_strategies, get_strategy_state_dir,
)


def get_journaled_closes() -> set[str]:
    """Return set of (asset, strategyKey, createdAt) for already-journaled closes."""
    journal = load_trade_journal()
    closes = set()
    for t in journal:
        if t.get("action") == "CLOSE":
            key = f"{t.get('asset', '')}:{t.get('strategyKey', '')}:{t.get('entryCreatedAt', '')}"
            closes.add(key)
    return closes


def reconcile():
    journaled = get_journaled_closes()
    strategies = get_enabled_strategies()

    for strat in strategies:
        state_dir = get_strategy_state_dir(strat["_key"])
        for f in state_dir.glob("dsl-*.json"):
            state = load_json(f)
            if not state:
                continue
            # Only process closed positions
            if state.get("active", True):
                continue
            # Check if already journaled
            key = f"{state.get('asset', '')}:{strat['_key']}:{state.get('createdAt', '')}"
            if key in journaled:
                continue

            # Record the close
            close_reason = state.get("closeReason", state.get("exitReason", "dsl_breach"))
            realized_pnl = float(state.get("realizedPnl", state.get("pnl", 0)))
            close_price = float(state.get("closePrice", state.get("exitPrice", 0)))

            record_trade({
                "action": "CLOSE",
                "asset": state.get("asset", ""),
                "direction": state.get("direction", ""),
                "entryPrice": state.get("entryPrice", 0),
                "closePrice": close_price,
                "size": state.get("size", 0),
                "leverage": state.get("leverage", 0),
                "strategyKey": strat["_key"],
                "entrySource": state.get("entrySource", state.get("entryMode", "unknown")),
                "entryScore": state.get("entryScore", state.get("score", 0)),
                "entryMode": state.get("entryMode", ""),
                "closeReason": close_reason,
                "realizedPnl": realized_pnl,
                "entryCreatedAt": state.get("createdAt", ""),
                "closedAt": state.get("closedAt", ""),
                "highWaterRoe": state.get("highWaterRoe", 0),
                "finalTierIndex": state.get("currentTierIndex", -1),
            })
            log(f"Reconciled CLOSE: {state.get('asset')} in {strat['_key']} "
                f"reason={close_reason} pnl={realized_pnl:.2f}")


if __name__ == "__main__":
    reconcile()
