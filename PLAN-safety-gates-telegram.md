# PLAN: User-Configurable Safety Gates via Telegram

## Problem

The 10 safety gates in `safety.py` read from `risk-regime.json`'s `globalGuardrails`
and from hardcoded values (like `min_scores` dict in `safety.py`). Users cannot
change these from Telegram — they must edit config files or go dev mode.

Meanwhile, `/rules` + `/rules_set` already exist for strategic overrides (TP/SL/DSL)
but they do NOT touch the actual gate parameters.

## Current Architecture

```
risk-regime.json → globalGuardrails → senpi_common.load_global_guardrails()
                                          ↓
safety.py evaluate_entry()  ← reads guardrails for:
  Gate 4:  maxPositionsTotal (3)
  Gate 6:  min_scores dict (HARDCODED in safety.py lines 97-106)
  Gate 7:  bannedAssetPrefixes (xyz:*)
  Gate 8:  perAssetCooldownMinutes (120)
  Gate 9:  directionalCapPct (70%)
  Gate 10: minLeverage (7), maxLeverage (10)

evaluate.py TradeEvaluator:
  - Calls evaluate_entry() first (10 gates)
  - THEN applies user-rules.json overrides (strategic TP/SL/DSL only)
  - ALSO has hardcoded leverage band check at line 288
  - Overrides min_score from user-rules.json evaluate.minScore (line 204-212)
```

## Design

### 1. New Telegram Commands

Add `/gates` (view) and `/gates_set` (modify) — mirrors the `/rules`/`/rules_set` pattern.

**`/gates`** — Shows all 10 gates with current values and their source (default vs user override):

```
🛡 Safety Gates

Gate 1: Entries Allowed     REGIME-GATED  (auto)
Gate 2: Auto-Entry          REGIME+BRAIN  (auto)
Gate 3: Valid Strategy      REQUIRED      (auto)
Gate 4: Max Positions       3             ← user override (default: 3)
Gate 5: Scanner Blocked     BRAIN POLICY  (auto)
Gate 6: Min Score (ORCA)    6             ← user override (default: 6)
        Min Score (MANTIS)  7             ← user override (default: 7)
        Min Score (FOX)     7             ← user override (default: 7)
        Min Score (KOMODO)  10            ← user override (default: 10)
        ... etc
Gate 7: Banned Prefixes     xyz:*         ← user override (default: xyz:*)
Gate 8: Cooldown            120 min       ← user override (default: 120)
Gate 9: Directional Cap     70%           ← user override (default: 70)
Gate 10: Leverage Band      7-10x         ← user override (default: 7-10)

3 gates are automatic (1,2,3,5) — not user-configurable.
Use /gates_set <key> <value> to change.
Use /gates_reset to restore defaults.
```

**`/gates_set`** — Modify a specific gate:

```
/gates_set max_positions 5
/gates_set cooldown 60
/gates_set min_lev 5
/gates_set max_lev 15
/gates_set dir_cap 80
/gates_set score_orca 8
/gates_set score_mantis 6
/gates_set banned_prefix xyz:,test:
```

**`/gates_reset`** — Restore all gates to defaults.

### 2. Storage

Add a new section to `user-rules.json` under key `"safety_gates"`:

```json
{
  "safety_gates": {
    "maxPositionsTotal": 3,
    "perAssetCooldownMinutes": 120,
    "directionalCapPct": 70,
    "minLeverage": 7,
    "maxLeverage": 10,
    "bannedAssetPrefixes": ["xyz:"],
    "minScores": {
      "orca": 6,
      "mantis": 7,
      "fox": 7,
      "komodo": 10,
      "condor": 10,
      "polar": 10,
      "sentinel": 5,
      "rhino": 5
    }
  }
}
```

Why `user-rules.json` instead of a separate file:
- Already git-synced by `/rules_set`
- Already the user sovereignty mechanism
- Telegram bot already has write+sync plumbing for it
- Keeps all user configuration in one place

