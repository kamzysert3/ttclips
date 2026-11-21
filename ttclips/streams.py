"""Topic-based stream discovery: Twitch clips + finished-stream VODs.

``config/streams.json`` lists *topics* (e.g. "GTA6"). For each topic ttclips
asks the platforms what was streamed recently:

* **clips** (Twitch) - the clips viewers already cut in that category,
  sorted by views. They are short, so each one is posted whole (reframed +
  captioned) without Gemini ranking.
* **vods** - finished streams: Twitch archive VODs in the category and
  YouTube live broadcasts that have ended (``eventType=completed``). These
  go through the normal whisper + Gemini highlight pipeline.

Rights guardrail: only streamers on ``allowed_streamers`` are queued for
posting. Everything else lands in ``data/stream_candidates.json`` so you can
review it and approve a streamer with ``ttclips --approve-streamer NAME``
(which also moves their pending candidates into the queue). Set
``"require_allowlist": false`` only if you have rights to everything in the
category - see docs/LEGAL.md.

Credentials (``.env``): ``TWITCH_CLIENT_ID`` + ``TWITCH_CLIENT_SECRET``
(free app at https://dev.twitch.tv/console) and/or ``YOUTUBE_API_KEY``
(YouTube Data API v3 key). A platform without credentials is skipped.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from ttclips.config import CACHE_DIR, CONFIG_DIR, DATA_DIR, load_env, load_json
from ttclips.platforms import configured_targets
from ttclips.state import (QueueItem, _read, _state_lock, _write, add_to_queue,
                           load_queue, video_posted_on_all)

log = logging.getLogger(__name__)

STREAMS_CONFIG = CONFIG_DIR / "streams.json"
CANDIDATES_FILE = DATA_DIR / "stream_candidates.json"
TWITCH_TOKEN_CACHE = CACHE_DIR / "twitch_token.json"

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

# A VOD whose end is this close to "now" is probably still live and growing.
STILL_LIVE_GRACE = timedelta(minutes=15)


# ---------------------------------------------------------------- http
def http_json(method: str, url: str, *, params: dict | None = None,
              headers: dict | None = None) -> dict:
    """One JSON request. Module-level so tests can monkeypatch it."""
    import httpx

    resp = httpx.request(method, url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- config
def load_streams_config() -> dict:
    return load_json(STREAMS_CONFIG, default={"enabled": False, "topics": []})


def _save_streams_config(cfg: dict) -> None:
    STREAMS_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")


def _norm(name: str) -> str:
    return str(name or "").strip().lower()


def is_allowed(streamer: str, streamer_id: str, cfg: dict) -> bool:
    if not cfg.get("require_allowlist", True):
        return True
    allowed = {_norm(s) for s in cfg.get("allowed_streamers", [])}
    return _norm(streamer) in allowed or _norm(streamer_id) in allowed


# ---------------------------------------------------------------- time helpers
def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_twitch_duration(value: str) -> float:
    """``"3h2m1s"`` -> 10921.0"""
    total = 0.0
    for amount, unit in re.findall(r"(\d+)([hms])", str(value or "")):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def parse_iso_duration(value: str) -> float:
    """ISO 8601 ``"PT1H2M3S"`` (YouTube) -> 3723.0"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(value or ""))
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


# ---------------------------------------------------------------- twitch
def _twitch_creds() -> tuple[str, str]:
    env = load_env()
    return env.get("TWITCH_CLIENT_ID", ""), env.get("TWITCH_CLIENT_SECRET", "")


def _twitch_token(client_id: str, secret: str) -> str:
    """App access token (client-credentials), cached until shortly before expiry."""
    try:
        cached = json.loads(TWITCH_TOKEN_CACHE.read_text(encoding="utf-8"))
        if cached.get("client_id") == client_id and cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]
    except (OSError, ValueError, KeyError):
        pass
    data = http_json("POST", TWITCH_TOKEN_URL, params={
        "client_id": client_id, "client_secret": secret,
        "grant_type": "client_credentials"})
    token = data["access_token"]
    try:
        TWITCH_TOKEN_CACHE.write_text(json.dumps({
            "client_id": client_id, "access_token": token,
            "expires_at": time.time() + float(data.get("expires_in", 3600))}),
            encoding="utf-8")
    except OSError as e:
        log.warning("Could not cache Twitch token: %s", e)
    return token


