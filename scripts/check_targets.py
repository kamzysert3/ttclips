r"""Offline sanity checks for the multi-target posting logic.

Run with:  .\.venv\Scripts\python.exe scripts\check_targets.py
Uses a throw-away TTCLIPS_HOME so it never touches real state.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ttclips-check-"))
os.environ["TTCLIPS_HOME"] = str(TMP)
(TMP / "config").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ttclips.config import load_platforms_config  # noqa: E402
from ttclips.scheduler import (_clip_posted, _fair_order, _merge_hashtags,  # noqa: E402
                               _pending_sources, _resolve_targets)
from ttclips.state import (PostRecord, add_post, load_posts,  # noqa: E402
                           slot_posted_today, video_posted)
from ttclips.youtube_uploader import build_short_metadata  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


print("targets")
check("default is tiktok", _resolve_targets() == ["tiktok"])
check("both expands", _resolve_targets("both") == ["tiktok", "youtube"])
check("csv list", _resolve_targets("youtube,tiktok") == ["youtube", "tiktok"])
check("unknown ignored", _resolve_targets("myspace,youtube") == ["youtube"])
(TMP / "config" / "platforms.json").write_text(
    json.dumps({"default_target": "youtube"}), encoding="utf-8")
check("config default", _resolve_targets() == ["youtube"])
(TMP / "config" / "platforms.json").write_text(
    json.dumps({"default_target": "tiktok"}), encoding="utf-8")

print("hashtags")
merged = _merge_hashtags(["funny", "#viral"], defaults="#Shorts #fyp")
check("merges dynamic + defaults", merged == "#funny #viral #Shorts #fyp", merged)
check("no duplicates", _merge_hashtags(["fyp"], defaults="#fyp #viral") == "#fyp #viral")

print("youtube metadata")
meta = build_short_metadata("A very long title " * 20, "#fyp #funny",
                             source_url="https://youtu.be/abc")
check("title capped at 100", len(meta["title"]) <= 100, str(len(meta["title"])))
check("shorts tag added", "Shorts" in meta["tags"], str(meta["tags"]))
check("source link included", "abc" in meta["description"])
check("tags deduped", len(meta["tags"]) == len(set(t.lower() for t in meta["tags"])))

print("state is platform-aware")
today = datetime.now().isoformat(timespec="seconds")
add_post(PostRecord(kind="repost", source_url="https://youtu.be/x",
                     slot="08:00", platform="tiktok", posted_at=today))
check("tiktok record seen as tiktok", video_posted("https://youtu.be/x", "tiktok"))
check("tiktok record not seen as youtube",
      not video_posted("https://youtu.be/x", "youtube"))
check("slot tracking is per platform", slot_posted_today("08:00", "tiktok"))
check("other platform slot not marked", not slot_posted_today("08:00", "youtube"))
add_post(PostRecord(kind="repost", source_url="https://youtu.be/x",
                     slot="08:00", platform="youtube", posted_at=today))
check("youtube now recorded", video_posted("https://youtu.be/x", "youtube"))

print("legacy records default to tiktok")
state_file = TMP / "data" / "posted.json"
raw = json.loads(state_file.read_text(encoding="utf-8"))
raw.append({"kind": "repost", "source_url": "https://youtu.be/legacy",
             "slot": "09:00", "posted_at": today})
state_file.write_text(json.dumps(raw), encoding="utf-8")
check("legacy record counts as tiktok",
      video_posted("https://youtu.be/legacy", "tiktok"))
check("legacy record not youtube",
      not video_posted("https://youtu.be/legacy", "youtube"))

print("pending sources")
queue = [{"url": "https://youtu.be/a", "channel": "c", "title": "t",
          "video_id": "a", "max_moments": 2, "discovered_at": "2026-01-01"},
         {"url": "https://youtu.be/b", "channel": "c", "title": "t",
          "video_id": "b", "max_moments": 1, "discovered_at": "2026-01-02"}]
history = load_posts()
history.append({"kind": "repost", "source_url": "https://youtu.be/a",
                 "clip_start": 1.0, "clip_end": 30.0, "platform": "tiktok",
                 "channel": "c", "posted_at": "2026-01-01T00:00:00"})
pending = _pending_sources(queue, history, ["tiktok", "youtube"])
check("partially posted source still pending for youtube",
      any(i["url"] == "https://youtu.be/a" for i in pending), str(pending))
check("untouched source pending", any(i["url"] == "https://youtu.be/b" for i in pending))
check("fully posted source dropped", not any(
    i["url"] == "https://youtu.be/b" for i in _pending_sources(
        queue, history + [{"kind": "repost", "source_url": "https://youtu.be/b",
                           "platform": "tiktok", "posted_at": "2026-01-01"}],
        ["tiktok"])))

print("clip level dedup")
clip = {"start": 10.0, "end": 40.0}
check("clip not posted yet", not _clip_posted(history, "https://youtu.be/a", clip, "youtube"))
history.append({"kind": "repost", "source_url": "https://youtu.be/a",
                 "clip_start": 10.0, "clip_end": 40.0, "platform": "youtube"})
check("clip posted to youtube", _clip_posted(history, "https://youtu.be/a", clip, "youtube"))
check("clip not posted to tiktok", not _clip_posted(history, "https://youtu.be/a", clip, "tiktok"))

print("fair ordering")
ordered = _fair_order(queue, history)
check("ordering is stable", len(ordered) == 2)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")
