"""Unit + scenario tests. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from ralph import git as G  # noqa: E402
from ralph.agents import parse_cost, read_status, render  # noqa: E402
from ralph.config import Config, deep_merge  # noqa: E402
from ralph.state import Ledger, parse_plan, render_marks  # noqa: E402

FIX = KIT / "tests" / "fixtures"
ENV = {**os.environ, "PATH": f"{FIX}:{KIT / 'bin'}:{os.environ['PATH']}", "RALPH_GLOBAL_CONFIG": "/nonexistent",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cmd, cwd, env=ENV, check=True):
    return subprocess.run(cmd, cwd=cwd, shell=True, env=env, capture_output=True, text=True, check=check)


class Pure(unittest.TestCase):
    def test_parse_plan(self):
        tasks = parse_plan(textwrap.dedent("""\
            # Plan
            - [ ] a-task: First
              Acceptance:
              - one
            - [x] b-task: Second
            - [~] c-task: Third
            """))
        self.assertEqual([t.slug for t in tasks], ["a-task", "b-task", "c-task"])
        self.assertIn("- one", tasks[0].body)
        self.assertEqual(tasks[1].mark, "x")

    def test_duplicate_slug_rejected(self):
        with self.assertRaises(ValueError):
            parse_plan("- [ ] a: x\n- [ ] a: y\n")

    def test_render_marks(self):
        out = render_marks("- [ ] a: x\n  body\n- [ ] b: y\n", {"a": "merged", "b": "pr_open"})
        self.assertIn("- [x] a: x", out)
        self.assertIn("- [~] b: y", out)
        self.assertIn("  body", out)

    def test_config_layers_and_agent_inheritance(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".ralph").mkdir()
            (repo / ".ralph/config.toml").write_text('[agents.development]\nmodel = "haiku"\n[run]\nmax_iterations = 7\n')
            cfg = Config.load(repo, ["budget.ceiling_pct=50", 'run.language="pt-BR"'])
            self.assertEqual(cfg["run"]["max_iterations"], 7)
            self.assertEqual(cfg["budget"]["ceiling_pct"], 50)
            self.assertEqual(cfg["run"]["language"], "pt-BR")
            self.assertEqual(cfg.agent("planning")["model"], "opus")
            self.assertEqual(cfg.agent("fix")["model"], "haiku")      # fix inherits development
            self.assertEqual(cfg.agent("test")["model"], "sonnet")    # default
            self.assertEqual(cfg.agent("test")["cli"], "claude")

    def test_base_branch_ignores_stale_cached_remote_head(self):
        # refs/remotes/origin/HEAD is a local cache set at clone time; git never refreshes it on
        # fetch. Simulate a repo cloned when the remote's default branch was "master", after the
        # remote's default moved to "main" — base_branch must report the live default, not the cache.
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            sh(f"git init -q --bare -b master {w}/remote.git", w)
            sh(f"git clone -q {w}/remote.git proj", w)
            sh("echo hi > f && git add . && git commit -qm init && git push -q origin master", w / "proj", ENV)
            sh("git -C remote.git symbolic-ref HEAD refs/heads/main", w, ENV)  # remote's default moves on
            self.assertEqual(G.base_branch(w / "proj", {}), "main")

    def test_deep_merge_does_not_mutate(self):
        a = {"x": {"y": 1}}
        deep_merge(a, {"x": {"z": 2}})
        self.assertEqual(a, {"x": {"y": 1}})

    def test_status_contract(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.assertEqual(read_status(p)[0], "ERROR")
            (p / "status.md").write_text("**ASK**\n\nBlue or green?\n")
            self.assertEqual(read_status(p), ("ASK", "", "Blue or green?"))
            (p / "status.md").write_text("MAYBE\n")
            self.assertEqual(read_status(p)[0], "ERROR")

    def test_cost_parsing(self):
        out = json.dumps({"type": "result", "total_cost_usd": 1.25, "result": "ok"})
        self.assertEqual(parse_cost("noise\n" + out, {"cli": "claude"}), 1.25)
        self.assertEqual(parse_cost("COST=0.5", {"cli": "command", "cost_regex": r"COST=([0-9.]+)"}), 0.5)
        self.assertEqual(parse_cost("garbage", {"cli": "claude"}), 0.0)

    def test_render_unknown_placeholders_blank(self):
        self.assertEqual(render("a {{x}} b {{ y }}", {"x": 1}), "a 1 b ")

    def test_ledger_window(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger({"ledger": f"{d}/l.jsonl", "window": "month", "plan_usd": 10, "ceiling_pct": 90})
            led.add(5.0)
            self.assertFalse(led.exhausted())
            led.add(4.0)
            self.assertTrue(led.exhausted())  # 9.0 >= 90% of 10
            with open(f"{d}/l.jsonl", "a") as f:
                f.write(json.dumps({"ts": 0, "cost": 100}) + "\n")  # 1970 is outside this month
            self.assertAlmostEqual(led.spent(), 9.0)


class VoiceWiring(unittest.TestCase):
    """The audio itself needs a mic and speakers; this checks the Linux command wiring with stand-ins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.log = self.bin / "log"
        for name, body in {
            "piper": 'cat > "$TMP_LOG.text"; while [ $# -gt 0 ]; do [ "$1" = --output_file ] && echo RIFF > "$2"; shift; done',
            "paplay": 'echo "paplay $1 $(cat "$1")" >> "$TMP_LOG"',
            "rec": 'echo "rec $*" >> "$TMP_LOG"; for a in "$@"; do case "$a" in *.wav) head -c 8000 /dev/zero > "$a";; esac; done',
        }.items():
            f = self.bin / name
            f.write_text(f"#!/bin/bash\n{body}\n")
            f.chmod(0o755)
        self.old = os.environ.copy()
        os.environ["PATH"] = f"{self.bin}:/usr/bin:/bin"
        os.environ["TMP_LOG"] = str(self.log)
        (self.bin / "voice.onnx").write_text("x")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def test_piper_then_player(self):
        from ralph.voice import Voice
        v = Voice({"tts": "auto", "piper_model": str(self.bin / "voice.onnx"), "player": "auto"})
        self.assertEqual(v.tts_backend(), "piper")
        self.assertTrue(v.speak("**Hello** `world`, see https://x.y"))
        self.assertIn("paplay", self.log.read_text())
        self.assertEqual(Path(f"{self.log}.text").read_text(), "Hello world, see a link")

    def test_sox_records_with_silence_detection(self):
        from ralph.voice import Voice
        wav = Voice({"record": "auto", "silence_seconds": 1.6}).record()
        self.assertTrue(wav.is_file())
        self.assertIn("-r 16000", self.log.read_text())
        self.assertIn("silence 1 0.1 2% 1 1.6 2%", self.log.read_text())


