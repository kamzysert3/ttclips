"""Main orchestrator / CLI entry-point for ttclips.

Usage:
    ttclips                             # post the current-time slot (if not yet posted today)
    ttclips --monitor                   # refresh YouTube feeds only
    ttclips --catchup                   # find missed slots and post them
    ttclips --once --slot 12:00 --type generated
    ttclips --dry-run                   # same as normal but skip upload
    ttclips --init                      # scaffold config/ + .env.example in $TTCLIPS_HOME
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from ttclips import __version__
from ttclips.config import CLIPS_DIR, LOG_FILE, load_posting_window, load_tiktok_config
from ttclips.broll import pick_background
from ttclips.clipper import transcribe, rank_highlights
from ttclips.downloader import download
from ttclips.encoder import cut_and_crop_clip, make_generated_clip
from ttclips.generator import generate_original
from ttclips.monitor import refresh_feeds
from ttclips.scaffold import init_home
from ttclips.state import (PostRecord, add_post, load_posts, load_queue,
                           remove_from_queue, slot_posted_today)
from ttclips.uploader import upload_clip, validate_session

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


def _format_caption(title: str, hashtags: str = "") -> str:
    ht = hashtags or HASHTAGS_DEFAULT
    return f"{title}\n\n{ht}"


def _merge_hashtags(dynamic, limit: int = 4) -> str:
    """Combine LLM hashtags with HASHTAGS_DEFAULT, skipping defaults already used.

    ``dynamic`` is a list of tags (with or without '#') or a string.
    Empty/missing dynamic falls back to the defaults alone.
    """
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
    for item in str(HASHTAGS_DEFAULT).split():
        tag = item.lstrip("#").strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return " ".join(f"#{t}" for t in out)


def _pick_fair_target(queue: list[dict], posted_urls: set[str], posts: list[dict]) -> dict | None:
    """Oldest unposted item from the channel posted least recently (round-robin)."""
    unposted = [item for item in queue if item["url"] not in posted_urls]
    if not unposted:
        return None
    last_by_channel: dict[str, str] = {}
    for p in posts:
        ch = p.get("channel", "")
        ts = p.get("posted_at", "")
        if ch and ts and ts > last_by_channel.get(ch, ""):
            last_by_channel[ch] = ts
    def _key(item):
        return (last_by_channel.get(item.get("channel", ""), ""), item.get("discovered_at", ""))
    return min(unposted, key=_key)


# ---------------------------------------------------------------- repost
def _do_repost(slot_time: str, *, dry_run: bool = False, render_only: bool = False) -> bool:
    queue = load_queue()
    if not queue:
        log.info("Queue empty — refreshing feeds ...")
        refresh_feeds()
        queue = load_queue()
    if not queue:
        log.warning("Queue still empty after refresh — nothing to repost")
        return False

    posts = load_posts()
    posted_urls = {r["source_url"] for r in posts}
    target = _pick_fair_target(queue, posted_urls, posts)
    if not target:
        log.info("All queued videos have been reposted")
        return False

    url   = target["url"]
    title = target.get("title", "clip")
    log.info("Downloading %s — %s ...", target["video_id"], title)
    try:
        video_path = download(url, filename_hint=target.get("channel", ""))
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
    num_clips = min(5, max(1, int(target.get("max_moments") or 1)))
    try:
        highlights = rank_highlights(segments, num_clips=num_clips)
    except Exception as e:
        log.error("Highlight ranking failed: %s", e)
        return False
    if not highlights:
        log.warning("No highlights found in video")
        return False

    posted_any = False
    for i, best in enumerate(highlights, start=1):
        clip_name = f"{target['video_id']}_{int(best['start'])}_{int(best['end'])}.mp4"
        clip_path = CLIPS_DIR / clip_name

        if dry_run:
            log.info("[DRY RUN] Would encode clip %d/%d %s (%.1fs–%.1fs, score %s)",
                     i, len(highlights), target["video_id"], best["start"], best["end"], best.get("score"))
            caption = _format_caption(best.get("title", title), _merge_hashtags(best.get("hashtags")))
            log.info("[DRY RUN] Caption: %s", caption)
            posted_any = True
            continue

        log.info("Encoding clip %d/%d ...", i, len(highlights))
        try:
            cut_and_crop_clip(
                source=video_path,
                clip_start=best["start"],
                clip_end=best["end"],
                segments=segments,
                out=clip_path,
                title=best.get("title", title)[:70],
            )
        except Exception as e:
            log.error("Clip encoding failed (clip %d/%d): %s", i, len(highlights), e)
            continue

        caption = _format_caption(best.get("title", title), _merge_hashtags(best.get("hashtags")))
        if render_only:
            log.info("[NO-UPLOAD] Clip %d/%d rendered (not posted): %s", i, len(highlights), clip_path)
            log.info("[NO-UPLOAD] Caption would be: %s", caption)
            posted_any = True
            continue

        log.info("Uploading to TikTok (clip %d/%d) ...", i, len(highlights))
        ok = upload_clip(str(clip_path), caption)
        if ok:
            add_post(PostRecord(
                kind="repost",
                source_url=url,
                title=(best.get("title") or title)[:100],
                file_path=str(clip_path),
                caption=caption,
                clip_start=best["start"],
                clip_end=best["end"],
                slot=slot_time,
                posted_at=datetime.now().isoformat(timespec="seconds"),
                channel=target.get("channel", ""),
            ))
            remove_from_queue(url)
            log.info("Reposted clip %d/%d: %s (slot %s)", i, len(highlights),
                     (best.get("title") or title)[:50], slot_time)
            posted_any = True
            if i < len(highlights):
                _human_delay()
    return posted_any


# ---------------------------------------------------------------- generated
def _do_generated(slot_time: str, hint: str = "", *, dry_run: bool = False, render_only: bool = False) -> bool:
    log.info("Generating original content — hint: %s", hint or "(auto)")
    try:
        result = generate_original(hint or "a trending niche topic", label=f"{slot_time}_gen")
    except Exception as e:
        log.error("Content generation failed: %s", e)
        return False

    caption = _format_caption(
        result.get("caption", "AI insight"),
        _merge_hashtags(result.get("hashtags"))
    )
    narration_path = Path(result["narration_path"])
    day = datetime.now().strftime("%Y%m%d")
    clip_name = f"{day}_{slot_time.replace(':', '')}_generated.mp4"
    clip_path = CLIPS_DIR / clip_name

    if dry_run:
        log.info("[DRY RUN] Would encode generated clip with caption: %s", caption)
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
            title=result.get("caption", "AI insight")[:70],
            out=clip_path,
            duration_sec=result.get("duration_sec"),
            background=bg_path,
        )
    except Exception as e:
        log.error("Generated clip encoding failed: %s", e)
        return False

    if render_only:
        log.info("[NO-UPLOAD] Generated clip rendered (not posted): %s", clip_path)
        log.info("[NO-UPLOAD] Caption would be: %s", caption)
        return True

    log.info("Uploading to TikTok ...")
    success = upload_clip(str(clip_path), caption)
    if success:
        add_post(PostRecord(
            kind="generated",
            title=caption[:100],
            file_path=str(clip_path),
            caption=caption,
            slot=slot_time,
            posted_at=datetime.now().isoformat(timespec="seconds"),
        ))
        log.info("Posted generated clip (slot %s)", slot_time)
    return success


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


def _missed_slots() -> list[dict]:
    now = datetime.now()
    slots = load_posting_window()
    missed = []
    for s in slots:
        sh, sm = map(int, s["time"].split(":"))
        slot_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if slot_dt < now and not slot_posted_today(s["time"]):
            missed.append(s)
    return missed


# ---------------------------------------------------------------- main
def run_monitor() -> int:
    added = refresh_feeds()
    log.info("Monitor complete — %d new videos added", len(added))
    return len(added)


def run_post(target_slot: dict | None = None, *, dry_run: bool = False, render_only: bool = False, force_type: str = "") -> bool:
    slot = target_slot or _upcoming_slot()
    if not slot:
        log.error("No posting slots configured")
        return False
    slot_time = slot["time"]
    if slot_posted_today(slot_time) and not (force_type or render_only or dry_run):
        log.info("Slot %s already posted today — skipping", slot_time)
        return False
    content_type = force_type or slot["type"]
    if content_type == "repost":
        return _do_repost(slot_time, dry_run=dry_run, render_only=render_only)
    elif content_type == "generated":
        return _do_generated(slot_time, hint=slot.get("hint", ""), dry_run=dry_run, render_only=render_only)
    else:
        log.error("Unknown content type: %s", content_type)
        return False


def run_catchup(*, dry_run: bool = False, render_only: bool = False) -> int:
    missed = _missed_slots()
    log.info("Catch-up: %d missed slots", len(missed))
    posted = 0
    for slot in missed:
        success = run_post(slot, dry_run=dry_run, render_only=render_only)
        if success:
            posted += 1
            _human_delay()
    return posted


def run_post_file(video_path: str, caption: str = "") -> bool:
    """Upload a single, already-generated clip without running the pipeline.

    Used by ``--post-file``. On success records a manual PostRecord so the
    clip is not re-uploaded later.
    """
    path = Path(video_path)
    if not path.exists():
        log.error("Video not found: %s", path)
        return False
    if not caption:
        title = path.stem.replace("_", " ").strip()
        caption = _format_caption(title, HASHTAGS_DEFAULT)
    log.info("Posting file directly (no pipeline): %s", path)
    ok = upload_clip(str(path), caption)
    if ok:
        add_post(PostRecord(
            kind="manual",
            title=caption[:100],
            file_path=str(path),
            caption=caption,
            slot="manual",
            posted_at=datetime.now().isoformat(timespec="seconds"),
        ))
        log.info("Posted file directly: %s", path)
    return ok


def _human_delay():
    time.sleep(random.uniform(8, 20))


def main() -> None:
    parser = argparse.ArgumentParser(prog="ttclips", description="ttclips — clip reposter for TikTok")
    parser.add_argument("--init", action="store_true",
                        help="Create config/ and .env.example in the ttclips home dir, then exit")
    parser.add_argument("--version", action="version", version=f"ttclips {__version__}")
    parser.add_argument("--monitor", action="store_true", help="Refresh YouTube feed queue only")
    parser.add_argument("--catchup", action="store_true", help="Post all missed slots for today")
    parser.add_argument("--once",    action="store_true", help="Post the current/upcoming slot then exit")
    parser.add_argument("--slot",    type=str, default="", help="Override target slot time HH:MM (e.g. 12:00)")
    parser.add_argument("--type",    type=str, default="", help="Override content type (repost|generated)")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except upload")
    parser.add_argument("--no-upload", action="store_true",
                        help="Render the real 9:16 MP4 into data/clips but do NOT post")
    parser.add_argument("--post-file", type=str, default="",
                        help="Upload an already-generated clip to TikTok (skips the full pipeline)")
    parser.add_argument("--caption", type=str, default="",
                        help="Caption override for --post-file (defaults to a title derived from the filename)")
    parser.add_argument("--validate-session", action="store_true", help="Check if TikTok cookie session is alive")
    args = parser.parse_args()

    if args.init:
        created = init_home()
        for path in created:
            log.info("Created %s", path)
        log.info("Initialized ttclips home — edit .env, then run `ttclips --monitor`.")
        sys.exit(0)

    if args.validate_session:
        valid = validate_session()
        log.info("Session valid: %s", valid)
        sys.exit(0 if valid else 1)

    if args.post_file:
        ok = run_post_file(args.post_file, args.caption)
        sys.exit(0 if ok else 1)

    if args.monitor:
        run_monitor()
        sys.exit(0)

    # Auto-refresh queue if empty
    if not load_queue():
        log.info("Queue empty — refreshing feeds before posting")
        run_monitor()

    if args.catchup:
        posted = run_catchup(dry_run=args.dry_run, render_only=args.no_upload)
        log.info("Catch-up done: %d posted", posted)
        sys.exit(0)

    # Normal / --once
    slot = _find_slot(args.slot) if args.slot else _upcoming_slot()
    run_post(slot, dry_run=args.dry_run, render_only=args.no_upload, force_type=args.type)


if __name__ == "__main__":
    main()