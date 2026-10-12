"""YouTube RSS feed poller: discovers new videos from configured channels."""
from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET

from ttclips.config import load_channels
from ttclips.state import QueueItem, add_to_queue, load_queue, queue_has, video_posted

log = logging.getLogger(__name__)

YT_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _channel_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _fetch_feed(channel_id: str) -> list[dict]:
    url = _channel_feed_url(channel_id)
    log.info("Fetching RSS: %s", url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ttclips/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except Exception as e:
        log.warning("Feed fetch failed for %s: %s", channel_id, e)
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        log.warning("XML parse failed for %s: %s", channel_id, e)
        return []
    entries = root.findall("atom:entry", YT_NS)
    results = []
    for entry in entries:
        vid_el = entry.find("atom:id", YT_NS)
        if vid_el is None:
            continue
        full_id = vid_el.text or ""
        if full_id.startswith("yt:video:"):
            vid = full_id[len("yt:video:"):]
        else:
            vid = full_id
        title_el = entry.find("atom:title", YT_NS)
        link_el = entry.find("atom:link", YT_NS)
        pub_el = entry.find("atom:published", YT_NS)
        author_el = entry.find("atom:author/atom:name", YT_NS)
        video_url = ""
        if link_el is not None:
            video_url = link_el.get("href", "")
        if not video_url and vid:
            video_url = f"https://www.youtube.com/watch?v={vid}"
        results.append({
            "video_id": vid,
            "title": (title_el.text if title_el is not None else "").strip(),
            "url": video_url,
            "published": pub_el.text.strip() if pub_el is not None and pub_el.text else "",
            "author": author_el.text.strip() if author_el is not None and author_el.text else "",
        })
    log.info("Found %d entries for channel %s", len(results), channel_id)
    return results


def _matches_keywords(title: str, channel_keywords: list[str]) -> bool:
    """Empty keywords accept all videos; otherwise at least one keyword must match."""
    if not channel_keywords:
        return True
    title_lower = title.lower()
    return any(k.lower() in title_lower for k in channel_keywords)


def refresh_feeds() -> list[dict]:
    """Poll all enabled channels and add newly discovered videos to the queue.

    Returns the total list of newly added items.
    """
    cfg = load_channels()
    channels = [ch for ch in cfg.get("channels", []) if ch.get("enabled", True)]
    excluded = [k.lower() for k in cfg.get("excluded_keywords", [])]
    existing_ids = {q["video_id"] for q in load_queue()}
    min_dur = cfg.get("min_duration_sec", 30)
    max_dur = cfg.get("max_duration_sec", 1800)
    added: list[dict] = []
    for ch in channels:
        cid = ch.get("channel_id", "")
        if not cid:
            continue
        entries = _fetch_feed(cid)
        for e in entries:
            vid = e["video_id"]
            if vid in existing_ids:
                continue
            if video_posted(e["url"]):
                continue
            title_lower = e["title"].lower()
            if any(ex.lower() in title_lower for ex in excluded):
                continue
            if not _matches_keywords(e["title"], ch.get("keywords", [])):
                continue
            item = QueueItem(
                url=e["url"],
                channel=ch.get("name", cid),
                title=e["title"],
                video_id=vid,
                max_moments=int(ch.get("max_moments", 1) or 1),
            )
            add_to_queue(item)
            existing_ids.add(vid)
            added.append(dict(asdict(item) if hasattr(item, "__dataclass_fields__") else item))
    log.info("Feeds refreshed: %d new videos added to queue", len(added))
    return added


def get_next_repost() -> dict | None:
    """Return the oldest unposted queue entry (for repost pipeline)."""
    queue = load_queue()
    for item in queue:
        if not video_posted(item["url"]):
            return item
    return None


def asdict(item):
    if hasattr(item, "__dataclass_fields__"):
        return {k: getattr(item, k) for k in item.__dataclass_fields__}
    return dict(item)