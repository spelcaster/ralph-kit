#!/usr/bin/env python3
"""Deterministic stand-in for an agent CLI. Reads the prompt on stdin, acts by phase."""
import json
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
run = Path(re.search(r"write `([^`]+)/status\.md`", prompt).group(1))
run.mkdir(parents=True, exist_ok=True)
calls = Path(sys.argv[2])
n = json.loads(calls.read_text()) if calls.exists() else {}


def bump(k):
    n[k] = n.get(k, 0) + 1
    calls.write_text(json.dumps(n))
    return n[k]


def status(s, msg, body=""):
    (run / "status.md").write_text(f"{s}\n{msg}\n{body}\n")


print("COST=0.25")
slug = (re.search(r"task `([a-z0-9-]+)`", prompt) or [None, "plan"])[1]
if "# Role: planner" in prompt:
    (run / "plan.md").write_text("# Plan\n- [ ] add-greeting: Add greeting module\n  Acceptance:\n  - greet() works\n"
                                 "- [ ] add-color: Add color setting\n  Acceptance:\n  - color is configurable\n")
    status("DONE", "plan")
elif "# Role: product interviewer" in prompt:
    mode = re.search(r"## Mode: (\w+)", prompt).group(1)
    answers = re.findall(r"^Human: (.*)$", prompt, re.M)
    if mode in ("finalize", "revise"):
        reqs = "\n".join(f"{i}. {a}" for i, a in enumerate(answers, 1))
        (run / "prd.md").write_text(f"# PRD\n\n## Requirements\n{reqs}\n\n## Open questions\n- none\n")
        status("DONE", "-", f"I drafted {len(answers)} requirements. Save it, or change something?")
    else:
        status("ASK", "-", f"Question {len(answers) + 1}: what else should it do?")
elif "# Role: developer — iteration" in prompt:
    k = bump(f"dev:{slug}")
    if slug == "add-color" and k == 1:
        status("ASK", "", "Which default color do you want, blue or green?")
        sys.exit(0)
    if slug == "add-color":
        assert "green" in prompt, "human answer must reach the next prompt"
    Path(f"{slug}.txt").write_text(f"{slug} v{k}\n")
    status("DONE" if k >= 2 else "CONTINUE", f"feat({slug}): step {k}", "did a step")
elif "# Role: test engineer" in prompt:
    Path(f"test_{slug}.txt").write_text("ok\n")
    status("DONE", f"test({slug}): harden tests")
elif "# Role: senior reviewer" in prompt:
    k = bump(f"review:{slug}")
    if slug == "add-greeting" and k == 1:
        (run / "review.md").write_text("- [ ] rename variable x\n")
        status("CHANGES", "", "needs a rename")
    else:
        status("APPROVE", "", "LGTM: small, tested, in scope")
elif "fixing PR" in prompt:
    assert "Failing CI checks" in prompt and "please rename" in prompt, prompt[:600]
    Path(f"{slug}.txt").write_text("fixed after CI + review\n")
    status("DONE", f"fix({slug}): CI and review", "fixed both")
else:
    status("ERROR?", "", "unknown phase")
