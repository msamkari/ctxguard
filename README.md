# ctxguard

A tiny Claude Code hook that keeps long sessions from silently draining your usage limits. Every turn re-reads the whole context, so a 300k-token session costs far more per message than a fresh one. ctxguard measures the current context size and the session age from the transcript and nudges Claude (and you) to wrap up and hand over to a new session.

- Stdlib-only Python, no model, no network, no dependencies.
- Never fails your session: any error prints nothing and exits 0.
- Works on Windows, Linux and macOS (Python 3.8+).

## Install

Add the plugin from your marketplace or load it locally:

```
claude --plugin-dir /path/to/ctxguard
```

The plugin registers two hooks (`UserPromptSubmit`, `PostToolUse`) that run `scripts/ctxguard.py`. It needs `python` or `python3` on PATH. It pre-approves no tools and asks for no permissions.

## Behaviour

| Level | Trigger (defaults) | What happens |
|---|---|---|
| soft | context >= 150k tokens or age >= 6 h | A reminder is added to the context: finish the step, keep it short, save notes, propose a fresh session. |
| strong | context >= 250k tokens or age >= 9 h | Stronger instruction: no new agents or workflows, finish the step, hand over. |
| block (opt-in) | context >= 500k tokens and `block_enabled` is true | The prompt is blocked with a message, unless the override file exists. |

On `UserPromptSubmit` the reminder is added to every prompt. On `PostToolUse` it is repeated only after the context grew by 25k tokens or 30 minutes passed. Blocking only ever happens on `UserPromptSubmit`, and it is OFF by default.

## Per-project configuration

Create `.claude/ctxguard.json` in your project (all keys optional):

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

Values must be positive numbers (booleans and strings are rejected), `block_enabled` must be a real boolean, and the order must satisfy soft <= strong <= block and age_soft_h <= age_strong_h. If anything is invalid the whole file is ignored and the defaults apply, so a typo can never enable blocking.

To continue in a blocked session anyway, create an empty file `.claude/ctxguard.override` in the project, and delete it afterwards.

## Environment overrides (for tests)

`CONTEXT_GUARD_SOFT`, `CONTEXT_GUARD_STRONG`, `CONTEXT_GUARD_BLOCK` (tokens), `CONTEXT_GUARD_AGE_SOFT`, `CONTEXT_GUARD_AGE_STRONG` (hours). They beat the project file; invalid values are ignored.

## Privacy and limits

- Reads only the transcript path Claude Code gives the hook (last ~400 KB plus the first line) and the optional config files above.
- Writes only a tiny counter file in the system temp directory (`ctxguard_<session>.txt`).
- Context size is taken from the last `usage` record (input + cache read + cache creation tokens); it is an estimate, not billing data.

## Tests

```
python -m unittest discover -s tests -v
```

## License

MIT