class Twitch:
    def __init__(self, client_id: str, secret: str):
        self.client_id = client_id
        self.headers = {"Client-Id": client_id,
                        "Authorization": f"Bearer {_twitch_token(client_id, secret)}"}

    def get(self, path: str, params: dict) -> list[dict]:
        return http_json("GET", f"{TWITCH_API}/{path}", params=params,
                         headers=self.headers).get("data", [])

    def game_id(self, topic: dict) -> str:
        if topic.get("twitch_game_id"):
            return str(topic["twitch_game_id"])
        name = topic.get("twitch_category", "")
        if not name:
            return ""
        games = self.get("games", {"name": name})
        if not games:
            log.warning("Twitch category %r not found (check the exact name on twitch.tv)", name)
            return ""
        return str(games[0]["id"])

    def clips(self, game_id: str, since: datetime, language: str = "") -> list[dict]:
        out = []
        for c in self.get("clips", {"game_id": game_id, "started_at": _rfc3339(since),
                                    "ended_at": _rfc3339(datetime.now(timezone.utc)),
                                    "first": 100}):
            if language and _norm(c.get("language")) != _norm(language):
                continue
            out.append({
                "platform": "twitch", "kind": "clip",
                "url": c.get("url") or f"https://clips.twitch.tv/{c['id']}",
                "video_id": f"twitchclip_{c['id']}", "cache_id": str(c["id"]),
                "title": c.get("title") or "Twitch clip",
                "streamer": c.get("broadcaster_name", ""),
                "streamer_id": str(c.get("broadcaster_id", "")),
                "views": int(c.get("view_count") or 0),
                "duration": float(c.get("duration") or 0),
                "created_at": c.get("created_at", ""),
            })
        return out

    def vods(self, game_id: str, since: datetime, language: str = "") -> list[dict]:
        params = {"game_id": game_id, "type": "archive", "period": "week",
                  "sort": "views", "first": 50}
        if language:
            params["language"] = language
        now = datetime.now(timezone.utc)
        out = []
        for v in self.get("videos", params):
            created = _parse_ts(v.get("created_at", ""))
            duration = parse_twitch_duration(v.get("duration", ""))
            if created is None or created < since:
                continue
            if created + timedelta(seconds=duration) > now - STILL_LIVE_GRACE:
                continue  # stream is (probably) still live - wait for it to end
            out.append({
                "platform": "twitch", "kind": "vod",
                "url": v.get("url") or f"https://www.twitch.tv/videos/{v['id']}",
                "video_id": f"twitchvod_v{v['id']}", "cache_id": f"v{v['id']}",
                "title": v.get("title") or "Twitch stream",
                "streamer": v.get("user_login") or v.get("user_name", ""),
                "streamer_id": str(v.get("user_id", "")),
                "views": int(v.get("view_count") or 0),
                "duration": duration,
                "created_at": v.get("created_at", ""),
            })
        return out


# ---------------------------------------------------------------- youtube
def youtube_finished_streams(api_key: str, query: str, since: datetime,
                             language: str = "") -> list[dict]:
    """Ended live broadcasts matching *query* (costs ~101 quota units)."""
    params = {"part": "snippet", "q": query, "type": "video", "eventType": "completed",
              "order": "viewCount", "publishedAfter": _rfc3339(since),
              "maxResults": 25, "key": api_key}
    if language:
        params["relevanceLanguage"] = language
    items = http_json("GET", f"{YOUTUBE_API}/search", params=params).get("items", [])
    ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    if not ids:
        return []
    details = http_json("GET", f"{YOUTUBE_API}/videos", params={
        "part": "contentDetails,statistics", "id": ",".join(ids), "key": api_key,
    }).get("items", [])
    by_id = {d["id"]: d for d in details}
    out = []
    for i in items:
        vid = i.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = i.get("snippet", {})
        d = by_id.get(vid, {})
        out.append({
            "platform": "youtube", "kind": "vod",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "video_id": vid, "cache_id": vid,
            "title": sn.get("title") or "YouTube stream",
            "streamer": sn.get("channelTitle", ""),
            "streamer_id": sn.get("channelId", ""),
            "views": int(d.get("statistics", {}).get("viewCount") or 0),
            "duration": parse_iso_duration(d.get("contentDetails", {}).get("duration", "")),
            "created_at": sn.get("publishedAt", ""),
        })
    return out


