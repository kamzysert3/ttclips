"""yt-dlp wrapper: download YouTube videos and extract metadata."""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from ttclips.binutils import ffmpeg_dir
from ttclips.config import DOWNLOADS_DIR

log = logging.getLogger(__name__)


def _video_id_from_url(url: str) -> str | None:
    m = re.search(r"[?&]v=([\w-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([\w-]{6,})", url)
    return m.group(1) if m else None


def download(url: str, *, filename_hint: str = "") -> Path:
    """Download video to downloads/ and return the path to the MP4 file.

    If a previously-merged ``{video_id}.mp4`` already exists it is reused,
    so a failed post step (e.g. Gemini outage) doesn't force a re-download.

    Parameters
    ----------
    url : str
        YouTube watch URL.
    filename_hint : str
        Optional label for logging (e.g. the channel name).
    """
    out_dir = DOWNLOADS_DIR / filename_hint.replace("/", "_") if filename_hint else DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    vid = _video_id_from_url(url)
    if vid:
        cached = out_dir / f"{vid}.mp4"
        if cached.exists() and cached.stat().st_size > 0:
            log.info("Reusing cached download: %s", cached)
            return cached

    if shutil.which("yt-dlp") is None:
        log.warning("yt-dlp not found on PATH; falling back to python import")
    log.info("Downloading %s (hint: %s)", url, filename_hint)

    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed.  pip install yt-dlp")

    ffmpeg_loc = ffmpeg_dir()
    if ffmpeg_loc is None:
        raise RuntimeError(
            "ffmpeg not found — it is required to merge the video and audio "
            "streams. Install it (e.g. `winget install Gyan.FFmpeg`) or set "
            "FFMPEG_BIN in .env."
        )

    opts: dict = {
        "paths": {"home": str(out_dir)},
        "outtmpl": {"default": "%(id)s.%(ext)s"},
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_loc,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "writethumbnail": False,
        "writesubtitles": False,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 30,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError(f"yt-dlp returned no info for {url}")
        out_path = ydl.prepare_filename(info)
        # After format postprocessing the actual file may have a different ext.
        mp4_path = out_dir / f"{info['id']}.mp4"
        if mp4_path.exists():
            out_path = mp4_path
        elif not Path(out_path).exists():
            # Try common postprocess result
            for ext in ("mp4", "mkv", "webm"):
                p = out_dir / f"{info['id']}.{ext}"
                if p.exists():
                    out_path = p
                    break
        else:
            out_path = Path(out_path)

        out_path = Path(out_path)
        if not out_path.exists():
            raise RuntimeError(
                f"Download completed but output file is missing: {out_path}. "
                "The video+audio merge likely failed — verify ffmpeg/ffprobe "
                "resolved correctly (see data/logs/ttclips.log)."
            )

        log.info("Downloaded %s -> %s", info["id"], out_path)
        return out_path


def get_info(url: str) -> dict | None:
    """Get video info dict without downloading.  Returns None on error."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed")
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("get_info failed for %s: %s", url, e)
        return None