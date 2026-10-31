"""Layered configuration: defaults < global < project < CLI overrides."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parent
DEFAULTS = PKG_DIR / "defaults.toml"
GLOBAL = Path(os.environ.get("RALPH_GLOBAL_CONFIG", "~/.config/ralph/config.toml")).expanduser()
PHASES = ("planning", "development", "test", "review", "fix")


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def set_dotted(cfg: dict, dotted: str, raw: str) -> None:
    """Apply `--set run.max_iterations=10` style overrides. Value parsed as TOML."""
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    node = cfg
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


class Config:
    def __init__(self, data: dict, sources: list[str]):
        self.data = data
        self.sources = sources

    @classmethod
    def load(cls, repo: Path | None, overrides: list[str] | None = None) -> "Config":
        data = _load(DEFAULTS)
        sources = [str(DEFAULTS)]
        for p in (GLOBAL, (repo / ".ralph" / "config.toml") if repo else None):
            if p and p.is_file():
                data = deep_merge(data, _load(p))
                sources.append(str(p))
        for item in overrides or []:
            if "=" not in item:
                raise SystemExit(f"--set expects key=value, got {item!r}")
            k, v = item.split("=", 1)
            set_dotted(data, k.strip(), v.strip())
            sources.append(f"--set {item}")
        return cls(data, sources)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data.get(section, {})

    def agent(self, phase: str) -> dict[str, Any]:
        agents = self.data.get("agents", {})
        base = agents.get("default", {})
        if phase == "fix":  # fix inherits development, then its own overrides
            base = deep_merge(base, agents.get("development", {}))
        return deep_merge(base, agents.get(phase, {}))
