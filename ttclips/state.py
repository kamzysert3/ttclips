"""Persistent state: dedup tracking for posted clips + monitor queue."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ttclips.config import QUEUE_FILE, STATE_FILE

_lock = threading.Lock()


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


@dataclass
class QueueItem:
    url: str
    channel: str
    title: str
    video_id: str
    max_moments: int = 1
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write(path: Path, items: list[dict]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- posted
def load_posts() -> list[dict]:
    return _read(STATE_FILE)


def is_posted(**fields) -> bool:
    with _lock:
        posts = load_posts()
        for p in posts:
            if all(p.get(k) == v for k, v in fields.items()):
                return True
    return False


def video_posted(source_url: str) -> bool:
    return is_posted(kind="repost", source_url=source_url)


def add_post(record: PostRecord) -> None:
    with _lock:
        posts = load_posts()
        posts.append(asdict(record))
        _write(STATE_FILE, posts)


def slot_posted_today(slot: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        return any(
            p.get("slot") == slot and (p.get("posted_at") or "").startswith(today)
            for p in load_posts()
        )


# ---------------------------------------------------------------- queue
def load_queue() -> list[dict]:
    return _read(QUEUE_FILE)


def queue_has(video_id: str) -> bool:
    return any(q.get("video_id") == video_id for q in load_queue())


def remove_from_queue(source_url: str) -> None:
    """Remove queue entries whose video URL has been posted (keeps queue pruned)."""
    with _lock:
        queue = load_queue()
        pruned = [q for q in queue if q.get("url") != source_url]
        if len(pruned) != len(queue):
            _write(QUEUE_FILE, pruned)


def add_to_queue(item: QueueItem) -> None:
    with _lock:
        queue = load_queue()
        if any(q.get("video_id") == item.video_id for q in queue):
            return
        queue.append(asdict(item))
        _write(QUEUE_FILE, queue)


def pull_next_unprocessed() -> dict | None:
    """Return oldest queue item that hasn't been posted, without removing it."""
    with _lock:
        queue = load_queue()
        posts = load_posts()
        posted = {(p.get("source_url"), p.get("kind")) for p in posts}
        for q in queue:
            if (q.get("url"), "repost") not in posted:
                return q
    return None