"""Human in the loop: tmux + Ghostty notifications, spoken questions, and waiting for answers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import voice

ATTENTION = Path("~/.local/state/ralph/attention.json").expanduser()
CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(s: str, n: int = 240) -> str:
    return CTRL.sub(" ", s)[:n]


def _client_ttys() -> list[str]:
    if shutil.which("tmux"):
        p = subprocess.run(["tmux", "list-clients", "-F", "#{client_tty}"], capture_output=True, text=True)
        ttys = [t for t in p.stdout.split() if t]
        if ttys:
            return ttys
    return ["/dev/tty"]


def _write_tty(tty: str, data: str) -> None:
    try:
        with open(tty, "w") as f:
            f.write(data)
            f.flush()
    except OSError:
        pass


def set_attention(repo: Path, message: str | None) -> None:
    ATTENTION.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(ATTENTION.read_text()) if ATTENTION.exists() else {}
    except json.JSONDecodeError:
        data = {}
    if message:
        data[str(repo)] = {"msg": message, "ts": time.time()}
    else:
        data.pop(str(repo), None)
    ATTENTION.write_text(json.dumps(data))
    if shutil.which("tmux"):
        subprocess.run(["tmux", "refresh-client", "-S"], capture_output=True)


def attention_line() -> str:
    """For tmux status-right: `#(ralph attention)`."""
    try:
        data = json.loads(ATTENTION.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not data:
        return ""
    names = ", ".join(Path(k).name for k in data)
    return f"🔔 ralph needs you: {names}"


def notify(title: str, body: str, cfg: dict, repo: Path) -> None:
    kinds = cfg.get("notify", [])
    title, body = _clean(title, 80), _clean(body)
    if "tmux" in kinds and shutil.which("tmux"):
        clients = subprocess.run(["tmux", "list-clients", "-F", "#{client_name}"], capture_output=True, text=True)
        for c in clients.stdout.split():
            subprocess.run(["tmux", "display-message", "-c", c, "-d", "8000", f"🔔 {title}: {body}"],
                           capture_output=True)
    ttys = _client_ttys() if ("osc" in kinds or "bell" in kinds) else []
    for tty in ttys:
        # written straight to the client tty, so it reaches Ghostty without needing tmux passthrough
        if "osc" in kinds:
            seq = (f"\x1b]777;notify;{title};{body}\x07" if str(cfg.get("osc", "777")) == "777"
                   else f"\x1b]9;{title}: {body}\x07")
            _write_tty(tty, seq)
        if "bell" in kinds:
            _write_tty(tty, "\a")
    if "desktop" in kinds and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "ralph", "-u", "critical", title, body], capture_output=True)
    if "attention" in kinds:
        set_attention(repo, f"{title}: {body}")
    print(f"\n🔔 {title}\n{body}\n", flush=True)


def ask(project, question: str, *, kind: str, context: dict | None = None) -> str | None:
    """Post a question, notify, speak it, and block until `ralph answer` (or timeout). Returns the answer."""
    hcfg, vcfg = project.cfg["hitl"], project.cfg["voice"]
    project.answer_path.unlink(missing_ok=True)
    q = {"kind": kind, "question": question, "ts": time.time(), **(context or {})}
    project.question_path.write_text(json.dumps(q, indent=2))
    title = f"ralph · {project.repo.name}"
    notify(title, question, hcfg, project.repo)
    if hcfg.get("speak", True):
        voice.speak(f"Ralph needs you on {project.repo.name}. {question}", vcfg, block=False)
    print("Answer with:  ralph answer   (voice)   or   ralph answer --text '...'", flush=True)
    timeout = int(hcfg.get("timeout_seconds", 0))
    renotify = int(hcfg.get("renotify_seconds", 600))
    start = last = time.time()
    try:
        while True:
            if project.answer_path.is_file():
                ans = project.answer_path.read_text().strip()
                project.answer_path.unlink(missing_ok=True)
                project.question_path.unlink(missing_ok=True)
                set_attention(project.repo, None)
                project.log(f"human answered: {ans[:200]}")
                return ans
            now = time.time()
            if timeout and now - start > timeout:
                project.log("no answer before hitl.timeout_seconds; stopping")
                return None
            if renotify and now - last > renotify:
                notify(title, "(still waiting) " + question, hcfg, project.repo)
                last = now
            time.sleep(1.5)
    except KeyboardInterrupt:
        return None


def pending_question(project) -> dict | None:
    if project.question_path.is_file():
        return json.loads(project.question_path.read_text())
    return None
