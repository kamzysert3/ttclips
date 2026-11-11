"""yt-dlp wrapper: download source videos and extract metadata.

Any site yt-dlp supports works (YouTube, Twitch VODs/clips, Vimeo, direct
video links, ...); YouTube is just the one the channel monitor polls.
"""
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


def download(url: str, *, filename_hint: str = "", cache_id: str = "") -> Path:
    """Download video to downloads/ and return the path to the MP4 file.

    If a previously-merged ``{video_id}.mp4`` already exists it is reused,
    so a failed post step (e.g. Gemini outage) doesn't force a re-download.

    Parameters
    ----------
    url : str
        Video URL (YouTube, Twitch, or anything yt-dlp supports).
    filename_hint : str
        Optional label for logging (e.g. the channel name).
    cache_id : str
        yt-dlp id of the video, for cache reuse on non-YouTube URLs.
    """
    out_dir = DOWNLOADS_DIR / filename_hint.replace("/", "_") if filename_hint else DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    vid = cache_id or _video_id_from_url(url)
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
        "noplaylist": True,
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


def _safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_")[:80] or "video"


def probe_source(url: str) -> dict:
    """Resolve a single-video URL to the metadata the pipeline needs.

    Returns ``{"url", "video_id", "cache_id", "title", "channel", "duration",
    "extractor"}``. ``video_id`` is prefixed with the site (``twitch_v123``)
    so ids from different sites can never collide in the cache or clip
    names. Raises ``ValueError`` for playlists/channels or unsupported links.
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed.  pip install yt-dlp")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise ValueError(f"Could not read {url}: {e}") from e
    if not info:
        raise ValueError(f"No video found at {url}")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries"):
        raise ValueError(f"{url} is a playlist/channel - pass a link to a single video")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "web").lower()
    raw_id = str(info.get("id") or "")
    if not raw_id:
        raise ValueError(f"yt-dlp returned no video id for {url}")
    site = "yt" if extractor == "youtube" else _safe_id(extractor.split(":")[0])
    return {
        "url": info.get("webpage_url") or url,
        "video_id": raw_id if site == "yt" else f"{site}_{_safe_id(raw_id)}",
        "cache_id": raw_id,
        "title": info.get("title") or raw_id,
        "channel": info.get("uploader") or info.get("channel") or site,
        "duration": float(info.get("duration") or 0),
        "extractor": extractor,
    }
