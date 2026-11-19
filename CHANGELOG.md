# Changelog

## 0.1.1

- Fix: a bad config field (wrong type, non-positive number) is now skipped on its own instead of discarding the whole file; the remaining valid fields still apply.
- New: the first reminder of a session reports what was ignored: `ctxguard config invalid: <reason>; using defaults for the rest.` (unreadable/non-object file, bad field, out-of-order thresholds, bad `CONTEXT_GUARD_*` env value, `block_enabled` in a project file). Missing files stay silent.
- Docs: Python older than 3.6 exits silently (guard inactive); documented in both READMEs.
- No change to permissions, hooks, license or name.

## 0.1.0

Red-team fix batch (before first publish, version unchanged):

- Fix: stdin is read as bytes and decoded as UTF-8, so non-ASCII project or transcript paths no longer silence the guard on Windows; hooks run `python3 -X utf8` then `python -X utf8`.
- Security: `block_enabled` is honoured only from `CTXGUARD_BLOCK=1` or the user file `~/.claude/ctxguard.json`, never from a project file. Thresholds have floors (soft/strong >= 20000, block >= 100000, ages >= 0.5 h) with strict ordering; also applied to env values.
- Fix: block message no longer prints "limit 0k".
- Fix: when no usage record is in the last 400 KB, the transcript is read backwards (doubling window) up to 8 MB.
- Age: age alone never gives the strong level while context < 30k tokens (resumed/compacted sessions keep their original age; documented limitation).
- Project dir is `CLAUDE_PROJECT_DIR` first, then the payload cwd.
- The override file expires after 12 hours (mtime); `PostToolUse` is throttled at block level too.
- Python older than 3.6 exits silently.

Original 0.1.0 contents:

- First release. Generalised from a private single-project hook.
- Hooks: `UserPromptSubmit` and `PostToolUse` run `scripts/ctxguard.py` (stdlib only, no model, no network; any error prints nothing and exits 0).
- Soft (150k tokens / 6 h) and strong (250k / 9 h) reminders with generic wording.
- Per-project `.claude/ctxguard.json` (soft, strong, block, age_soft_h, age_strong_h, block_enabled) with strict type and ordering validation; an invalid file means defaults.
- Blocking at 500k tokens is OFF by default and only active with `block_enabled: true`; override with `.claude/ctxguard.override`.
- `PostToolUse` repeats at most once per +25k tokens or 30 minutes, including age-only triggers.
- Python 3.8 safe timestamp parsing (fractional seconds, offsets).
- Environment overrides for tests (`CONTEXT_GUARD_*`).
