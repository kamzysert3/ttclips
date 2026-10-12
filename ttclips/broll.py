"""Stock b-roll for generated clips via the free Pexels Videos API."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ttclips.config import DATA_DIR, load_env
from ttclips.encoder import probe_duration

log = logging.getLogger(__name__)

ENV = load_env()

PEXELS_API_URL = "https://api.pexels.com/v1/videos/search"
BROLL_DIR = DATA_DIR / "broll"
BROLL_DIR.mkdir(parents=True, exist_ok=True)

PEXELS_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 90
MIN_CLIP_SEC = 2.0


def _key() -> str:
    key = ENV.get("PEXELS_API_KEY", "").strip()
    if not key:
        log.warning("PEXELS_API_KEY not set — generated clips will fall back to the gradient background")
    return key


def _search(keyword: str, per_page: int = 5) -> list[dict]:
    key = _key()
    if not key:
        return []
    try:
        resp = httpx.get(
            PEXELS_API_URL,
            params={"query": keyword, "per_page": per_page, "orientation": "portrait"},
            headers={"Authorization": key},
            timeout=PEXELS_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("videos", []) or []
    except Exception as e:
        log.warning("Pexels search failed for '%s': %s", keyword, e)
        return []


def _best_file(video: dict) -> dict | None:
    """Pick the frame file best suited to a 1080x1920 crop.

    Preferences: portrait > landscape, hd > sd, higher resolution, <= 1920 on
    both axes, mp4 container.
    """
    files = video.get("video_files") or []
    mp4 = [f for f in files if (f.get("file_type") or "").endswith("mp4")] or files
    scored = []
    for f in mp4:
        try:
            w = int(f.get("width") or 0)
            h = int(f.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or w > 1920 or h > 1920:
            continue
        portrait = 1.0 if h >= w else 0.5
        hd = 1 if f.get("quality") == "hd" else 0
        scored.append((hd, portrait * min(w, h), f))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _download(url: str, dest: Path) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
        part.replace(dest)
    except Exception as e:
        part.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        log.warning("B-roll download failed (%s): %s", url[:80], e)
        return None
    if probe_duration(dest) < MIN_CLIP_SEC:
        log.warning("B-roll too short: %s", dest.name)
        dest.unlink(missing_ok=True)
        return None
    return dest


def pick_background(keywords: list[str], min_duration: float = 30.0, label: str = "generated") -> Path | None:
    """Fetch the best available b-roll clip for the given topic keywords.

    Ranks search results by their documented duration (shortest clip that
    already covers the narration wins; otherwise the longest available), then
    downloads the best first and caches each download in ``data/broll/``.
    Returns a path to a local MP4 or ``None`` if nothing usable could be
    fetched (caller falls back to the gradient background).
    """
    if not _key():
        return None
    stem = (label or "generated").replace(" ", "_").replace("/", "_").replace(":", "_")[:40]
    candidates: list[tuple[float, dict, dict, str]] = []
    for kw in (keywords or []):
        if not kw or not kw.strip():
            continue
        for video in _search(kw):
            vf = _best_file(video)
            if not vf or not vf.get("link"):
                continue
            try:
                dur = float(video.get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            candidates.append((dur, video, vf, kw))

    def _rank(c: tuple[float, dict, dict, str]):
        dur = c[0]
        return (0, dur) if dur >= min_duration else (1, -dur)

    candidates.sort(key=_rank)

    for dur, video, vf, kw in candidates:
        dest = BROLL_DIR / f"{stem}_{video.get('id')}_{vf.get('id')}.mp4"
        clip = _download(vf["link"], dest)
        if not clip:
            continue
        actual = probe_duration(clip)
        log.info("B-roll '%s' -> %s (%.0fs, %sx%s)",
                 kw, clip.name, actual, vf.get("width"), vf.get("height"))
        return clip

    log.warning("No usable b-roll found for keywords: %s", keywords)
    return None