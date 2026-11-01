"""Git + GitHub. Runs on the host only: the sandbox never holds push credentials."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

RUN_DIR = ".ralph-run"  # agent <-> orchestrator mailbox inside each worktree (never committed)


class GitError(RuntimeError):
    pass


def sh(args: list[str], cwd: Path | None = None, check: bool = True, timeout: int = 600) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"{' '.join(args)}\n{p.stdout}\n{p.stderr}".strip())
    return p.stdout.strip()


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return sh(["git", *args], cwd=cwd, check=check)


def find_repo(start: Path) -> Path:
    """Main checkout, even when called from inside a task worktree."""
    try:
        common = Path(git(start, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except GitError:
        raise SystemExit("ralph: not inside a git repository (every project must be a git repo)")
    return common.parent if common.name == ".git" else Path(git(start, "rev-parse", "--show-toplevel"))


def common_git_dir(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def ensure_excluded(repo: Path) -> None:
    excl = common_git_dir(repo) / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    text = excl.read_text() if excl.exists() else ""
    if f"/{RUN_DIR}/" not in text:
        excl.write_text(text + ("" if text.endswith("\n") or not text else "\n") + f"/{RUN_DIR}/\n")


def base_branch(repo: Path, cfg: dict) -> str:
    if cfg.get("base"):
        return cfg["base"]
    remote = cfg.get("remote", "origin")
    # Ask the remote live first: refs/remotes/{remote}/HEAD is a local cache set at clone time
    # (or by `git remote set-head`) and git never refreshes it on fetch, so it goes stale the
    # moment the remote's default branch changes — trusting it first silently targets the wrong branch.
    out = git(repo, "ls-remote", "--symref", remote, "HEAD", check=False)
    m = re.search(r"ref:\s*refs/heads/(\S+)\s+HEAD", out)
    if m:
        return m.group(1)
    ref = git(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    if ref:
        return ref.rsplit("/", 1)[-1]
    out = git(repo, "remote", "show", remote, check=False)
    m = re.search(r"HEAD branch: (\S+)", out)
    if m and m.group(1) != "(unknown)":
        return m.group(1)
    return "main"


def has_remote(repo: Path, remote: str) -> bool:
    return remote in git(repo, "remote", check=False).splitlines()


def base_ref(repo: Path, cfg: dict) -> str:
    """'<remote>/<base>', or just the local branch when the repo has no such remote (local-only repo)."""
    base = base_branch(repo, cfg)
    remote = cfg.get("remote", "origin")
    return f"{remote}/{base}" if has_remote(repo, remote) else base


def worktree_path(repo: Path, cfg: dict, slug: str) -> Path:
    root = cfg.get("worktree_root", "../{repo}.worktrees").format(repo=repo.name)
    return (repo / root / slug).resolve()


def create_worktree(repo: Path, cfg: dict, slug: str) -> tuple[Path, str]:
    remote = cfg.get("remote", "origin")
    branch = f"{cfg.get('branch_prefix', 'ralph/')}{slug}"
    path = worktree_path(repo, cfg, slug)
    if has_remote(repo, remote):
        git(repo, "fetch", remote, base_branch(repo, cfg))
    if path.exists():
        return path, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base_ref(repo, cfg))
    (path / RUN_DIR).mkdir(exist_ok=True)
    return path, branch


def remove_worktree(repo: Path, path: Path, branch: str) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "branch", "-D", branch, check=False)
    git(repo, "worktree", "prune", check=False)


def has_changes(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain"))


def merge_in_progress(wt: Path) -> bool:
    return bool(git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False))


def conflict_markers(wt: Path) -> list[str]:
    out = git(wt, "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", check=False)
    return [f for f in out.splitlines() if f]


def commit(wt: Path, message: str, trailers: dict[str, str]) -> str | None:
    if not has_changes(wt) and not merge_in_progress(wt):
        return None
    git(wt, "add", "-A")
    args = ["commit", "--no-verify", "-m", message]
    for k, v in trailers.items():
        args += ["--trailer", f"{k}: {v}"]
    git(wt, *args)
    return git(wt, "rev-parse", "--short", "HEAD")


def merge_base_into(wt: Path, cfg: dict) -> list[str]:
    """Merge the latest base into the task branch. Returns conflicted files (markers left in place)."""
    remote = cfg.get("remote", "origin")
    if has_remote(wt, remote):
        git(wt, "fetch", remote, base_branch(wt, cfg))
    p = subprocess.run(["git", "merge", "--no-edit", base_ref(wt, cfg)], cwd=wt, capture_output=True, text=True)
    if p.returncode == 0:
        return []
    files = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
    if not files:
        raise GitError(p.stdout + p.stderr)
    return files


def push(wt: Path, cfg: dict, branch: str) -> None:
    remote = cfg.get("remote", "origin")
    if not has_remote(wt, remote):
        raise GitError(f"no git remote named '{remote}' — ralph opens a PR for every task, so add one "
                        f"(e.g. `git remote add {remote} <url>`) or set [git] remote in .ralph/config.toml")
    git(wt, "push", "-u", remote, branch)


# ---------------- GitHub (gh CLI) ----------------
def gh(cwd: Path, *args: str, check: bool = True) -> str:
    return sh(["gh", *args], cwd=cwd, check=check)


def pr_for_branch(wt: Path, branch: str, state: str = "open") -> int | None:
    out = gh(wt, "pr", "list", "--head", branch, "--state", state, "--json", "number", "--limit", "1", check=False)
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    return items[0]["number"] if items else None


def create_pr(wt: Path, cfg: dict, branch: str, title: str, body: str) -> int:
    existing = pr_for_branch(wt, branch)
    if existing:
        return existing
    base = base_branch(wt, cfg)
    body_file = wt / RUN_DIR / "pr_body.md"
    body_file.write_text(body)
    args = ["pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file)]
    if cfg.get("pr_draft"):
        args.append("--draft")
    labeled = list(args)
    for lab in cfg.get("pr_labels", []):
        labeled += ["--label", lab]
    try:
        out = gh(wt, *labeled)
    except GitError:  # labels that don't exist make gh fail; retry without them
        out = gh(wt, *args)
    m = re.search(r"/pull/(\d+)", out)
    if not m:
        raise GitError(f"could not parse PR number from: {out}")
    return int(m.group(1))


def pr_status(wt: Path, number: int) -> dict:
    fields = "state,mergedAt,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,headRefOid,url,reviews,comments"
    return json.loads(gh(wt, "pr", "view", str(number), "--json", fields))


def pr_inline_comments(wt: Path, number: int) -> list[dict]:
    out = gh(wt, "api", f"repos/{{owner}}/{{repo}}/pulls/{number}/comments", "--paginate", check=False)
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def failed_check_logs(wt: Path, checks: list[dict], max_lines: int = 150) -> str:
    chunks = []
    for c in checks:
        url = c.get("detailsUrl") or c.get("targetUrl") or ""
        m = re.search(r"/actions/runs/(\d+)", url)
        if not m:
            chunks.append(f"### {c.get('name') or c.get('context')}: failed ({url})")
            continue
        log = gh(wt, "run", "view", m.group(1), "--log-failed", check=False)
        tail = "\n".join(log.splitlines()[-max_lines:])
        chunks.append(f"### {c.get('name')}\n```\n{tail}\n```")
    return "\n\n".join(chunks)
