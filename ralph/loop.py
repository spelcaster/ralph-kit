"""The Ralph loop: plan -> (develop -> test -> review)* -> PR -> wait for merge -> next task."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import agents, git as G, hitl, voice
from .git import RUN_DIR
from .sandbox import GATES_SH, Sandbox, sandbox_name
from .state import Ledger, Project, Task, file_hash, parse_plan

PHASE_STATUSES = {
    "planning": "DONE | ASK",
    "development": "DONE | CONTINUE | ASK | BLOCKED",
    "test": "DONE | CONTINUE | ASK | BLOCKED",
    "fix": "DONE | CONTINUE | ASK | BLOCKED",
    "review": "APPROVE | CHANGES | ASK",
    "interview": "ASK | DONE",
}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
RALPH_MARK = "<!-- ralph -->"


class StopRun(Exception):
    def __init__(self, reason: str, code: int):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _tail(text: str, n: int = 120) -> str:
    return "\n".join(text.splitlines()[-n:])


class Runner:
    def __init__(self, project: Project, *, once: bool = False, wait: bool | None = None):
        self.p = project
        self.cfg = project.cfg
        self.run_cfg = self.cfg["run"]
        self.git_cfg = self.cfg["git"]
        self.sandbox = Sandbox(self.cfg["sandbox"], G.common_git_dir(project.repo))
        self.ledger = Ledger(self.cfg["budget"])
        self.iterations = 0
        self.once = once
        self.wait = self.run_cfg.get("wait_for_merge", True) if wait is None else wait
        self.tasks_finished = 0

    # ------------------------------------------------------------------ entry
    def run(self) -> int:
        G.ensure_excluded(self.p.repo)
        spent, ceil = self.ledger.spent(), self.ledger.ceiling()
        self.p.log(f"ralph run · {self.p.repo.name} · max {self.run_cfg['max_iterations']} iterations · "
                   f"budget ${spent:.2f}/${ceil:.2f} ({self.cfg['budget']['window']})")
        try:
            while True:
                prd = self.p.find_prd()
                if not prd:
                    raise StopRun("no PRD found (looked in " + ", ".join(self.cfg["prd"]["paths"]) + ")", 3)
                inflight = self.p.open_task()
                if inflight:
                    task, st = inflight
                    if st["status"] == "active":
                        self.work_task(task, st, prd)
                    else:
                        outcome = self.settle_pr(task, st, prd)
                        if outcome == "merged":
                            self.tasks_finished += 1
                            if self.once:
                                raise StopRun("--once: task merged", 0)
                        elif outcome == "waiting":
                            if not self.wait:
                                raise StopRun(f"PR #{st['pr']} is waiting for review/merge (--no-wait)", 0)
                            time.sleep(int(self.run_cfg.get("merge_poll_seconds", 60)))
                    continue
                if not self.p.plan_path.is_file() or self.p.state.get("prd_hash") != file_hash(prd):
                    self.plan(prd)
                    continue
                nxt = self.p.next_todo()
                if not nxt:
                    raise StopRun("plan is empty: every task is merged 🎉", 0)
                st = self.p.task_state(nxt.slug)
                st.update(status="active", phase=self.run_cfg["phases"][0], started=time.time())
                self.p.save()
                self.p.log(f"▶ task {nxt.slug}: {nxt.title}")
        except StopRun as s:
            self.p.log(f"■ stop: {s.reason}")
            if s.code not in (0,) or "empty" in s.reason:
                hitl.notify(f"ralph · {self.p.repo.name} stopped", s.reason, self.cfg["hitl"], self.p.repo)
                if self.cfg["hitl"].get("speak", True):
                    voice.speak(f"Ralph stopped on {self.p.repo.name}. {s.reason}", self.cfg["voice"], block=False)
            return s.code
        except KeyboardInterrupt:
            self.p.log("■ interrupted; state saved, `ralph run` resumes where it left off")
            return 130
        except Exception as e:  # last resort: never let a bare traceback kill the run silently
            self.p.log(f"■ stop: unexpected error ({e}); state saved, `ralph run` resumes where it left off\n"
                       + traceback.format_exc())
            hitl.notify(f"ralph · {self.p.repo.name} crashed", str(e), self.cfg["hitl"], self.p.repo)
            if self.cfg["hitl"].get("speak", True):
                voice.speak(f"Ralph crashed on {self.p.repo.name}. {e}", self.cfg["voice"], block=False)
            return 1

    # ------------------------------------------------------------------ limits
    def check_limits(self, st: dict | None = None, iterations: bool = True) -> None:
        mx = int(self.run_cfg["max_iterations"])
        if iterations and self.iterations >= mx:
            raise StopRun(f"max iterations reached ({mx})", 2)
        if self.ledger.exhausted():
            b = self.cfg["budget"]
            raise StopRun(f"budget ceiling reached: ${self.ledger.spent():.2f} of ${self.ledger.ceiling():.2f} "
                          f"({b['ceiling_pct']}% of ${b['plan_usd']} per {b['window']})", 4)
        per = int(self.run_cfg.get("max_iterations_per_task") or 0)
        if st is not None and per and st.get("iterations", 0) >= per:
            ans = self.ask(f"Task {st.get('slug', '')} used {per} iterations without finishing. "
                           "Say continue to give it more, skip to abandon it, or stop.", "task-cap")
            if "skip" in ans.lower() or "pular" in ans.lower():
                st["status"] = "abandoned"
                self.p.save()
                raise _Skip()
            st["iterations"] = 0
            st["human_answer"] = ans

    def ask(self, question: str, kind: str, **ctx) -> str:
        ans = hitl.ask(self.p, question, kind=kind, context=ctx)
        if ans is None:
            raise StopRun("waiting for a human answer (run `ralph answer`, then `ralph run`)", 5)
        if ans.strip().lower() in ("stop", "parar", "pare"):
            raise StopRun("human said stop", 5)
        return ans

    # ------------------------------------------------------------------ agent step
    def box(self, *parts: str) -> str:
        return sandbox_name(self.p.repo.name, *parts)

    def agent_step(self, phase: str, task: Task | None, st: dict, workdir: Path, ctx: dict, *,
                   box: str, run_dir: Path | None = None, count: bool = True):
        run_dir = run_dir or workdir / RUN_DIR
        if count:
            self.iterations += 1
        st["iterations"] = st.get("iterations", 0) + 1
        slug = task.slug if task else "plan"
        full_ctx = {
            "language": self.run_cfg.get("language", "en"), "allowed_statuses": PHASE_STATUSES[phase],
            "iteration": st["iterations"], "phase": phase, "base_ref": G.base_ref(self.p.repo, self.git_cfg),
            "task_slug": slug, "task_title": task.title if task else "", "task_body": task.body if task else "",
            "human_answer": st.pop("human_answer", "") or "(none)",
            "feedback": st.get("feedback") or "(none — first iteration or previous one was clean)",
            "run_dir": str(run_dir), "gates_cmd": f"bash {GATES_SH}",
            **ctx,
        }
        prompt = agents.render(agents.load_prompt(self.p.dir, phase), full_ctx)
        agent_cfg = dict(self.cfg.agent(phase))
        remaining = max(self.ledger.ceiling() - self.ledger.spent(), 0.01)
        cap = float(agent_cfg.get("max_budget_usd") or 0)
        agent_cfg["max_budget_usd"] = min(cap, remaining) if cap else remaining
        log = self.p.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{phase}-{st['iterations']}.log"
        self.p.log(f"  [{slug}] {phase} #{st['iterations']} → running agent "
                   f"(cli={agent_cfg.get('cli', 'claude')}, model={agent_cfg.get('model', '')})…")
        out = agents.invoke(self.sandbox, box, agent_cfg, prompt, workdir, run_dir,
                            timeout=int(self.cfg["sandbox"].get("agent_timeout_seconds", 2700)), log_file=log,
                            heartbeat=lambda s: self.p.log(f"  [{slug}] {phase} #{st['iterations']} … "
                                                           f"still running ({int(s)}s elapsed)"))
        self.ledger.add(out.cost, project=self.p.repo.name, task=slug, phase=phase, model=agent_cfg.get("model"))
        if count:
            self.p.log(f"  [{slug}] {phase} #{st['iterations']} → {out.status}  ${out.cost:.2f}  "
                       f"{out.commit_msg or out.body[:80]}  ({self.iterations}/{self.run_cfg['max_iterations']})")
        return out, agent_cfg

    def remember_answer(self, workdir: Path, question: str, answer: str) -> None:
        notes = workdir / RUN_DIR / "notes.md"
        notes.parent.mkdir(exist_ok=True)
        with notes.open("a") as f:
            f.write(f"\n## Human decision\nQ: {question}\nA: {answer}\n")

    # ------------------------------------------------------------------ planning
    def plan(self, prd: Path) -> None:
        self.p.log("✎ planning from " + str(prd.relative_to(self.p.repo)))
        st = self.p.state.setdefault("planning", {"iterations": 0})
        # plan against the latest base in a throwaway detached worktree, never your working checkout
        remote = self.git_cfg["remote"]
        if G.has_remote(self.p.repo, remote):
            G.git(self.p.repo, "fetch", remote, G.base_branch(self.p.repo, self.git_cfg), check=False)
        wt = G.worktree_path(self.p.repo, self.git_cfg, "_planning")
        G.git(self.p.repo, "worktree", "remove", "--force", str(wt), check=False)
        G.git(self.p.repo, "worktree", "add", "--detach", str(wt), G.base_ref(self.p.repo, self.git_cfg))
        # the planner reads the code read-only and may only write into a scratch dir
        scratch = self.p.state_dir / "runs" / "planning"
        box = self.box("plan")
        try:
            self.sandbox.ensure(box, scratch, [(wt, "ro")])
            existing = self.p.plan_path.read_text() if self.p.plan_path.is_file() else "(no plan yet)"
            errors = 0
            while True:
                self.check_limits()
                out, _ = self.agent_step("planning", None, st, wt, {"prd": prd.read_text(), "plan": existing},
                                         box=box, run_dir=scratch)
                if out.status == "ASK":
                    ans = self.ask(out.body, "planning")
                    st["human_answer"] = f"Q: {out.body}\nA: {ans}"
                    continue
                new = scratch / "plan.md"
                try:
                    tasks = parse_plan(new.read_text()) if new.is_file() else []
                except ValueError as e:
                    tasks, out.body = [], str(e)
                if out.status == "DONE" and tasks:
                    break
                errors += 1
                st["feedback"] = f"Your last attempt produced no valid plan: {out.body}"
                if errors >= int(self.run_cfg["max_consecutive_agent_errors"]):
                    ans = self.ask("The planner failed to produce a valid plan several times. "
                                   "Say retry with a hint, or stop.", "planning-failed")
                    st["human_answer"], errors = ans, 0
            self.p.plan_path.write_text(new.read_text())
            for t in tasks:  # status is orchestrator-owned: keep what we already know
                self.p.task_state(t.slug)
            self.p.state["prd_hash"] = file_hash(prd)
            self.p.state.pop("planning", None)
            self.p.save()
            self.p.log(f"✎ plan ready: {len(tasks)} tasks → .ralph/plan.md")
        finally:
            self.sandbox.remove(box)
            G.git(self.p.repo, "worktree", "remove", "--force", str(wt), check=False)

    # ------------------------------------------------------------------ core driver
    def drive(self, task: Task, st: dict, wt: Path, phases: list[str], ctx: dict, box: str) -> str:
        """Run agent iterations until the last phase signs off. Every iteration ends in a commit."""
        phase: str = st["phase"] if st.get("phase") in phases else phases[0]
        gate_fails = agent_errors = 0
        while True:
            st["phase"] = phase
            self.p.save()
            self.check_limits(st)
            out, agent_cfg = self.agent_step(phase, task, st, wt, ctx, box=box)
            if out.status in ("ASK", "BLOCKED"):
                ans = self.ask(out.body or "The agent is blocked and needs you.", "agent", task=task.slug, phase=phase)
                self.remember_answer(wt, out.body, ans)
                st["human_answer"] = ans
                continue
            if out.status == "ERROR":
                agent_errors += 1
                st["feedback"] = f"The previous iteration failed: {out.body}. Finish by writing .ralph-run/status.md."
                if agent_errors >= int(self.run_cfg["max_consecutive_agent_errors"]):
                    ans = self.ask(f"The {phase} agent failed {agent_errors} times in a row on {task.slug}: "
                                   f"{out.body[:200]}. Say retry, or stop.", "agent-errors", task=task.slug)
                    st["human_answer"], agent_errors = ans, 0
            else:
                agent_errors = 0

            self.p.log(f"  [{task.slug}] running gates…")
            gates = self.sandbox.gates(box, wt, self.cfg["gates"],
                                       heartbeat=lambda s: self.p.log(f"  [{task.slug}] gates … "
                                                                      f"still running ({int(s)}s elapsed)"))
            ok = gates.code == 0
            (self.p.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{task.slug}-gates.log").write_text(
                gates.stdout + "\n" + gates.stderr)
            summary = " ".join(ln[5:] for ln in gates.stdout.splitlines() if ln.startswith("GATE "))
            self.p.log(f"  [{task.slug}] gates {'PASS' if ok else 'FAIL'}  {summary}")
            sha = G.commit(wt, out.commit_msg or f"chore({task.slug}): {phase} iteration {st['iterations']}", {
                "Ralph-Task": task.slug, "Ralph-Phase": phase, "Ralph-Iteration": str(st["iterations"]),
                "Ralph-Status": out.status, "Ralph-Gates": "pass" if ok else "fail",
                "Ralph-Model": str(agent_cfg.get("model", "")), "Ralph-Cost": f"{out.cost:.4f}",
            })
            if sha:
                self.p.log(f"  [{task.slug}] committed {sha}")
            st["last_gates"] = summary

            if not ok:
                gate_fails += 1
                st["feedback"] = "Quality gates FAILED after your last iteration. Fix these first:\n```\n" + \
                    _tail(gates.stdout + "\n" + gates.stderr) + "\n```"
                if gate_fails >= int(self.run_cfg["max_consecutive_gate_failures"]):
                    ans = self.ask(f"Quality gates failed {gate_fails} times in a row on {task.slug}. "
                                   f"Failing: {summary}. Say how to proceed, or stop.", "gates", task=task.slug)
                    st["human_answer"], gate_fails = ans, 0
                if phase != "review":
                    continue
                phase = "fix" if phases == ["fix"] else "development"
                continue
            gate_fails = 0
            st["feedback"] = ""

            if phase == "review":
                if out.status == "APPROVE":
                    return out.body
                review = wt / RUN_DIR / "review.md"
                st["feedback"] = "The reviewer requested changes:\n" + (review.read_text() if review.is_file() else out.body)
                phase = "development"
                continue
            if out.status in ("DONE", "APPROVE"):
                i = phases.index(phase)
                if i + 1 < len(phases):
                    phase = phases[i + 1]
                    continue
                return out.body
            # CONTINUE (or recovered ERROR): another iteration of the same phase

    # ------------------------------------------------------------------ task lifecycle
    def work_task(self, task: Task, st: dict, prd: Path) -> None:
        wt, branch = G.create_worktree(self.p.repo, self.git_cfg, task.slug)
        box = self.sandbox.ensure(self.box(task.slug), wt)
        st.update(slug=task.slug, branch=branch, worktree=str(wt), sandbox=box)
        self.p.save()
        phases = [ph for ph in self.run_cfg["phases"] if ph in ("development", "test", "review")] or ["development"]
        try:
            verdict = self.drive(task, st, wt, phases, {"prd": prd.read_text()}, box)
        except _Skip:
            self.sandbox.remove(box)
            return
        ahead = int(G.git(wt, "rev-list", "--count", f"{G.base_ref(wt, self.git_cfg)}..HEAD") or 0)
        if not ahead:
            ans = self.ask(f"Task {task.slug} finished without any code changes. Say skip to drop it, "
                           "or tell me what to do instead.", "no-changes", task=task.slug)
            if "skip" in ans.lower() or "pular" in ans.lower():
                st["status"] = "abandoned"
                self.sandbox.remove(box)
            else:
                st["human_answer"], st["phase"] = ans, phases[0]
            self.p.save()
            return
        G.push(wt, self.git_cfg, branch)
        body = (f"{RALPH_MARK}\n## Task `{task.slug}`: {task.title}\n\n{task.body}\n"
                f"## Review verdict\n{verdict or '(review phase disabled)'}\n\n"
                f"## Quality gates\n`{st.get('last_gates', '')}`\n\n---\n"
                f"Opened by ralph after {st['iterations']} iterations. Iteration history is in the commit "
                f"trailers (`git log --format='%h %s %(trailers:key=Ralph-Phase,valueonly)'`).\n")
        pr = G.create_pr(wt, self.git_cfg, branch, task.title, body)
        st.update(status="pr_open", pr=pr, phase=None, fix_attempts=0)
        self.p.save()
        self.p.log(f"⇪ PR #{pr} opened for {task.slug}")
        hitl.notify(f"ralph · PR #{pr} ready", f"{task.title} — review and merge to continue",
                    self.cfg["hitl"], self.p.repo)
        if self.cfg["hitl"].get("speak", True):
            voice.speak(f"Pull request {pr} is ready for review: {task.title}", self.cfg["voice"], block=False)

    def settle_pr(self, task: Task, st: dict, prd: Path) -> str:
        wt = Path(st["worktree"])
        if not wt.exists():
            wt, _ = G.create_worktree(self.p.repo, self.git_cfg, task.slug)
        pr = st["pr"]
        box = st.get("sandbox") or self.box(task.slug)
        s = G.pr_status(wt, pr)
        if s["state"] == "MERGED":
            st["status"] = "merged"
            st["merged_at"] = s.get("mergedAt")
            self.p.save()
            self.sandbox.remove(box)
            G.remove_worktree(self.p.repo, wt, st["branch"])
            hitl.set_attention(self.p.repo, None)
            self.p.log(f"✔ {task.slug} merged (PR #{pr}) — marked done")
            return "merged"
        if s["state"] == "CLOSED":
            ans = self.ask(f"Pull request {pr} for {task.title} was closed without merging. "
                           "Say retry with what to change, or skip to drop the task.", "pr-closed", task=task.slug)
            if "skip" in ans.lower() or "pular" in ans.lower():
                st["status"] = "abandoned"
                self.sandbox.remove(box)
                G.remove_worktree(self.p.repo, wt, st["branch"])
            else:
                st.update(status="active", phase=self.run_cfg["phases"][0], pr=None, human_answer=ans,
                          feedback=f"PR #{pr} was closed without merging. The human said: {ans}")
            self.p.save()
            return "retry"

        # OPEN: figure out what is wrong, if anything
        handled = set(st.get("handled_comments", []))
        problems, new_ids = [], []
        checks = s.get("statusCheckRollup") or []
        failed = [c for c in checks if (c.get("conclusion") or c.get("state") or "").upper() in FAILED]
        pending = [c for c in checks if (c.get("status") or c.get("state") or "").upper() in PENDING]
        conflicted = s.get("mergeable") == "CONFLICTING"
        if conflicted:
            problems.append("## Merge conflicts with the base branch\nResolve the conflict markers listed below.")
        if failed and s["headRefOid"] not in st.get("fixed_heads", []):
            problems.append("## Failing CI checks\n" + G.failed_check_logs(wt, failed))
        comments = []
        for r in s.get("reviews") or []:
            if r.get("body") and RALPH_MARK not in r["body"]:
                comments.append((f"review:{r.get('id') or r.get('submittedAt')}", r.get("author", {}).get("login"),
                                 f"[{r.get('state')}] {r['body']}"))
        for c in s.get("comments") or []:
            if RALPH_MARK not in (c.get("body") or ""):
                comments.append((f"comment:{c.get('id') or c.get('createdAt')}", c.get("author", {}).get("login"),
                                 c.get("body", "")))
        for c in G.pr_inline_comments(wt, pr):
            if RALPH_MARK not in (c.get("body") or ""):
                comments.append((f"inline:{c['id']}", c.get("user", {}).get("login"),
                                 f"{c.get('path')}:{c.get('line') or c.get('original_line')}\n{c.get('body')}"))
        fresh = [c for c in comments if c[0] not in handled]
        if fresh:
            problems.append("## Review comments to address\n" + "\n\n".join(f"- @{a}: {b}" for _, a, b in fresh))
            new_ids = [c[0] for c in fresh]

        if not problems:
            st["fix_attempts"] = 0
            why = "checks running" if pending else ("changes requested" if s.get("reviewDecision") == "CHANGES_REQUESTED"
                                                    else "waiting for review and merge")
            if st.get("waiting_note") != (s["headRefOid"], why):
                st["waiting_note"] = [s["headRefOid"], why]
                self.p.save()
                self.p.log(f"… PR #{pr} ({task.slug}): {why}. The next task starts only after it is merged.")
                if why != "checks running":
                    hitl.notify(f"ralph · PR #{pr} waiting", f"{task.title}: {why}", self.cfg["hitl"], self.p.repo)
            return "waiting"

        st["fix_attempts"] = st.get("fix_attempts", 0) + 1
        if st["fix_attempts"] > 3:
            ans = self.ask(f"I tried to fix pull request {pr} three times and it still has problems. "
                           "Say retry with a hint, or stop.", "fix-loop", task=task.slug)
            st.update(fix_attempts=0, human_answer=ans)
        self.p.log(f"⚠ PR #{pr} ({task.slug}) needs work: " + "; ".join(p.splitlines()[0][3:] for p in problems))
        conflict_files = G.merge_base_into(wt, self.git_cfg) if conflicted else []
        if conflict_files:
            problems.append("Conflicted files:\n" + "\n".join(f"- {f}" for f in conflict_files))
        st["phase"] = "fix"
        self.sandbox.ensure(box, wt)
        try:
            summary = self.drive(task, st, wt, ["fix"], {"pr_number": pr, "pr_problems": "\n\n".join(problems),
                                                          "prd": prd.read_text()}, box)
        except _Skip:
            return "retry"
        if G.conflict_markers(wt):
            st["feedback"] = "Conflict markers are still present in: " + ", ".join(G.conflict_markers(wt))
            self.p.save()
            return "fixed"
        G.commit(wt, f"fix({task.slug}): address PR #{pr} feedback", {"Ralph-Task": task.slug, "Ralph-Phase": "fix"})
        if failed:
            st.setdefault("fixed_heads", []).append(s["headRefOid"])
        G.push(wt, self.git_cfg, st["branch"])
        st["handled_comments"] = sorted(handled | set(new_ids))
        st["phase"] = None
        self.p.save()
        G.gh(wt, "pr", "comment", str(pr), "--body", f"{RALPH_MARK}\n🤖 Addressed in latest push:\n\n{summary}",
             check=False)
        self.p.log(f"⇪ pushed fixes to PR #{pr}")
        return "fixed"


class _Skip(Exception):
    """Raised when the human abandons the current task."""
