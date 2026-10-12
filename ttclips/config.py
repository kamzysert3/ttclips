"""Shared paths + config loading for ttclips.

Everything is resolved from a single *home* directory:

    $TTCLIPS_HOME  ->  current working directory

This lets an installed CLI run against any project folder (``ttclips init``)
while a source checkout keeps working unchanged when the process CWD is the
repo root (``config/`` and ``data/`` live there).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


def resolve_home() -> Path:
    """Return the ttclips home dir ($TTCLIPS_HOME or the current directory)."""
    raw = os.environ.get("TTCLIPS_HOME") or os.getcwd()
    home = Path(raw).expanduser()
    try:
        home = home.resolve()
    except OSError:  # pragma: no cover - unusual filesystem edge case
        pass
    return home


HOME = resolve_home()
CONFIG_DIR = HOME / "config"
DATA_DIR = HOME / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
CLIPS_DIR = DATA_DIR / "clips"
LOGS_DIR = DATA_DIR / "logs"
BROLL_DIR = DATA_DIR / "broll"
PROBE_DIR = DATA_DIR / "probe"
STATE_FILE = DATA_DIR / "posted.json"
QUEUE_FILE = DATA_DIR / "queue.json"
COOKIES_FILE = DATA_DIR / "cookies.txt"
LOG_FILE = LOGS_DIR / "ttclips.log"

for _d in (DATA_DIR, DOWNLOADS_DIR, CLIPS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_channels() -> dict:
    return load_json(CONFIG_DIR / "channels.json", default={"channels": []})


def load_posting_window() -> list[dict]:
    data = load_json(CONFIG_DIR / "posting_window.json", default={"slots": []})
    return data.get("slots", [])


def load_tiktok_config() -> dict:
    return load_json(CONFIG_DIR / "tiktok.json", default={})


def load_env() -> dict:
    """Minimal key=value dotenv loader (no external dep needed)."""
    env_file = HOME / ".env"
    if not env_file.exists():
        return {}
    out = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


RESERVED_NAMES = {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
RESERVED_NAMES.update({"CON", "PRN", "AUX", "NUL"})


def safe_stem(name: str) -> str:
    """Make a string safe as a Windows filename stem (no path separators / illegal chars)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned[:60] or "clip"
