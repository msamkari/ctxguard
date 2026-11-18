import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "ctxguard.py")
ENV_KEYS = ["CONTEXT_GUARD_SOFT", "CONTEXT_GUARD_STRONG", "CONTEXT_GUARD_BLOCK",
            "CONTEXT_GUARD_AGE_SOFT", "CONTEXT_GUARD_AGE_STRONG",
            "CLAUDE_PROJECT_DIR", "CTXGUARD_BLOCK", "PYTHONUTF8", "PYTHONIOENCODING"]


def iso(hours_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.123Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(self.proj, ".claude"))
        self.tdir = os.path.join(self.tmp.name, "t")
        os.makedirs(self.tdir)
        self.n = 0
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, ".claude"))

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, tokens, age_h=0.1, name="t.jsonl"):
        p = os.path.join(self.tmp.name, name)
        first = {"type": "user", "timestamp": iso(age_h), "message": {"content": "hi"}}
        usage = {"input_tokens": 10, "cache_read_input_tokens": tokens - 20,
                 "cache_creation_input_tokens": 10, "output_tokens": 5}
        last = {"type": "assistant", "message": {"usage": usage}}
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(first) + "\n" + json.dumps(last) + "\n")
        return p

    def config(self, obj, raw=None):
        with open(os.path.join(self.proj, ".claude", "ctxguard.json"), "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(obj))

    def user_config(self, obj):
        with open(os.path.join(self.home, ".claude", "ctxguard.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj))

    def run_hook(self, tpath, event="UserPromptSubmit", sid="s1", stdin=None, env=None, cwd=None):
        self.n += 1
        e = dict(os.environ)
        for k in ENV_KEYS:
            e.pop(k, None)
        e["TMPDIR"] = e["TEMP"] = e["TMP"] = self.tdir
        e["HOME"] = e["USERPROFILE"] = self.home
        e.update(env or {})
        payload = stdin if stdin is not None else json.dumps({
            "hook_event_name": event, "transcript_path": tpath, "session_id": sid,
            "cwd": self.proj if cwd is None else cwd})
        r = subprocess.run([sys.executable, SCRIPT], input=payload.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8")

    def ctx(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]


class TestLevels(Base):
    def test_100k_silent(self):
        self.assertEqual(self.run_hook(self.transcript(100000)), "")

    def test_160k_soft(self):
        msg = self.ctx(self.run_hook(self.transcript(160000)))
        self.assertIn("SOFT RULE", msg)
        self.assertIn("160k", msg)

    def test_300k_strong(self):
        self.assertIn("STRONG RULE", self.ctx(self.run_hook(self.transcript(300000))))

    def test_520k_not_blocked_by_default(self):
        out = self.run_hook(self.transcript(520000))
        self.assertNotIn('"block"', out)
        self.assertIn("STRONG RULE", self.ctx(out))

    def test_age_soft_and_strong(self):
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(1000, age_h=7))))
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(1000, age_h=10))))
        self.assertIn("STRONG RULE", self.ctx(self.run_hook(self.transcript(40000, age_h=10))))

    def test_age_young_silent(self):
        self.assertEqual(self.run_hook(self.transcript(1000, age_h=5)), "")

    def test_env_override(self):
        out = self.run_hook(self.transcript(60000), env={"CONTEXT_GUARD_SOFT": "50000"})
        self.assertIn("SOFT RULE", self.ctx(out))

    def test_bad_env_ignored(self):
        self.assertEqual(self.run_hook(self.transcript(100000), env={"CONTEXT_GUARD_SOFT": "abc"}), "")

    def test_event_name_echoed(self):
        out = json.loads(self.run_hook(self.transcript(160000), event="PostToolUse"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_unknown_event_silent(self):
        self.assertEqual(self.run_hook(self.transcript(300000), event="Stop"), "")


class TestPostToolThrottle(Base):
    def test_throttle_then_step(self):
        t = self.transcript(160000)
        self.assertNotEqual(self.run_hook(t, "PostToolUse", sid="a"), "")
        self.assertEqual(self.run_hook(t, "PostToolUse", sid="a"), "")
        t2 = self.transcript(190000)
        self.assertNotEqual(self.run_hook(t2, "PostToolUse", sid="a"), "")

    def test_user_prompt_never_throttled(self):
        t = self.transcript(160000)
        self.assertNotEqual(self.run_hook(t, sid="b"), "")
        self.assertNotEqual(self.run_hook(t, sid="b"), "")

    def test_age_only_first_post_tool_emits(self):
        t = self.transcript(1000, age_h=7)
        self.assertNotEqual(self.run_hook(t, "PostToolUse", sid="c"), "")
        self.assertEqual(self.run_hook(t, "PostToolUse", sid="c"), "")

    def test_weird_session_id(self):
        t = self.transcript(160000)
        self.assertNotEqual(self.run_hook(t, "PostToolUse", sid="../../x y"), "")


class TestBadInputs(Base):
    def test_missing_transcript(self):
        self.assertEqual(self.run_hook(os.path.join(self.tmp.name, "nope.jsonl")), "")

    def test_corrupt_transcript(self):
        p = os.path.join(self.tmp.name, "bad.jsonl")
        with open(p, "wb") as fh:
            fh.write(b"\xff\xfe{{{not json\n\x00\x00\n\"usage\" garbage\n")
        self.assertEqual(self.run_hook(p), "")

    def test_empty_transcript(self):
        p = os.path.join(self.tmp.name, "e.jsonl")
        open(p, "w").close()
        self.assertEqual(self.run_hook(p), "")

    def test_usage_wrong_types(self):
        p = os.path.join(self.tmp.name, "w.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": "garbage"}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": "9999999", "cache_read_input_tokens": [1]}}}) + "\n")
            fh.write(json.dumps({"message": {"usage": "x"}}) + "\n")
        self.assertEqual(self.run_hook(p), "")

    def test_garbage_stdin(self):
        for s in ["", "not json", "[]", "null", "42", "{\"transcript_path\": 5}", "{\"transcript_path\": null}"]:
            self.assertEqual(self.run_hook(None, stdin=s), "")

    def test_invalid_utf8_stdin(self):
        e = dict(os.environ)
        r = subprocess.run([sys.executable, SCRIPT], input=b"\xff\xfe\x00{", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=e, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_missing_cwd_uses_fallback(self):
        t = self.transcript(160000)
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "transcript_path": t, "session_id": "z"})
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(t, stdin=payload)))


