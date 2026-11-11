"""Main orchestrator / CLI entry-point for ttclips.

Usage:
    ttclips                             # post the current-time slot (if not yet posted today)
    ttclips --monitor                   # refresh YouTube feeds only
    ttclips --catchup                   # find missed slots and post them
    ttclips --once --slot 12:00 --type generated
    ttclips --dry-run                   # same as normal but skip upload
    ttclips --init --yes                # scaffold config/ + .env.example in $TTCLIPS_HOME
    ttclips --target youtube            # post to YouTube Shorts instead of TikTok
    ttclips --url <youtube|twitch|any video link or file>   # clip one specific video
    ttclips --review                    # edit + approve each caption before uploading
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from ttclips import __version__
from ttclips.config import (CLIPS_DIR, LOG_FILE, load_posting_window,
                            load_tiktok_config, load_youtube_config)
from ttclips.broll import pick_background
from ttclips.clipper import transcribe, rank_highlights
from ttclips.downloader import download, probe_source
from ttclips.encoder import cut_and_crop_clip, make_generated_clip
from ttclips.generator import generate_original
from ttclips.monitor import refresh_feeds
from ttclips.platforms import PLATFORMS, configured_targets, resolve_targets
from ttclips.review import SKIPPED, ReviewAbort, is_interactive, review_post
from ttclips.scaffold import init_home, require_ownership_ack
from ttclips.state import (PostRecord, add_post, load_posts, load_queue,
                           platform_of, remove_from_queue, slot_posted_today)
from ttclips.uploader import publish_clip, validate_session
from ttclips.youtube_uploader import (build_short_metadata, upload_short,
                                      validate_session as validate_youtube_session)

# ---------------------------------------------------------------------------
# Logging
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger("ttclips")

HASHTAGS_DEFAULT = load_tiktok_config().get("default_hashtags", "#fyp #viral #funny #trending")
YT_HASHTAGS_DEFAULT = load_youtube_config().get("default_hashtags", "#Shorts #fyp")

# Set by --review: edit/approve every caption before it is uploaded.
REVIEW = False


def _resolve_targets(override: str = "") -> list[str]:
    """Resolve the platforms to post to.

    Precedence: CLI ``--target`` > per-slot ``targets`` > config/platforms.json
    > ``tiktok``. ``both`` expands to every supported platform.
    """
    return resolve_targets(override)


def _format_caption(title: str, hashtags: str = "") -> str:
    ht = hashtags or HASHTAGS_DEFAULT
    return f"{title}\n\n{ht}"


def _merge_hashtags(dynamic, limit: int = 4, defaults: str = "") -> str:
    """Combine LLM hashtags with the platform defaults, skipping duplicates.

    ``dynamic`` is a list of tags (with or without '#') or a string.
    Empty/missing dynamic falls back to the defaults alone.
    """
    base = defaults if defaults is not None else HASHTAGS_DEFAULT
    seen: set[str] = set()
    out: list[str] = []
    items = dynamic if isinstance(dynamic, list) else str(dynamic or "").split()
    for item in items:
        tag = str(item).lstrip("#").strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= limit:
            break
    for item in str(base).split():
        tag = item.lstrip("#").strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return " ".join(f"#{t}" for t in out)


def _hashtags_for(platform: str, dynamic) -> str:
    defaults = YT_HASHTAGS_DEFAULT if platform == "youtube" else HASHTAGS_DEFAULT
    return _merge_hashtags(dynamic, defaults=defaults)


def _fair_order(candidates: list[dict], posts: list[dict]) -> list[dict]:
    """Order candidates oldest-first by the channel posted least recently."""
    if not candidates:
        return []
    last_by_channel: dict[str, str] = {}
    for p in posts:
        ch = p.get("channel", "")
        ts = p.get("posted_at", "")
        if ch and ts and ts > last_by_channel.get(ch, ""):
            last_by_channel[ch] = ts

    def _key(item):
        return (last_by_channel.get(item.get("channel", ""), ""), item.get("discovered_at", ""))

    return sorted(candidates, key=_key)


def _pick_fair_target(candidates: list[dict], posts: list[dict]) -> dict | None:
    """Oldest item from the channel posted least recently (round-robin)."""
    ordered = _fair_order(candidates, posts)
    return ordered[0] if ordered else None


# ---------------------------------------------------------------- platform state
def _posted_count(posts: list[dict], source_url: str, platform: str) -> int:
    """Number of clips already posted for *source_url* on *platform*."""
    return sum(
        1 for p in posts
        if p.get("kind") == "repost"
        and p.get("source_url") == source_url
        and platform_of(p) == platform
    )


def _clip_posted(posts: list[dict], source_url: str, clip: dict, platform: str) -> bool:
    """True when this exact clip was already posted to *platform*."""
    start = round(float(clip.get("start", 0.0)), 2)
    end = round(float(clip.get("end", 0.0)), 2)
    for p in posts:
        if (p.get("kind") == "repost"
                and p.get("source_url") == source_url
                and platform_of(p) == platform
                and abs(float(p.get("clip_start", -1)) - start) < 0.05
                and abs(float(p.get("clip_end", -1)) - end) < 0.05):
            return True
    return False


def _pending_sources(queue: list[dict], posts: list[dict], targets: list[str]) -> list[dict]:
    """Queue items that still owe a post on at least one target platform."""
    pending: list[dict] = []
    for item in queue:
        url = item.get("url", "")
        expected = min(5, max(1, int(item.get("max_moments") or 1)))
        if any(_posted_count(posts, url, platform) < expected for platform in targets):
            pending.append(item)
    return pending


def _tiktok_caption(title: str, hashtags: str) -> str:
    """TikTok caption; an empty hashtag line (cleared in review) adds none."""
    return f"{title}\n\n{hashtags}" if hashtags else title


def _caption_preview(platform: str, title: str, hashtags: str, source_url: str = "") -> str:
    """Exactly what will be posted, for the review prompt."""
    if platform == "youtube":
        meta = build_short_metadata(title, hashtags, source_url=source_url)
        return f"Title:       {meta['title']}\nDescription: " + \
            meta["description"].replace("\n", "\n             ")
    return _tiktok_caption(title, hashtags)


def _publish(platform: str, clip_path: Path, *, title: str, hashtags: str,
             source_url: str = "", kind: str = "repost", channel: str = "",
             clip_start: float = 0.0, clip_end: float = 0.0,
             slot: str = "") -> tuple[bool, str]:
    """Upload one rendered clip to *platform* and record it in the state file.

    Returns ``(success, remote_id)``. Upload errors are logged and reported
    as a failure — one platform failing never aborts the whole run. With
    ``--review`` the user edits/approves the caption first; a skipped upload
    returns ``(False, SKIPPED)``.
    """
    if REVIEW:
        reviewed = review_post(
            platform, title, hashtags, clip_path,
            preview=lambda t, h: _caption_preview(platform, t, h, source_url),
            duration=(clip_end - clip_start) if clip_end > clip_start else None,
        )
        if reviewed is None:
            return False, SKIPPED
        title, hashtags = reviewed
    remote_id = ""
    caption = ""
    try:
        if platform == "youtube":
            meta = build_short_metadata(title, hashtags, source_url=source_url)
            log.info("Uploading to YouTube Shorts: %s", meta["title"])
            ok, remote_id = upload_short(str(clip_path), meta["title"], meta["description"],
                                         tags=meta["tags"])
            caption = meta["description"]
        else:
            caption = _tiktok_caption(title, hashtags)
            log.info("Uploading to TikTok ...")
            ok, remote_id = publish_clip(str(clip_path), caption,
                                         is_aigc=(kind == "generated"))
    except Exception as e:
        log.error("Upload to %s failed: %s", platform, e)
        return False, ""
    if ok:
        add_post(PostRecord(
            kind=kind,
            source_url=source_url,
            title=(title or caption)[:100],
            file_path=str(clip_path),
            caption=caption,
            clip_start=clip_start,
            clip_end=clip_end,
            slot=slot,
            posted_at=datetime.now().isoformat(timespec="seconds"),
            channel=channel,
            platform=platform,
            remote_id=remote_id,
        ))
    return ok, remote_id


# ---------------------------------------------------------------- repost
def _do_repost(slot_time: str, *, dry_run: bool = False, render_only: bool = False,
               targets: list[str] | None = None) -> bool:
    targets = targets or _resolve_targets()
    # The platforms a source must reach before it leaves the queue. A run for
    # a single platform must not evict a source another platform still owes.
    queue_platforms = configured_targets()
    queue = load_queue()
    if not queue:
        log.info("Queue empty — refreshing feeds ...")
        refresh_feeds()
        queue = load_queue()
    if not queue:
        log.warning("Queue still empty after refresh — nothing to repost")
        return False

    posts = load_posts()
    candidates = _fair_order(_pending_sources(queue, posts, targets), posts)
    if not candidates:
        log.info("All queued videos have been reposted")
        return False

    for item in candidates:
        result = _process_source(item, targets=targets, slot_time=slot_time, posts=posts,
                                 dry_run=dry_run, render_only=render_only,
                                 queue_platforms=queue_platforms)
        if result is None:  # nothing left to post for this source; try the next
            continue
        return result
    return False


def _process_source(item: dict, *, targets: list[str], slot_time: str, posts: list[dict],
                    dry_run: bool = False, render_only: bool = False,
                    queue_platforms: list[str] | None = None) -> bool | None:
    """Download, transcribe, rank, render and publish one source video.

    ``item`` needs ``url`` and ``video_id``; ``local_path`` skips the
    download. ``queue_platforms`` enables queue bookkeeping (``None`` for
    ad-hoc ``--url`` runs, which never touch the queue). Returns ``None``
    when every highlight is already posted on every target (nothing to do),
    otherwise whether anything was posted/rendered.
    """
    url = item["url"]
    title = item.get("title", "clip")
    posted_any = False
    if item.get("local_path"):
        video_path = Path(item["local_path"])
        log.info("Using local file %s", video_path)
    else:
        log.info("Downloading %s — %s ...", item["video_id"], title)
        try:
            video_path = download(url, filename_hint=item.get("channel", ""),
                                  cache_id=item.get("cache_id", ""))
        except Exception as e:
            log.error("Download failed: %s", e)
            return False

    log.info("Transcribing ...")
    try:
        segments = transcribe(video_path)
    except Exception as e:
        log.error("Transcription failed: %s", e)
        return False
    if not segments:
        log.warning("Empty transcription")
        return False

    log.info("Ranking highlights via Gemini ...")
    num_clips = min(5, max(1, int(item.get("max_moments") or 1)))
    try:
        highlights = rank_highlights(segments, num_clips=num_clips,
                                     cache_key=item["video_id"])
    except Exception as e:
        log.error("Highlight ranking failed: %s", e)
        return False
    if not highlights:
        log.warning("No highlights found in video")
        return False

    # Which (clip, platform) pairs still need a post?
    todo: list[tuple[dict, list[str]]] = []
    for best in highlights:
        missing = [p for p in targets if not _clip_posted(posts, url, best, p)]
        if missing:
            todo.append((best, missing))
    if not todo:
        if queue_platforms is None:
            log.info("Every highlight of %s is already posted to %s — nothing to do",
                     item["video_id"], ", ".join(targets))
        # Every clip is already on every platform of this run. The source
        # only leaves the queue once *all configured* platforms have it.
        elif all(_posted_count(posts, url, p) >= len(highlights) for p in queue_platforms):
            log.info("Source %s is fully posted — dropping it from the queue",
                     item["video_id"])
            if not dry_run:
                remove_from_queue(url)
        else:
            log.info("Source %s still owes a post on %s — keeping it queued",
                     item["video_id"],
                     ", ".join(p for p in queue_platforms
                               if _posted_count(posts, url, p) < len(highlights)))
        return None

    for i, (best, missing) in enumerate(todo, start=1):
        clip_name = f"{item['video_id']}_{int(best['start'])}_{int(best['end'])}.mp4"
        clip_path = CLIPS_DIR / clip_name
        clip_title = best.get("title", title)

        if dry_run:
            log.info("[DRY RUN] Would encode clip %d/%d %s (%.1fs–%.1fs, score %s)",
                     i, len(todo), item["video_id"], best["start"], best["end"], best.get("score"))
            for platform in missing:
                log.info("[DRY RUN] %s caption: %s", platform,
                         _format_caption(clip_title, _hashtags_for(platform, best.get("hashtags"))))
            posted_any = True
            continue

        log.info("Encoding clip %d/%d ...", i, len(todo))
        try:
            cut_and_crop_clip(
                source=video_path,
                clip_start=best["start"],
                clip_end=best["end"],
                segments=segments,
                out=clip_path,
                title=clip_title[:70],
            )
        except Exception as e:
            log.error("Clip encoding failed (clip %d/%d): %s", i, len(todo), e)
            continue

        if render_only:
            log.info("[NO-UPLOAD] Clip %d/%d rendered (not posted): %s", i, len(todo), clip_path)
            for platform in missing:
                log.info("[NO-UPLOAD] %s caption would be: %s", platform,
                         _format_caption(clip_title, _hashtags_for(platform, best.get("hashtags"))))
            posted_any = True
            continue

        for platform in missing:
            ok, remote_id = _publish(
                platform, clip_path,
                title=clip_title,
                hashtags=_hashtags_for(platform, best.get("hashtags")),
                source_url=url,
                kind="repost",
                channel=item.get("channel", ""),
                clip_start=best["start"],
                clip_end=best["end"],
                slot=slot_time,
            )
            if ok:
                posts.append({
                    "kind": "repost", "source_url": url,
                    "clip_start": best["start"], "clip_end": best["end"],
                    "platform": platform, "remote_id": remote_id,
                })
                log.info("Reposted clip %d/%d to %s: %s (slot %s)", i, len(todo),
                         platform, (clip_title or title)[:50], slot_time)
                posted_any = True
                if platform != missing[-1]:
                    _human_delay()
            elif remote_id == SKIPPED:
                log.info("Skipped %s for clip %d/%d (review)", platform, i, len(todo))
            else:
                log.error("Upload to %s failed for clip %d/%d", platform, i, len(todo))

    # Drop the source from the queue once every clip is on every
    # configured platform (a run for a single platform never evicts a
    # source that another platform still owes).
    if queue_platforms is not None and all(
            _posted_count(posts, url, platform) >= len(highlights)
            for platform in queue_platforms):
        remove_from_queue(url)
    return posted_any


# ---------------------------------------------------------------- generated
def _do_generated(slot_time: str, hint: str = "", *, dry_run: bool = False,
                  render_only: bool = False, targets: list[str] | None = None) -> bool:
    targets = targets or _resolve_targets()
    log.info("Generating original content — hint: %s", hint or "(auto)")
    try:
        result = generate_original(hint or "a trending niche topic", label=f"{slot_time}_gen")
    except Exception as e:
        log.error("Content generation failed: %s", e)
        return False

    narration_path = Path(result["narration_path"])
    day = datetime.now().strftime("%Y%m%d")
    clip_name = f"{day}_{slot_time.replace(':', '')}_generated.mp4"
    clip_path = CLIPS_DIR / clip_name
    title = result.get("caption", "AI insight")

    if dry_run:
        log.info("[DRY RUN] Would encode generated clip with caption: %s",
                 _format_caption(title, _hashtags_for("tiktok", result.get("hashtags"))))
        log.info("[DRY RUN] Script (first 120 chars): %s", result["script"][:120])
        log.info("[DRY RUN] B-roll keywords: %s", result.get("keywords", []))
        return True

    log.info("Fetching b-roll background ...")
    bg_path = None
    try:
        bg_path = pick_background(
            result.get("keywords", []),
            min_duration=result.get("duration_sec") or 30.0,
            label=slot_time,
        )
    except Exception as e:
        log.warning("B-roll fetch failed (will use gradient): %s", e)

    log.info("Encoding generated clip ...")
    try:
        make_generated_clip(
            narration_mp3=narration_path,
            script=result["script"],
            title=title[:70],
            out=clip_path,
            duration_sec=result.get("duration_sec"),
            background=bg_path,
        )
    except Exception as e:
        log.error("Generated clip encoding failed: %s", e)
        return False

    if render_only:
        log.info("[NO-UPLOAD] Generated clip rendered (not posted): %s", clip_path)
        for platform in targets:
            log.info("[NO-UPLOAD] %s caption would be: %s", platform,
                     _format_caption(title, _hashtags_for(platform, result.get("hashtags"))))
        return True

    posted_any = False
    for platform in targets:
        ok, _remote = _publish(
            platform, clip_path,
            title=title,
            hashtags=_hashtags_for(platform, result.get("hashtags")),
            kind="generated",
            slot=slot_time,
        )
        if ok:
            log.info("Posted generated clip to %s (slot %s)", platform, slot_time)
            posted_any = True
    return posted_any


# ---------------------------------------------------------------- slot helpers
def _find_slot(target_time: str) -> dict | None:
    slots = load_posting_window()
    return next((s for s in slots if s["time"] == target_time), None)


def _upcoming_slot() -> dict | None:
    now = datetime.now()
    current_hm = f"{now.hour:02d}:{0:02d}"
    slots = load_posting_window()
    for s in slots:
        if s["time"] >= current_hm:
            return s
    return slots[-1] if slots else None


def _missed_slots(targets: list[str] | None = None) -> list[dict]:
    now = datetime.now()
    slots = load_posting_window()
    platforms = targets or ["tiktok"]
    missed = []
    for s in slots:
        sh, sm = map(int, s["time"].split(":"))
        slot_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if slot_dt < now and any(
            not slot_posted_today(s["time"], platform=p) for p in platforms
        ):
            missed.append(s)
    return missed


# ---------------------------------------------------------------- main
def run_monitor() -> int:
    added = refresh_feeds()
    log.info("Monitor complete — %d new videos added", len(added))
    return len(added)


def run_post(target_slot: dict | None = None, *, dry_run: bool = False,
             render_only: bool = False, force_type: str = "",
             targets: list[str] | None = None) -> bool:
    slot = target_slot or _upcoming_slot()
    if not slot:
        log.error("No posting slots configured")
        return False
    slot_time = slot["time"]
    if targets is None:
        slot_targets = slot.get("targets") or ""
        targets = _resolve_targets(slot_targets)
    pending = [t for t in targets if not slot_posted_today(slot_time, platform=t)]
    if not pending and not (force_type or render_only or dry_run):
        log.info("Slot %s already posted today (%s) — skipping",
                 slot_time, ", ".join(targets))
        return False
    active = pending or targets
    log.info("Slot %s -> %s", slot_time, ", ".join(active))
    content_type = force_type or slot["type"]
    if content_type == "repost":
        return _do_repost(slot_time, dry_run=dry_run, render_only=render_only, targets=active)
    elif content_type == "generated":
        return _do_generated(slot_time, hint=slot.get("hint", ""), dry_run=dry_run,
                             render_only=render_only, targets=active)
    else:
        log.error("Unknown content type: %s", content_type)
        return False


def run_catchup(*, dry_run: bool = False, render_only: bool = False,
                targets: list[str] | None = None) -> int:
    platforms = targets or _resolve_targets()
    missed = _missed_slots(platforms)
    log.info("Catch-up: %d missed slots", len(missed))
    posted = 0
    for slot in missed:
        success = run_post(slot, dry_run=dry_run, render_only=render_only, targets=platforms)
        if success:
            posted += 1
            _human_delay()
    return posted


def run_post_file(video_path: str, caption: str = "", target: str = "") -> bool:
    """Upload a single, already-generated clip without running the pipeline.

    Used by ``--post-file``. On success records a manual PostRecord so the
    clip is not re-uploaded later.
    """
    path = Path(video_path)
    if not path.exists():
        log.error("Video not found: %s", path)
        return False
    targets = _resolve_targets(target)
    title = caption or path.stem.replace("_", " ").strip()
    posted_any = False
    for platform in targets:
        # An explicit TikTok --caption is used verbatim; YouTube always gets
        # its default hashtags (title/description are separate fields there).
        hashtags = "" if (caption and platform == "tiktok") else _hashtags_for(platform, "")
        log.info("Posting file directly to %s: %s", platform, path)
        ok, remote_id = _publish(platform, path, title=title, hashtags=hashtags,
                                 kind="manual", slot="manual")
        if ok:
            log.info("Posted file directly to %s: %s", platform, path)
            posted_any = True
    return posted_any


def run_url(source: str, *, targets: list[str] | None = None, num_clips: int = 1,
            dry_run: bool = False, render_only: bool = False) -> bool:
    """Clip one specific video instead of the monitored queue (``--url``).

    *source* is a YouTube/Twitch/any yt-dlp-supported link, or a local video
    file. The queue is never touched; per-platform dedup still applies, so
    re-running the same link only posts highlights that are not up yet.
    """
    targets = targets or _resolve_targets()
    local = Path(source).expanduser()
    if local.is_file():
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", local.stem).strip("_")[:60] or "local"
        digest = hashlib.sha1(str(local.resolve()).encode()).hexdigest()[:8]
        item = {"url": local.resolve().as_uri(), "video_id": f"local_{stem}_{digest}",
                "title": local.stem.replace("_", " "), "channel": "local",
                "local_path": str(local)}
    else:
        if not re.match(r"^https?://", source.strip(), re.I):
            log.error("--url needs an http(s) link or an existing video file: %s", source)
            return False
        try:
            item = probe_source(source.strip())
        except ValueError as e:
            log.error("%s", e)
            return False
        if item["duration"] > 3 * 3600:
            log.warning("This video is %.1f hours long — downloading and transcribing it "
                        "will take a while.", item["duration"] / 3600)
    item["max_moments"] = min(5, max(1, int(num_clips)))
    log.info("Clipping %s (%s) — %d clip(s) -> %s", item["title"], item["channel"],
             item["max_moments"], ", ".join(targets))
    log.info("Reminder: only post content you own or have rights to (docs/LEGAL.md).")
    result = _process_source(item, targets=targets, slot_time="manual", posts=load_posts(),
                             dry_run=dry_run, render_only=render_only, queue_platforms=None)
    return bool(result)


def _human_delay():
    time.sleep(random.uniform(8, 20))


def main() -> None:
    parser = argparse.ArgumentParser(prog="ttclips", description="ttclips — clip reposter for TikTok and YouTube Shorts")
    parser.add_argument("--init", action="store_true",
                        help="Create config/ and .env.example in the ttclips home dir, then exit")
    parser.add_argument("--yes", action="store_true",
                        help="Acknowledge you own (or have rights to) the content you will repost "
                             "(required for --init)")
    parser.add_argument("--version", action="version", version=f"ttclips {__version__}")
    parser.add_argument("--monitor", action="store_true", help="Refresh YouTube feed queue only")
    parser.add_argument("--catchup", action="store_true", help="Post all missed slots for today")
    parser.add_argument("--once",    action="store_true", help="Post the current/upcoming slot then exit")
    parser.add_argument("--slot",    type=str, default="", help="Override target slot time HH:MM (e.g. 12:00)")
    parser.add_argument("--type",    type=str, default="", help="Override content type (repost|generated)")
    parser.add_argument("--target",  type=str, default="",
                        help="Target platform(s): tiktok|youtube|both (default: config/platforms.json)")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except upload")
    parser.add_argument("--no-upload", action="store_true",
                        help="Render the real 9:16 MP4 into data/clips but do NOT post")
    parser.add_argument("--post-file", type=str, default="",
                        help="Upload an already-generated clip (skips the full pipeline)")
    parser.add_argument("--caption", type=str, default="",
                        help="Caption override for --post-file (defaults to a title derived from the filename)")
    parser.add_argument("--url", type=str, default="", metavar="LINK",
                        help="Clip this video instead of the monitored channels: a YouTube, "
                             "Twitch (VOD/clip) or any yt-dlp-supported link, or a local video file")
    parser.add_argument("--clips", type=int, default=1, metavar="N",
                        help="With --url: number of highlight clips to cut (1-5, default 1)")
    parser.add_argument("--review", action="store_true",
                        help="Show each caption before uploading so you can edit it; "
                             "press Enter to upload (needs an interactive terminal)")
    parser.add_argument("--validate-session", action="store_true", help="Check if TikTok cookie session is alive")
    parser.add_argument("--validate-youtube", action="store_true", help="Check if the YouTube cookie session is alive")
    args = parser.parse_args()
    try:
        _run(args)
    except ReviewAbort:
        log.info("Stopped from the review prompt — nothing else will be posted.")
        sys.exit(130)


def _run(args) -> None:
    global REVIEW
    if args.review:
        if not is_interactive():
            log.error("--review needs an interactive terminal (it waits for you to press "
                      "Enter); drop it for scheduled/unattended runs.")
            sys.exit(2)
        REVIEW = True

    if args.init:
        if not require_ownership_ack(args.yes):
            sys.exit(1)
        created = init_home()
        for path in created:
            log.info("Created %s", path)
        log.info("Initialized ttclips home — edit .env, then run `ttclips --monitor`.")
        sys.exit(0)

    if args.validate_session:
        try:
            valid = validate_session()
        except Exception as e:
            log.error("TikTok session check failed: %s", e)
            sys.exit(1)
        log.info("Session valid: %s", valid)
        sys.exit(0 if valid else 1)

    if args.validate_youtube:
        try:
            valid = validate_youtube_session()
        except Exception as e:
            log.error("YouTube session check failed: %s", e)
            sys.exit(1)
        log.info("YouTube session valid: %s", valid)
        sys.exit(0 if valid else 1)

    if args.post_file:
        ok = run_post_file(args.post_file, args.caption, args.target)
        sys.exit(0 if ok else 1)

    if args.url:
        ok = run_url(args.url, targets=_resolve_targets(args.target) if args.target else None,
                     num_clips=args.clips, dry_run=args.dry_run, render_only=args.no_upload)
        sys.exit(0 if ok else 1)

    if args.monitor:
        run_monitor()
        sys.exit(0)

    # Auto-refresh queue if empty
    if not load_queue():
        log.info("Queue empty — refreshing feeds before posting")
        run_monitor()

    if args.catchup:
        posted = run_catchup(dry_run=args.dry_run, render_only=args.no_upload,
                             targets=_resolve_targets(args.target) if args.target else None)
        log.info("Catch-up done: %d posted", posted)
        sys.exit(0)

    # Normal / --once
    slot = _find_slot(args.slot) if args.slot else _upcoming_slot()
    run_post(slot, dry_run=args.dry_run, render_only=args.no_upload,
             force_type=args.type,
             targets=_resolve_targets(args.target) if args.target else None)


if __name__ == "__main__":
    main()
