# Telegram Bot UX Improvement Plan

> Status: PLANNING -- no code written yet.
> Target: `dashboard/telegram_bot.py` only (1270 lines).
> Dependency: `python-telegram-bot>=21.0` (already in requirements.txt).
> Constraints: No changes to `_waifu_cli()`, `worker.py` scheduler, git sync patterns, or `@authorized` decorator.

---

## 0. Current State Analysis

### Architecture
- Bot runs as async background task inside dashboard FastAPI (`server.py` lifespan) or as a daemon thread from `worker.py`.
- Single entry point: `create_bot_application()` builds the `Application`, registers 16 `CommandHandler`s + 1 free-text `MessageHandler`.
- All command handlers are thin: they call `_waifu_cli()` (subprocess) or read JSON files directly, then `_safe_reply()`.
- No inline keyboards, no callback queries, no confirmation flows exist today.

### Key Imports Available (but unused)
The current file only imports `CommandHandler`, `MessageHandler`, `ContextTypes`, `filters`. It does NOT import:
- `CallbackQueryHandler`
- `InlineKeyboardMarkup` / `InlineKeyboardButton`
- `ReplyKeyboardMarkup`

These are all available in `python-telegram-bot>=21.0`.

### Pain Points Identified in Current Code
1. **Dangerous actions execute immediately** -- `cmd_emergency_stop` and `cmd_jido` run with zero confirmation.
2. **Slow commands go silent** -- `_waifu_cli()` can take up to 120s with no intermediate feedback. The user sees nothing after invoking `/jido`, `/evaluate`, `/review`, `/howl`, `/whale`, `/arena`.
3. **No inline actions** -- After viewing `/status`, the user must type `/emergency_stop` or `/regime` separately.
4. **Settings require memorising key names** -- `/rules_set` and `/gates_set` require exact key names like `eval_minscore` or `score_komodo`. The "help" fallback lists them all, but it's a wall of text.
5. **No persistent quick-access menu** -- Only slash commands. No inline buttons for common flows.
6. **`cmd_gates` and `cmd_rules` are read-only walls of text** -- No way to toggle/adjust from the displayed view.

