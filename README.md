# ralph-kit (Linux, any distro)

A Ralph loop you drive by voice, with every agent running inside **Docker Sandboxes**. You talk the PRD
through with Ralph. Then agents build it one task at a time. Each task runs in its own microVM, on its own
git worktree, and becomes its own pull request. The next task starts only after that PR is merged.

```
 ┌───────────── you talk ─────────────┐
 │  ralph prd: Ralph asks, you answer │  interviewer agent in a sandbox (repo mounted read-only)
 │  "done" → draft · "approve" → save │
 └───────────────┬────────────────────┘
                 ▼
           .ralph/prd.md ──planner (sandbox)──▶ .ralph/plan.md
                                                    │ one task at a time
                                                    ▼
  host: git worktree ../<repo>.worktrees/<task>, branch ralph/<task>
  sbx microVM "ralph-<repo>-<task>"
     <worktree> rw · <repo>/.git ro · gates ro · egress via sbx policy proxy
     development ⇄ gates → test ⇄ gates → review ─ CHANGES ─▶ development
        every iteration: fresh agent process, then a commit made on the host with trailers
                                                    │ APPROVE
                                                    ▼
  host: push + gh pr create ──▶ CI red / review comments / conflicts ──▶ fix (same sandbox) ──▶ push
                                                    │ merged
                                                    ▼
                         sandbox removed · worktree removed · task done · next task
```

## Concepts

| concept | what it means here |
|---|---|
| **PRD** | The source of truth for what to build. You write it by talking (`ralph prd`) or edit `.ralph/prd.md`. |
| **Plan** | `.ralph/plan.md`, an ordered list of small tasks. Each task is sized to be one reviewable PR. Ralph owns the checkboxes: a task is ticked only when its PR is **merged**. |
| **Iteration** | One agent run with fresh context. Its only memory is `<worktree>/.ralph-run/notes.md`, plus git history. After every iteration, Ralph runs the gates and commits. |
| **Phases** | development → test → review for each task, and fix for an open PR. Each phase can use its own agent and model. |
| **Gates** | Lint, typecheck and unit tests, run inside the sandbox after every iteration. The agent can't skip them. Failures become the next iteration's feedback. |
| **Sandbox** | A Docker Sandboxes microVM per task. It can write only to the task's worktree. It sees git metadata read-only. Network goes through the sbx policy proxy. It has no GitHub token. |
| **Human in the loop** | Agents ask by writing `ASK`. Ralph notifies you through tmux, Ghostty and the bell, reads the question aloud, and waits for your spoken answer. |

## Install (any Linux distro)

`install.sh` never runs a package manager or `sudo`. It checks what you already have and lists anything
missing, with a hint for each. You install those yourself and run it again. The only things it installs
go under your home directory: a Python venv with faster-whisper and piper (installed with pip), the
piper voice, and the `ralph` link.