class TestConfig(Base):
    def test_config_lowers_threshold(self):
        self.config({"soft": 50000, "strong": 90000})
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(60000))))
        self.assertIn("STRONG RULE", self.ctx(self.run_hook(self.transcript(95000))))

    def test_config_raises_threshold(self):
        self.config({"soft": 400000, "strong": 450000})
        self.assertEqual(self.run_hook(self.transcript(300000)), "")

    def test_config_age(self):
        self.config({"age_soft_h": 1, "age_strong_h": 2})
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(10, age_h=1.5))))

    def test_env_beats_config(self):
        self.config({"soft": 50000})
        self.assertEqual(self.run_hook(self.transcript(60000), env={"CONTEXT_GUARD_SOFT": "90000"}), "")

    def test_bad_configs_fall_back_to_defaults(self):
        bad = ['{"soft": "50000"}', '{"soft": true}', '{"soft": -5}', '{"soft": 0}', '{"soft": null}',
               '{"soft": 300000, "strong": 200000}', '{"block_enabled": "yes"}', '{"block_enabled": 1}',
               '[1,2]', '"x"', 'null', '{not json', '', '\x00\x00', '{"soft": 1e999}', '{"soft": NaN}',
               '{"soft": 1000, "block_enabled": 5}']
        for raw in bad:
            with self.subTest(raw=raw):
                self.config(None, raw=raw)
                self.assertEqual(self.run_hook(self.transcript(100000)), "")
                self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(160000))))
                self.assertNotIn('"block"', self.run_hook(self.transcript(520000)))

    def test_bad_config_with_block_enabled_does_not_enable(self):
        self.config(None, raw='{"block_enabled": true, "soft": "x"}')
        self.assertNotIn('"block"', self.run_hook(self.transcript(520000)))

    def test_unknown_keys_ignored(self):
        self.config({"soft": 50000, "extra": [1]})
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(60000))))

    def test_config_dir_is_not_a_file(self):
        os.makedirs(os.path.join(self.proj, ".claude", "ctxguard.json"))
        self.assertEqual(self.run_hook(self.transcript(100000)), "")


