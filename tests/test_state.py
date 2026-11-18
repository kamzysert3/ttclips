from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest

import ttclips.state as state
from ttclips.state import PostRecord, QueueItem


def _post(url="https://youtu.be/a", platform="tiktok", slot="08:00", **kw):
    return PostRecord(kind="repost", source_url=url, platform=platform, slot=slot,
                      posted_at=datetime.now().isoformat(timespec="seconds"), **kw)


# ---------------------------------------------------------------- posts
def test_posts_are_tracked_per_platform(state_files):
    state.add_post(_post(platform="tiktok"))
    assert state.video_posted("https://youtu.be/a", "tiktok")
    assert not state.video_posted("https://youtu.be/a", "youtube")
    assert not state.video_posted_on_all("https://youtu.be/a", ["tiktok", "youtube"])
    state.add_post(_post(platform="youtube"))
    assert state.video_posted_on_all("https://youtu.be/a", ["tiktok", "youtube"])


def test_video_posted_on_all_empty_platforms_is_false(state_files):
    state.add_post(_post())
    assert not state.video_posted_on_all("https://youtu.be/a", [])


def test_legacy_records_without_platform_count_as_tiktok(state_files):
    state.STATE_FILE.write_text(json.dumps([
        {"kind": "repost", "source_url": "u", "slot": "08:00", "posted_at": "2020-01-01"}
    ]), encoding="utf-8")
    assert state.video_posted("u", "tiktok")
    assert not state.video_posted("u", "youtube")


def test_slot_posted_today_is_platform_scoped(state_files):
    state.add_post(_post(slot="12:00", platform="youtube"))
    assert state.slot_posted_today("12:00", "youtube")
    assert not state.slot_posted_today("12:00", "tiktok")
    assert not state.slot_posted_today("18:00", "youtube")


def test_slot_posted_yesterday_does_not_count(state_files):
    state.STATE_FILE.write_text(json.dumps([
        {"kind": "repost", "slot": "08:00", "platform": "tiktok",
         "posted_at": "2001-01-01T08:00:00"}
    ]), encoding="utf-8")
    assert not state.slot_posted_today("08:00", "tiktok")


# ---------------------------------------------------------------- queue
def test_queue_dedups_and_prunes(state_files):
    item = QueueItem(url="https://youtu.be/x", channel="c", title="t", video_id="x")
    state.add_to_queue(item)
    state.add_to_queue(item)
    assert len(state.load_queue()) == 1
    assert state.queue_has("x")
    state.remove_from_queue("https://youtu.be/x")
    assert state.load_queue() == []


def test_pull_next_unprocessed_respects_platform(state_files):
    state.add_to_queue(QueueItem(url="u1", channel="c", title="t", video_id="1"))
    state.add_to_queue(QueueItem(url="u2", channel="c", title="t", video_id="2"))
    state.add_post(_post(url="u1", platform="tiktok"))
    assert state.pull_next_unprocessed("tiktok")["url"] == "u2"
    assert state.pull_next_unprocessed("youtube")["url"] == "u1"


# ---------------------------------------------------------------- durability
def test_corrupt_state_is_quarantined_not_clobbered(state_files):
    original = '[{"kind": "repost", "source_url": "precious"'  # truncated JSON
    state.STATE_FILE.write_text(original, encoding="utf-8")

    state.add_post(_post(url="new"))

    backups = list(state_files.glob("posted.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert [p["source_url"] for p in state.load_posts()] == ["new"]


def test_non_list_state_is_quarantined(state_files):
    state.STATE_FILE.write_text('{"oops": true}', encoding="utf-8")
    assert state.load_posts() == []
    assert list(state_files.glob("posted.json.corrupt-*"))


def test_write_leaves_no_temp_files(state_files):
    state.add_post(_post())
    leftovers = [p.name for p in state_files.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert not state.LOCK_FILE.exists()


# ---------------------------------------------------------------- locking
def test_lock_is_reentrant(state_files):
    with state._state_lock():
        with state._state_lock():
            assert state.LOCK_FILE.exists()
        assert state.LOCK_FILE.exists()
    assert not state.LOCK_FILE.exists()


def test_stale_lock_is_broken(state_files, monkeypatch):
    state.LOCK_FILE.write_text("99999", encoding="utf-8")
    old = time.time() - state.LOCK_STALE_SEC - 5
    os.utime(state.LOCK_FILE, (old, old))
    with state._state_lock(timeout=1):
        pass
    assert not state.LOCK_FILE.exists()


def test_held_lock_times_out(state_files):
    state.LOCK_FILE.write_text("1", encoding="utf-8")  # fresh lock held by "someone"
    with pytest.raises(TimeoutError):
        with state._state_lock(timeout=0.2):
            pass


def test_concurrent_threads_lose_no_writes(state_files):
    def worker(n):
        for i in range(10):
            state.add_post(_post(url=f"t{n}-{i}"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(state.load_posts()) == 50


_CHILD = """
import sys
from datetime import datetime
import ttclips.state as s
for i in range(int(sys.argv[2])):
    s.add_post(s.PostRecord(kind="repost", source_url=f"{sys.argv[1]}-{i}",
               posted_at=datetime.now().isoformat()))
"""


def test_write_survives_concurrent_unlocked_reader(state_files):
    """Readers that bypass the lock must not make writes crash on Windows."""
    state.add_post(_post(url="seed"))
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            state.load_posts()

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(30):
            state.add_post(_post(url=f"w{i}"))
    finally:
        stop.set()
        t.join()
    assert len(state.load_posts()) == 31


def test_concurrent_processes_lose_no_writes(tmp_path):
    """Two overlapping scheduled tasks must not drop each other's records."""
    env = dict(os.environ, TTCLIPS_HOME=str(tmp_path))
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, f"p{n}", "15"], env=env)
             for n in range(3)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    posts = json.loads((tmp_path / "data" / "posted.json").read_text(encoding="utf-8"))
    assert len(posts) == 45
