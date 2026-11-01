"""Hardened execution via Docker Sandboxes (`sbx`): one microVM per task, agents and gates run inside it.

Layout of a task sandbox (workspaces are mounted at the same path as on the host):
  <worktree>            rw   the task's git worktree, the only thing the agent can change
  <repo>/.git           ro   git metadata, so the agent can read history but never commit or push
  <kit>/sandbox         ro   gates.sh
Network egress goes through the sbx policy proxy (deny-by-default with the "Locked Down" policy, plus the
per-sandbox allowlist below). Model credentials are injected by the proxy (`sbx secret set -g anthropic`);
no GitHub token ever enters a sandbox: commits, pushes and PRs happen on the host.

Sandboxes are already reused for the life of whatever they're doing (a task's whole dev/test/review loop,
one PRD conversation): `ensure()` is a no-op if the name already exists. `sandbox.keep = true` goes further
and skips the teardown at the end too, so the next `ralph talk`/`prd`/`gates` reuses a warm sandbox instead
of booting a new one. Clean up what it leaves behind with `ralph sandboxes --prune`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import PKG_DIR

KIT_DIR = PKG_DIR.parent
GATES_DIR = KIT_DIR / "sandbox"
GATES_SH = GATES_DIR / "gates.sh"


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    seconds: float = 0.0


def sandbox_name(*parts: str) -> str:
    raw = "-".join(["ralph", *parts]).lower()
    return re.sub(r"[^a-z0-9.+-]+", "-", raw).strip("-")[:63]


class Sandbox:
    """engine = "sbx" (Docker Sandboxes) or "none" (runs on the host: tests only)."""

    def __init__(self, cfg: dict, git_common_dir: Path):
        self.cfg = cfg
        self.engine = cfg.get("engine", "sbx")
        self.sbx = os.environ.get("RALPH_SBX", "sbx")
        self.git_dir = git_common_dir
        self.agent = cfg.get("agent", "claude")
        if self.engine == "none" and not os.environ.get("RALPH_ALLOW_UNSANDBOXED"):
            print("ralph: WARNING sandbox.engine = none -> agents run on your host with full permissions.",
                  file=sys.stderr)
        if self.engine == "sbx" and not shutil.which(self.sbx):
            raise SystemExit("ralph: `sbx` not found. Install Docker Sandboxes: ./install.sh (see README)")

    # ------------------------------------------------------------------ lifecycle
    def _sbx(self, *args: str, check: bool = True, timeout: int = 600, stdin: str | None = None) -> Result:
        start = time.time()
        p = subprocess.run([self.sbx, *args], input=stdin, capture_output=True, text=True, timeout=timeout)
        r = Result(p.returncode, p.stdout, p.stderr, seconds=time.time() - start)
        if check and p.returncode != 0:
            raise RuntimeError(f"sbx {' '.join(args)} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
        return r

    def _list_sandboxes(self, r: Result) -> list[dict]:
        """`sbx ls --json` wraps the array as {"sandboxes": [...]}."""
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            return []
        return data.get("sandboxes", []) if isinstance(data, dict) else data

    def exists(self, name: str) -> bool:
        if self.engine != "sbx":
            return True
        r = self._sbx("ls", "--json", check=False)
        return any(s.get("name") == name for s in self._list_sandboxes(r))

    def ensure(self, name: str, primary: Path, extra: list[tuple[Path, str]] | None = None) -> str:
        """Create the sandbox if needed. primary is mounted rw; extra entries are (path, "ro"|"rw")."""
        if self.engine != "sbx" or self.exists(name):
            return name
        primary.mkdir(parents=True, exist_ok=True)
        print(f"ralph: creating sandbox {name} (first time can take a minute)…", file=sys.stderr, flush=True)
        args = ["create", "--name", name, "-q"]
        if self.cfg.get("memory"):
            args += ["--memory", str(self.cfg["memory"])]
        if self.cfg.get("cpus"):
            args += ["--cpus", str(self.cfg["cpus"])]
        if self.cfg.get("template"):
            args += ["--template", str(self.cfg["template"])]
        for kit in self.cfg.get("kits", []):
            args += ["--kit", str(kit)]
        mounts = [str(primary)]
        for path, mode in [(self.git_dir, "ro"), (GATES_DIR, "ro"), *(extra or [])]:
            mounts.append(f"{path}:ro" if mode == "ro" else str(path))
        self._sbx(*args, self.agent, *mounts, timeout=900)
        domains = [d for d in self.cfg.get("allow_domains", []) if d]
        if domains:
            self._sbx("policy", "allow", "network", name, ",".join(domains), check=False)
        return name

    def remove(self, name: str, *, force: bool = False) -> None:
        """Tear down a sandbox, unless cfg.keep asks to leave it running for next time (force overrides)."""
        if self.engine != "sbx" or (self.cfg.get("keep") and not force):
            return
        if self.exists(name):
            self._sbx("rm", "--force", name, check=False)

    def list_ralph(self) -> list[str]:
        if self.engine != "sbx":
            return []
        r = self._sbx("ls", "--json", check=False)
        return [s["name"] for s in self._list_sandboxes(r) if str(s.get("name", "")).startswith("ralph-")]

    # ------------------------------------------------------------------ execution
    def run(self, name: str, cmd: list[str], workdir: Path, *, stdin: str = "", env: dict | None = None,
            timeout: int = 1800, heartbeat: Callable[[float], None] | None = None,
            heartbeat_interval: int = 60) -> Result:
        env = {"GIT_OPTIONAL_LOCKS": "0", "CI": "1",
               # the worktree is owned by the host user; let git read it from inside the VM
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
               **(env or {})}
        start = time.time()
        if self.engine == "sbx":
            full = [self.sbx, "exec", "-i", "-w", str(workdir)]
            for k, v in env.items():
                full += ["-e", f"{k}={v}"]
            full += [name, *cmd]
            run_env, cwd = None, None
        else:
            full, run_env, cwd = cmd, {**os.environ, **env}, workdir
        # long calls (agent iterations, gates) otherwise block silently for up to `timeout` seconds
        # with no sign of life; tick a heartbeat so a stuck/slow call is visible, not indistinguishable
        # from a hang.
        stop = threading.Event()
        ticker = None
        if heartbeat:
            def _tick():
                while not stop.wait(heartbeat_interval):
                    heartbeat(time.time() - start)
            ticker = threading.Thread(target=_tick, daemon=True)
            ticker.start()
        try:
            p = subprocess.run(full, input=stdin, capture_output=True, text=True, timeout=timeout,
                               cwd=cwd, env=run_env)
            return Result(p.returncode, p.stdout, p.stderr, seconds=time.time() - start)
        except subprocess.TimeoutExpired as e:
            if self.engine == "sbx":  # stop the runaway process; the sandbox itself stays for the next try
                subprocess.run([self.sbx, "exec", name, "pkill", "-f", cmd[0]], capture_output=True, timeout=60)
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return Result(124, out, "timeout", timed_out=True, seconds=time.time() - start)
        finally:
            stop.set()
            if ticker:
                ticker.join(timeout=1)

    def gates(self, name: str, worktree: Path, gates_cfg: dict, *,
              heartbeat: Callable[[float], None] | None = None) -> Result:
        env = {f"RALPH_GATE_{k.upper()}": gates_cfg.get(k, "") for k in ("setup", "lint", "typecheck", "test")}
        env["RALPH_GATES_REQUIRE"] = " ".join(gates_cfg.get("require", []))
        return self.run(name, ["bash", str(GATES_SH)], worktree, env=env, heartbeat=heartbeat,
                        timeout=int(gates_cfg.get("timeout_seconds", 1200)))
