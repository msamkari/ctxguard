# ctxguard

A tiny Claude Code hook that keeps long sessions from silently draining your usage limits. Every turn re-reads the whole context, so a 300k-token session costs far more per message than a fresh one. ctxguard measures the current context size and the session age from the transcript and nudges Claude (and you) to wrap up and hand over to a new session.

- Stdlib-only Python, no model, no network, no dependencies.
- Never fails your session: any error prints nothing and exits 0.
- Works on Windows, Linux and macOS (Python 3.8+; older Pythons exit silently).

## Install

Add the plugin from your marketplace or load it locally:

```
claude --plugin-dir /path/to/ctxguard
```

The plugin registers two hooks (`UserPromptSubmit`, `PostToolUse`) that run `scripts/ctxguard.py`. It needs `python3` or `python` on PATH (tried in that order, UTF-8 mode on). It pre-approves no tools and asks for no permissions.

## Behaviour

| Level | Trigger (defaults) | What happens |
|---|---|---|
| soft | context >= 150k tokens or age >= 6 h | A reminder is added to the context: finish the step, keep it short, save notes, propose a fresh session. |
| strong | context >= 250k tokens or age >= 9 h (age alone needs context >= 30k) | Stronger instruction: no new agents or workflows, finish the step, hand over. |
| block (opt-in) | context >= 500k tokens and blocking is enabled by you (see Configuration) | The prompt is blocked with a message, unless the override file exists. |

On `UserPromptSubmit` the reminder is added to every prompt. On `PostToolUse` it is repeated only after the context grew by 25k tokens or 30 minutes passed. Blocking only ever happens on `UserPromptSubmit`, and it is OFF by default.

## Configuration

Two optional files, same keys (all optional): the user file `~/.claude/ctxguard.json` and the project file `<project>/.claude/ctxguard.json`. Precedence: defaults, then user file, then project file, then environment variables.

```json
{
  "soft": 150000,
  "strong": 250000,
  "block": 500000,
  "age_soft_h": 6,
  "age_strong_h": 9,
  "block_enabled": false
}
```

- Values must be positive numbers (booleans and strings are rejected). Floors: soft and strong >= 20000, block >= 100000, ages >= 0.5 h. Order must be strictly soft < strong < block and age_soft_h < age_strong_h. A file (layer) that breaks any rule is ignored whole.
- **Blocking is opt-in and only the user can enable it**: set the environment variable `CTXGUARD_BLOCK=1`, or `"block_enabled": true` in the USER file `~/.claude/ctxguard.json`. `block_enabled` in a project file is ignored, so a cloned repo can never lock your sessions.
- The project directory is `CLAUDE_PROJECT_DIR` when set, otherwise the hook's current directory.
- To continue in a blocked session anyway, create an empty file `.claude/ctxguard.override` in the project. It is honoured for 12 hours (by modification time), then ignored.
- Age limits: the session age is measured from the first line of the transcript, so a resumed or compacted session keeps its original age. To limit the damage, age alone never triggers the strong level while the context is under 30k tokens.

## Environment overrides (for tests)

`CONTEXT_GUARD_SOFT`, `CONTEXT_GUARD_STRONG`, `CONTEXT_GUARD_BLOCK` (tokens), `CONTEXT_GUARD_AGE_SOFT`, `CONTEXT_GUARD_AGE_STRONG` (hours). They beat the files and follow the same floors; invalid values are ignored.

## Privacy and limits

- Reads only the transcript path Claude Code gives the hook (last ~400 KB plus the first line; if no usage record is there it reads backwards up to 8 MB) and the optional config files above.
- Writes only a tiny counter file in the system temp directory (`ctxguard_<session>.txt`).
- Context size is taken from the last `usage` record (input + cache read + cache creation tokens); it is an estimate, not billing data.

## Tests

```
python -m unittest discover -s tests -v
```

## License

MIT
