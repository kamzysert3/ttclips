"""Topic-based stream discovery (ttclips.streams) + whole-clip posting."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import ttclips.scheduler as sched
import ttclips.streams as streams
from ttclips.state import load_posts, load_queue

from tests.test_url_and_review import pipeline  # noqa: F401  (fixture)

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


CLIPS = [
    {"id": "FunnyClipA", "url": "https://clips.twitch.tv/FunnyClipA", "broadcaster_id": "1",
     "broadcaster_name": "AllowedGuy", "title": "insane heist", "view_count": 5000,
     "duration": 28.5, "created_at": _iso(NOW - timedelta(hours=2)), "language": "en"},
    {"id": "StrangerClip", "url": "https://clips.twitch.tv/StrangerClip", "broadcaster_id": "2",
     "broadcaster_name": "Stranger", "title": "wow", "view_count": 9000,
     "duration": 30.0, "created_at": _iso(NOW - timedelta(hours=1)), "language": "en"},
    {"id": "LowViews", "url": "https://clips.twitch.tv/LowViews", "broadcaster_id": "1",
     "broadcaster_name": "AllowedGuy", "title": "meh", "view_count": 3,
     "duration": 20.0, "created_at": _iso(NOW - timedelta(hours=1)), "language": "en"},
    {"id": "Spanish", "url": "https://clips.twitch.tv/Spanish", "broadcaster_id": "1",
     "broadcaster_name": "AllowedGuy", "title": "hola", "view_count": 9999,
     "duration": 20.0, "created_at": _iso(NOW - timedelta(hours=1)), "language": "es"},
]
VODS = [
    # finished 3h stream
    {"id": "100", "user_id": "1", "user_login": "allowedguy", "title": "GTA6 day 1",
     "url": "https://www.twitch.tv/videos/100", "view_count": 800, "duration": "3h0m0s",
     "created_at": _iso(NOW - timedelta(hours=5))},
    # still live: started 1h ago, 1h long so far
    {"id": "101", "user_id": "1", "user_login": "allowedguy", "title": "live now",
     "url": "https://www.twitch.tv/videos/101", "view_count": 900, "duration": "1h0m0s",
     "created_at": _iso(NOW - timedelta(hours=1))},
    # too long
    {"id": "102", "user_id": "1", "user_login": "allowedguy", "title": "marathon",
     "url": "https://www.twitch.tv/videos/102", "view_count": 900, "duration": "10h0m0s",
     "created_at": _iso(NOW - timedelta(hours=20))},
]
YT_SEARCH = {"items": [{"id": {"videoId": "ytLive1"}, "snippet": {
    "title": "GTA 6 stream", "channelTitle": "YT Streamer", "channelId": "UCabc",
    "publishedAt": _iso(NOW - timedelta(hours=6))}}]}
YT_VIDEOS = {"items": [{"id": "ytLive1", "contentDetails": {"duration": "PT2H1M"},
                        "statistics": {"viewCount": "12000"}}]}


@pytest.fixture
def fake_api(monkeypatch, state_files, tmp_path):
    calls: list[tuple[str, str, dict]] = []

    def http_json(method, url, *, params=None, headers=None):
        calls.append((method, url, dict(params or {})))
        if url == streams.TWITCH_TOKEN_URL:
            return {"access_token": "tok", "expires_in": 3600}
        if url.endswith("/games"):
            return {"data": [{"id": "999", "name": params["name"]}]}
        if url.endswith("/clips"):
            return {"data": CLIPS}
        if url.endswith("/videos") and "api.twitch.tv" in url:
            return {"data": VODS}
        if url.endswith("/search"):
            return {"items": YT_SEARCH["items"]}
        if url.endswith("/videos"):
            return YT_VIDEOS
        raise AssertionError(f"unexpected call {url}")

    cfg_path = tmp_path / "streams.json"
    monkeypatch.setattr(streams, "http_json", http_json)
    monkeypatch.setattr(streams, "STREAMS_CONFIG", cfg_path)
    monkeypatch.setattr(streams, "CANDIDATES_FILE", tmp_path / "candidates.json")
    monkeypatch.setattr(streams, "TWITCH_TOKEN_CACHE", tmp_path / "twitch_token.json")
    monkeypatch.setattr(streams, "load_env", lambda: {
        "TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "sec", "YOUTUBE_API_KEY": "ytk"})
    monkeypatch.setattr(streams, "configured_targets", lambda: ["tiktok"])

    def write_cfg(**over):
        cfg = {"enabled": True, "require_allowlist": True,
               "allowed_streamers": ["allowedguy", "UCabc"], "max_vod_hours": 4,
               "topics": [{"name": "GTA6", "twitch_category": "Grand Theft Auto VI",
                           "youtube_query": "GTA 6", "language": "en",
                           "min_clip_views": 100, "max_queue_per_run": 10,
                           "hashtags": ["#gta6"]}]}
        cfg.update(over)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    write_cfg()
    return {"calls": calls, "write_cfg": write_cfg, "cfg_path": cfg_path}


def test_duration_parsers():
    assert streams.parse_twitch_duration("3h2m1s") == 10921
    assert streams.parse_twitch_duration("45s") == 45
    assert streams.parse_iso_duration("PT1H2M3S") == 3723
    assert streams.parse_iso_duration("P1DT1S") == 86401
    assert streams.parse_iso_duration("garbage") == 0


def test_disabled_does_nothing(fake_api):
    fake_api["write_cfg"](enabled=False)
    assert streams.refresh_streams() == {"queued": [], "candidates": 0}
    assert fake_api["calls"] == []


def test_refresh_queues_allowed_and_holds_strangers(fake_api):
    result = streams.refresh_streams()
    queued = {q["video_id"]: q for q in load_queue()}
    # allowed clip (whole), finished VOD (ranked) and the allowlisted YouTube channel
    assert set(queued) == {"twitchclip_FunnyClipA", "twitchvod_v100", "ytLive1"}
    clip = queued["twitchclip_FunnyClipA"]
    assert clip["whole"] is True and clip["max_moments"] == 1
    assert clip["hashtags"] == ["gta6"] and clip["topic"] == "GTA6"
    assert clip["source"] == "twitch_clip" and clip["cache_id"] == "FunnyClipA"
    vod = queued["twitchvod_v100"]
    assert vod["whole"] is False and vod["cache_id"] == "v100"
    assert queued["ytLive1"]["url"] == "https://www.youtube.com/watch?v=ytLive1"
    # stranger is held for review, not queued
    assert result["candidates"] == 1
    assert [c["streamer"] for c in streams.load_candidates()] == ["Stranger"]
    # completed-broadcast search was used
    search = next(p for _, u, p in fake_api["calls"] if u.endswith("/search"))
    assert search["eventType"] == "completed" and search["q"] == "GTA 6"


def test_refresh_is_idempotent(fake_api):
    streams.refresh_streams()
    again = streams.refresh_streams()
    assert again == {"queued": [], "candidates": 0}
    assert len(load_queue()) == 3
    assert len(streams.load_candidates()) == 1


def test_queue_cap_per_topic(fake_api):
    cfg = fake_api["write_cfg"]()
    cfg["topics"][0]["max_queue_per_run"] = 1
    fake_api["cfg_path"].write_text(json.dumps(cfg), encoding="utf-8")
    assert len(streams.refresh_streams()["queued"]) == 1


def test_allowlist_can_be_turned_off(fake_api):
    fake_api["write_cfg"](require_allowlist=False, allowed_streamers=[])
    result = streams.refresh_streams()
    assert "twitchclip_StrangerClip" in {q["video_id"] for q in load_queue()}
    assert result["candidates"] == 0


def test_approve_streamer_moves_candidates_and_updates_config(fake_api):
    streams.refresh_streams()
    moved = streams.approve_streamer("stranger")  # case-insensitive
    assert moved == 1
    assert "twitchclip_StrangerClip" in {q["video_id"] for q in load_queue()}
    assert streams.load_candidates() == []
    cfg = json.loads(fake_api["cfg_path"].read_text(encoding="utf-8"))
    assert "stranger" in cfg["allowed_streamers"]
    # approving again is a no-op
    assert streams.approve_streamer("Stranger") == 0
    assert cfg["allowed_streamers"].count("stranger") == 1


def test_twitch_token_is_cached(fake_api):
    streams.refresh_streams()
    streams.refresh_streams()
    token_calls = [c for c in fake_api["calls"] if c[1] == streams.TWITCH_TOKEN_URL]
    assert len(token_calls) == 1


def test_missing_credentials_skip_platforms(fake_api, monkeypatch):
    monkeypatch.setattr(streams, "load_env", lambda: {})
    assert streams.refresh_streams() == {"queued": [], "candidates": 0}
    assert fake_api["calls"] == []


def test_candidates_summary(fake_api):
    streams.refresh_streams()
    lines = streams.candidates_summary()
    assert len(lines) == 1 and "Stranger" in lines[0] and "GTA6" in lines[0]


# ---------------------------------------------------------------- whole-clip posting
def test_whole_clip_skips_ranking_and_posts_full_length(pipeline, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sched, "rank_highlights",
                        lambda *a, **k: pytest.fail("whole clips must not be ranked"))
    monkeypatch.setattr(sched, "transcribe", lambda p, **k: [])  # no speech is fine
    monkeypatch.setattr(sched, "probe_duration", lambda p: 28.5)
    item = {"url": "https://clips.twitch.tv/X", "video_id": "twitchclip_X", "cache_id": "X",
            "title": "insane heist", "channel": "AllowedGuy", "whole": True,
            "hashtags": ["gta6"], "max_moments": 1}
    ok = sched._process_source(item, targets=["tiktok"], slot_time="12:00", posts=[],
                               queue_platforms=None)
    assert ok
    assert pipeline["download"] == [("https://clips.twitch.tv/X", "X")]
    (platform, caption), = pipeline["uploads"]
    assert platform == "tiktok"
    assert caption.startswith("insane heist") and "#gta6" in caption
    post, = load_posts()
    assert (post["clip_start"], post["clip_end"]) == (0.0, 28.5)


def test_topic_hashtags_lead_ranked_vod_captions(pipeline):  # noqa: F811
    item = {"url": "https://www.twitch.tv/videos/100", "video_id": "twitchvod_v100",
            "title": "GTA6 day 1", "channel": "allowedguy", "hashtags": ["gta6"],
            "max_moments": 1}
    assert sched._process_source(item, targets=["tiktok"], slot_time="12:00", posts=[],
                                 queue_platforms=None)
    (_, caption), = pipeline["uploads"]
    tags = caption.split("\n\n")[-1].split()
    assert tags[0] == "#gta6" and "#funny" in tags