### 3. Validation Bounds

Each user-configurable gate has hard bounds. The system REJECTS values outside these:

| Key                    | Type    | Min | Max | Rationale                         |
|------------------------|---------|-----|-----|-----------------------------------|
| maxPositionsTotal      | int     | 1   | 10  | Below 1 = no trading; above 10 = unfocused |
| perAssetCooldownMinutes| int     | 0   | 1440| 0 = disabled; 1440 = 24hr max     |
| directionalCapPct      | int     | 50  | 100 | Below 50 = too restrictive        |
| minLeverage            | int     | 1   | 50  | Must be <= maxLeverage            |
| maxLeverage            | int     | 1   | 50  | Must be >= minLeverage            |
| minScores.*            | int     | 1   | 20  | Sensible signal score range       |
| bannedAssetPrefixes    | list    | —   | —   | Validated as comma-separated      |

### 4. Code Changes (5 files)

#### A. `scripts/lib/senpi_common.py`

Modify `load_global_guardrails()` to merge user-rules overrides:

```python
def load_global_guardrails() -> dict:
    """Load globalGuardrails, with user-rules.json safety_gates overrides."""
    regime = load_regime()
    guardrails = regime.get("globalGuardrails", {})
    merged = dict(DEFAULT_GLOBAL_GUARDRAILS)
    merged.update({k: v for k, v in guardrails.items() if v is not None})

    # Layer: user-rules safety_gates overrides
    user_gates = load_json(CONFIG_DIR / "user-rules.json", default={}).get("safety_gates", {})
    for key in ("maxPositionsTotal", "perAssetCooldownMinutes",
                "directionalCapPct", "minLeverage", "maxLeverage",
                "bannedAssetPrefixes"):
        if key in user_gates and user_gates[key] is not None:
            merged[key] = user_gates[key]

    return merged
```

Add `load_user_min_scores()`:

```python
def load_user_min_scores() -> dict | None:
    """Load user-overridden min scores from user-rules.json, or None."""
    user_gates = load_json(CONFIG_DIR / "user-rules.json", default={}).get("safety_gates", {})
    scores = user_gates.get("minScores")
    if scores and isinstance(scores, dict):
        return {k: int(v) for k, v in scores.items() if isinstance(v, (int, float))}
    return None
```

#### B. `waifu_cli/safety.py`

Change the hardcoded `min_scores` dict (lines 97-106) to read from `senpi_common.load_user_min_scores()`:

```python
# Gate 6: Score threshold
DEFAULT_MIN_SCORES = {
    "orca": 6, "mantis": 7, "fox": 7, "komodo": 10,
    "condor": 10, "polar": 10, "sentinel": 5, "rhino": 5,
}
min_scores = load_user_min_scores() or dict(DEFAULT_MIN_SCORES)
```

Import `load_user_min_scores` from senpi_common.

#### C. `dashboard/telegram_bot.py`

Add 3 new command handlers + register them:

1. `cmd_gates` — Display all gates with current values
2. `cmd_gates_set` — Modify a gate with validation
3. `cmd_gates_reset` — Reset safety_gates to null (falls back to defaults)

Add to `COMMANDS` list:
```python
("gates", "View safety gates", "All 10 entry gates with current values."),
("gates_set", "Modify safety gate", "Usage: /gates_set <key> <value>"),
("gates_reset", "Reset gates to defaults", "Remove all user overrides."),
```

