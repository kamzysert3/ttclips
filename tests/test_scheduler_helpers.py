"""Pure helpers behind slot fairness and per-platform dedup."""
from __future__ import annotations

import ttclips.scheduler as sched


def _rec(url, platform="tiktok", start=10.0, end=40.0, channel="c", posted_at="2026-01-01"):
    return {"kind": "repost", "source_url": url, "platform": platform,
            "clip_start": start, "clip_end": end, "channel": channel,
            "posted_at": posted_at}


def test_merge_hashtags_dedups_and_limits():
    out = sched._merge_hashtags(["#Funny", "funny", "cats", "a", "b", "c"],
                                limit=3, defaults="#fyp #cats")
    assert out == "#Funny #cats #a #fyp"


def test_merge_hashtags_string_and_empty():
    assert sched._merge_hashtags("x y", defaults="#z") == "#x #y #z"
    assert sched._merge_hashtags(None, defaults="#z") == "#z"


def test_fair_order_prefers_least_recently_posted_channel():
    posts = [_rec("u", channel="A", posted_at="2026-01-02"),
             _rec("u", channel="B", posted_at="2026-01-01")]
    queue = [{"channel": "A", "discovered_at": "1"},
             {"channel": "B", "discovered_at": "2"},
             {"channel": "C", "discovered_at": "3"}]
    assert [q["channel"] for q in sched._fair_order(queue, posts)] == ["C", "B", "A"]
    assert sched._pick_fair_target([], posts) is None


def test_clip_posted_is_platform_and_time_scoped():
    posts = [_rec("u", "tiktok", 10.0, 40.0)]
    assert sched._clip_posted(posts, "u", {"start": 10.01, "end": 39.99}, "tiktok")
    assert not sched._clip_posted(posts, "u", {"start": 10.0, "end": 40.0}, "youtube")
    assert not sched._clip_posted(posts, "u", {"start": 50.0, "end": 80.0}, "tiktok")
    assert not sched._clip_posted(posts, "other", {"start": 10.0, "end": 40.0}, "tiktok")


def test_pending_sources_waits_for_every_target():
    queue = [{"url": "u", "max_moments": 1}, {"url": "v", "max_moments": 2}]
    posts = [_rec("u", "tiktok"), _rec("v", "tiktok"), _rec("v", "youtube")]
    assert [q["url"] for q in sched._pending_sources(queue, posts, ["tiktok"])] == ["v"]
    assert [q["url"] for q in sched._pending_sources(queue, posts, ["tiktok", "youtube"])] == ["u", "v"]
    posts += [_rec("u", "youtube"), _rec("v", "tiktok", 50, 80), _rec("v", "youtube", 50, 80)]
    assert sched._pending_sources(queue, posts, ["tiktok", "youtube"]) == []


def test_pending_sources_caps_moments_at_five():
    queue = [{"url": "u", "max_moments": 99}]
    posts = [_rec("u", start=i * 100, end=i * 100 + 30) for i in range(5)]
    assert sched._pending_sources(queue, posts, ["tiktok"]) == []
