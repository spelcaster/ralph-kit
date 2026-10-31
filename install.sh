#!/usr/bin/env bash
# Install ralph on any Linux distro. Never calls a distro package manager or sudo:
# it checks what's present and tells you what to install yourself.
#
#   ./install.sh                       CLI + global config + dependency check
#   ./install.sh --voice               + voice: piper and faster-whisper in ralph's own venv, a piper voice
#   ./install.sh --voice --lang pt_BR  Portuguese (Brazil) voice instead of English
#
# Only two things are installed, both under your home directory:
#   ~/.local/share/ralph/venv          Python venv (pip, not the distro): faster-whisper, piper-tts
#   ~/.local/share/ralph/voices        the piper voice model
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${RALPH_BIN_DIR:-$HOME/.local/bin}"
SHARE="$HOME/.local/share/ralph"
VOICE=0; LANG_CODE="en_US"
while [ $# -gt 0 ]; do
  case "$1" in
    --voice) VOICE=1 ;;
    --lang) LANG_CODE="${2:?--lang needs a value, e.g. pt_BR}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac; shift
done

MISSING_REQ=(); MISSING_OPT=()
step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
miss() { printf '  \033[31m✗\033[0m %s\n' "$1"; MISSING_REQ+=("$1|$2"); }
opt()  { printf '  \033[33m–\033[0m %s\n' "$1"; MISSING_OPT+=("$1|$2"); }
have() { command -v "$1" >/dev/null 2>&1; }
need() {  # need <executable> <what it's for> <how to get it>
  if have "$1"; then ok "$1 ($2)"; else miss "$1 ($2)" "$3"; fi
}
want() {
  if have "$1"; then ok "$1 ($2)"; else opt "$1 ($2)" "$3"; fi
}
first_of() {  # first_of <label> <hint> exe...  — at least one must exist
  local label="$1" hint="$2" e; shift 2
  for e in "$@"; do have "$e" && { ok "$label: $e"; return 0; }; done
  miss "$label (one of: $*)" "$hint"; return 1
}

[ "$(uname -s)" = Linux ] || { echo "this version of ralph targets Linux only"; exit 1; }

# ------------------------------------------------------------------ core
step "core"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -n "$PY" ]; then
  ok "python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
  if "$PY" -c 'import venv, ensurepip' 2>/dev/null; then ok "python venv + ensurepip"
  else miss "python venv/ensurepip module" "your distro splits it out of python (often a package named python3-venv)"; fi
else
  miss "python 3.11+" "install Python 3.11 or newer"
fi
need git "worktrees, commits" "https://git-scm.com/downloads"
need gh "opening and tracking pull requests" "https://cli.github.com (then: gh auth login)"
want tmux "notifications + push-to-talk keys" "https://github.com/tmux/tmux/wiki/Installing"
want curl "downloading the voice model" "install curl"

# ------------------------------------------------------------------ sandboxes
step "Docker Sandboxes"
need sbx "runs every agent in a microVM" "https://docs.docker.com/ai/sandboxes/install/ (then: sbx login)"
if [ -e /dev/kvm ]; then
  ok "/dev/kvm present"
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then ok "/dev/kvm accessible by $USER"
  else miss "access to /dev/kvm" "add yourself to the kvm group (e.g. usermod -aG kvm $USER) and log in again"; fi
else
  miss "/dev/kvm (KVM virtualization)" "enable virtualization in firmware, or nested virtualization if this is a VM"
fi
if have sbx; then
  if sbx ls --json >/dev/null 2>&1; then ok "sbx daemon reachable and signed in"
  else opt "sbx not signed in / daemon not running" "run: sbx login"; fi
  if sbx secret ls 2>/dev/null | grep -q anthropic; then ok "anthropic credential stored in sbx"
  else opt "no anthropic credential in sbx" "run: sbx secret set -g anthropic"; fi
fi

# ------------------------------------------------------------------ notifications (all optional)
step "notifications"
want notify-send "desktop pop-ups (optional, Ghostty notifications work without it)" "libnotify"

# ------------------------------------------------------------------ voice
if [ "$VOICE" = 1 ]; then
  step "voice: audio devices"
  if first_of "microphone recorder" \
      "install sox (recommended: auto-stops on silence), or PipeWire (pw-record) / alsa-utils (arecord)" \
      rec pw-record arecord && have rec; then
    if sox -h 2>/dev/null | grep -A1 -i 'AUDIO DEVICE DRIVERS' | grep -qiE 'pulseaudio|alsa'; then
      ok "sox can talk to pulseaudio/alsa"
    else
      opt "sox has no pulseaudio/alsa driver" "install sox's pulseaudio or alsa format plugin"
    fi
  fi
  first_of "audio player" "install PipeWire (pw-play), pulseaudio-utils (paplay) or alsa-utils (aplay)" paplay pw-play aplay
  want espeak-ng "fallback voice if piper isn't set up" "install espeak-ng"
fi

