#!/usr/bin/env python3
"""
Autonomous Brain Orchestrator.

Builds a unified strategic snapshot from local state so the mechanical layer
has one coherent policy surface to consume without depending on an external Oz
agent run.
"""

import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    load_regime,
    load_trade_journal,
    load_pending_entries,
    save_pending_entries,
    get_enabled_strategies,
    current_regime_params,
    BRAIN_STATE_FILE,
    CODEBASE_INDEX_FILE,
    PLAYBOOK_STATE_FILE,
    OUTPUTS_DIR,
    MEMORY_DIR,
    POSITION_STATE_DIR,
    CONFIG_DIR,
    STATE_DIR,
    get_all_open_positions,
    directional_exposure_snapshot,
    record_heartbeat,
)


SCANNERS = [
    "polar",
    "fox",
    "mantis",
    "orca",
    "komodo",
    "condor",
    "sentinel",
    "rhino",
    "barracuda",
    "bison",
    "shark",
]

CORE_CRONS = {"risk-arbiter", "dsl-runner", "health", "orca", "polar", "fox", "mantis"}

SCANNER_CONFIG_FILES = {
    "polar": "polar-config.json",
    "fox": "fox-config.json",
    "mantis": "mantis-config.json",
    "orca": "scanner-config.json",
    "komodo": "scanner-config.json",
    "condor": "condor-config.json",
    "barracuda": "barracuda-config.json",
    "bison": "bison-config.json",
    "shark": "shark-config.json",
    "sentinel": "sentinel-config.json",
    "rhino": "rhino-config.json",
}

SCANNER_BASELINES = {
    "orca": {
        "basePriority": 68,
        "deadWeightMin": 18,
        "minHighWaterRoe": 2.5,
        "rotationPriorityGap": 8,
        "minTraderRatio": 0.25,
        "minTraderCountFloor": 20,
        "minConvictionRatio": 0.60,
        "minConcentrationRatio": 0.55,
    },
    "komodo": {
        "basePriority": 70,
        "deadWeightMin": 25,
        "minHighWaterRoe": 3.0,
        "rotationPriorityGap": 8,
        "minTraderRatio": 0.30,
        "minTraderCountFloor": 16,
        "minConvictionRatio": 0.65,
        "minConcentrationRatio": 0.60,
    },
    "condor": {
        "basePriority": 58,
        "deadWeightMin": 40,
        "minHighWaterRoe": 4.5,
        "rotationPriorityGap": 10,
        "minTraderRatio": 0.40,
        "minTraderCountFloor": 24,
        "minConvictionRatio": 0.75,
        "minConcentrationRatio": 0.70,
    },
    "barracuda": {
        "basePriority": 52,
        "deadWeightMin": 60,
        "minHighWaterRoe": 4.0,
        "rotationPriorityGap": 10,
        "minTraderRatio": 0.45,
        "minTraderCountFloor": 18,
        "minConvictionRatio": 0.70,
        "minConcentrationRatio": 0.70,
    },
    "bison": {
        "basePriority": 55,
        "deadWeightMin": 50,
        "minHighWaterRoe": 6.0,
        "rotationPriorityGap": 12,
        "minTraderRatio": 0.45,
        "minTraderCountFloor": 24,
        "minConvictionRatio": 0.78,
        "minConcentrationRatio": 0.72,
    },
    "shark": {
        "basePriority": 62,
        "deadWeightMin": 12,
        "minHighWaterRoe": 1.5,
        "rotationPriorityGap": 5,
        "minTraderRatio": 0.20,
        "minTraderCountFloor": 20,
        "minConvictionRatio": 0.55,
        "minConcentrationRatio": 0.50,
    },
    "sentinel": {
        "basePriority": 66,
        "deadWeightMin": 30,
        "minHighWaterRoe": 3.5,
        "rotationPriorityGap": 9,
        "minTraderRatio": 0.35,
        "minTraderCountFloor": 18,
        "minConvictionRatio": 0.70,
        "minConcentrationRatio": 0.65,
    },
    "rhino": {
        "basePriority": 60,
        "deadWeightMin": 35,
        "minHighWaterRoe": 5.0,
        "rotationPriorityGap": 10,
        "minTraderRatio": 0.40,
        "minTraderCountFloor": 20,
        "minConvictionRatio": 0.75,
        "minConcentrationRatio": 0.68,
    },
}


