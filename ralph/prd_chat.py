"""Write the PRD by talking: Ralph interviews you out loud, you answer out loud, Ralph drafts, you approve.

Voice commands (short utterances, so they never fire in the middle of a real answer):
  done / write it        -> Ralph drafts the PRD from the conversation
  approve / save it      -> (after a draft) saves it to .ralph/prd.md
  repeat / say again     -> Ralph repeats the last thing it said
  scratch that / undo    -> forgets your last answer and asks that question again
  pause / later          -> saves the conversation; `ralph prd --resume` continues it
Anything else during review is taken as a change request for the draft.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .loop import Runner, StopRun
from .state import Project
from .voice import Voice

WORD_LIMIT_FOR_COMMANDS = 5


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s']", " ", text.lower()).strip()


class PrdChat:
    def __init__(self, project: Project, *, typed: bool = False, mute: bool = False, resume: bool = False):
        self.p = project
        self.cfg = project.cfg
        self.words = self.cfg["prd_chat"]
        self.runner = Runner(project)
        self.voice = Voice(self.cfg["voice"])
        self.typed, self.mute = typed, mute
        self.session_path = project.state_dir / "prd-session.json"
        # outside the repo: the repo itself is mounted read-only alongside this (rw) scratch
        # dir, and a writable mount nested inside a read-only mount of its own ancestor breaks sbx
        self.scratch = (project.repo.parent / f".{project.repo.name}.ralph-scratch" / "prd").resolve()
        self.box = self.runner.box("prd")
        if resume and self.session_path.is_file():
            self.s = json.loads(self.session_path.read_text())
        else:
            self.s = {"turns": [], "phase": "interview", "draft": "", "started": time.time()}
        self.agent_state: dict = {"iterations": 0}

    # ------------------------------------------------------------------ io
    def say(self, text: str) -> None:
        print(f"\n🤖 Ralph: {text}", flush=True)
        if not self.mute:
            self.voice.speak(text)

    def hear(self) -> str:
        if self.typed:
            try:
                return input("🧑 you> ").strip()
            except EOFError:
                return "pause"
        for _ in range(3):
            text = self.voice.hear()
            if text:
                print(f"🧑 you: {text}", flush=True)
                return text
            print("(didn't catch that, go ahead)", flush=True)
        return ""

    def intent(self, text: str) -> str | None:
        t = _norm(text)
        if not t or len(t.split()) > WORD_LIMIT_FOR_COMMANDS:
            return None
        for name in ("approve", "done", "repeat", "undo", "pause"):
            for phrase in self.words.get(f"{name}_words", []):
                if re.search(rf"(^|\s){re.escape(_norm(phrase))}(\s|$)", t):
                    return name
        return None

    # ------------------------------------------------------------------ state
    def save(self) -> None:
        self.session_path.write_text(json.dumps(self.s, indent=2, ensure_ascii=False))

    def add(self, role: str, text: str) -> None:
        self.s["turns"].append({"role": role, "text": text})
        self.save()

    def transcript(self) -> str:
        if not self.s["turns"]:
            return "(nothing yet: open the conversation with your first question)"
        return "\n".join(f"{'Ralph' if t['role'] == 'ralph' else 'Human'}: {t['text']}" for t in self.s["turns"])

    def last_ralph(self) -> str:
        for t in reversed(self.s["turns"]):
            if t["role"] == "ralph":
                return t["text"]
        return ""

    # ------------------------------------------------------------------ agent
    def think(self, mode: str) -> tuple[str, str]:
        self.runner.check_limits(iterations=False)
        prd = self.p.find_prd()
        (self.scratch / "prd.md").unlink(missing_ok=True)
        print("   …thinking", flush=True)
        ctx = {
            "mode": mode, "repo_path": str(self.p.repo), "transcript": self.transcript(),
            "prd": prd.read_text() if prd else "(none yet)", "draft": self.s.get("draft") or "(none yet)",
        }
        out, _ = self.runner.agent_step("interview", None, self.agent_state, self.p.repo, ctx,
                                        box=self.box, run_dir=self.scratch, count=False)
        if out.status == "DONE":
            draft = self.scratch / "prd.md"
            if draft.is_file() and draft.read_text().strip():
                self.s["draft"] = draft.read_text()
                self.s["phase"] = "review"
            else:
                return "ERROR", "I meant to write the draft but it didn't come out. Shall I try again?"
        if out.status not in ("ASK", "DONE"):
            detail = out.body.strip()
            reason = f" ({detail})" if detail else ""
            return "ERROR", f"Sorry, I lost my train of thought{reason}. Could you say that again?"
        return out.status, out.body.strip() or "Go on."

    # ------------------------------------------------------------------ conversation
    def run(self) -> int:
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.runner.sandbox.ensure(self.box, self.scratch, [(self.p.repo, "ro")])
        try:
            return self._loop()
        except StopRun as e:
            self.save()
            self.say(f"I have to stop: {e.reason}. The conversation is saved.")
            return e.code
        except KeyboardInterrupt:
            self.save()
            print("\n(paused: `ralph prd --resume` continues)")
            return 130
        finally:
            self.runner.sandbox.remove(self.box)

    def _loop(self) -> int:
        if self.s["turns"]:
            say = "Welcome back. " + (self.last_ralph() or "Where were we?")
            if self.s["phase"] == "review":
                self.show_draft()
        else:
            status, say = self.think("interview")
            self.add("ralph", say)
        max_turns = int(self.words.get("max_turns", 40))
        while True:
            self.say(say)
            heard = self.hear()
            if not heard:
                say = "I can't hear you. I'll pause here; run ralph prd with resume when you're ready."
                self.say(say)
                return 0
            it = self.intent(heard)
            if it == "repeat":
                continue
            if it == "pause":
                self.save()
                self.say("Paused. Run ralph prd with resume to pick this up.")
                return 0
            if it == "undo":
                say = self.undo()
                continue

            if self.s["phase"] == "review":
                if it in ("approve", "done"):
                    path = self.write_prd()
                    self.say(f"Saved to {path.relative_to(self.p.repo)}. When you're ready, run ralph run.")
                    self.session_path.unlink(missing_ok=True)
                    return 0
                self.add("human", heard)
                status, say = self.think("revise")
            else:
                self.add("human", heard)
                answers = sum(1 for t in self.s["turns"] if t["role"] == "human")
                mode = "finalize" if it in ("done", "approve") or answers >= max_turns else "interview"
                status, say = self.think(mode)
            if status == "ERROR":
                self.s["turns"].pop()  # let the human repeat that answer
                self.save()
                continue
            self.add("ralph", say)
            if status == "DONE":
                self.show_draft()

    def undo(self) -> str:
        turns = self.s["turns"]
        # drop the current question and the human answer before it; ask the previous question again
        if turns and turns[-1]["role"] == "ralph":
            turns.pop()
        if turns and turns[-1]["role"] == "human":
            turns.pop()
        self.save()
        return "Okay, forgotten. " + (self.last_ralph() or "Let's start over: what do you want to build?")

    def show_draft(self) -> None:
        print("\n" + "─" * 72 + "\n" + self.s["draft"].rstrip() + "\n" + "─" * 72)

    def write_prd(self) -> Path:
        prd = self.p.find_prd() or (self.p.repo / self.cfg["prd"]["paths"][0])
        prd.parent.mkdir(parents=True, exist_ok=True)
        if prd.exists():
            backup = self.p.state_dir / f"prd.backup-{time.strftime('%Y%m%d-%H%M%S')}.md"
            backup.write_text(prd.read_text())
            subprocess.run(["git", "--no-pager", "diff", "--no-index", "--stat", str(backup), str(prd)],
                           capture_output=True)
        prd.write_text(self.s["draft"].rstrip() + "\n")
        transcript = self.p.state_dir / f"prd-conversation-{time.strftime('%Y%m%d-%H%M%S')}.md"
        transcript.write_text(self.transcript() + "\n")
        print(f"\n✔ {prd.relative_to(self.p.repo)} written · transcript kept in {transcript.relative_to(self.p.repo)}")
        print("  Commit it when you're happy:  git add .ralph/prd.md && git commit -m 'docs: PRD'")
        return prd