### Risks and Edge Cases in Existing Code
- `_safe_reply()` checks `update.message` but `CallbackQuery` uses `query.message` -- all callback handlers must use `query.edit_message_text()` or `query.message.reply_text()`, NOT `_safe_reply(update, ...)`.
- Telegram's callback data limit is **64 bytes**. All `callback_data` strings must stay under this. No complex JSON payloads.
- `_waifu_cli()` uses `shutil.which("waifu")` -- the subprocess model is fine, but we need a "progress" pattern for slow commands.
- The `@authorized` decorator wraps `async def wrapper(update, context)` -- callback handlers have the same signature so the decorator works, BUT `update.callback_query` is where data lives, not `update.message`.
- Git sync in `cmd_rules_set` and `cmd_gates_set` is inline (30s timeout) -- these block the handler. For interactive flows, we should keep this pattern (it's proven) but add feedback.
- `ENABLE_SECTIONS` auto-enables sections when setting a value -- interactive toggles must handle this correctly.

---

## 1. Implementation Phases

### Phase 1: Infrastructure (CallbackQuery foundation)
**Priority: Must be first. Everything else depends on this.**

#### 1.1 -- New imports
Add to existing import block from `telegram.ext`:
- `CallbackQueryHandler`
Add to existing import block from `telegram`:
- `InlineKeyboardButton`, `InlineKeyboardMarkup`

#### 1.2 -- New helper: `_safe_edit(query, text, **kwargs)`
For callback queries, edit the existing message instead of sending a new one.
Catch `BadRequest` (message unchanged) gracefully and log.

#### 1.3 -- New helper: `_progress_reply(update, text="...") -> Optional[Message]`
Send a temporary "working..." message that gets replaced with the result later.
Returns the Message object so we can call `msg.edit_text()` after completion.

#### 1.4 -- New helper: `_answer_and_edit(query, text, reply_markup=None, **kwargs)`
Standard callback pattern -- call `await query.answer()` first (required by Telegram API),
then call `_safe_edit()`. Single function so we never forget `answer()`.

#### 1.5 -- Callback data naming convention
All `callback_data` strings use a prefix-based scheme, all must stay under 64 bytes:

| Prefix | Purpose | Example |
|--------|---------|---------|
| `act:` | Immediate actions | `act:emergency_stop_confirm`, `act:jido_confirm` |
| `nav:` | Navigation between menus | `nav:rules_main`, `nav:gates_scores` |
| `set:` | Setting a value | `set:evaluate:minScore:7` |
| `tog:` | Toggle enable/disable | `tog:fixed_tp_roe:enabled` |
| `noop` | Placeholder for disabled buttons | (just the string `noop`) |

Validate all generated callback_data with `assert len(cb) <= 64`.

#### 1.6 -- Register `CallbackQueryHandler`
Add to `create_bot_application()`, AFTER all CommandHandlers:
```python
app.add_handler(CallbackQueryHandler(handle_callback))
```

---

### Phase 2: Confirmation for Dangerous Actions
**Priority: High. Prevents accidental executions.**

#### 2.1 -- Rewrite `cmd_emergency_stop`
Current behaviour: Immediately calls `_waifu_cli("emergency-stop")`.
New behaviour: Show inline confirmation keyboard with CONFIRM and Cancel buttons.
- CONFIRM triggers `act:emergency_stop_confirm` callback which runs the actual stop.
- Cancel triggers `act:emergency_stop_cancel` callback which edits the message to "Cancelled."

The confirmation message should list what emergency stop does:
- Sets regime to RISK_OFF
- Blocks all new entries
- Sends Telegram alert
- Existing positions stay open (managed by DSL)

#### 2.2 -- Rewrite `cmd_jido`
Current behaviour: Immediately runs jido (up to 120s, user sees nothing).
New behaviour: Show confirmation keyboard with three options:
- "Run Jido" (live execution) -- `act:jido_confirm`
- "Dry Run" (preview only) -- `act:jido_dry`
- "Cancel" -- `act:jido_cancel`

The callbacks then call `_waifu_cli()` with the appropriate arguments.

#### 2.3 -- Implement central `handle_callback(update, context)`
Parse `update.callback_query.data`, route by prefix:
- `act:*` -> `_handle_action_callback(query, action)`
- `nav:*` -> `_handle_nav_callback(query, page)`
- `set:*` -> `_handle_set_callback(query, data)`
- `tog:*` -> `_handle_toggle_callback(query, data)`
- `noop` -> `query.answer()` only

Must call `await query.answer()` at the top of every branch.

#### 2.4 -- Implement `_handle_action_callback`
Routes action strings to their implementations:
- `emergency_stop_confirm` -> send progress message, call `_waifu_cli("emergency-stop")`, edit message with result
- `emergency_stop_cancel` -> edit message to "Cancelled."
- `jido_confirm` -> send progress, call `_waifu_cli("jido", timeout=120)`, edit with result
- `jido_dry` -> send progress, call `_waifu_cli("jido --dry-run", timeout=120)`, edit with result
- `jido_cancel` -> edit to "Cancelled."
- `evaluate_confirm` -> same pattern
- `evaluate_dry` -> same pattern
- `evaluate_cancel` -> edit to "Cancelled."
- `status_refresh` -> re-run status, edit message
- `gates_reset_confirm` -> run reset logic, edit with result
- `gates_reset_cancel` -> edit to "Cancelled."

---

### Phase 3: Progress Feedback for Slow Commands
**Priority: High. Fixes the "2 minutes of silence" problem.**

#### 3.1 -- Modify `cmd_evaluate`
Add confirmation keyboard (same pattern as jido):
- "Execute" -- `act:evaluate_confirm`
- "Dry Run" -- `act:evaluate_dry`
- "Cancel" -- `act:evaluate_cancel`

#### 3.2 -- Add progress messages to read-only slow commands
For `cmd_review`, `cmd_howl`, `cmd_whale`, `cmd_arena`, `cmd_regime`:
Before calling `_waifu_cli()`, send a progress message:
```
msg = await _progress_reply(update, "Generating portfolio report...")
output = await run_script_async(...)
await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")
```

This replaces the silent wait with visible feedback. The message gets edited in-place when done.

---

### Phase 4: Inline Action Buttons on Status
**Priority: High. "Remote control" feel.**

#### 4.1 -- Rewrite `cmd_status` with inline keyboard
After displaying status text, append inline buttons for immediate actions:

Row 1 (if positions exist):
- "Jido" -> `act:jido_prompt` (shows jido confirmation)
- "Evaluate" -> `act:evaluate_prompt` (shows evaluate confirmation)

Row 2:
- "Refresh" -> `act:status_refresh` (re-runs status, edits message)
- "Review" -> shows review output in new message
- "Gates" -> navigates to gates view

The status message stays as-is (waifu CLI output in code block) with buttons appended below.

#### 4.2 -- Add callbacks for status action buttons
- `status_refresh`: Re-run `waifu status`, edit the message with new output + same keyboard.
- `jido_prompt`, `evaluate_prompt`: Edit message to show the jido/evaluate confirmation keyboard.

---

### Phase 5: Interactive Rules Editor
**Priority: Medium. Replaces memorising `/rules_set <key> <value>`.**

#### 5.1 -- Add inline keyboard to `cmd_rules`
After displaying rules text, append navigation buttons:

Row 1:
- "Evaluate" -> `nav:rules_evaluate`
- "Jido" -> `nav:rules_jido`

Row 2:
- "TP/SL" -> `nav:rules_tpsl`
- "Partial" -> `nav:rules_partial`

Row 3:
- "DSL Override" -> `nav:rules_dsl`

#### 5.2 -- Rules section sub-menus
Each `nav:rules_<section>` callback shows a focused view of that section with editable fields as buttons.

Example for `nav:rules_evaluate`:
```
Evaluate Rules

minScore: 7        [Change]
maxLeverage: 10x   [Change]
maxPositions: 3    [Change]
cooldown: 120min   [Change]

[Back to Rules]
```

Each "Change" button uses `set:evaluate:minScore:pick` callback (which shows the value picker).

#### 5.3 -- Value picker for numeric fields
When user taps a "Change" button, show a picker with common values:

For `minScore`: show buttons 5, 6, 7, 8, 9, 10, 11, 12
For `maxLeverage`: show buttons 7, 8, 9, 10
For `maxPositions`: show buttons 1, 2, 3, 4, 5
For `cooldownMinutes`: show buttons 30, 60, 90, 120, 180, 240

Plus a "Back" button and a "Type manually" hint pointing to `/rules_set`.

The callback data for picking value 7 would be: `set:evaluate:minScore:7` (27 bytes, well under 64).

#### 5.4 -- Write-on-select handler
`_handle_set_callback` receives something like `evaluate:minScore:7`:
1. Look up `(section, field, converter)` in `RULES_KEY_MAP` (reuse existing validation).
2. Convert and validate the value.
3. Write to `user-rules.json` (atomic write pattern from existing code).
4. Git sync (reuse existing subprocess pattern).
5. Edit the message to show confirmation.
6. Offer "Back to section" navigation.

---

### Phase 6: Interactive Gates Editor
**Priority: Medium. Same pattern as rules.**

#### 6.1 -- Add inline keyboard to `cmd_gates`
After displaying gates text, append section buttons:

Row 1: "Positions" -> `nav:gates_positions`, "Leverage" -> `nav:gates_leverage`
Row 2: "Cooldown" -> `nav:gates_cooldown`, "Bans" -> `nav:gates_bans`
Row 3: "Scanner Scores" -> `nav:gates_scores`
Row 4: "Reset All" -> `act:gates_reset_confirm`

#### 6.2 -- Gates section sub-menus
Same picker pattern as rules. For scanner scores, show a grid of scanner names as buttons, each leading to its score picker (values 1-15 in rows of 5).

#### 6.3 -- Confirmation for gates_reset
Instead of executing immediately, show:
"Reset ALL gate overrides to defaults? This removes custom positions, leverage, cooldown, and score settings."
[Confirm] [Cancel]

---

### Phase 7: Toggle Controls
**Priority: Low-medium.**

#### 7.1 -- Toggle buttons for TP/SL/partial sections
In the rules sub-menus for `fixed_tp_roe`, `fixed_sl_roe`, `partial_tp`, `partial_sl`, show a toggle:
- If enabled: "ON" button (green text, `tog:fixed_tp_roe:enabled`)
- If disabled: "OFF" button (`tog:fixed_tp_roe:enabled`)

The toggle callback flips the boolean and refreshes the section view.

#### 7.2 -- `_handle_toggle_callback`
Receives `fixed_tp_roe:enabled`:
1. Load `user-rules.json`.
2. Read current value, flip it.
3. Write, sync, edit message with updated view.

---

### Phase 8: Menu Command
**Priority: Low. Nice-to-have.**

#### 8.1 -- New `/menu` command
Shows a "home panel" with all major actions as inline buttons:

```
Senpi -- Control Panel

[Status]  [Regime]
[Jido]    [Evaluate]
[Review]  [Rules]
[Gates]   [Howl]
[Whale]   [Arena]
[Emergency Stop]
```

Each button navigates to the corresponding command view. This gives a "mini-app" feel without requiring a web app.

#### 8.2 -- Update COMMANDS list
Add to the `COMMANDS` array:
```python
("menu", "Control panel", "Interactive dashboard with all actions and settings."),
```
This gets registered with BotFather so `/menu` appears in the slash command menu.

---

## 2. Handler Change Summary

### Modified Handlers

| Handler | Change | Phase |
|---------|--------|-------|
| `cmd_emergency_stop` | Add confirmation keyboard | 2 |
| `cmd_jido` | Add confirmation keyboard | 2 |
| `cmd_evaluate` | Add confirmation keyboard | 3 |
| `cmd_status` | Add inline action buttons | 4 |
| `cmd_review` | Add progress feedback | 3 |
| `cmd_howl` | Add progress feedback | 3 |
| `cmd_whale` | Add progress feedback | 3 |
| `cmd_arena` | Add progress feedback | 3 |
| `cmd_regime` | Add progress feedback | 3 |
| `cmd_rules` | Add navigation keyboard | 5 |
| `cmd_gates` | Add navigation keyboard | 6 |
| `create_bot_application` | Add CallbackQueryHandler + /menu | 1, 8 |

### New Handlers

| Handler | Purpose | Phase |
|---------|---------|-------|
| `handle_callback(update, context)` | Central callback router | 1 |
| `_handle_action_callback(query, action)` | Route act:* callbacks | 2 |
| `_handle_nav_callback(query, page)` | Route nav:* callbacks | 5 |
| `_handle_set_callback(query, data)` | Route set:* callbacks | 5 |
| `_handle_toggle_callback(query, data)` | Route tog:* callbacks | 7 |
| `cmd_menu(update, context)` | Home panel | 8 |

### New Helper Functions

| Function | Purpose |
|----------|---------|
| `_safe_edit(query, text, **kwargs)` | Edit callback query message |
| `_progress_reply(update, text)` | Send temporary "working..." message |
| `_answer_and_edit(query, text, **kwargs)` | Answer callback + edit message |
| `_show_value_picker(query, section, field)` | Numeric value picker UI |
| `_show_rules_evaluate(query)` | Evaluate rules sub-menu |
| `_show_rules_jido(query)` | Jido rules sub-menu |
| `_show_rules_tpsl(query)` | TP/SL toggle sub-menu |
| `_show_rules_partial(query)` | Partial TP/SL sub-menu |
| `_show_gates_positions(query)` | Gates positions sub-menu |
| `_show_gates_leverage(query)` | Gates leverage sub-menu |
| `_show_gates_scores(query)` | Per-scanner scores sub-menu |

---

## 3. Implementation Order

```
Phase 1 (Infrastructure)                    [Must be first]
  |
  v
Phase 2 (Dangerous Action Confirmation)     [Highest value, blocks mistakes]
  |
  v
Phase 3 (Progress Feedback)                 [Fixes the silence problem]
  |
  v
Phase 4 (Status Inline Actions)             [The "remote control" feel]
  |
  v
Phase 5 (Interactive Rules Editor)          [Replaces key memorisation]
  |
  v
Phase 6 (Interactive Gates Editor)          [Same pattern as rules]
  |
  v
Phase 7 (Toggle Controls)                   [Quick on/off for TP/SL sections]
  |
  v
Phase 8 (Menu Command)                      [Polish layer]
```

Phases 1-4 are the core UX upgrade. Phases 5-8 are progressive enhancement.

---

## 4. What We Will NOT Change

| Item | Reason |
|------|--------|
| `_waifu_cli()` subprocess logic | Core execution path. Changes here break all commands. |
| `worker.py` scheduler | Independent subsystem. Bot changes must not affect cron timing. |
| `@authorized` decorator | Security boundary. Must wrap every handler and callback. |
| Git sync pattern (`senpi_common.git_sync()`) | Proven atomic write + sync. Keep as-is in set handlers. |
| `handle_free_text()` / Hermes brain dispatch | Separate subsystem. Not touched. |
| `server.py` / FastAPI integration | Bot startup lifecycle managed by server lifespan. |
| `_strip_tui_artifacts()` | Hermes output cleaner. Independent. |
| `_count_open_positions()` / `_daily_stats()` | Pure data readers. Used as-is. |
| `load_json()` / `relative_time()` | Shared helpers. Not modified. |
| `RULES_KEY_MAP`, `GATES_KEY_MAP`, `RULES_CONFIRMATIONS` | Existing validation maps. Reused verbatim in callback handlers. |
| File write pattern (write `.tmp` then rename) | Atomic writes. Keep in all set/toggle handlers. |

---

## 5. Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Callback data exceeds 64 bytes | Validate all generated callback_data with `len()` check. Use short keys. |
| Message edit fails (message too old, >48h) | `_safe_edit()` catches the exception and logs. Falls back gracefully. |
| Race condition on `user-rules.json` | Bot processes one update at a time (default threading model). Atomic writes protect against corruption. |
| Keyboard clutter on small screens | Max 3-4 buttons per row. Use sub-menus instead of cramming everything on one view. |
| Breaking existing `/rules_set` and `/gates_set` commands | Keep them working as-is. Interactive menus are additive. Users can still type commands. |
| `@authorized` not checking callback queries properly | The decorator reads `update.effective_chat.id` which works for both messages and callbacks. Already correct. |

---

## 6. Code Size Estimate

- Current: 1270 lines
- Infrastructure (Phase 1): ~50 lines
- Confirmation (Phase 2): ~80 lines
- Progress (Phase 3): ~60 lines
- Status buttons (Phase 4): ~50 lines
- Rules editor (Phase 5): ~200 lines
- Gates editor (Phase 6): ~150 lines
- Toggles (Phase 7): ~50 lines
- Menu (Phase 8): ~40 lines
- **Total estimate: ~680 new lines -> ~1950 lines total**

---

## 7. Testing Strategy

1. **Phase 1**: Verify `CallbackQueryHandler` doesn't break existing command handlers (commands match first by design).
2. **Phase 2**: Test emergency stop Cancel (no execution). Test Confirm (execution happens).
3. **Phase 3**: Verify progress messages appear and get replaced by results.
4. **Phase 4**: Verify status refresh edits same message (no duplicates).
5. **Phase 5-6**: Verify rules/gates changes persist to `user-rules.json` and git sync succeeds. Verify `/rules` still shows correct values after interactive changes.
6. **Phase 7**: Verify toggles flip boolean without touching other fields.
7. **All phases**: Verify `@authorized` rejects unauthenticated callbacks.
8. **Edge case**: Test callback on message older than 48h (Telegram won't allow edit -- verify graceful degradation).

---

## 8. Open Questions

1. **`_waifu_cli` reuse in callbacks**: The confirmation callbacks need subprocess logic. Should we call `run_script_async` directly or wrap through `_waifu_cli`? Recommend `run_script_async` directly -- it already exists and is clean.

2. **Edit vs new message for long outputs**: Some waifu CLI outputs exceed Telegram's 4096 char limit. After edit, we can't split. Recommend: edit the "working..." message with a summary, then send full output as follow-up message.

3. **Callback data for score pickers**: Scanner score values 1-20 means 20 buttons per picker -- too many. Recommend: show only the "interesting" range (3-15) as buttons, with a "Type manually" fallback to `/gates_set score_orca 8`.

4. **Concurrent callback handling**: If user taps two buttons rapidly, race conditions on `user-rules.json` writes. The atomic write pattern is safe for single-writer. Bot processes one update at a time by default, so this is fine.
