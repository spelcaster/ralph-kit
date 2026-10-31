"""Voice for Linux: record (sox / PipeWire / ALSA) -> speech-to-text, and text-to-speech -> speakers.

Every backend is pluggable through [voice] in the config. A Voice object keeps the speech-to-text model
loaded, which is what makes a back-and-forth conversation feel responsive.
"""
from __future__ import annotations

import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any


def speakable(text: str, limit: int = 900) -> str:
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"[*_#>\[\]|]", "", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "... The rest is on screen."
    return t


def _read_wav_f32(wav: Path):
    """Read a 16-bit PCM wav straight into a float32 numpy array, mono.

    Bypasses faster-whisper's decode_audio(), which calls PyAV's av.open() with a
    metadata_errors= kwarg that newer PyAV releases dropped (SYSTRAN/faster-whisper#...).
    Our recorders (sox/pw-record/arecord) always write 16-bit PCM, so we can decode
    it ourselves with the stdlib and skip PyAV entirely.
    """
    import numpy as np

    with wave.open(str(wav), "rb") as f:
        raw = f.readframes(f.getnframes())
        channels = f.getnchannels()
        width = f.getsampwidth()
    if width != 2:
        raise ValueError(f"expected 16-bit PCM wav, got {width * 8}-bit")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _player(cfg: dict) -> list[str] | None:
    want = cfg.get("player", "auto")
    for name in ([want] if want != "auto" else ["paplay", "pw-play", "aplay"]):
        if shutil.which(name):
            return [name] + (["-q"] if name == "aplay" else [])
    return None


