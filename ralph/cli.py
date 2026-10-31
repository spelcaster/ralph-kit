"""ralph — a hardened, voice-driven Ralph loop.  Run `ralph --help`."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import git as G, hitl, voice
from .config import PKG_DIR, Config
from .state import Ledger, Project

TEMPLATES = PKG_DIR.parent / "templates" / "project"


def _project(args) -> Project:
    repo = G.find_repo(Path.cwd())
    overrides = list(getattr(args, "set", None) or [])
    for flag, key in (("max_iterations", "run.max_iterations"), ("ceiling", "budget.ceiling_pct"),
                      ("budget", "budget.plan_usd"), ("window", "budget.window")):
        v = getattr(args, flag, None)
        if v is not None:
            overrides.append(f"{key}={json.dumps(v)}")
    return Project(repo, Config.load(repo, overrides))


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    repo = G.find_repo(Path.cwd())
    d = repo / ".ralph"
    d.mkdir(exist_ok=True)
    for name in ("config.toml", "prd.md"):
        dst = d / name
        if dst.exists():
            print(f"exists  {dst.relative_to(repo)}")
        else:
            shutil.copy(TEMPLATES / name, dst)
            print(f"created {dst.relative_to(repo)}")
    (d / ".gitignore").write_text("state/\nplan.md\n")
    G.ensure_excluded(repo)
    print("\nNext: `ralph prd` (talk it through) or edit .ralph/prd.md, commit .ralph/, then `ralph run`.")
    return 0


def cmd_run(args) -> int:
    from .loop import Runner
    p = _project(args)
    return Runner(p, once=args.once, wait=False if args.no_wait else None).run()


def cmd_plan(args) -> int:
    from .loop import Runner, StopRun
    p = _project(args)
    prd = p.find_prd()
    if not prd:
        print("ralph: no PRD found", file=sys.stderr)
        return 3
    try:
        Runner(p).plan(prd)
    except StopRun as s:
        print(s.reason)
        return s.code
    print(p.plan_path.read_text())
    return 0


def status_text(p: Project) -> tuple[str, str]:
    lines, spoken = [], []
    tasks = p.tasks()
    icons = {"todo": "·", "active": "▶", "pr_open": "⇪", "merged": "✔", "abandoned": "✗"}
    for t in tasks:
        s = p.state["tasks"].get(t.slug, {"status": "todo"})
        extra = f"  PR #{s['pr']}" if s.get("pr") else ""
        extra += f"  ({s.get('phase')}, {s.get('iterations', 0)} it)" if s["status"] == "active" else ""
        lines.append(f" {icons.get(s['status'], '?')} {t.slug:<32} {s['status']:<9}{extra}")
    done = sum(1 for t in tasks if p.state["tasks"].get(t.slug, {}).get("status") == "merged")
    led = Ledger(p.cfg["budget"])
    b = p.cfg["budget"]
    head = (f"{p.repo.name}: {done}/{len(tasks)} tasks merged · budget ${led.spent():.2f} of "
            f"${led.ceiling():.2f} ({b['ceiling_pct']}% of ${b['plan_usd']}/{b['window']})")
    spoken.append(f"{done} of {len(tasks)} tasks merged.")
    inflight = p.open_task()
    if inflight:
        t, s = inflight
        spoken.append(f"Working on {t.title}" + (f", pull request {s['pr']} is open." if s.get("pr") else "."))
    q = hitl.pending_question(p)
    if q:
        lines.append(f"\n❓ waiting for you: {q['question']}\n   → ralph answer")
        spoken.append(f"I'm waiting for your answer: {q['question']}")
    if not p.find_prd():
        lines.append("\n(no PRD yet: write .ralph/prd.md or run `ralph prd`)")
    return head + "\n" + "\n".join(lines), " ".join(spoken)


def cmd_status(args) -> int:
    p = _project(args)
    text, spoken = status_text(p)
    if args.short:
        print(text.splitlines()[0])
    else:
        print(text)
    if args.speak:
        voice.speak(spoken, p.cfg["voice"])
    return 0


def cmd_answer(args) -> int:
    p = _project(args)
    q = hitl.pending_question(p)
    if not q:
        print("ralph: no pending question")
        return 1
    print(f"❓ {q['question']}\n")
    if args.text is not None:
        ans = args.text
    else:
        v = voice.Voice(p.cfg["voice"])
        if not args.no_replay:
            v.speak(q["question"])
        ans = v.listen()
    p.answer_path.write_text(ans.strip() + "\n")
    print("✔ answer sent; the loop picks it up within a couple of seconds")
    return 0


def cmd_prd(args) -> int:
    """Talk your PRD into existence: Ralph interviews you by voice, drafts, and saves when you approve."""
    from .prd_chat import PrdChat
    p = _project(args)
    return PrdChat(p, typed=args.type, mute=args.mute, resume=args.resume).run()


def cmd_talk(args) -> int:
    """One button for everything voice: answers a pending question, otherwise opens the PRD conversation."""
    p = _project(args)
    if hitl.pending_question(p):
        args.text, args.no_replay = None, False
        return cmd_answer(args)
    args.type = args.mute = False
    args.resume = (p.state_dir / "prd-session.json").is_file()
    return cmd_prd(args)


def cmd_say(args) -> int:
    cfg = (_project(args).cfg if _in_git() else Config.load(None))["voice"]
    return 0 if voice.Voice(cfg).speak(" ".join(args.text)) else 1


def cmd_listen(args) -> int:
    cfg = (_project(args).cfg if _in_git() else Config.load(None))["voice"]
    v = voice.Voice(cfg)
    print(f"(stt: {v.stt_backend()} · tts: {v.tts_backend()})")
    print(v.listen(confirm=False))
    return 0


def cmd_log(args) -> int:
    repo = G.find_repo(Path.cwd())
    fmt = "%C(yellow)%h%Creset %ad %C(cyan)%(trailers:key=Ralph-Task,valueonly,separator=)%Creset " \
          "[%(trailers:key=Ralph-Phase,valueonly,separator=) gates:%(trailers:key=Ralph-Gates,valueonly,separator=) " \
          "$%(trailers:key=Ralph-Cost,valueonly,separator=)] %s"
    cmd = ["git", "log", "--all", "--date=format:%m-%d %H:%M", f"--format={fmt}", "--grep=^Ralph-Task:",
           f"-n{args.n}"]
    if args.task:
        cmd[cmd.index("--grep=^Ralph-Task:")] = f"--grep=^Ralph-Task: {args.task}$"
    return subprocess.call(cmd, cwd=repo)


def cmd_gates(args) -> int:
    from .sandbox import Sandbox, sandbox_name
    p = _project(args)
    wt = Path(G.git(Path.cwd(), "rev-parse", "--show-toplevel"))
    sb = Sandbox(p.cfg["sandbox"], G.common_git_dir(p.repo))
    task = next((s for s in p.state["tasks"].values() if s.get("worktree") == str(wt) and s.get("sandbox")), None)
    box = task["sandbox"] if task else sb.ensure(sandbox_name(p.repo.name, "gates"), wt)
    try:
        res = sb.gates(box, wt, p.cfg["gates"])
    finally:
        if not task:
            sb.remove(box)
    print(res.stdout, res.stderr, sep="\n")
    return res.code


def cmd_sandboxes(args) -> int:
    from .sandbox import Sandbox
    p = _project(args)
    sb = Sandbox(p.cfg["sandbox"], G.common_git_dir(p.repo))
    names = sb.list_ralph()
    live = {s.get("sandbox") for s in p.state["tasks"].values() if s.get("status") in ("active", "pr_open")}
    for n in names:
        tag = "in use" if n in live else "orphan"
        print(f"{n:<50} {tag}")
        if args.prune and n not in live and n.startswith(f"ralph-{p.repo.name.lower()}"):
            sb.remove(n, force=True)
            print("   removed")
    if not names:
        print("(no ralph sandboxes)")
    return 0


def cmd_config(args) -> int:
    p = _project(args)
    print("# sources (later wins):\n" + "\n".join(f"#   {s}" for s in p.cfg.sources))
    print(json.dumps(p.cfg.data, indent=2))
    return 0


def cmd_attention(args) -> int:
    print(hitl.attention_line())
    return 0


def cmd_doctor(args) -> int:
    import platform
    ok = True

    def check(name: str, good, hint: str = "") -> None:
        nonlocal ok
        print(f"{'✔' if good else '✗'} {name}" + ("" if good else f"  — {hint}"))
        ok &= bool(good)

    check("Linux", platform.system() == "Linux", "this version targets Linux only")
    check("KVM (/dev/kvm)", os.path.exists("/dev/kvm"), "enable virtualization; local sandboxes need KVM")
    check("user in kvm group", os.access("/dev/kvm", os.R_OK | os.W_OK) if os.path.exists("/dev/kvm") else False,
          "sudo usermod -aG kvm $USER, then log in again")
    for b, hint in (("git", "apt install git"), ("gh", "https://cli.github.com"), ("sbx", "./install.sh"),
                    ("tmux", "apt install tmux")):
        check(b, shutil.which(b), hint)
    if shutil.which("sbx"):
        r = subprocess.run(["sbx", "ls", "--json"], capture_output=True, text=True)
        check("sbx signed in and daemon up", r.returncode == 0, (r.stderr.strip() or "run: sbx login")[:120])
        r = subprocess.run(["sbx", "secret", "ls"], capture_output=True, text=True)
        check("model credential stored (sbx secret ls shows anthropic)", "anthropic" in r.stdout,
              "sbx secret set -g anthropic   (API key or Claude subscription)")
    if shutil.which("gh"):
        check("gh auth", subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0, "gh auth login")
    cfg = (_project(args).cfg if _in_git() else Config.load(None))["voice"]
    v = voice.Voice(cfg)
    check("text to speech", v.tts_backend(), "./install.sh --voice (piper) or apt install espeak-ng")
    try:
        stt = v.stt_backend()
    except SystemExit:
        stt = None
    check("speech to text", stt, "./install.sh --voice (faster-whisper)")
    rec_ok = any(shutil.which(b) for b in ("rec", "pw-record", "arecord"))
    check("microphone recorder (sox / pw-record / arecord)", rec_ok, "sudo apt install sox libsox-fmt-pulse")
    if args.smoke and shutil.which("sbx") and _in_git():
        p = _project(args)
        from .agents import build_command
        from .sandbox import Sandbox, sandbox_name
        sb = Sandbox(p.cfg["sandbox"], G.common_git_dir(p.repo))
        scratch = p.state_dir / "runs" / "doctor"
        box = sb.ensure(sandbox_name(p.repo.name, "doctor"), scratch)
        default_agent = p.cfg.agent("development")
        try:
            if default_agent.get("cli", "claude") == "claude":
                res = sb.run(box, ["claude", "-p", "--output-format", "json", "--max-turns", "1", "Reply with OK"],
                             scratch, timeout=300)
                check("agent answers inside a sandbox", '"is_error":false' in res.stdout.replace(" ", ""),
                      (res.stdout + res.stderr)[-200:])
            else:  # cli = "command": no shared JSON contract, so just check the CLI runs inside the sandbox
                cmd = build_command(default_agent)
                res = sb.run(box, cmd, scratch, stdin="Reply with OK", timeout=300)
                check(f"agent command answers inside a sandbox ({cmd[0]})", res.code == 0,
                      (res.stdout + res.stderr)[-200:])
        finally:
            sb.remove(box)
    return 0 if ok else 1


def _in_git() -> bool:
    return subprocess.run(["git", "rev-parse"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ralph", description="Hardened, voice-driven Ralph loop.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--set", action="append", metavar="KEY=VALUE", help="override config, e.g. run.language='pt-BR'")
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "create .ralph/ with config and PRD templates")
    sp = add("run", cmd_run, "run the loop until a stop condition")
    sp.add_argument("--max-iterations", type=int)
    sp.add_argument("--ceiling", type=float, help="stop at this %% of the plan budget")
    sp.add_argument("--budget", type=float, help="plan budget in USD for the window")
    sp.add_argument("--window", choices=["month", "week", "day", "5h", "run"])
    sp.add_argument("--once", action="store_true", help="stop after one task is merged")
    sp.add_argument("--no-wait", action="store_true", help="exit instead of waiting for a PR to be merged")
    add("plan", cmd_plan, "(re)build .ralph/plan.md from the PRD")
    sp = add("status", cmd_status, "show tasks, PRs, budget, pending question")
    sp.add_argument("--speak", action="store_true")
    sp.add_argument("--short", action="store_true")
    sp = add("answer", cmd_answer, "answer the pending question (voice by default)")
    sp.add_argument("--text")
    sp.add_argument("--no-replay", action="store_true", help="don't read the question aloud first")
    sp = add("prd", cmd_prd, "write the PRD in a spoken conversation with Ralph")
    sp.add_argument("--resume", action="store_true", help="continue the last conversation")
    sp.add_argument("--type", action="store_true", help="type your answers instead of speaking")
    sp.add_argument("--mute", action="store_true", help="don't speak Ralph's lines aloud")
    add("talk", cmd_talk, "push-to-talk: answers a pending question, otherwise opens the PRD conversation")
    sp = add("say", cmd_say, "speak text with the configured voice")
    sp.add_argument("text", nargs="+")
    add("listen", cmd_listen, "record and print a transcript (test your mic/STT)")
    sp = add("log", cmd_log, "iteration history from git commit trailers")
    sp.add_argument("-n", type=int, default=40)
    sp.add_argument("--task")
    add("gates", cmd_gates, "run lint/typecheck/tests for the current worktree in the sandbox")
    add("config", cmd_config, "print the effective config and where it came from")
    add("attention", cmd_attention, "one line for the tmux status bar")
    sp = add("sandboxes", cmd_sandboxes, "list ralph's Docker Sandboxes")
    sp.add_argument("--prune", action="store_true", help="remove sandboxes no task is using")
    sp = add("doctor", cmd_doctor, "check dependencies")
    sp.add_argument("--smoke", action="store_true", help="also start a sandbox and ask the agent to reply")

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except BrokenPipeError:  # e.g. `ralph status | head -1`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
