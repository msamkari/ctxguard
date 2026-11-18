"""ctxguard: context guard hook (stdlib only, no model, no network).

Reads the hook JSON from stdin, measures the CURRENT context size and the
session age from the transcript, and nudges (or, opt-in, blocks) so long
sessions hand over to a fresh one. Events: UserPromptSubmit and PostToolUse.

Levels:
 1 soft:   context >= SOFT (default 150k tokens) or age >= 6 h
           (UserPromptSubmit: every prompt; PostToolUse: once per +25k tokens
           or every 30 minutes)
 2 strong: context >= STRONG (250k) or age >= 9 h
 3 block:  context >= BLOCK (500k) AND block_enabled=true in the project config:
           UserPromptSubmit is blocked unless .claude/ctxguard.override exists
           in the project (delete it afterwards). Off by default.

Project config (optional): <project>/.claude/ctxguard.json with the keys
soft, strong, block (tokens), age_soft_h, age_strong_h (hours), block_enabled
(bool). Any invalid value or ordering means the whole file is ignored.
Env overrides (tests): CONTEXT_GUARD_SOFT, CONTEXT_GUARD_STRONG,
CONTEXT_GUARD_BLOCK (tokens), CONTEXT_GUARD_AGE_SOFT, CONTEXT_GUARD_AGE_STRONG
(hours). Env beats the project file; invalid env values are ignored.

Never fails the session: any error prints nothing and exits 0.
"""
import datetime
import json
import os
import re
import sys
import tempfile
import time

DEFAULTS = {
    "soft": 150000.0,
    "strong": 250000.0,
    "block": 500000.0,
    "age_soft_h": 6.0,
    "age_strong_h": 9.0,
}
ENV_NAMES = {
    "soft": "CONTEXT_GUARD_SOFT",
    "strong": "CONTEXT_GUARD_STRONG",
    "block": "CONTEXT_GUARD_BLOCK",
    "age_soft_h": "CONTEXT_GUARD_AGE_SOFT",
    "age_strong_h": "CONTEXT_GUARD_AGE_STRONG",
}
CONFIG_REL = os.path.join(".claude", "ctxguard.json")
OVERRIDE_REL = os.path.join(".claude", "ctxguard.override")
TAIL_BYTES = 400000
POST_STEP = 25000
POST_MIN_SECONDS = 1800


def _num(v):
    """Positive finite number (not bool) or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return None
    return v


def _ordered(c):
    return c["soft"] <= c["strong"] <= c["block"] and c["age_soft_h"] <= c["age_strong_h"]


def load_config(project_dir):
    """Return (thresholds dict, block_enabled bool). Bad file = defaults."""
    cfg = dict(DEFAULTS)
    block_enabled = False
    try:
        path = os.path.join(project_dir, CONFIG_REL)
        if os.path.isfile(path) and os.path.getsize(path) <= 65536:
            with open(path, "rb") as fh:
                raw = json.loads(fh.read().decode("utf-8-sig"))
            ok = isinstance(raw, dict)
            new = dict(DEFAULTS)
            be = False
            if ok:
                for key in DEFAULTS:
                    if key in raw:
                        n = _num(raw[key])
                        if n is None:
                            ok = False
                            break
                        new[key] = n
                if ok and "block_enabled" in raw:
                    if isinstance(raw["block_enabled"], bool):
                        be = raw["block_enabled"]
                    else:
                        ok = False
            if ok and _ordered(new):
                cfg, block_enabled = new, be
    except Exception:
        cfg, block_enabled = dict(DEFAULTS), False
    for key, name in ENV_NAMES.items():
        try:
            n = _num(float(os.environ[name]))
        except Exception:
            n = None
        if n is not None:
            cfg[key] = n
    if not _ordered(cfg):
        cfg = dict(DEFAULTS)
    return cfg, block_enabled


_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(ts):
    """ISO-8601 timestamp to epoch seconds (py3.8 safe) or None."""
    m = _TS.match(str(ts).strip())
    if not m:
        return None
    y, mo, d, h, mi, s = [int(x) for x in m.groups()[:6]]
    tz = m.group(7)
    off = 0
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        off = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    dt = datetime.datetime(y, mo, d, h, mi, s, tzinfo=datetime.timezone.utc)
    return dt.timestamp() - off


def measure(path):
    """Return (context_tokens, first_timestamp_epoch or None) from a transcript JSONL."""
    size = os.path.getsize(path)
    ctx = 0
    with open(path, "rb") as fh:
        head = fh.readline(1000000)
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read().decode("utf-8", errors="replace").splitlines()
    first = None
    try:
        first = parse_ts(json.loads(head.decode("utf-8", errors="replace")).get("timestamp"))
    except Exception:
        first = None
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            u = (json.loads(line).get("message") or {}).get("usage") or {}
            if not isinstance(u, dict):
                continue
            total = 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                v = u.get(k, 0)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    v = 0
                total += int(v)
        except Exception:
            continue
        if total:
            ctx = total
            break
    return ctx, first


def read_state(state):
    """Return (last_ctx, mtime) or (None, None)."""
    try:
        with open(state) as fh:
            last = int(fh.read().strip() or 0)
        return last, os.path.getmtime(state)
    except Exception:
        return None, None


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    path = data.get("transcript_path")
    event = data.get("hook_event_name", "")
    if event not in ("UserPromptSubmit", "PostToolUse"):
        return 0
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id", "x")))[:40] or "x"
    if not isinstance(path, str) or not path or not os.path.isfile(path):
        return 0
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cfg, block_enabled = load_config(cwd)
    try:
        ctx, first = measure(path)
    except Exception:
        return 0
    age_h = max(0.0, (time.time() - first) / 3600.0) if first else 0.0
    level = 0
    if ctx >= cfg["soft"] or age_h >= cfg["age_soft_h"]:
        level = 1
    if ctx >= cfg["strong"] or age_h >= cfg["age_strong_h"]:
        level = 2
    if block_enabled and ctx >= cfg["block"]:
        level = 3
    if level == 0:
        return 0
    state = os.path.join(tempfile.gettempdir(), "ctxguard_%s.txt" % sid)
    if event == "PostToolUse" and level < 3:
        last, mtime = read_state(state)
        if last is not None and ctx < last + POST_STEP and time.time() - mtime < POST_MIN_SECONDS:
            return 0
    try:
        with open(state, "w") as fh:
            fh.write(str(int(ctx)))
    except Exception:
        pass
    override = os.path.join(cwd, OVERRIDE_REL)
    k = int(ctx / 1000)
    if level == 3 and event == "UserPromptSubmit" and not os.path.exists(override):
        print(json.dumps({"decision": "block", "reason": (
            "ctxguard: this session is about %dk tokens (limit %dk). Open a NEW session and continue "
            "from a short handoff summary. To continue here anyway, create the file "
            ".claude/ctxguard.override in the project (then delete it)." % (k, int(cfg["block"] / 1000)))}))
        return 0
    base = ("CONTEXT GUARD: this session's context is about %dk tokens and it is %.1f h old. "
            "Every turn re-reads all of it, which is what drains usage limits." % (k, age_h))
    if level >= 2:
        msg = base + (" STRONG RULE: do not launch new agents or workflows in this session. Finish the current "
                      "step, save any state or notes, tell the user plainly that a fresh session is needed, "
                      "and hand over with a short summary.")
    else:
        msg = base + (" SOFT RULE: finish the current step, keep it short, save any state or notes, "
                      "and propose a fresh session at the next natural break.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
