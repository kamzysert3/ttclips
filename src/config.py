"""Shared paths + config loading for ttclips."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
CLIPS_DIR = DATA_DIR / "clips"
LOGS_DIR = DATA_DIR / "logs"
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
    env_file = ROOT / ".env"
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