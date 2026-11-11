"""Persistent state: dedup tracking for posted clips + monitor queue."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ttclips.config import DATA_DIR, QUEUE_FILE, STATE_FILE

log = logging.getLogger(__name__)

_thread_lock = threading.RLock()
LOCK_FILE = DATA_DIR / ".state.lock"
LOCK_TIMEOUT_SEC = 30.0
LOCK_STALE_SEC = 120.0
_depth = threading.local()


@contextmanager
def _state_lock(lock_file: Path | None = None, timeout: float = LOCK_TIMEOUT_SEC):
    """Serialize state read-modify-write across threads *and* processes.

    Scheduled tasks (e.g. a TikTok run and a YouTube run) can overlap, so a
    thread lock alone is not enough. A lock file created with O_EXCL works on
    every OS without extra deps; a lock older than ``LOCK_STALE_SEC`` is
    assumed to belong to a crashed process and is broken. Re-entrant within
    a thread.
    """
    path = Path(lock_file) if lock_file is not None else LOCK_FILE
    with _thread_lock:
        depth = getattr(_depth, "n", 0)
        if depth:
            _depth.n = depth + 1
            try:
                yield
            finally:
                _depth.n -= 1
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > LOCK_STALE_SEC:
                        path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not acquire state lock {path}")
                time.sleep(0.05)
        _depth.n = 1
        try:
            yield
        finally:
            _depth.n = 0
            try:
                path.unlink()
            except FileNotFoundError:
                pass




@dataclass
class PostRecord:
    kind: str  # "repost" | "generated"
    source_url: str = ""  # YouTube URL for reposts
    title: str = ""
    file_path: str = ""
    caption: str = ""
    clip_start: float = 0.0
    clip_end: float = 0.0
    slot: str = ""  # e.g. "08:00"
    posted_at: str = ""
    channel: str = ""
    platform: str = "tiktok"  # "tiktok" | "youtube"
    remote_id: str = ""  # posted video id / url on the target platform


@dataclass
class QueueItem:
    url: str
    channel: str
    title: str
    video_id: str
    max_moments: int = 1
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _quarantine(path: Path, reason: str) -> None:
    """Move an unreadable state file aside so the next write cannot clobber
    the only copy of the post history."""
    backup = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        path.replace(backup)
        log.error("State file %s is unreadable (%s); moved to %s", path, reason, backup)
    except OSError as e:
        log.error("State file %s is unreadable (%s) and could not be moved: %s",
                  path, reason, e)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _quarantine(path, f"invalid JSON: {e}")
        return []
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
        return []
    if not isinstance(data, list):
        _quarantine(path, "top-level value is not a list")
        return []
    return data


def _write(path: Path, items: list[dict]) -> None:
    """Atomic write: unique temp file + fsync + os.replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        # On Windows a concurrent *reader* (load_posts() outside the lock)
        # briefly blocks replacing the target; retry instead of failing.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- posted
def load_posts() -> list[dict]:
    return _read(STATE_FILE)


def platform_of(record: dict) -> str:
    """Platform a post record belongs to (records written before multi-target
    posting existed are TikTok posts)."""
    return str(record.get("platform") or "tiktok").lower()


def _field_matches(record: dict, key: str, value) -> bool:
    if key == "platform":
        return platform_of(record) == str(value).lower()
    return record.get(key) == value


def is_posted(**fields) -> bool:
    with _state_lock():
        posts = load_posts()
        for p in posts:
            if all(_field_matches(p, k, v) for k, v in fields.items()):
                return True
    return False


def video_posted(source_url: str, platform: str = "tiktok") -> bool:
    return is_posted(kind="repost", source_url=source_url, platform=platform)


def source_posted_on(source_url: str, platform: str) -> bool:
    """True when *source_url* has at least one clip posted to *platform*."""
    return video_posted(source_url, platform=platform)


def video_posted_on_all(source_url: str, platforms: list[str]) -> bool:
    """True when *source_url* has been posted on every listed platform."""
    if not platforms:
        return False
    return all(video_posted(source_url, platform=p) for p in platforms)


def add_post(record: PostRecord) -> None:
    with _state_lock():
        posts = load_posts()
        posts.append(asdict(record))
        _write(STATE_FILE, posts)


def slot_posted_today(slot: str, platform: str = "tiktok") -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with _state_lock():
        return any(
            p.get("slot") == slot
            and (p.get("posted_at") or "").startswith(today)
            and platform_of(p) == str(platform).lower()
            for p in load_posts()
        )


# ---------------------------------------------------------------- queue
def load_queue() -> list[dict]:
    return _read(QUEUE_FILE)


def queue_has(video_id: str) -> bool:
    return any(q.get("video_id") == video_id for q in load_queue())


def remove_from_queue(source_url: str) -> None:
    """Remove queue entries whose video URL has been posted (keeps queue pruned)."""
    with _state_lock():
        queue = load_queue()
        pruned = [q for q in queue if q.get("url") != source_url]
        if len(pruned) != len(queue):
            _write(QUEUE_FILE, pruned)


def add_to_queue(item: QueueItem) -> None:
    with _state_lock():
        queue = load_queue()
        if any(q.get("video_id") == item.video_id for q in queue):
            return
        queue.append(asdict(item))
        _write(QUEUE_FILE, queue)


def pull_next_unprocessed(platform: str = "tiktok") -> dict | None:
    """Return oldest queue item not yet posted to *platform*, without removing it."""
    platform = str(platform).lower()
    with _state_lock():
        queue = load_queue()
        posts = load_posts()
        posted = {(p.get("source_url"), p.get("kind"), platform_of(p)) for p in posts}
        for q in queue:
            if (q.get("url"), "repost", platform) not in posted:
                return q
    return None