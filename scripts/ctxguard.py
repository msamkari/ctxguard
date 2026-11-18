"""ctxguard: context guard hook (stdlib only, no model, no network).

Reads the hook JSON from stdin, measures the CURRENT context size and the
session age from the transcript, and nudges (or, opt-in, blocks) so long
sessions hand over to a fresh one. Events: UserPromptSubmit and PostToolUse.

Levels:
 1 soft:   context >= SOFT (default 150k tokens) or age >= 6 h
           (UserPromptSubmit: every prompt; PostToolUse: once per +25k tokens
           or every 30 minutes)
 2 strong: context >= STRONG (250k) or age >= 9 h (age alone never gives
           strong while the context is under 30k tokens)
 3 block:  context >= BLOCK (500k) AND blocking enabled by the USER (env
           CTXGUARD_BLOCK=1 or ~/.claude/ctxguard.json block_enabled=true;
           a project file can never enable it). UserPromptSubmit is blocked
           unless a fresh (<12 h) .claude/ctxguard.override exists in the
           project. PostToolUse never blocks.

Config files (all keys optional): ~/.claude/ctxguard.json (user) and
<project>/.claude/ctxguard.json (project, thresholds only). Keys: soft,
strong, block (tokens), age_soft_h, age_strong_h (hours), block_enabled
(user file only). Floors: soft>=20000, strong>=20000, block>=100000, ages
>=0.5 h; strictly soft<strong<block and age_soft_h<age_strong_h. A layer
that violates anything is ignored whole. Precedence: defaults < user file <
project file < env.
Env overrides: CONTEXT_GUARD_SOFT/STRONG/BLOCK (tokens),
CONTEXT_GUARD_AGE_SOFT/AGE_STRONG (hours), same floors; invalid values are
ignored. The project dir is CLAUDE_PROJECT_DIR, else the payload cwd.

Never fails the session: any error prints nothing and exits 0.
"""
import sys

if sys.version_info < (3, 6):
    sys.exit(0)

import datetime
import json
import os
import re
import tempfile
import time

DEFAULTS = {
    "soft": 150000.0,
    "strong": 250000.0,
    "block": 500000.0,
    "age_soft_h": 6.0,
    "age_strong_h": 9.0,
}
FLOORS = {
    "soft": 20000.0,
    "strong": 20000.0,
    "block": 100000.0,
    "age_soft_h": 0.5,
    "age_strong_h": 0.5,
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
MAX_SCAN_BYTES = 8000000
OVERRIDE_MAX_AGE_S = 12 * 3600
AGE_STRONG_MIN_CTX = 30000
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
    return (c["soft"] < c["strong"] < c["block"] and c["age_soft_h"] < c["age_strong_h"]
            and all(c[k] >= FLOORS[k] for k in FLOORS))


def _read_layer(path, allow_block):
    """Parse one config file. Return (partial dict, block flag) or None if invalid."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 65536:
            return None
        with open(path, "rb") as fh:
            raw = json.loads(fh.read().decode("utf-8-sig", "replace"))
        if not isinstance(raw, dict):
            return None
        part = {}
        for key in DEFAULTS:
            if key in raw:
                n = _num(raw[key])
                if n is None:
                    return None
                part[key] = n
        be = False
        if allow_block and "block_enabled" in raw:
            if not isinstance(raw["block_enabled"], bool):
                return None
            be = raw["block_enabled"]
        return part, be
    except Exception:
        return None


def load_config(project_dir):
    """Return (thresholds dict, block_enabled bool). An invalid layer is ignored."""
    cfg = dict(DEFAULTS)
    block_enabled = False
    layers = []
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""
    if home and os.path.isabs(home):
        layers.append((os.path.join(home, ".claude", "ctxguard.json"), True))
    if project_dir:
        layers.append((os.path.join(project_dir, CONFIG_REL), False))
    for path, allow_block in layers:
        got = _read_layer(path, allow_block)
        if got is None:
            continue
        new = dict(cfg)
        new.update(got[0])
        if _ordered(new):
            cfg = new
            if allow_block:
                block_enabled = got[1]
    new = dict(cfg)
    for key, name in ENV_NAMES.items():
        try:
            n = _num(float(os.environ[name]))
        except Exception:
            n = None
        if n is not None:
            new[key] = n
    if _ordered(new):
        cfg = new
    if os.environ.get("CTXGUARD_BLOCK") == "1":
        block_enabled = True
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


def _usage_from(lines):
    for line in reversed(lines):
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
            return total
    return 0


def measure(path):
    """Return (context_tokens, first_timestamp_epoch or None) from a transcript JSONL.

    Reads the last TAIL_BYTES; if no usage line is found there, doubles the
    window backwards until MAX_SCAN_BYTES, then gives up (ctx 0).
    """
    size = os.path.getsize(path)
    ctx = 0
    with open(path, "rb") as fh:
        head = fh.readline(1000000)
        window = TAIL_BYTES
        while True:
            start = max(0, size - window)
            fh.seek(start)
            lines = fh.read(size - start).decode("utf-8", errors="replace").splitlines()
            if start > 0 and lines:
                lines = lines[1:]  # the first line of the window may be cut
            ctx = _usage_from(lines)
            if ctx or start == 0 or window >= MAX_SCAN_BYTES:
                break
            window = min(window * 2, MAX_SCAN_BYTES)
    first = None
    try:
        first = parse_ts(json.loads(head.decode("utf-8", errors="replace")).get("timestamp"))
    except Exception:
        first = None
    return ctx, first


def read_state(state):
    """Return (last_ctx, mtime) or (None, None)."""
    try:
        with open(state) as fh:
            last = int(fh.read().strip() or 0)
        return last, os.path.getmtime(state)
    except Exception:
        return None, None


def override_active(root):
    """Override file exists and its mtime is within the last 12 h (a future mtime = expired)."""
    try:
        p = os.path.join(root, OVERRIDE_REL)
        if not os.path.isfile(p):
            return False
        age = time.time() - os.path.getmtime(p)
        return 0 <= age < OVERRIDE_MAX_AGE_S
    except Exception:
        return False


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
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
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if not root:
        root = data.get("cwd")
    if not isinstance(root, str) or not root:
        root = os.getcwd()
    cfg, block_enabled = load_config(root)
    try:
        ctx, first = measure(path)
    except Exception:
        return 0
    age_h = max(0.0, (time.time() - first) / 3600.0) if first else 0.0
    level = 0
    if ctx >= cfg["soft"] or age_h >= cfg["age_soft_h"]:
        level = 1
    if ctx >= cfg["strong"] or (age_h >= cfg["age_strong_h"] and ctx >= AGE_STRONG_MIN_CTX):
        level = 2
    if block_enabled and ctx >= cfg["block"]:
        level = 3
    if level == 0:
        return 0
    state = os.path.join(tempfile.gettempdir(), "ctxguard_%s.txt" % sid)
    if event == "PostToolUse":
        last, mtime = read_state(state)
        if last is not None and ctx < last + POST_STEP and time.time() - mtime < POST_MIN_SECONDS:
            return 0
    try:
        with open(state, "w") as fh:
            fh.write(str(int(ctx)))
    except Exception:
        pass
    k = int(ctx / 1000)
    if level == 3 and event == "UserPromptSubmit" and not override_active(root):
        print(json.dumps({"decision": "block", "reason": (
            "ctxguard: this session is about %dk tokens (limit %dk). Open a NEW session and continue "
            "from a short handoff summary. To continue here anyway, create the file "
            ".claude/ctxguard.override in the project (it expires after 12 hours)."
            % (k, max(1, int(round(cfg["block"] / 1000.0)))))}))
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
