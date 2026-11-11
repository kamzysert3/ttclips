r"""End-to-end simulation of the repost fan-out across platforms.

Downloads, transcription, ranking and both uploaders are stubbed, so this
exercises only ttclips' own scheduling / dedup / queue logic.

Run with:  .\.venv\Scripts\python.exe scripts\check_repost_flow.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ttclips-flow-"))
os.environ["TTCLIPS_HOME"] = str(TMP)
(TMP / "config").mkdir(parents=True, exist_ok=True)
(TMP / "data").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ttclips.scheduler as sched  # noqa: E402
from ttclips.state import load_posts, load_queue  # noqa: E402

SRC = "https://www.youtube.com/watch?v=vid123"
CLIP = {"start": 10.0, "end": 40.0, "title": "Best bit", "hashtags": ["funny"],
        "score": 9}

uploads: list[tuple[str, str]] = []
fail_for: set[str] = set()


def fake_download(url, *, filename_hint="", cache_id=""):
    path = TMP / "data" / "downloads" / "vid123.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    return path


def fake_transcribe(video_path, **kw):
    return [{"start": 0.0, "end": 5.0, "text": "hello world", "words": []}]


def fake_rank(segments, num_clips=3, *, cache_key=""):
    return [dict(CLIP)]


def fake_cut(source, clip_start, clip_end, segments, out, title=""):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake mp4")
    return out


def fake_upload_clip(video_path, caption, *, cookies=""):
    uploads.append(("tiktok", caption))
    return True


def fake_publish_clip(video_path, caption, **kw):
    return fake_upload_clip(video_path, caption), ""


def fake_upload_short(video_path, title, description, **kw):
    uploads.append(("youtube", title))
    if "youtube" in fail_for:
        return False, ""
    return True, "https://youtu.be/abc123"


sched.download = fake_download
sched.transcribe = fake_transcribe
sched.rank_highlights = fake_rank
sched.cut_and_crop_clip = fake_cut
sched.publish_clip = fake_publish_clip
sched.upload_short = fake_upload_short

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def write_queue() -> None:
    (TMP / "data" / "queue.json").write_text(json.dumps([{
        "url": SRC, "channel": "Demo", "title": "Demo video",
        "video_id": "vid123", "max_moments": 1,
        "discovered_at": "2026-01-01T00:00:00",
    }]), encoding="utf-8")


def reset_state() -> None:
    """Forget everything that was posted (fresh scenario)."""
    (TMP / "data" / "posted.json").write_text("[]", encoding="utf-8")


def set_default_target(target: str) -> None:
    (TMP / "config" / "platforms.json").write_text(
        json.dumps({"default_target": target}), encoding="utf-8")


set_default_target("both")


print("cross-post to both platforms")
write_queue()
uploads.clear()
ok = sched._do_repost("08:00", targets=["tiktok", "youtube"])
check("repost reported success", ok)
check("both platforms uploaded", sorted(p for p, _ in uploads) == ["tiktok", "youtube"],
      str(uploads))
check("queue drained", load_queue() == [], str(load_queue()))
posts = load_posts()
check("two records written", len(posts) == 2, str(posts))
check("platforms recorded",
      sorted(p.get("platform") for p in posts) == ["tiktok", "youtube"])
check("remote id stored",
      any(p.get("remote_id") == "https://youtu.be/abc123" for p in posts))

print("re-running does not repost")
uploads.clear()
ok = sched._do_repost("08:00", targets=["tiktok", "youtube"])
check("nothing posted", not ok and not uploads, str(uploads))

print("a failed platform is retried later")
reset_state()
write_queue()
uploads.clear()
fail_for.add("youtube")
ok = sched._do_repost("08:00", targets=["tiktok", "youtube"])
check("tiktok still posted", any(p == "tiktok" for p, _ in uploads), str(uploads))
check("youtube was attempted but failed",
      any(p == "youtube" for p, _ in uploads)
      and not any(p.get("platform") == "youtube" for p in load_posts()),
      str(uploads))
check("source stays queued", len(load_queue()) == 1, str(load_queue()))
fail_for.discard("youtube")

print("retry posts only the missing platform")
uploads.clear()
ok = sched._do_repost("08:00", targets=["tiktok", "youtube"])
check("only youtube retried", [p for p, _ in uploads] == ["youtube"], str(uploads))
check("queue drained after retry", load_queue() == [], str(load_queue()))
check("no duplicate tiktok record",
      sum(1 for p in load_posts() if p.get("platform") == "tiktok") == 1,
      str(load_posts()))

print("platforms are independent")
reset_state()
write_queue()
uploads.clear()
ok = sched._do_repost("09:00", targets=["tiktok"])
check("tiktok-only run works", ok and [p for p, _ in uploads] == ["tiktok"], str(uploads))
check("source stays queued for the other platform", len(load_queue()) == 1,
      str(load_queue()))

print("the other platform picks the same source up")
uploads.clear()
ok = sched._do_repost("09:00", targets=["youtube"])
check("youtube-only run works", ok and [p for p, _ in uploads] == ["youtube"], str(uploads))
check("queue drained once every platform has it", load_queue() == [], str(load_queue()))
check("one record per platform",
      sorted(p.get("platform") for p in load_posts()) == ["tiktok", "youtube"],
      str(load_posts()))

print("a youtube-only run posts the same source again")
reset_state()
write_queue()
uploads.clear()
ok = sched._do_repost("09:00", targets=["youtube"])
check("youtube-only run works", ok and [p for p, _ in uploads] == ["youtube"], str(uploads))
check("still queued - tiktok has not had it yet", len(load_queue()) == 1,
      str(load_queue()))
check("only a youtube record written",
      [p.get("platform") for p in load_posts()] == ["youtube"], str(load_posts()))

print("a tiktok-only config keeps the legacy queue behaviour")
set_default_target("tiktok")
reset_state()
write_queue()
uploads.clear()
ok = sched._do_repost("09:00", targets=["tiktok"])
check("tiktok posted", ok and [p for p, _ in uploads] == ["tiktok"], str(uploads))
check("queue drained (configured platform is satisfied)", load_queue() == [],
      str(load_queue()))

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")