# ---------------------------------------------------------------- discovery
def _topic_hashtags(topic: dict) -> list[str]:
    tags = topic.get("hashtags") or []
    return [str(t).lstrip("#").strip() for t in tags if str(t).strip()]


def discover_topic(topic: dict, cfg: dict, *, twitch: Twitch | None,
                   youtube_key: str) -> list[dict]:
    """All candidates for one topic that pass the topic's filters."""
    modes = topic.get("modes") or ["clips", "vods"]
    since = datetime.now(timezone.utc) - timedelta(hours=float(topic.get("lookback_hours", 24)))
    lang = topic.get("language", "")
    max_vod = float(cfg.get("max_vod_hours", 4)) * 3600
    found: list[dict] = []

    if twitch is not None and (topic.get("twitch_category") or topic.get("twitch_game_id")):
        try:
            gid = twitch.game_id(topic)
            if gid and "clips" in modes:
                min_views = int(topic.get("min_clip_views", 100))
                found += [c for c in twitch.clips(gid, since, lang) if c["views"] >= min_views]
            if gid and "vods" in modes:
                found += twitch.vods(gid, since, lang)
        except Exception as e:
            log.warning("Twitch discovery failed for topic %s: %s", topic.get("name"), e)

    if youtube_key and topic.get("youtube_query") and "vods" in modes:
        try:
            found += youtube_finished_streams(youtube_key, topic["youtube_query"], since, lang)
        except Exception as e:
            log.warning("YouTube discovery failed for topic %s: %s", topic.get("name"), e)

    keep = []
    for c in found:
        if c["kind"] == "vod":
            if c["views"] < int(topic.get("min_vod_views", 0)):
                continue
            if c["duration"] and c["duration"] > max_vod:
                log.info("Skipping %s - %.1fh is over max_vod_hours", c["url"],
                         c["duration"] / 3600)
                continue
        c["topic"] = topic.get("name", "")
        c["hashtags"] = _topic_hashtags(topic)
        c["max_moments"] = 1 if c["kind"] == "clip" else int(topic.get("max_moments", 2))
        keep.append(c)
    keep.sort(key=lambda c: c["views"], reverse=True)
    return keep


def _queue_item(c: dict) -> QueueItem:
    return QueueItem(
        url=c["url"],
        channel=c["streamer"] or c["platform"],
        title=c["title"],
        video_id=c["video_id"],
        max_moments=int(c.get("max_moments", 1)),
        source=f"{c['platform']}_{c['kind']}",
        whole=c["kind"] == "clip",
        hashtags=list(c.get("hashtags") or []),
        cache_id=c.get("cache_id", ""),
        duration=float(c.get("duration") or 0),
        topic=c.get("topic", ""),
    )


def load_candidates() -> list[dict]:
    return _read(CANDIDATES_FILE)


def _add_candidates(new: list[dict]) -> int:
    if not new:
        return 0
    with _state_lock():
        existing = load_candidates()
        seen = {c.get("video_id") for c in existing}
        added = [c for c in new if c["video_id"] not in seen]
        if added:
            _write(CANDIDATES_FILE, existing + added)
    return len(added)