def summarize_file(path: Path) -> str:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    first = ""
    for line in lines:
        if line.startswith("#!"):
            continue
        if line in ('"""', "'''", "{", "["):
            continue
        first = line
        break
    if not first:
        first = lines[0]
    if first.startswith('"""') or first.startswith("'''"):
        first = first.strip("\"'")
    if first.startswith("#"):
        first = first.lstrip("#").strip()
    return first[:140]


def build_codebase_index() -> dict:
    categories = {
        "entrypoints": [],
        "shared_lib": [],
        "mechanical": [],
        "strategic": [],
        "dashboard": [],
        "config": [],
        "memory": [],
    }

    for path in sorted(STATE_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(STATE_DIR).as_posix()
        if (
            rel.startswith(".git/")
            or "/__pycache__/" in rel
            or rel.startswith("__pycache__/")
        ):
            continue

        item = {
            "path": rel,
            "summary": summarize_file(path),
        }
        if rel in ("worker.py", "dashboard/server.py", "dashboard/telegram_bot.py"):
            categories["entrypoints"].append(item)
        elif rel.startswith("scripts/lib/"):
            categories["shared_lib"].append(item)
        elif rel.startswith("scripts/vps/"):
            categories["mechanical"].append(item)
        elif rel.startswith("scripts/oz/"):
            categories["strategic"].append(item)
        elif rel.startswith("dashboard/"):
            categories["dashboard"].append(item)
        elif rel.startswith("config/"):
            categories["config"].append(item)
        elif rel.startswith("memory/"):
            categories["memory"].append(item)

    return {
        "generatedAt": now_iso(),
        "root": str(STATE_DIR),
        "counts": {key: len(value) for key, value in categories.items()},
        "categories": categories,
        "controlPlane": {
            "mechanicalLayer": [
                "worker.py schedules the VPS scripts",
                "scripts/vps/*.py execute entries, exits, safety, monitoring",
                "scripts/lib/senpi_common.py is the shared runtime surface",
            ],
            "strategicLayer": [
                "scripts/oz/setup-oz-agents.sh defines scheduled Oz agents",
                "memory/howl-analysis-prompt.md and memory/whale-index-prompt.md hold learning procedures",
                "outputs/autonomous-brain.json is the local strategic snapshot for deterministic consumers",
            ],
            "stateBus": [
                "config/*.json stores policy inputs",
                "state/*.json stores runtime execution state",
                "memory/trade-journal.json stores learning history",
                "outputs/*.json stores analysis and observability artifacts",
            ],
        },
    }


def normalize_source(raw: str) -> str:
    source = (raw or "unknown").lower()
    source = source.replace("auto-", "")
    for scanner in SCANNERS:
        if scanner in source:
            return scanner
    if "stalker" in source or "striker" in source:
        return "orca"
    if source in ("", "unknown"):
        return "unknown"
    return source


def trade_stats() -> tuple[dict, dict]:
    journal = load_trade_journal()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = defaultdict(
        lambda: {"opens": 0, "closes": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )

    recent_loss_streak = 0
    for trade in reversed(journal):
        if trade.get("action") != "CLOSE":
            continue
        pnl = float(trade.get("realizedPnl", 0) or 0)
        if pnl < 0:
            recent_loss_streak += 1
        else:
            break

    daily_pnl = 0.0
    closes_today = 0
    wins_today = 0
    for trade in journal:
        source = normalize_source(trade.get("entrySource", trade.get("entryMode", "")))
        bucket = stats[source]
        if trade.get("action") == "OPEN":
            bucket["opens"] += 1
            continue
        if trade.get("action") != "CLOSE":
            continue

        pnl = float(trade.get("realizedPnl", 0) or 0)
        bucket["closes"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

        if trade.get("recordedAt", "").startswith(today):
            daily_pnl += pnl
            closes_today += 1
            if pnl > 0:
                wins_today += 1

    performance = {}
    for source, bucket in stats.items():
        closes = bucket["closes"]
        performance[source] = {
            **bucket,
            "winRate": round(bucket["wins"] / closes * 100, 1) if closes else 0.0,
            "avgPnl": round(bucket["pnl"] / closes, 2) if closes else 0.0,
        }

    global_stats = {
        "dailyPnl": round(daily_pnl, 2),
        "dailyCloses": closes_today,
        "dailyWinRate": round(wins_today / closes_today * 100, 1)
        if closes_today
        else 0.0,
        "recentLossStreak": recent_loss_streak,
        "journalSize": len(journal),
    }
    return performance, global_stats


def extract_learning_signals(
    arena_state: dict, arena_learnings: dict, latest_report: dict
) -> dict:
    winning_traits = arena_state.get("insights", {}).get("winningTraits", [])
    losing_traits = arena_state.get("insights", {}).get("losingTraits", [])
    recommendations = arena_state.get("insights", {}).get("recommendations", [])

    learning_text = " ".join(
        [
            " ".join(winning_traits),
            " ".join(losing_traits),
            " ".join(recommendations),
            str(arena_learnings),
            str(latest_report),
        ]
    ).lower()

    signals = {
        "preferSelectivity": "fewer trades" in learning_text
        or "selectivity" in learning_text,
        "feeDrag": "fee drag" in learning_text,
        "highConviction": "higher conviction" in learning_text
        or "conviction" in learning_text,
        "useDsl": "dsl" in learning_text or "high water" in learning_text,
    }
    signals["topRecommendations"] = recommendations[:3]
    return signals


def pending_summary() -> dict:
    from datetime import datetime, timezone, timedelta

    pending = load_pending_entries()
    # Filter out stale entries (>30 min old) to prevent feedback loop
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    fresh = [
        e for e in pending if e.get("timestamp", e.get("detectedAt", "")) >= cutoff
    ]
    # If we filtered anything, save the cleaned list
    if len(fresh) < len(pending):
        save_pending_entries(fresh)
    by_scanner = defaultdict(int)
    for entry in fresh:
        scanner = normalize_source(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "")))
        )
        by_scanner[scanner] += 1
    return {
        "total": len(fresh),
        "byScanner": dict(sorted(by_scanner.items())),
        "latest": fresh[-5:],
    }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_scanner_profiles(perf: dict) -> dict:
    profiles = {}
    for scanner in SCANNERS:
        base = dict(SCANNER_BASELINES.get(scanner, {}))
        bucket = perf.get(scanner, {})
        closes = int(bucket.get("closes", 0) or 0)
        wins = int(bucket.get("wins", 0) or 0)
        losses = int(bucket.get("losses", 0) or 0)
        pnl = float(bucket.get("pnl", 0.0) or 0.0)
        avg_pnl = float(bucket.get("avgPnl", 0.0) or 0.0)
        win_rate = float(bucket.get("winRate", 0.0) or 0.0)
        confidence = clamp(closes / 12.0, 0.0, 1.0)

        edge_score = (
            (win_rate - 50.0) * 0.7 + avg_pnl * 1.2 + (wins - losses) * 2.0 + pnl * 0.08
        )
        if closes == 0:
            edge_score = 0.0
        edge_score = clamp(edge_score, -35.0, 35.0)
        weighted_edge = edge_score * confidence

        priority = int(
            round(clamp(base.get("basePriority", 55) + weighted_edge, 5, 99))
        )
        dead_weight_min = float(base.get("deadWeightMin", 30))
        min_high_water = float(base.get("minHighWaterRoe", 3.0))
        rotation_gap = int(base.get("rotationPriorityGap", 8))
        trader_ratio = float(base.get("minTraderRatio", 0.3))
        trader_floor = int(base.get("minTraderCountFloor", 20))
        conviction_ratio = float(base.get("minConvictionRatio", 0.65))
        concentration_ratio = float(base.get("minConcentrationRatio", 0.6))

        status = "baseline"
        if closes >= 6 and weighted_edge <= -8:
            status = "cold"
            dead_weight_min = max(10.0, dead_weight_min * 0.75)
            min_high_water += 1.0
            rotation_gap = max(4, rotation_gap - 2)
            trader_ratio = min(0.55, trader_ratio + 0.08)
            conviction_ratio = min(0.9, conviction_ratio + 0.08)
            concentration_ratio = min(0.9, concentration_ratio + 0.08)
        elif closes >= 6 and weighted_edge >= 8:
            status = "hot"
            dead_weight_min = dead_weight_min * 1.15
            min_high_water = max(1.0, min_high_water - 0.5)
            rotation_gap += 2
            trader_ratio = max(0.15, trader_ratio - 0.05)
            conviction_ratio = max(0.45, conviction_ratio - 0.05)
            concentration_ratio = max(0.45, concentration_ratio - 0.05)

        profiles[scanner] = {
            "version": "1.1",
            "scanner": scanner,
            "status": status,
            "priority": priority,
            "basePriority": base.get("basePriority", 55),
            "realizedEdgeScore": round(edge_score, 2),
            "weightedEdgeScore": round(weighted_edge, 2),
            "sampleConfidence": round(confidence, 2),
            "sampleCloses": closes,
            "sampleWins": wins,
            "sampleLosses": losses,
            "sampleWinRate": round(win_rate, 1),
            "samplePnl": round(pnl, 2),
            "sampleAvgPnl": round(avg_pnl, 2),
            "deadWeightMin": round(dead_weight_min, 1),
            "minHighWaterRoe": round(min_high_water, 1),
            "rotationPriorityGap": rotation_gap,
            "minTraderRatio": round(trader_ratio, 2),
            "minTraderCountFloor": trader_floor,
            "minConvictionRatio": round(conviction_ratio, 2),
            "minConcentrationRatio": round(concentration_ratio, 2),
        }
    return profiles