# ------------------------------------------------------------------ install what we own (home dir only)
step "ralph"
if [ -n "$PY" ] && "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
  if [ ! -x "$SHARE/venv/bin/python" ]; then
    "$PY" -m venv "$SHARE/venv" && ok "venv created at $SHARE/venv" || miss "venv creation" "check that $PY can create venvs"
  else
    ok "venv $SHARE/venv"
  fi
fi
mkdir -p "$BIN"; ln -sf "$KIT/bin/ralph" "$BIN/ralph" && ok "linked $BIN/ralph"
case ":$PATH:" in *":$BIN:"*) ;; *) opt "$BIN is not on PATH" "add to your shell profile: export PATH=\"$BIN:\$PATH\"";; esac

CFG="$HOME/.config/ralph/config.toml"
voice_name="en_US-lessac-medium"; voice_path="en/en_US/lessac/medium"; lang="en"
if [ "$LANG_CODE" = pt_BR ]; then voice_name="pt_BR-faber-medium"; voice_path="pt/pt_BR/faber/medium"; lang="pt-BR"; fi
if [ ! -f "$CFG" ]; then
  mkdir -p "$(dirname "$CFG")"
  cat >"$CFG" <<EOF
# Global ralph config: applies to every project unless .ralph/config.toml overrides it.
[budget]
plan_usd = 200.0     # USD-equivalent of your plan per window
ceiling_pct = 90
window = "month"

[run]
language = "$lang"

[voice]
piper_model = "$SHARE/voices/$voice_name.onnx"
stt_language = "auto"
EOF
  ok "wrote $CFG"
else
  ok "kept existing $CFG"
fi

if [ "$VOICE" = 1 ] && [ -x "$SHARE/venv/bin/python" ]; then
  step "voice: speech models (installed into ralph's venv with pip)"
  VPY="$SHARE/venv/bin/python"
  if "$VPY" -c 'import faster_whisper' 2>/dev/null; then ok "faster-whisper already installed"
  elif "$VPY" -m pip install -q faster-whisper; then ok "faster-whisper installed"
  else miss "faster-whisper" "pip could not install it; check network access to pypi.org"; fi
  if [ -x "$SHARE/venv/bin/piper" ]; then ok "piper already installed"
  elif "$VPY" -m pip install -q piper-tts; then ok "piper installed"
  else miss "piper-tts" "pip could not install it; check network access to pypi.org"; fi
  # faster-whisper's GPU backend (ctranslate2) dlopen's libcublas.so.12 / libcudnn.so.9 at runtime. If the
  # system CUDA install is a different major version, pip the exact libs into the venv instead of touching
  # the system one; bin/ralph adds them to LD_LIBRARY_PATH when present.
  if have nvidia-smi && ! ldconfig -p 2>/dev/null | grep -q 'libcublas\.so\.12'; then
    if "$VPY" -c 'import nvidia.cublas.lib, nvidia.cudnn.lib' 2>/dev/null; then
      ok "cuBLAS 12 / cuDNN 9 already in the venv"
    elif "$VPY" -m pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"; then
      ok "cuBLAS 12 / cuDNN 9 installed into the venv (~1.5GB download; your system CUDA is untouched)"
    else
      opt "cuBLAS 12 / cuDNN 9 for the venv" "pip could not install them; faster-whisper falls back to CPU"
    fi
  fi
  [ -x "$SHARE/venv/bin/piper" ] && ln -sf "$SHARE/venv/bin/piper" "$BIN/piper"
  mkdir -p "$SHARE/voices"
  for ext in onnx onnx.json; do
    f="$SHARE/voices/$voice_name.$ext"
    if [ -s "$f" ]; then ok "voice $voice_name.$ext"
    elif have curl && curl -fsSL -o "$f.part" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/$voice_path/$voice_name.$ext"; then
      mv "$f.part" "$f" && ok "downloaded voice $voice_name.$ext"
    else
      rm -f "$f.part"
      miss "piper voice $voice_name.$ext" "download https://huggingface.co/rhasspy/piper-voices/resolve/main/$voice_path/$voice_name.$ext into $SHARE/voices/"
    fi
  done
  echo "  (the speech-recognition model downloads on first use)"
fi

# ------------------------------------------------------------------ summary
step "summary"
if [ ${#MISSING_REQ[@]} -eq 0 ]; then
  ok "all required dependencies found"
else
  echo "  Install these yourself, then re-run ./install.sh:"
  for m in "${MISSING_REQ[@]}"; do printf '   \033[31m✗\033[0m %-46s %s\n' "${m%%|*}" "${m#*|}"; done
fi
if [ ${#MISSING_OPT[@]} -gt 0 ]; then
  echo "  Optional / to do:"
  for m in "${MISSING_OPT[@]}"; do printf '   \033[33m–\033[0m %-46s %s\n' "${m%%|*}" "${m#*|}"; done
fi
cat <<EOF

  Next:
    sbx login                        choose the "Locked Down" network policy
    sbx secret set -g anthropic      model credential (API key; for a Claude subscription see README)
    gh auth login
    echo "source-file $KIT/tmux/ralph.tmux.conf" >> ~/.tmux.conf
    cd your-project && ralph doctor --smoke && ralph init && ralph prd
EOF
[ ${#MISSING_REQ[@]} -eq 0 ]