class Scenarios(unittest.TestCase):
    """Real git + worktrees + orchestrator, fake agent and fake gh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = Path(self.tmp.name)
        self.env = {**ENV, "FAKE_GH_DB": str(self.w / "gh.json"), "FAKE_GH_REMOTE": str(self.w / "remote.git"),
                    "FAKE_SBX_DB": str(self.w / "sbx.json")}
        sh(f"git init -q --bare -b main {self.w}/remote.git", self.w)
        sh(f"git clone -q {self.w}/remote.git proj", self.w)
        self.repo = self.w / "proj"
        sh("echo hi > README.md && git add . && git commit -qm init && git push -q origin main "
           "&& git remote set-head origin main", self.repo, self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, extra="", prd=True, test_gate="ls *.txt >/dev/null", run_extra="", sandbox_extra=""):
        sh("ralph init", self.repo, self.env)
        if prd:
            (self.repo / ".ralph/prd.md").write_text("# PRD\nGreet people.\n")
        else:
            (self.repo / ".ralph/prd.md").unlink()
        (self.repo / ".ralph/config.toml").write_text(textwrap.dedent(f"""\
            [run]
            merge_poll_seconds = 0
            {run_extra}
            [budget]
            plan_usd = 100
            ledger = "{self.w}/ledger.jsonl"
            [sandbox]
            engine = "sbx"
            {sandbox_extra}
            [agents.default]
            cli = "command"
            command = ["python3", "{FIX}/fake_agent.py", "{{model}}", "{self.w}/calls.json"]
            cost_regex = "COST=([0-9.]+)"
            [gates]
            lint = "true"
            typecheck = "true"
            test = "{test_gate}"
            [hitl]
            notify = []
            speak = false
            timeout_seconds = 3
            """) + extra)

    def run_ralph(self, *args):
        return sh("ralph run " + " ".join(args), self.repo, self.env, check=False)

    def test_no_prd_exits_3(self):
        self.configure(prd=False)
        r = self.run_ralph()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("no PRD found", r.stdout)

    def test_max_iterations_exits_2(self):
        self.configure()
        r = self.run_ralph("--max-iterations", "3")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("max iterations reached (3)", r.stdout)

    def test_budget_ceiling_exits_4(self):
        self.configure()
        r = self.run_ralph("--budget", "1", "--ceiling", "90")  # 0.25/iteration -> stops after 4
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("budget ceiling reached: $1.00 of $0.90", r.stdout)

    def test_unmerged_pr_blocks_next_task(self):
        self.configure()
        db = self.w / "gh.json"
        r = self.run_ralph("--no-wait")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("waiting", r.stdout)  # CI failure fixed, now checks running -> waiting
        state = json.loads((self.repo / ".ralph/state/state.json").read_text())
        self.assertEqual(state["tasks"]["add-greeting"]["status"], "pr_open")
        self.assertEqual(state["tasks"]["add-color"]["status"], "todo")
        self.assertTrue(db.exists())
        # resuming continues from the open PR, merges it, and only then starts the next task
        r2 = self.run_ralph("--once")
        self.assertIn("add-greeting merged", r2.stdout)
        self.assertNotIn("task add-color", r2.stdout)

    def test_failing_gates_feed_back_then_ask_human(self):
        self.configure(test_gate="exit 1", run_extra="max_consecutive_gate_failures = 2")
        r = self.run_ralph()
        self.assertEqual(r.returncode, 5, r.stdout)  # human never answered within hitl.timeout_seconds
        self.assertIn("Quality gates failed 2 times", r.stdout)
        logs = sorted((self.repo / ".ralph/state/logs").glob("*development-2.log"))
        self.assertTrue(logs)
        # every iteration is committed even when red, with the gate result in the trailers
        out = sh("git log ralph/add-greeting '--format=%(trailers:key=Ralph-Gates,valueonly,separator=)'", self.repo).stdout
        self.assertIn("fail", out)

    def sbx_db(self):
        return json.loads((self.w / "sbx.json").read_text())

    def test_prd_conversation_interview_draft_revise_approve(self):
        self.configure(prd=False)
        script = "\n".join([
            "a shared shopping list for families",   # answer 1
            "scratch that",                           # undo answer 1
            "a shared grocery list for families",     # answer 1 again
            "everyone can add items offline",         # answer 2
            "repeat",                                 # Ralph repeats the question, nothing recorded
            "done",                                   # -> draft
            "add a dark mode",                        # review: change request -> revised draft
            "approve",                                # save
        ]) + "\n"
        r = subprocess.run("ralph prd --type --mute", cwd=self.repo, shell=True, env=self.env, input=script,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        prd = (self.repo / ".ralph/prd.md").read_text()
        self.assertIn("grocery list", prd)
        self.assertNotIn("shopping list", prd)            # undone
        self.assertIn("add a dark mode", prd)             # revision applied
        self.assertNotIn("repeat", prd)
        self.assertIn("Question 3", r.stdout)             # third question after two kept answers
        self.assertFalse((self.repo / ".ralph/state/prd-session.json").exists())
        self.assertTrue(list((self.repo / ".ralph/state").glob("prd-conversation-*.md")))
        db = self.sbx_db()
        self.assertEqual(db["boxes"], {})                 # interviewer sandbox removed
        mounts = {m["path"]: m["ro"] for m in db["history"]["ralph-proj-prd"]}
        self.assertTrue(mounts[str(self.repo)])           # interviewer sees the code read-only
        self.assertFalse(mounts[str(self.repo / ".ralph/state/runs/prd")])  # and writes only its scratch dir

    def test_prd_conversation_pause_and_resume(self):
        self.configure(prd=False)
        r = subprocess.run("ralph prd --type --mute", cwd=self.repo, shell=True, env=self.env,
                           input="an invoicing tool\npause\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        session = json.loads((self.repo / ".ralph/state/prd-session.json").read_text())
        self.assertEqual([t["role"] for t in session["turns"]], ["ralph", "human", "ralph"])
        r = subprocess.run("ralph prd --type --mute --resume", cwd=self.repo, shell=True, env=self.env,
                           input="for freelancers\nwrite it\nlooks good\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Welcome back", r.stdout)
        prd = (self.repo / ".ralph/prd.md").read_text()
        self.assertIn("an invoicing tool", prd)
        self.assertIn("for freelancers", prd)

    def test_sandbox_keep_reuses_then_prune(self):
        self.configure(prd=False, sandbox_extra="keep = true")
        r = subprocess.run("ralph prd --type --mute", cwd=self.repo, shell=True, env=self.env,
                           input="an invoicing tool\npause\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        box = self.sbx_db()["boxes"]
        self.assertIn("ralph-proj-prd", box)              # kept running instead of torn down
        r = subprocess.run("ralph prd --type --mute --resume", cwd=self.repo, shell=True, env=self.env,
                           input="for freelancers\nwrite it\nlooks good\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = sh("ralph sandboxes", self.repo, self.env).stdout
        self.assertIn("ralph-proj-prd", out)
        self.assertIn("orphan", out)                       # not tied to any active/pr_open task
        sh("ralph sandboxes --prune", self.repo, self.env)
        self.assertEqual(self.sbx_db()["boxes"], {})      # --prune removes it even though keep = true

    def test_long_answers_are_never_commands(self):
        from ralph.prd_chat import PrdChat
        self.configure()
        from ralph.state import Project
        p = Project(self.repo, Config.load(self.repo))
        chat = PrdChat.__new__(PrdChat)
        chat.words = p.cfg["prd_chat"]
        self.assertEqual(chat.intent("done"), "done")
        self.assertEqual(chat.intent("Pronto!"), "done")
        self.assertEqual(chat.intent("ok, approve"), "approve")
        self.assertIsNone(chat.intent("it is done when the user can stop the timer and export"))
        self.assertIsNone(chat.intent("undocumented"))

    def test_task_sandbox_mounts(self):
        self.configure()
        self.run_ralph("--max-iterations", "2")
        box = self.sbx_db()["boxes"]["ralph-proj-add-greeting"]
        paths = {m["path"]: m["ro"] for m in box["mounts"]}
        wt = str((self.repo / "../proj.worktrees/add-greeting").resolve())
        self.assertEqual(paths[wt], False)                          # worktree writable
        self.assertEqual(paths[str(self.repo / ".git")], True)      # git metadata read-only
        self.assertEqual(paths[str(KIT / "sandbox")], True)         # gates read-only
        self.assertEqual(len(paths), 3)                             # nothing else from the host

    def test_merge_conflict_markers_left_for_agent(self):
        sh("git worktree add -q -b feat ../wt origin/main", self.repo)
        wt = self.w / "wt"
        sh("echo feature > README.md && git commit -qam feat", wt)
        sh("echo upstream > README.md && git commit -qam up && git push -q origin main", self.repo)
        files = G.merge_base_into(wt, {"remote": "origin", "base": "main"})
        self.assertEqual(files, ["README.md"])
        self.assertTrue(G.merge_in_progress(wt))
        self.assertEqual(G.conflict_markers(wt), ["README.md"])


if __name__ == "__main__":
    unittest.main()