def determine_execution_policy(
    regime: dict, perf: dict, trade_meta: dict, pending: dict, arena_signals: dict
) -> dict:
    health = load_json(OUTPUTS_DIR / "health-state.json", default={})
    arbiter = load_json(OUTPUTS_DIR / "arbiter-state.json", default={})
    heartbeats = load_json(OUTPUTS_DIR / "cron-heartbeats.json", default={})
    stale_crons = health.get("staleCrons", [])
    core_stale = sorted(c for c in stale_crons if c in CORE_CRONS)

    regime_mode = regime.get("riskMode", "BASELINE")
    raw_params = regime.get("regimes", {}).get(regime_mode) or regime.get(
        "regimes", {}
    ).get("BASELINE", {})
    base = raw_params or current_regime_params()
    peak = float(arbiter.get("peakEquity", 0) or 0)
    equity = float(arbiter.get("lastEquity", 0) or 0)
    drawdown_pct = round((peak - equity) / peak * 100, 2) if peak > 0 else 0.0

    reasons = []
    block_new_entries = False
    allow_auto_entry = True

    max_slots_cap = int(base.get("maxSlots", 2) or 2)
    max_leverage_cap = float(base.get("maxLeverageCrypto", 10) or 10)
    alloc_pct_cap = float(base.get("allocPctPerSlot", 30) or 30)

    if regime_mode == "RISK_OFF":
        block_new_entries = True
        reasons.append("regime is RISK_OFF")

    if health and health.get("mcporterOk") is False:
        block_new_entries = True
        reasons.append("mcporter health check failed")

    if core_stale:
        block_new_entries = True
        reasons.append(f"core stale crons: {', '.join(core_stale)}")

    loss_streak = trade_meta["recentLossStreak"]
    pending_total = pending["total"]
    if loss_streak >= 2:
        max_leverage_cap = min(max_leverage_cap, 8)
        alloc_pct_cap = min(alloc_pct_cap, 25)
        reasons.append(f"recent loss streak {loss_streak}")
    if loss_streak >= 3:
        max_slots_cap = min(max_slots_cap, 1)
        allow_auto_entry = False

    if drawdown_pct >= 4:
        max_slots_cap = min(max_slots_cap, 1)
        max_leverage_cap = min(max_leverage_cap, 8)
        alloc_pct_cap = min(alloc_pct_cap, 20)
        reasons.append(f"drawdown {drawdown_pct:.1f}%")

    if pending_total >= 8:
        allow_auto_entry = False
        alloc_pct_cap = min(alloc_pct_cap, 20)
        reasons.append(f"pending backlog {pending_total}")

    if arena_signals.get("preferSelectivity"):
        max_slots_cap = min(max_slots_cap, 2)
        reasons.append("arena favors selectivity")

    if arena_signals.get("feeDrag"):
        max_leverage_cap = min(max_leverage_cap, 8)
        allow_auto_entry = False if pending_total >= 5 else allow_auto_entry
        reasons.append("arena flagged fee drag")

    scanner_profiles = build_scanner_profiles(perf)
    scanner_ranked = sorted(
        (
            (name, profile.get("priority", 50), profile)
            for name, profile in scanner_profiles.items()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    blocked_scanners = [
        name
        for name, _, profile in scanner_ranked
        if profile.get("sampleCloses", 0) >= 6
        and profile.get("weightedEdgeScore", 0) <= -10
    ]
    preferred_scanners = [
        name for name, _, profile in scanner_ranked if name not in blocked_scanners
    ][:3]
    priority_by_scanner = {
        name: profile.get("priority", 50) for name, _, profile in scanner_ranked
    }

    mode = "PROTECT" if block_new_entries else "CAUTION" if reasons else "ACTIVE"
    return {
        "generatedAt": now_iso(),
        "mode": mode,
        "blockNewEntries": block_new_entries,
        "allowAutoEntry": allow_auto_entry and not block_new_entries,
        "maxSlotsCap": max_slots_cap,
        "maxLeverageCap": max_leverage_cap,
        "allocPctCap": alloc_pct_cap,
        "strategyCaps": {
            strat["_key"]: {"maxSlotsCap": max_slots_cap}
            for strat in get_enabled_strategies()
        },
        "reasons": reasons[:8],
        "risk": {
            "drawdownPctFromPeak": drawdown_pct,
            "recentLossStreak": loss_streak,
            "pendingSignals": pending_total,
            "staleCrons": stale_crons,
            "coreHeartbeatCount": len(heartbeats),
        },
        "signalPolicy": {
            "preferredScanners": preferred_scanners,
            "blockedScanners": blocked_scanners,
            "priorityByScanner": priority_by_scanner,
            "scannerProfiles": scanner_profiles,
        },
    }


def latest_howl_report() -> dict:
    files = sorted(
        (
            path
            for path in MEMORY_DIR.glob("howl-*.md")
            if re.fullmatch(r"howl-\d{4}-\d{2}-\d{2}\.md", path.name)
        ),
        reverse=True,
    )
    if not files:
        return {}
    path = files[0]
    try:
        content = path.read_text(errors="ignore")
    except OSError:
        return {"file": path.name}

    highlights = []
    for line in content.splitlines():
        clean = line.strip()
        if clean.startswith("- ") or clean.startswith("* "):
            highlights.append(clean[2:].strip())
        if len(highlights) >= 3:
            break

    return {
        "file": path.name,
        "updatedAt": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "highlights": highlights,
    }


def score_thresholds() -> dict:
    thresholds = {}
    for scanner, filename in SCANNER_CONFIG_FILES.items():
        config = load_json(CONFIG_DIR / filename, default={})
        entry = config.get("entry", {}) if isinstance(config, dict) else {}
        if scanner == "orca":
            thresholds[scanner] = {"STALKER": 6, "STRIKER": 9}
            continue
        if scanner == "komodo":
            thresholds[scanner] = {"CONSENSUS": 10}
            continue
        min_score = entry.get("minScore")
        if min_score is not None:
            thresholds[scanner] = {"minScore": min_score}
    return thresholds


def build_playbook_state(regime: dict, execution_policy: dict) -> dict:
    positions = []
    for pos in get_all_open_positions():
        playbook = pos.get("playbook", {})
        positions.append(
            {
                "asset": pos.get("asset"),
                "direction": pos.get("direction"),
                "strategyKey": pos.get("strategyKey"),
                "scanner": pos.get("scanner", playbook.get("scanner")),
                "entryScore": pos.get(
                    "entryScore", playbook.get("entry", {}).get("score")
                ),
                "marginUsd": pos.get(
                    "margin", playbook.get("entry", {}).get("marginUsd", 0)
                ),
                "leverage": pos.get(
                    "leverage", playbook.get("entry", {}).get("leverage", 0)
                ),
                "priority": playbook.get("priority"),
                "createdAt": pos.get("createdAt"),
                "highWaterRoe": pos.get("highWaterRoe", 0),
                "phase": pos.get("phase"),
            }
        )

    regime_mode = regime.get("riskMode", "BASELINE")
    raw_params = regime.get("regimes", {}).get(regime_mode) or regime.get(
        "regimes", {}
    ).get("BASELINE", {})
    return {
        "schemaVersion": "1.0",
        "generatedAt": now_iso(),
        "profile": {
            "mode": regime_mode,
            "hybrid": "deterministic-mechanical + oz-supervisory",
        },
        "limits": {
            "regime": raw_params,
            "brainCaps": {
                "maxSlotsCap": execution_policy.get("maxSlotsCap"),
                "maxLeverageCap": execution_policy.get("maxLeverageCap"),
                "allocPctCap": execution_policy.get("allocPctCap"),
                "allowAutoEntry": execution_policy.get("allowAutoEntry"),
            },
            "guardrails": regime.get("globalGuardrails", {}),
        },
        "scoreThresholds": score_thresholds(),
        "scannerProfiles": execution_policy.get("signalPolicy", {}).get(
            "scannerProfiles", {}
        ),
        "portfolio": {
            "directionalExposure": directional_exposure_snapshot(),
            "activePositions": positions,
        },
        "tradeLoop": [
            "scan",
            "evaluate",
            "trade",
            "protect",
            "review",
        ],
    }


def main():
    if not acquire_lock("autonomous-brain"):
        return

    try:
        record_heartbeat("brain")

        index = build_codebase_index()
        save_json(CODEBASE_INDEX_FILE, index)

        regime = load_regime()
        perf, trade_meta = trade_stats()
        pending = pending_summary()
        arena_state = load_json(OUTPUTS_DIR / "arena-state.json", default={})
        arena_learnings = load_json(OUTPUTS_DIR / "arena-learnings.json", default={})
        latest_report = load_json(OUTPUTS_DIR / "latest-report.json", default={})
        learning_signals = extract_learning_signals(
            arena_state, arena_learnings, latest_report
        )
        execution_policy = determine_execution_policy(
            regime, perf, trade_meta, pending, learning_signals
        )

        brain_state = {
            "generatedAt": now_iso(),
            "codebaseIndexAt": index.get("generatedAt"),
            "riskMode": regime.get("riskMode", "BASELINE"),
            "executionPolicy": execution_policy,
            "signalPolicy": execution_policy.get("signalPolicy", {}),
            "systemHealth": {
                "health": load_json(OUTPUTS_DIR / "health-state.json", default={}),
                "arbiter": load_json(OUTPUTS_DIR / "arbiter-state.json", default={}),
            },
            "pending": pending,
            "performance": {
                "byScanner": perf,
                "global": trade_meta,
            },
            "selfLearning": {
                "arenaInsights": arena_state.get("insights", {}),
                "arenaLearnings": arena_learnings,
                "latestReport": latest_report,
                "howl": latest_howl_report(),
                "signals": learning_signals,
            },
            "summary": {
                "status": execution_policy.get("mode"),
                "preferredScanners": execution_policy.get("signalPolicy", {}).get(
                    "preferredScanners", []
                ),
                "blockedScanners": execution_policy.get("signalPolicy", {}).get(
                    "blockedScanners", []
                ),
                "reasons": execution_policy.get("reasons", []),
            },
        }
        save_json(BRAIN_STATE_FILE, brain_state)
        save_json(PLAYBOOK_STATE_FILE, build_playbook_state(regime, execution_policy))
        log(
            "Brain updated: "
            f"mode={execution_policy.get('mode')} "
            f"block={execution_policy.get('blockNewEntries')} "
            f"preferred={execution_policy.get('signalPolicy', {}).get('preferredScanners', [])}"
        )
    finally:
        release_lock("autonomous-brain")


if __name__ == "__main__":
    main()