class TestBlocking(Base):
    B = {"CTXGUARD_BLOCK": "1"}

    def test_block_on_via_env(self):
        out = json.loads(self.run_hook(self.transcript(520000), env=self.B))
        self.assertEqual(out["decision"], "block")
        self.assertIn("ctxguard.override", out["reason"])
        self.assertIn("limit 500k", out["reason"])

    def test_block_on_via_user_file(self):
        self.user_config({"block_enabled": True})
        self.assertEqual(json.loads(self.run_hook(self.transcript(520000)))["decision"], "block")

    def test_env_must_be_exactly_1(self):
        for v in ("0", "true", "", "yes"):
            self.assertNotIn('"block"', self.run_hook(self.transcript(520000), env={"CTXGUARD_BLOCK": v}))

    def test_project_file_cannot_enable_block(self):
        self.config({"block_enabled": True})
        out = self.run_hook(self.transcript(520000))
        self.assertNotIn('"block"', out)
        self.assertIn("STRONG RULE", self.ctx(out))

    def test_redteam_repro_tiny_project_config(self):
        self.config({"soft": 1, "strong": 2, "block": 3, "block_enabled": True})
        self.assertEqual(self.run_hook(self.transcript(54000)), "")
        self.assertNotIn('"block"', self.run_hook(self.transcript(54000), env=self.B))

    def test_floors_rejected_in_project_and_env(self):
        for obj in ({"soft": 19999}, {"block": 99999}, {"age_soft_h": 0.4}, {"soft": 60000, "strong": 60000}):
            with self.subTest(obj=obj):
                self.config(obj)
                self.assertEqual(self.run_hook(self.transcript(100000)), "")
        self.config({})
        self.assertEqual(self.run_hook(self.transcript(30000), env={"CONTEXT_GUARD_SOFT": "10"}), "")

    def test_block_on_below_limit_not_blocked(self):
        self.assertNotIn('"block"', self.run_hook(self.transcript(400000), env=self.B))

    def test_block_off_explicit(self):
        self.user_config({"block_enabled": False})
        self.assertNotIn('"block"', self.run_hook(self.transcript(520000)))

    def test_user_file_wrong_type_ignored(self):
        self.user_config({"block_enabled": 1})
        self.assertNotIn('"block"', self.run_hook(self.transcript(520000)))

    def test_block_custom_limit_from_project(self):
        self.config({"block": 300000})
        out = json.loads(self.run_hook(self.transcript(320000), env=self.B))
        self.assertEqual(out["decision"], "block")
        self.assertIn("limit 300k", out["reason"])

    def override(self, age_s):
        p = os.path.join(self.proj, ".claude", "ctxguard.override")
        open(p, "w").close()
        t = time.time() - age_s
        os.utime(p, (t, t))

    def test_override_file_fresh(self):
        self.override(60)
        out = self.run_hook(self.transcript(520000), env=self.B)
        self.assertNotIn('"block"', out)
        self.assertIn("STRONG RULE", self.ctx(out))

    def test_override_expires_after_12h(self):
        self.override(13 * 3600)
        self.assertEqual(json.loads(self.run_hook(self.transcript(520000), env=self.B))["decision"], "block")

    def test_override_future_mtime_expired(self):
        self.override(-3600)
        self.assertEqual(json.loads(self.run_hook(self.transcript(520000), env=self.B))["decision"], "block")

    def test_post_tool_never_blocks(self):
        out = self.run_hook(self.transcript(520000), "PostToolUse", env=self.B)
        self.assertNotIn('"block"', out)
        self.assertIn("STRONG RULE", self.ctx(out))

    def test_post_tool_throttled_at_block_level(self):
        t = self.transcript(520000)
        self.assertNotEqual(self.run_hook(t, "PostToolUse", sid="blk", env=self.B), "")
        self.assertEqual(self.run_hook(t, "PostToolUse", sid="blk", env=self.B), "")

    def test_old_override_path_not_honoured(self):
        os.makedirs(os.path.join(self.proj, "Jobs"))
        open(os.path.join(self.proj, "Jobs", ".context_guard_override"), "w").close()
        self.assertEqual(json.loads(self.run_hook(self.transcript(520000), env=self.B))["decision"], "block")

    def test_user_file_thresholds_and_project_beats_user(self):
        self.user_config({"soft": 40000, "strong": 80000})
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(self.transcript(50000))))
        self.config({"soft": 60000, "strong": 90000})
        self.assertEqual(self.run_hook(self.transcript(50000)), "")