| needed for | executables / files |
|---|---|
| core | python 3.11+ with `venv`, `git`, `gh` |
| sandboxes | `sbx` ([install](https://docs.docker.com/ai/sandboxes/install/)), `/dev/kvm` readable and writable by you |
| voice (`--voice`) | a recorder: `rec` from sox (recommended), `pw-record` or `arecord`; a player: `paplay`, `pw-play` or `aplay` |
| optional | `tmux`, `curl`, `notify-send`, `espeak-ng` |

```bash
./install.sh --voice                 # or: ./install.sh --voice --lang pt_BR   (exit code 1 while something required is missing)
sbx login                            # pick the "Locked Down" network policy when asked
sbx secret set -g anthropic          # model credential, injected by the sbx proxy (never stored in a sandbox)
gh auth login
echo "source-file $PWD/tmux/ralph.tmux.conf" >> ~/.tmux.conf && tmux source ~/.tmux.conf
cd some-git-repo && ralph doctor --smoke
```

`--smoke` starts a real sandbox and asks Claude to reply. That proves KVM, sign-in, credentials, and
network policy all work together.

**Claude subscription instead of an API key:** Docker documents that the sbx proxy can resolve a
subscription for the `anthropic` service. It doesn't spell out how this works for brand-new sandboxes
with nobody watching. If `ralph doctor --smoke` stops at a login prompt, use an API key for unattended
runs.

## Use

```bash
ralph init                 # .ralph/config.toml + PRD template (commit .ralph/)
ralph prd                  # spoken interview → draft → "approve" → .ralph/prd.md
ralph run                  # loop until a stop condition
```

**Talking to Ralph.** Short phrases work as commands. Anything longer than five words always counts as an
answer:

| say | effect |
|---|---|
| "done", "write it", "pronto" | Ralph drafts the PRD from the conversation |
| "approve", "save it", "aprovado" | saves the draft (backup and transcript go in `.ralph/state/`) |
| "repeat", "repete" | Ralph says its last line again |
| "scratch that", "esquece" | forgets your last answer and asks that question again |
| "pause", "later" | saves the conversation; `ralph prd --resume` continues it |

While Ralph shows a draft, whatever you say is treated as a change request.

Other modes: `ralph prd --type` lets you type instead of speak, and `--mute` turns off Ralph's voice. The
phrase lists are in `[prd_chat]`, so you can add your own.

**tmux keys.**

| keys | what happens |
|---|---|
| `prefix v` | Push-to-talk. Answers a pending question, or opens or resumes the PRD conversation. |
| `prefix a` | Answer the pending question by voice. |
| `prefix S` | Hear the status. |
| `prefix R` | Status and recent iterations in a popup. |

**Other commands:** `ralph status`, `ralph log` (timeline from commit trailers), `ralph plan`, `ralph gates`,
`ralph sandboxes [--prune]`, `ralph config`, `ralph answer --text "..."`, `ralph say ...`, `ralph listen`.

## Configuration

Settings are layered, and later layers win: kit defaults in `ralph/defaults.toml`, then
`~/.config/ralph/config.toml`, then `.ralph/config.toml`, then `--set key=value` on the command line.
`ralph config` shows the effective value of every setting and where it came from.

- **Agents per task type:** `[agents.planning|development|test|review|fix|interview]` all inherit from
  `[agents.default]`. Set `cli = "claude"`, or `cli = "command"` with `command = [...]` for any CLI that
  reads the prompt on stdin.
- **Sandbox:** in `[sandbox]` you can set `allow_domains` (applied per sandbox), `kits` (extra tooling,
  passed to `sbx --kit`), `template`, and `memory`/`cpus`. A sandbox already stays up for as long as it's
  doing something — a task's whole dev/test/review loop, one PRD conversation — but it's torn down once
  that's done, so the next `ralph talk`/`prd`/`gates` boots a fresh one. Set `keep = true` to skip that
  teardown and reuse a warm sandbox instead; clean up what it leaves behind with `ralph sandboxes --prune`.
  One sandbox is built per task and reused across all of that task's phases, so if `[agents.*]` uses more
  than one CLI (e.g. codex for `test`, Claude Code for everything else), either install the extra one on
  first use (see Limits below) or set `template` to your own image that ships everything preinstalled.
  `agent` stays a built-in kit name (`"claude"`, `"codex"`, `"shell"`, ...) — it picks which `sbx create`
  subcommand runs, it isn't an image reference. `template` is the image: a registry ref (`sbx create
  --template`), or a local one loaded into the sandbox runtime's own image store first with `sbx template
  load <tar>` (from `docker save`) or `sbx template save <sandbox> <tag>`. Build the image itself by
  extending a `docker/sandbox-templates` base; see
  https://docs.docker.com/ai/sandboxes/customize/author/base-images/.
- **Gates:** `[gates]` takes explicit commands. Otherwise Ralph auto-detects, in this order: make/just
  targets, then Node/TS, Python, Go and Rust. If a required gate has nothing to run, it fails.
- **Stop conditions:**

  | condition | default | how to change |
  |---|---|---|
  | max iterations per run | 25 | `--max-iterations` |
  | budget ceiling | 90% of `plan_usd` per window | `--ceiling`, `--budget`, `--window` |
  | plan empty | stops when every task is merged | — |
  | no PRD found | stops | — |

  Each Claude call gets `--max-budget-usd` set to whatever is left of the budget.
- **Voice:** `[voice]` controls Linux audio: piper or espeak for speech, sox (stops recording when you go
  quiet) or pw-record/arecord for the mic, faster-whisper for transcription (the model stays loaded for
  the whole conversation). For Portuguese, set `language = "pt-BR"` and use the `pt_BR-faber-medium`
  voice.

## Verify

```bash
python3 -m unittest discover -s tests -v   # config, plan, ledger, stop conditions, voice wiring,
                                           # PRD conversation (undo/repeat/revise/approve/pause+resume),
                                           # sandbox mounts, conflicts, merge gating
bash tests/e2e.sh                          # full loop through a fake `sbx` and fake `gh`
```

The fake `sbx` accepts the same commands and flags that Ralph sends to the real one, and it rejects a
working directory that isn't mounted in the sandbox. Real microVMs, audio, and GitHub are what
`ralph doctor --smoke` and your first run exercise.

## Limits

- Only local sandboxes are supported for now. Cloud sandboxes need an API key, and they come later.
- The sandbox starts from the agent's base image. Gate tools (ruff, mypy, pytest, tsc through npx) are
  installed on first use and stay for the life of the task's sandbox. Go and Rust toolchains, and any
  non-default `[agents.*]` CLI (e.g. codex), have to come from a kit or a custom `template` image.
- Monorepos: auto-detection only looks at the repo root, so set `[gates]` commands explicitly.
