"""Binary resolution helpers: locate ffmpeg/ffprobe for yt-dlp + encoder.

Resolution order: explicit env override (FFMPEG_BIN/FFPROBE_BIN) -> winget
Gyan.FFmpeg install -> system PATH. Returns None when nothing is found so
callers can fail loudly with a clear message.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

from src.config import load_env

log = logging.getLogger(__name__)

ENV = load_env()


def find_binary(env_key: str, name: str) -> str | None:
    """Resolve the path of *name* (e.g. 'ffmpeg.exe'), or None if unavailable."""
    env_val = (ENV.get(env_key) or "").strip()
    if env_val and Path(env_val).exists():
        return env_val
    # winget Gyan.FFmpeg layout: <Packages>/Gyan.FFmpeg*/ffmpeg-*-full_build/bin/
    pkg_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for candidate in glob.glob(str(pkg_root / "Gyan.FFmpeg*" / "*" / "bin" / name)):
        return candidate
    return shutil.which(name)


@lru_cache(maxsize=None)
def ffmpeg_bin() -> str | None:
    return find_binary("FFMPEG_BIN", "ffmpeg.exe")


@lru_cache(maxsize=None)
def ffprobe_bin() -> str | None:
    return find_binary("FFPROBE_BIN", "ffprobe.exe")


@lru_cache(maxsize=None)
def ffmpeg_dir() -> str | None:
    """Directory containing ffmpeg.exe (also holds ffprobe.exe), or None."""
    bin_path = ffmpeg_bin()
    if not bin_path:
        return None
    return str(Path(bin_path).parent)