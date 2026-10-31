"""Agent adapters (one per task type) and the agent <-> orchestrator status contract."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import PKG_DIR
from .sandbox import Result, Sandbox

PROMPTS = PKG_DIR / "prompts"
STATUSES = {"DONE", "CONTINUE", "ASK", "BLOCKED", "APPROVE", "CHANGES"}


@dataclass
class AgentOutcome:
    status: str          # DONE | CONTINUE | ASK | BLOCKED | APPROVE | CHANGES | ERROR
    commit_msg: str
    body: str
    cost: float
    result: Result


NO_CONTRACT = {"interview"}  # conversational phases don't get the coding-iteration contract


def load_prompt(project_dir: Path, phase: str) -> str:
    """Project override in .ralph/prompts/<phase>.md wins over the shipped template."""
    for base in (project_dir / "prompts", PROMPTS):
        p = base / f"{phase}.md"
        if p.is_file():
            body = p.read_text()
            break
    else:
        raise FileNotFoundError(f"no prompt template for phase {phase}")
    contract = ""
    for base in (() if phase in NO_CONTRACT else (project_dir / "prompts", PROMPTS)):
        c = base / "_contract.md"
        if c.is_file():
            contract = c.read_text()
            break
    return body + "\n\n" + contract


def render(template: str, ctx: dict) -> str:
    # {{name}} placeholders; unknown names render empty so templates can be customised freely
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(ctx.get(m.group(1), "")), template)


def build_command(agent: dict) -> list[str]:
    cli = agent.get("cli", "claude")
    model = str(agent.get("model", ""))
    if cli == "claude":
        cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if agent.get("max_turns"):
            cmd += ["--max-turns", str(agent["max_turns"])]
        if agent.get("max_budget_usd"):  # hard cap per invocation, so one iteration can't blow the ceiling
            cmd += ["--max-budget-usd", f"{float(agent['max_budget_usd']):.2f}"]
        return cmd + [str(a) for a in agent.get("extra_args", [])]
    if cli == "command":
        cmd = [str(a).replace("{model}", model) for a in agent.get("command", [])]
        if not cmd:
            raise SystemExit("agents.*.cli = 'command' needs a `command = [...]` list")
        return cmd + [str(a) for a in agent.get("extra_args", [])]
    raise SystemExit(f"unknown agent cli: {cli}")


def parse_cost(stdout: str, agent: dict) -> float:
    if agent.get("cli", "claude") == "claude":
        try:
            data = json.loads(stdout.strip().splitlines()[-1] if stdout.strip() else "{}")
            return float(data.get("total_cost_usd") or data.get("cost_usd") or 0.0)
        except (json.JSONDecodeError, ValueError, IndexError):
            return 0.0
    rx = agent.get("cost_regex")
    if rx:
        m = re.search(rx, stdout)
        if m:
            return float(m.group(1))
    return 0.0


def read_status(run_dir: Path) -> tuple[str, str, str]:
    f = run_dir / "status.md"
    if not f.is_file():
        return "ERROR", "", f"agent did not write {f}"
    lines = f.read_text().strip().splitlines()
    status = (lines[0].strip().upper().strip("*`# ") if lines else "")
    if status not in STATUSES:
        return "ERROR", "", f"invalid status line: {lines[0] if lines else '(empty)'}"
    msg = lines[1].strip() if len(lines) > 1 else ""
    body = "\n".join(lines[2:]).strip()
    return status, msg, body


def invoke(sandbox: Sandbox, box: str, agent: dict, prompt: str, workdir: Path, run_dir: Path, *,
           timeout: int, log_file: Path) -> AgentOutcome:
    """Run one agent iteration inside sandbox `box`, with `workdir` as cwd. Status comes back via run_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.md").unlink(missing_ok=True)
    cmd = build_command(agent)
    res = sandbox.run(box, cmd, workdir, stdin=prompt, timeout=timeout)
    log_file.write_text(f"$ {' '.join(cmd)}\n--- exit {res.code} in {res.seconds:.0f}s\n"
                        f"--- stdout\n{res.stdout}\n--- stderr\n{res.stderr}\n")
    cost = parse_cost(res.stdout, agent)
    if res.timed_out:
        return AgentOutcome("ERROR", "", "agent timed out", cost, res)
    status, msg, body = read_status(run_dir)
    if status == "ERROR" and res.code != 0:
        detail = res.stderr.strip()[-500:]
        try:  # Claude Code reports auth/API errors in the JSON result, not on stderr
            data = json.loads(res.stdout.strip().splitlines()[-1])
            if data.get("is_error"):
                detail = str(data.get("result") or data.get("subtype") or detail)
        except (json.JSONDecodeError, IndexError, AttributeError):
            pass
        body = f"agent exited {res.code}: {detail}"
    return AgentOutcome(status, msg, body, cost, res)