def refresh_streams(platform: str) -> dict:
    """Discover stream content for every enabled topic.

    Allowlisted streamers go straight into the posting queue (capped by each
    topic's ``max_queue_per_run``); the rest become review candidates.
    Returns ``{"queued": [...], "candidates": N}``.
    """
    cfg = load_streams_config()
    result = {"queued": [], "candidates": 0}
    if not cfg.get("enabled"):
        log.info("Stream discovery is disabled (config/streams.json \"enabled\": false)")
        return result
    topics = [t for t in cfg.get("topics", []) if t.get("enabled", True)]
    if not topics:
        return result

    cid, secret = _twitch_creds()
    twitch = None
    if cid and secret:
        try:
            twitch = Twitch(cid, secret)
        except Exception as e:
            log.warning("Twitch auth failed: %s", e)
    else:
        log.info("TWITCH_CLIENT_ID/SECRET not set - skipping Twitch")
    yt_key = load_env().get("YOUTUBE_API_KEY", "")
    if not yt_key:
        log.info("YOUTUBE_API_KEY not set - skipping YouTube stream search")

    queued_ids = {q.get("video_id") for q in load_queue()}
    known_candidates = {c.get("video_id") for c in load_candidates()}
    targets = configured_targets()
    review: list[dict] = []

    if platform == "twitch":
        yt_key = ""
        log.info("Discovering Twitch clips + VODs for %d topic(s)", len(topics))
    elif platform == "youtube":
        twitch = None
        log.info("Discovering YouTube finished streams for %d topic(s)", len(topics))

    for topic in topics:
        cap = int(topic.get("max_queue_per_run", 3))
        n = 0
        for c in discover_topic(topic, cfg, twitch=twitch, youtube_key=yt_key):
            vid = c["video_id"]
            if vid in queued_ids or vid in known_candidates:
                continue
            if video_posted_on_all(c["url"], targets):
                continue
            if not is_allowed(c["streamer"], c["streamer_id"], cfg):
                review.append(c)
                known_candidates.add(vid)
                continue
            if n >= cap:
                continue
            add_to_queue(_queue_item(c))
            queued_ids.add(vid)
            result["queued"].append(c)
            n += 1
        log.info("Topic %s: %d queued", topic.get("name"), n)

    result["candidates"] = _add_candidates(review)
    if result["candidates"]:
        log.info("%d stream item(s) from streamers not on allowed_streamers saved to %s - "
                 "approve a streamer with `ttclips --approve-streamer NAME`",
                 result["candidates"], CANDIDATES_FILE)
    return result


def approve_streamer(name: str) -> int:
    """Add *name* to ``allowed_streamers`` and queue their pending candidates.

    Returns how many candidates moved into the queue.
    """
    cfg = load_streams_config()
    allowed = cfg.setdefault("allowed_streamers", [])
    if _norm(name) not in {_norm(s) for s in allowed}:
        allowed.append(name)
        _save_streams_config(cfg)
        log.info("Added %s to allowed_streamers", name)
    moved = 0
    with _state_lock():
        remaining = []
        for c in load_candidates():
            if _norm(name) in (_norm(c.get("streamer")), _norm(c.get("streamer_id"))):
                add_to_queue(_queue_item(c))
                moved += 1
            else:
                remaining.append(c)
        if moved:
            _write(CANDIDATES_FILE, remaining)
    log.info("Queued %d pending item(s) from %s", moved, name)
    return moved



def candidates_summary(platform: str, limit: int = 20) -> list[str]:
    """Human-readable lines for ``ttclips --stream-candidates``."""
    if platform:
        candidates = [c for c in load_candidates() if c.get("platform") == platform]
        lines = [f"Stream candidates for platform {platform}:"]
        for candidate in candidates:
            lines.append(f"  {candidate.get('streamer', '?'):<24} {candidate.get('views', 0):>9,} views  "
                         f"{candidate.get('url', '')}")
        return lines
    by_streamer: dict[str, list[dict]] = {}
    for c in load_candidates():
        by_streamer.setdefault(c.get("streamer") or "?", []).append(c)
    rows = sorted(by_streamer.items(), key=lambda kv: -sum(c.get("views", 0) for c in kv[1]))
    lines = []
    for streamer, items in rows[:limit]:
        views = sum(c.get("views", 0) for c in items)
        topics = ", ".join(sorted({c.get("topic", "") for c in items if c.get("topic")}))
        lines.append(f"{streamer:<24} {len(items):>3} item(s)  {views:>9,} views  [{topics}]  "
                     f"{items[0].get('platform', '')}")
    return lines