class TestFixes(Base):
    def test_utf8_stdin_nonascii_paths_under_cp1252(self):
        proj = os.path.join(self.tmp.name, "مشروع")
        os.makedirs(os.path.join(proj, ".claude"))
        with open(os.path.join(proj, ".claude", "ctxguard.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"soft": 50000, "strong": 90000}))
        tdir = os.path.join(self.tmp.name, "مسجل")
        os.makedirs(tdir)
        t = os.path.join(tdir, "t.jsonl")
        with open(t, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": iso(0.1)}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 60000}}}) + "\n")
        # 60k is below the 150k default soft, so a reminder proves the project config was found
        out = self.run_hook(None, stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "transcript_path": t,
                                                    "session_id": "u", "cwd": proj}, ensure_ascii=False),
                            env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertIn("SOFT RULE", self.ctx(out))

    def test_usage_behind_huge_line_found(self):
        p = os.path.join(self.tmp.name, "h.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": iso(0.1)}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 170000}}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"content": "x" * 900000}}) + "\n")
        self.assertIn("170k", self.ctx(self.run_hook(p)))

    def test_usage_behind_more_than_cap_gives_up_fast(self):
        p = os.path.join(self.tmp.name, "g.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": iso(0.1)}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 170000}}}) + "\n")
            for _ in range(9):
                fh.write(json.dumps({"type": "user", "message": {"content": "x" * 1000000}}) + "\n")
        t0 = time.time()
        self.assertEqual(self.run_hook(p), "")
        self.assertLess(time.time() - t0, 10)

    def test_age_alone_never_strong_when_context_small(self):
        out = self.run_hook(self.transcript(5000, age_h=48))
        self.assertIn("SOFT RULE", self.ctx(out))
        self.assertNotIn("STRONG", out)

    def test_claude_project_dir_beats_cwd(self):
        self.config({"soft": 50000, "strong": 90000})
        sub = os.path.join(self.proj, "sub")
        os.makedirs(sub)
        out = self.run_hook(self.transcript(60000), cwd=sub, env={"CLAUDE_PROJECT_DIR": self.proj})
        self.assertIn("SOFT RULE", self.ctx(out))
        self.assertEqual(self.run_hook(self.transcript(60000), cwd=sub), "")

    def test_old_python_guard(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("sys.version_info < (3, 6)")
        self.assertLess(i, src.index("import datetime"))
        self.assertIn("sys.exit(0)", src[i:i + 80])

    def test_hooks_try_python3_first_with_utf8(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as fh:
            h = json.load(fh)["hooks"]
        for ev in h.values():
            cmd = ev[0]["hooks"][0]["command"]
            self.assertLess(cmd.index("python3 -X utf8"), cmd.index("python -X utf8"))


class TestNeverFails(Base):
    def test_unwritable_state_dir(self):
        e = {"TMPDIR": os.path.join(self.tmp.name, "does", "not", "exist"),
             "TEMP": os.path.join(self.tmp.name, "does", "not", "exist"),
             "TMP": os.path.join(self.tmp.name, "does", "not", "exist")}
        env = dict(os.environ)
        env.update(e)
        t = self.transcript(160000)
        payload = json.dumps({"hook_event_name": "PostToolUse", "transcript_path": t, "session_id": "q", "cwd": self.proj})
        r = subprocess.run([sys.executable, SCRIPT], input=payload.encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_transcript_is_directory(self):
        self.assertEqual(self.run_hook(self.tmp.name), "")

    def test_large_transcript_tail_only(self):
        p = os.path.join(self.tmp.name, "big.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": iso(0.1)}) + "\n")
            for _ in range(3000):
                fh.write(json.dumps({"type": "user", "message": {"content": "x" * 400}}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 170000}}}) + "\n")
        self.assertIn("SOFT RULE", self.ctx(self.run_hook(p)))

    def test_parse_ts_variants(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import ctxguard
        finally:
            sys.path.pop(0)
        base = ctxguard.parse_ts("2026-01-01T00:00:00Z")
        self.assertEqual(ctxguard.parse_ts("2026-01-01T00:00:00.5Z"), base)
        self.assertEqual(ctxguard.parse_ts("2026-01-01T02:00:00+02:00"), base)
        self.assertEqual(ctxguard.parse_ts("2025-12-31T22:00:00-0200"), base)
        self.assertIsNone(ctxguard.parse_ts("nope"))
        self.assertIsNone(ctxguard.parse_ts(None))


class TestPackage(unittest.TestCase):
    def test_manifest(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertEqual((m["name"], m["version"], m["license"]), ("ctxguard", "0.1.0", "MIT"))
        self.assertEqual(m["author"]["name"], "msamkari")
        self.assertEqual(m["homepage"], "https://github.com/msamkari/ctxguard")

    def test_hooks_layout(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as fh:
            h = json.load(fh)["hooks"]
        self.assertEqual(sorted(h), ["PostToolUse", "UserPromptSubmit"])
        for ev in h.values():
            cmd = ev[0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/ctxguard.py", cmd)
            self.assertNotIn("\\", cmd)
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".claude-plugin", "hooks")))

    def test_no_preapproved_tools(self):
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                if f.endswith((".json", ".md", ".py")) and f != "test_ctxguard.py":
                    with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                        self.assertNotIn("allowed-tools", fh.read(), f)

    def test_no_personal_strings(self):
        pats = [r"C:[\\/]+Users", r"/home/[a-z]", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}",
                r"(sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{16,}",
                "Agent" + "_Creator", "HAND" + "OVER", "Dropb" + "ox", "series" + "_log", "state" + "_manifest"]
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".github")]
            for f in files:
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    text = fh.read()
                for p in pats:
                    self.assertIsNone(re.search(p, text), "%s matches %s" % (f, p))

    def test_script_py38_syntax(self):
        import ast
        with open(SCRIPT, encoding="utf-8") as fh:
            ast.parse(fh.read(), feature_version=(3, 8))
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("match ", ":=", "removeprefix", "|= "):
            self.assertNotIn(banned, src.replace("match = ", ""))

    def test_script_has_no_network_imports(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        for mod in ("socket", "urllib", "http", "requests", "subprocess"):
            self.assertIsNone(re.search(r"^\s*(import|from)\s+%s\b" % mod, src, re.M), mod)


if __name__ == "__main__":
    unittest.main()
