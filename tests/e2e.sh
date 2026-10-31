#!/usr/bin/env bash
# End-to-end: PRD -> plan -> develop/test/review (with a review round-trip) -> PR -> CI failure + review
# comment -> fix -> merge -> next task with a human question answered -> merge -> plan empty.
# Uses a fake agent and a fake `gh`; git, worktrees, gates and the orchestrator are real.
set -euo pipefail
KIT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
export PATH="$KIT/tests/fixtures:$KIT/bin:$PATH" FAKE_GH_DB="$W/gh.json" FAKE_GH_REMOTE="$W/remote.git" FAKE_SBX_DB="$W/sbx.json"
export RALPH_GLOBAL_CONFIG=/nonexistent
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

git init -q --bare -b main "$W/remote.git"
git clone -q "$W/remote.git" "$W/proj" 2>/dev/null
cd "$W/proj"
echo "# demo" > README.md && git add . && git commit -qm init && git push -q origin main
git remote set-head origin main

ralph init >/dev/null
printf '# PRD\nGreet people, in color.\n' > .ralph/prd.md
cat > .ralph/config.toml <<EOF
[run]
max_iterations = 25
merge_poll_seconds = 0
[budget]
plan_usd = 100
ledger = "$W/ledger.jsonl"
[sandbox]
engine = "sbx"
[agents.default]
cli = "command"
command = ["python3", "$KIT/tests/fixtures/fake_agent.py", "{model}", "$W/calls.json"]
cost_regex = "COST=([0-9.]+)"
[gates]
lint = "true"
typecheck = "true"
test = "ls *.txt >/dev/null"
[hitl]
notify = []
speak = false
EOF
git add .ralph && git commit -qm "ralph config" && git push -q origin main

# a "human" that answers by text when ralph asks (stands in for `ralph answer` by voice)
( for _ in $(seq 1 600); do
    if [ -f .ralph/state/question.json ]; then ralph answer --text "green please" >/dev/null; break; fi
    sleep 0.2
  done ) &

set +e; ralph run > "$W/run.log" 2>&1; code=$?; set -e
git fetch -q origin; git log origin/main --format="%h %s | %(trailers:key=Ralph-Phase,valueonly,separator=)" > "$W/hist"; cat "$W/hist"
cat "$W/run.log"
echo "────────── assertions"
fail() { echo "✗ $*"; exit 1; }
[ $code -eq 0 ] || fail "exit code $code"
grep -q "plan is empty" "$W/run.log" || fail "did not finish the plan"
ralph status | grep -q "2/2 tasks merged" || fail "status not 2/2"
grep -q "\[x\] add-greeting" .ralph/plan.md && grep -q "\[x\] add-color" .ralph/plan.md || fail "plan marks"
git fetch -q origin
git show origin/main:add-greeting.txt | grep -q "fixed after CI" || fail "fix not merged"
git show origin/main:add-color.txt >/dev/null || fail "task 2 not merged"
[ "$(git log origin/main --format=%s | grep -c 'Merge PR')" = 2 ] || fail "expected 2 merges"
grep -q '| fix' "$W/hist" || fail "missing trailers"
grep -q "COMMENT <!-- ralph -->" "$W/gh.json" || fail "no PR reply after fix"
grep -q "reviewer requested changes" .ralph/state/logs/* 2>/dev/null || grep -q "CHANGES" "$W/run.log" || fail "no review loop"
[ -z "$(git worktree list | grep -v "$W/proj ")" ] || fail "worktrees not cleaned: $(git worktree list)"
python3 - "$W/sbx.json" <<'PY' || fail "sandbox lifecycle"
import json, sys
db = json.load(open(sys.argv[1]))
created = [l for l in db["log"] if l.startswith("create")]
assert len(created) == 3, created                    # planner + one per task
assert db["boxes"] == {}, db["boxes"]                 # all removed after merge
assert "ralph-proj-add-greeting" in db["policy"], db["policy"]
assert sum(1 for l in db["log"] if l.startswith("exec")) >= 20
PY
python3 - "$W/ledger.jsonl" <<'PY' || fail "ledger"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert len(rows) >= 9 and abs(sum(r["cost"] for r in rows) - 0.25 * len(rows)) < 1e-9, rows
PY
ralph log -n 5
echo "✔ e2e passed ($(wc -l < "$W/ledger.jsonl") agent iterations)"