Key map (mirrors RULES_KEY_MAP):
```python
GATES_KEY_MAP = {
    "max_positions":  ("safety_gates", "maxPositionsTotal", int),
    "cooldown":       ("safety_gates", "perAssetCooldownMinutes", int),
    "dir_cap":        ("safety_gates", "directionalCapPct", int),
    "min_lev":        ("safety_gates", "minLeverage", int),
    "max_lev":        ("safety_gates", "maxLeverage", int),
    "banned_prefix":  ("safety_gates", "bannedAssetPrefixes", lambda v: v.split(",")),
    "score_orca":     ("safety_gates:minScores", "orca", int),
    "score_mantis":   ("safety_gates:minScores", "mantis", int),
    "score_fox":      ("safety_gates:minScores", "fox", int),
    "score_komodo":   ("safety_gates:minScores", "komodo", int),
    "score_condor":   ("safety_gates:minScores", "condor", int),
    "score_polar":    ("safety_gates:minScores", "polar", int),
    "score_sentinel": ("safety_gates:minScores", "sentinel", int),
    "score_rhino":    ("safety_gates:minScores", "rhino", int),
}
```

Validation function:
```python
GATES_BOUNDS = {
    "maxPositionsTotal":       (1, 10),
    "perAssetCooldownMinutes": (0, 1440),
    "directionalCapPct":       (50, 100),
    "minLeverage":             (1, 50),
    "maxLeverage":             (1, 50),
}

def validate_gate(key, value):
    bounds = GATES_BOUNDS.get(key)
    if bounds:
        lo, hi = bounds
        if not (lo <= value <= hi):
            return False, f"Must be {lo}-{hi}"
    # Cross-field: minLeverage <= maxLeverage
    if key == "minLeverage":
        current_max = load_current_gate("maxLeverage")
        if value > current_max:
            return False, f"min ({value}) > current max ({current_max}). Set max_lev first."
    if key == "maxLeverage":
        current_min = load_current_gate("minLeverage")
        if value < current_min:
            return False, f"max ({value}) < current min ({current_min}). Set min_lev first."
    return True, "ok"
```

#### D. `waifu_cli/commands/evaluate.py`

The `MIN_SCORES` dict at line 165 is also used in `_determine_recommendation`.
Update to also check `load_user_min_scores()`:
- Line 207-212 already handles `evaluate.minScore` from user-rules (single global override)
- Add: if no global override, check per-scanner overrides from `safety_gates.minScores`

This is a secondary check — safety.py already handles it at the gate level.

#### E. Bot command registration

In the `setup_bot()` or wherever handlers are registered, add:
```python
app.add_handler(CommandHandler("gates", cmd_gates))
app.add_handler(CommandHandler("gates_set", cmd_gates_set))
app.add_handler(CommandHandler("gates_reset", cmd_gates_reset))
```

## Execution Order

1. **senpi_common.py** — Add `load_user_min_scores()`, modify `load_global_guardrails()` to layer user overrides
2. **safety.py** — Replace hardcoded min_scores with `load_user_min_scores()`, update import
3. **telegram_bot.py** — Add `cmd_gates`, `cmd_gates_set`, `cmd_gates_reset` + GATES_KEY_MAP + validation + registration
4. **evaluate.py** — Verify secondary min_scores check is consistent
5. **Test** — Verify `/gates` reads correctly, `/gates_set` writes+validates, gates actually enforce new values

## Gates NOT Configurable (Auto-Only)

Gates 1, 2, 3, 5 are automatic/system-level:
- **Gate 1** (Entries Allowed): Driven by regime — already controllable via `/emergency_stop`
- **Gate 2** (Auto-Entry): Driven by regime + brain — already controllable via `/rules_set jido_auto`
- **Gate 3** (Valid Strategy): Must be configured — not a parameter
- **Gate 5** (Scanner Blocked): Brain policy — not a simple value

These remain as-is. The `/gates` display shows them as "automatic" for transparency.

## Risk Mitigation

- Hard bounds prevent degenerate configs (0 positions, 1000x leverage)
- Cross-field validation (min <= max leverage)
- Git sync after every change (audit trail)
- `updatedAt`/`updatedBy` tracking
- `/gates_reset` for one-click recovery to defaults
- Default values unchanged — only stored when user explicitly sets them