class Voice:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._fw_model: Any = None  # faster-whisper model, loaded once

    # ================================================================ text to speech
    def tts_backend(self) -> str | None:
        want = self.cfg.get("tts", "auto")
        if want != "auto":
            return None if want == "off" else want
        if self.cfg.get("tts_command"):
            return "command"
        if shutil.which("piper") and Path(self.cfg.get("piper_model", "")).expanduser().is_file():
            return "piper"
        if shutil.which("espeak-ng"):
            return "espeak"
        if shutil.which("spd-say"):
            return "spd-say"
        return None

    def speak(self, text: str, block: bool = True) -> bool:
        text = speakable(text)
        backend = self.tts_backend()
        if not text or not backend:
            return False
        if backend == "piper":
            wav = Path(tempfile.mkdtemp(prefix="ralph-tts-")) / "say.wav"
            player = _player(self.cfg)
            if not player:
                print("ralph: no audio player (install pulseaudio-utils, pipewire or alsa-utils)", file=sys.stderr)
                return False
            model = str(Path(self.cfg["piper_model"]).expanduser())
            script = (f"piper --model {shlex.quote(model)} --output_file {shlex.quote(str(wav))} >/dev/null 2>&1 "
                      f"&& {' '.join(map(shlex.quote, player))} {shlex.quote(str(wav))}; rm -f {shlex.quote(str(wav))}")
            return self._run_shell(script, text, block)
        if backend == "espeak":
            voice, rate = self.cfg.get("tts_voice", ""), int(self.cfg.get("tts_rate") or 0)
            cmd = ["espeak-ng"] + (["-v", voice] if voice else []) + (["-s", str(rate)] if rate else []) + [text]
        elif backend == "spd-say":
            cmd = ["spd-say", "-w"] + (["-l", self.cfg["tts_voice"]] if self.cfg.get("tts_voice") else []) + [text]
        elif backend == "command":
            return self._run_shell(self.cfg["tts_command"], text, block)
        else:
            return False
        if block:
            subprocess.run(cmd, capture_output=True)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True

    @staticmethod
    def _run_shell(script: str, text: str, block: bool) -> bool:
        p = subprocess.Popen(script, shell=True, stdin=subprocess.PIPE, text=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not block)
        assert p.stdin is not None
        p.stdin.write(text)
        p.stdin.close()
        if block:
            p.wait()
        return True

    # ================================================================ recording
    def _record_cmd(self, out: Path) -> tuple[list[str] | str, bool]:
        """Returns (command, stops_by_itself)."""
        want = self.cfg.get("record", "auto")
        silence = str(self.cfg.get("silence_seconds", 1.6))
        if want == "command" or (want == "auto" and self.cfg.get("record_command")):
            return self.cfg["record_command"].replace("{out}", shlex.quote(str(out))), True
        if want in ("sox", "auto") and shutil.which("rec"):
            # wait for speech, then stop after `silence` seconds of quiet
            return (["rec", "-q", "-c", "1", "-r", "16000", "-b", "16", str(out),
                     "silence", "1", "0.1", "2%", "1", silence, "2%"], True)
        if want in ("pw-record", "auto") and shutil.which("pw-record"):
            return ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(out)], False
        if want in ("arecord", "auto") and shutil.which("arecord"):
            return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", str(out)], False
        raise SystemExit("ralph: no recorder. Install sox (`sudo apt install sox libsox-fmt-pulse`).")

    def record(self) -> Path:
        out = Path(tempfile.mkdtemp(prefix="ralph-mic-")) / "speech.wav"
        cmd, auto_stop = self._record_cmd(out)
        hint = "stops when you pause, or press Enter" if auto_stop else "press Enter when you're done"
        print(f"🎙  listening… ({hint})", flush=True)
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            while p.poll() is None:
                if sys.stdin.isatty():
                    r, _, _ = select.select([sys.stdin], [], [], 0.15)
                    if r:
                        sys.stdin.readline()
                        break
                else:
                    time.sleep(0.15)
        except KeyboardInterrupt:
            p.send_signal(signal.SIGINT)
            p.wait(timeout=5)
            raise
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if not out.is_file() or out.stat().st_size < 4000:
            return out if out.is_file() else Path("/nonexistent")
        return out

    # ================================================================ speech to text
    def stt_backend(self) -> str:
        want = self.cfg.get("stt", "auto")
        if want != "auto":
            return want
        if self.cfg.get("stt_command"):
            return "command"
        try:
            import faster_whisper  # noqa: F401
            return "faster-whisper"
        except ImportError:
            pass
        if shutil.which("whisper-cli") and Path(self.cfg.get("whisper_model", "")).expanduser().is_file():
            return "whisper-cpp"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai-api"
        raise SystemExit("ralph: no speech-to-text. Run ./install.sh --voice (installs faster-whisper).")

    def transcribe(self, wav: Path) -> str:
        if not wav.is_file():
            return ""
        backend = self.stt_backend()
        lang = self.cfg.get("stt_language", "auto")
        if backend == "faster-whisper":
            if self._fw_model is None:
                from faster_whisper import WhisperModel
                print("(loading speech model…)", flush=True)
                self._fw_model = WhisperModel(self.cfg.get("stt_model", "small"), device="auto", compute_type="int8")
            segments, _ = self._fw_model.transcribe(_read_wav_f32(wav), language=None if lang == "auto" else lang,
                                                    vad_filter=True, beam_size=5)
            out = " ".join(s.text for s in segments)
        elif backend == "whisper-cpp":
            model = str(Path(self.cfg["whisper_model"]).expanduser())
            out = subprocess.run(["whisper-cli", "-m", model, "-f", str(wav), "-nt", "-np", "-l", lang],
                                 capture_output=True, text=True, check=True).stdout
        elif backend == "openai-api":
            out = _openai_transcribe(wav, lang)
        elif backend == "command":
            out = subprocess.run(self.cfg["stt_command"].replace("{wav}", shlex.quote(str(wav))), shell=True,
                                 capture_output=True, text=True, check=True).stdout
        else:
            raise SystemExit(f"unknown voice.stt: {backend}")
        return re.sub(r"\s+", " ", out).strip()

    def hear(self) -> str:
        """One utterance: record + transcribe. Empty string if nothing was said."""
        return self.transcribe(self.record())

    def listen(self, confirm: bool | None = None) -> str:
        """An answer you can review: record, transcribe, then send / redo / edit."""
        confirm = self.cfg.get("confirm", True) if confirm is None else confirm
        while True:
            text = self.hear()
            if not text:
                print("(didn't catch that)")
                continue
            if not confirm or not sys.stdin.isatty():
                return text
            print(f"\n📝 {text}\n")
            choice = input("[Enter] send · [r] record again · [e] edit · [q] cancel > ").strip().lower()
            if choice == "":
                return text
            if choice == "e":
                return input("edit> ").strip() or text
            if choice == "q":
                raise SystemExit(1)


def _openai_transcribe(wav: Path, lang: str) -> str:
    boundary = uuid.uuid4().hex
    parts = [("model", "whisper-1")] + ([("language", lang.split("-")[0])] if lang != "auto" else [])
    body = b""
    for k, v in parts:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["text"]


# convenience wrappers used by the loop / hitl
def speak(text: str, cfg: dict, block: bool = True) -> bool:
    return Voice(cfg).speak(text, block=block)
