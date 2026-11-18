# Changelog

## 0.1.0

- First release. Generalised from a private single-project hook.
- Hooks: `UserPromptSubmit` and `PostToolUse` run `scripts/ctxguard.py` (stdlib only, no model, no network; any error prints nothing and exits 0).
- Soft (150k tokens / 6 h) and strong (250k / 9 h) reminders with generic wording.
- Per-project `.claude/ctxguard.json` (soft, strong, block, age_soft_h, age_strong_h, block_enabled) with strict type and ordering validation; an invalid file means defaults.
- Blocking at 500k tokens is OFF by default and only active with `block_enabled: true`; override with `.claude/ctxguard.override`.
- `PostToolUse` repeats at most once per +25k tokens or 30 minutes, including age-only triggers.
- Python 3.8 safe timestamp parsing (fractional seconds, offsets).
- Environment overrides for tests (`CONTEXT_GUARD_*`).
