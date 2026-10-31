#!/usr/bin/env bash
# Stack-agnostic quality gates: setup -> lint -> typecheck -> test.
#
# For each gate, the first match wins:
#   1. RALPH_GATE_<NAME>        explicit command from [gates] in .ralph/config.toml
#   2. make / just target       lint | typecheck (type-check, check-types) | test (test-unit, unit)
#   3. detected stacks          Node/TS, Python, Go, Rust (all detected stacks run; monorepo-friendly at the root)
#
# A gate command exiting 99 means "nothing to run" (e.g. pytest found no tests) and is reported as MISSING.
# Gates listed in RALPH_GATES_REQUIRE fail the run when MISSING. Output ends with one "GATE <name> <RESULT>" per gate.
set -uo pipefail

REQUIRE=" ${RALPH_GATES_REQUIRE-lint typecheck test} "
TMP="$(mktemp -d)"
declare -A CMDS RESULT
GATES=(setup lint typecheck test)

add() { CMDS[$1]+="$2"$'\n'; }
have() { command -v "$1" >/dev/null 2>&1; }
export PATH="$HOME/.local/bin:$PATH"

# Sandboxes start from the agent's base image; install missing gate tools on first use.
# They persist for the life of the task's sandbox (one microVM per task).
need_py_tools() {
  local need=() t
  for t in ruff mypy; do have "$t" || need+=("$t"); done
  python3 -c 'import pytest' 2>/dev/null || need+=(pytest)
  [ ${#need[@]} -eq 0 ] && return 0
  python3 -m pip install -q --user --break-system-packages "${need[@]}" 2>/dev/null \
    || python3 -m pip install -q --user "${need[@]}"
}

make_target() {   # prints the first existing make/just target among the arguments
  local t
  for t in "$@"; do
    if [ -f Makefile ] && grep -qE "^${t}:" Makefile; then echo "make $t"; return 0; fi
    if have just && { [ -f justfile ] || [ -f Justfile ] || [ -f .justfile ]; } \
       && just --summary 2>/dev/null | tr ' ' '\n' | grep -qx "$t"; then echo "just $t"; return 0; fi
  done
  return 1
}

export -f have need_py_tools

# ------------------------------------------------------------------ Node / TypeScript
detect_node() {
  [ -f package.json ] || return 0
  local pm=npm install="npm install --no-audit --no-fund"
  if   [ -f pnpm-lock.yaml ]; then pm=pnpm; install="pnpm install --frozen-lockfile"
  elif [ -f yarn.lock ];      then pm=yarn; install="yarn install --immutable || yarn install --frozen-lockfile"
  elif [ -f bun.lockb ] || [ -f bun.lock ]; then pm=bun; install="bun install --frozen-lockfile"
  elif [ -f package-lock.json ]; then install="npm ci --no-audit --no-fund"
  fi
  script() { jq -er --arg n "$1" '.scripts[$n] // empty' package.json >/dev/null 2>&1; }
  first_script() { local s; for s in "$@"; do script "$s" && { echo "$s"; return 0; }; done; return 1; }

  add setup "$install"
  local s
  if s=$(first_script lint lint:check); then add lint "$pm run $s"
  elif ls eslint.config.* .eslintrc* >/dev/null 2>&1; then add lint "eslint ."
  elif ls biome.json biome.jsonc >/dev/null 2>&1; then add lint "biome check ."
  fi
  if s=$(first_script typecheck type-check check-types tsc); then add typecheck "$pm run $s"
  elif [ -f tsconfig.json ]; then add typecheck "{ if command -v tsc >/dev/null; then tsc --noEmit; else npx -y -p typescript tsc --noEmit; fi; }"
  fi
  if s=$(first_script test:unit unit test); then
    if [ "$s" = test ] && jq -er '.scripts.test' package.json | grep -q 'no test specified'; then :; else
      add test "$pm run $s"
    fi
  fi
}

# ------------------------------------------------------------------ Python
mypy_cmd() {  # run mypy against the project interpreter, whether mypy lives in .venv or in the image
  echo "{ if python -c 'import mypy' 2>/dev/null; then python -m mypy $* .; else mypy --python-executable \"\$(command -v python)\" $* .; fi; }"
}
detect_python() {
  { [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] || ls requirements*.txt >/dev/null 2>&1; } || return 0
  add setup "need_py_tools"
  if [ -f uv.lock ]; then
    add setup "uv sync --frozen --all-extras"
  elif [ ! -d .venv ]; then
    local req="python3 -m venv --system-site-packages .venv"
    if [ -f pyproject.toml ] || [ -f setup.py ]; then
      req+=" && { .venv/bin/pip install -q -e '.[dev]' 2>/dev/null || .venv/bin/pip install -q -e . ; }"
    fi
    for f in requirements.txt requirements-dev.txt requirements/dev.txt; do
      [ -f "$f" ] && req+=" && .venv/bin/pip install -q -r $f"
    done
    add setup "$req"
  fi
  # the test runner must live in the project interpreter so it can import the project
  add setup "{ [ ! -x .venv/bin/python ] || .venv/bin/python -c 'import pytest' 2>/dev/null || .venv/bin/python -m pip install -q pytest; }"
  add lint "ruff check ."
  if { [ -f pyrightconfig.json ] || grep -q '^\[tool\.pyright\]' pyproject.toml 2>/dev/null; } && have pyright; then
    add typecheck "pyright"
  elif [ -f mypy.ini ] || [ -f .mypy.ini ] || grep -q '^\[tool\.mypy\]' pyproject.toml 2>/dev/null; then
    add typecheck "$(mypy_cmd)"
  else
    add typecheck "$(mypy_cmd --ignore-missing-imports)"
  fi
  add test "{ python -m pytest -q; rc=\$?; [ \$rc -eq 5 ] && exit 99; exit \$rc; }"
}

# ------------------------------------------------------------------ Go
detect_go() {
  [ -f go.mod ] || return 0
  add setup "go mod download"
  if have golangci-lint; then add lint "golangci-lint run"
  else add lint "go vet ./... && test -z \"\$(gofmt -l .)\" || { gofmt -l .; exit 1; }"
  fi
  add typecheck "go build ./..."
  add test "go test ./..."
}

# ------------------------------------------------------------------ Rust
detect_rust() {
  [ -f Cargo.toml ] || return 0
  add setup "cargo fetch"
  add lint "cargo fmt --all --check && cargo clippy --all-targets -- -D warnings"
  add typecheck "cargo check --all-targets"
  add test "cargo test"
}

# ------------------------------------------------------------------ resolve
declare -A SRC
for g in "${GATES[@]}"; do
  var="RALPH_GATE_${g^^}"
  if [ -n "${!var:-}" ]; then CMDS[$g]="${!var}"$'\n'; SRC[$g]=config; fi
done
for pair in "lint:lint" "typecheck:typecheck type-check check-types" "test:test-unit unit test" "setup:setup install"; do
  g="${pair%%:*}"; [ -n "${SRC[$g]:-}" ] && continue
  if cmd=$(make_target ${pair#*:}); then CMDS[$g]="$cmd"$'\n'; SRC[$g]="make"; fi
done
declare -A BEFORE
for g in "${GATES[@]}"; do BEFORE[$g]="${CMDS[$g]:-}"; done
detect_node; detect_python; detect_go; detect_rust
for g in "${GATES[@]}"; do   # explicit/make sources win: drop anything stack detection appended to them
  if [ -n "${SRC[$g]:-}" ]; then CMDS[$g]="${BEFORE[$g]}"; else SRC[$g]=detected; fi
done

# ------------------------------------------------------------------ run
run_gate() {
  local g="$1" cmds="${CMDS[$1]:-}" log="$TMP/$1.log" rc=0 missing=0
  if [ -z "${cmds//[$'\n ']/}" ]; then RESULT[$g]=MISSING; return; fi
  [ -d .venv ] && export PATH="$PWD/.venv/bin:$PATH" VIRTUAL_ENV="$PWD/.venv"
  [ -d node_modules/.bin ] && export PATH="$PWD/node_modules/.bin:$PATH"
  echo "── $g (${SRC[$g]})"
  while IFS= read -r c; do
    [ -z "$c" ] && continue
    echo "\$ $c" >>"$log"
    bash -c "$c" >>"$log" 2>&1; rc=$?
    if [ $rc -eq 99 ]; then missing=1; continue; fi
    [ $rc -ne 0 ] && break
  done <<<"$cmds"
  if [ $rc -ne 0 ] && [ $rc -ne 99 ]; then
    RESULT[$g]=FAIL
    echo "   FAILED (exit $rc). Last lines:"; tail -n 80 "$log" | sed 's/^/   /'
  elif [ $missing -eq 1 ]; then RESULT[$g]=MISSING
  else RESULT[$g]=PASS
  fi
}

fail=0
for g in "${GATES[@]}"; do
  run_gate "$g"
  if [ "$g" = setup ] && [ "${RESULT[setup]}" = FAIL ]; then
    for x in lint typecheck test; do RESULT[$x]=SKIP; done; fail=1; break
  fi
done

echo
for g in "${GATES[@]}"; do
  r="${RESULT[$g]:-SKIP}"
  [ "$g" = setup ] && [ "$r" = MISSING ] && r=SKIP
  if [ "$r" = MISSING ] && [[ "$REQUIRE" == *" $g "* ]]; then
    echo "   $g: no $g command found. Add one (package script, make target, or [gates].$g in .ralph/config.toml)."
    fail=1
  fi
  [ "$r" = FAIL ] && fail=1
  echo "GATE $g $r"
done
rm -rf "$TMP"
exit $fail
