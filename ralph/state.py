"""Project state: PRD discovery, plan parsing, task status, cost ledger, run log."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

TASK_RE = re.compile(r"^- \[(?P<mark>[ x~\-])\] (?P<slug>[a-z0-9][a-z0-9\-]*):\s*(?P<title>.+?)\s*$")
MARKS = {"todo": " ", "active": "~", "pr_open": "~", "merged": "x", "abandoned": "-"}


@dataclass
class Task:
    slug: str
    title: str
    body: str = ""
    mark: str = " "


def parse_plan(text: str) -> list[Task]:
    tasks: list[Task] = []
    for line in text.splitlines():
        m = TASK_RE.match(line)
        if m:
            tasks.append(Task(m["slug"], m["title"], "", m["mark"]))
        elif tasks and (line.startswith("  ") or line.startswith("\t")):
            tasks[-1].body += line.strip() + "\n"
        elif tasks and line.strip() and not line.startswith("#"):
            # unindented non-task text ends the current task body
            pass
    seen: set[str] = set()
    for t in tasks:
        if t.slug in seen:
            raise ValueError(f"duplicate task slug in plan: {t.slug}")
        seen.add(t.slug)
    return tasks


def render_marks(text: str, statuses: dict[str, str]) -> str:
    """Rewrite checkbox marks in plan.md to reflect orchestrator-owned status."""
    out = []
    for line in text.splitlines():
        m = TASK_RE.match(line)
        if m and m["slug"] in statuses:
            mark = MARKS.get(statuses[m["slug"]], " ")
            line = f"- [{mark}] {m['slug']}: {m['title']}"
        out.append(line)
    return "\n".join(out) + "\n"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class Project:
    """Everything Ralph keeps lives in <repo>/.ralph/ (state/ is gitignored)."""

    def __init__(self, repo: Path, cfg):
        self.repo = repo
        self.cfg = cfg
        self.dir = repo / ".ralph"
        self.state_dir = self.dir / "state"
        self.logs = self.state_dir / "logs"
        self.plan_path = self.dir / "plan.md"
        self.state_path = self.state_dir / "state.json"
        self.question_path = self.state_dir / "question.json"
        self.answer_path = self.state_dir / "answer.txt"
        self.logs.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("state/\nplan.md\n")
        self.state = self._read_state()

    # ---- PRD / plan ----
    def find_prd(self) -> Path | None:
        for rel in self.cfg["prd"].get("paths", []):
            p = self.repo / rel
            if p.is_file() and p.read_text().strip():
                return p
        return None

    def tasks(self) -> list[Task]:
        if not self.plan_path.is_file():
            return []
        return parse_plan(self.plan_path.read_text())

    def sync_plan_marks(self) -> None:
        if self.plan_path.is_file():
            st = {k: v["status"] for k, v in self.state["tasks"].items()}
            self.plan_path.write_text(render_marks(self.plan_path.read_text(), st))

    # ---- task state ----
    def _read_state(self) -> dict:
        if self.state_path.is_file():
            return json.loads(self.state_path.read_text())
        return {"prd_hash": None, "tasks": {}}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.state_path)
        self.sync_plan_marks()

    def task_state(self, slug: str) -> dict:
        return self.state["tasks"].setdefault(slug, {"status": "todo", "iterations": 0})

    def open_task(self) -> tuple[Task, dict] | None:
        """The task that is in flight (active or PR open). At most one by design."""
        for t in self.tasks():
            s = self.state["tasks"].get(t.slug)
            if s and s["status"] in ("active", "pr_open"):
                return t, s
        return None

    def next_todo(self) -> Task | None:
        for t in self.tasks():
            if self.task_state(t.slug)["status"] == "todo" and t.mark not in ("x", "-"):
                return t
        return None

    # ---- log ----
    def log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        print(line, flush=True)
        with (self.state_dir / "ralph.log").open("a") as f:
            f.write(line + "\n")


# ---- cost ledger (global: one plan, many projects) ----
def window_start(window: str, run_started: float) -> float:
    now = datetime.now(timezone.utc)
    if window == "run":
        return run_started
    if window == "5h":
        return (now - timedelta(hours=5)).timestamp()
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if window == "week":
        d = now - timedelta(days=now.weekday())
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    raise ValueError(f"unknown budget window: {window}")


class Ledger:
    def __init__(self, budget_cfg: dict, run_started: float | None = None):
        self.path = Path(budget_cfg["ledger"]).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = budget_cfg
        self.run_started = run_started or time.time()

    def add(self, cost: float, **meta) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "cost": cost, **meta}) + "\n")

    def spent(self) -> float:
        if not self.path.is_file():
            return 0.0
        since = window_start(self.cfg["window"], self.run_started)
        total = 0.0
        for line in self.path.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) >= since:
                total += float(e.get("cost", 0))
        return total

    def ceiling(self) -> float:
        return float(self.cfg["plan_usd"]) * float(self.cfg["ceiling_pct"]) / 100.0

    def exhausted(self) -> bool:
        return self.spent() >= self.ceiling()
