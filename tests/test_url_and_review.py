"""--url (ad-hoc source) and --review (caption approval) features."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

import ttclips.downloader as downloader
import ttclips.review as review
import ttclips.scheduler as sched
from ttclips.state import load_posts, load_queue

CLIP = {"start": 10.0, "end": 40.0, "title": "Best bit", "hashtags": ["funny"], "score": 9}


# ---------------------------------------------------------------- review prompt
def _script(*answers):
    it = iter(answers)
    return lambda *_a: next(it)


def _review(edits, asks, platform="tiktok", **kw):
    out: list[str] = []
    played: list[Path] = []
    result = review.review_post(
        platform, "Original title", "#a #b", Path("clip.mp4"),
        preview=lambda t, h: f"{t}\n\n{h}" if h else t,
        edit=_script(*edits), ask=_script(*asks), out=out.append,
        player=played.append, **kw)
    return result, out, played


def test_enter_accepts_prefilled_values():
    # edit() returns what the user leaves in the prefilled field
    result, out, _ = _review(["Original title", "#a #b"], [""])
    assert result == ("Original title", "#a #b")
    assert "Original title\n\n#a #b" in out


def test_edits_are_returned_and_whitespace_normalised():
    result, _, _ = _review(["  New   title ", "#x  #y"], [""])
    assert result == ("New title", "#x #y")


def test_edit_again_then_play_then_upload():
    result, _, played = _review(["t1", "#h1", "t2", "#h2"], ["e", "p", ""])
    assert result == ("t2", "#h2")
    assert played == [Path("clip.mp4")]


def test_skip_returns_none():
    result, out, _ = _review(["t", "#h"], ["s"])
    assert result is None
    assert any("Skipped" in line for line in out)


def test_quit_raises_abort_and_is_not_swallowed_by_except_exception():
    with pytest.raises(review.ReviewAbort):
        try:
            _review(["t", "#h"], ["q"])
        except Exception:  # the pipeline's broad handlers must not catch it
            pytest.fail("ReviewAbort was swallowed")


def test_unknown_key_reprompts():
    result, out, _ = _review(["t", "#h"], ["zzz", ""])
    assert result == ("t", "#h")
    assert any("Press Enter" in line for line in out)


def test_tiktok_length_warning():
    _, out, _ = _review(["x" * 2300, ""], [""])
    assert any("TOO LONG" in line for line in out)


def test_fallback_input_keeps_value_on_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)  # force ImportError
    answers = iter(["", "changed", "-"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    assert review._prefilled_input("Title › ", "keep me") == "keep me"
    assert review._prefilled_input("Title › ", "old") == "changed"
    assert review._prefilled_input("Title › ", "old") == ""        # "-" clears


# ---------------------------------------------------------------- pipeline fakes
@pytest.fixture
def pipeline(monkeypatch, state_files, tmp_path):
    """Stub download/transcribe/rank/encode/upload; record uploads."""
    uploads: list[tuple[str, str]] = []
    calls = {"download": []}

    def fake_download(url, *, filename_hint="", cache_id=""):
        calls["download"].append((url, cache_id))
        p = tmp_path / "src.mp4"
        p.write_bytes(b"src")
        return p

    def fake_cut(source, clip_start, clip_end, segments, out, title=""):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out

    monkeypatch.setattr(sched, "download", fake_download)
    monkeypatch.setattr(sched, "transcribe", lambda p, **k: [{"start": 0, "end": 5, "text": "hi"}])
    monkeypatch.setattr(sched, "rank_highlights", lambda segs, num_clips=1, cache_key="":
                        [dict(CLIP, start=CLIP["start"] + 100 * i, end=CLIP["end"] + 100 * i)
                         for i in range(num_clips)])
    monkeypatch.setattr(sched, "cut_and_crop_clip", fake_cut)
    monkeypatch.setattr(sched, "CLIPS_DIR", tmp_path / "clips")
    monkeypatch.setattr(sched, "_human_delay", lambda: None)
    monkeypatch.setattr(sched, "publish_clip",
                        lambda path, caption, **kw: uploads.append(("tiktok", caption)) or (True, "tt1"))
    monkeypatch.setattr(sched, "upload_short",
                        lambda path, title, desc, **kw: uploads.append(("youtube", title)) or (True, "yt1"))
    monkeypatch.setattr(sched, "REVIEW", False)
    calls["uploads"] = uploads
    return calls


def _fake_probe(url):
    return {"url": "https://www.twitch.tv/videos/123", "video_id": "twitch_v123",
            "cache_id": "v123", "title": "Stream", "channel": "streamer",
            "duration": 3600.0, "extractor": "twitch:vod"}


# ---------------------------------------------------------------- --url
def test_run_url_twitch_posts_without_touching_queue(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    ok = sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok", "youtube"],
                       num_clips=2)
    assert ok
    assert pipeline["download"] == [("https://www.twitch.tv/videos/123", "v123")]
    assert sorted(p for p, _ in pipeline["uploads"]) == ["tiktok", "tiktok", "youtube", "youtube"]
    posts = load_posts()
    assert {p["slot"] for p in posts} == {"manual"}
    assert {p["channel"] for p in posts} == {"streamer"}
    assert load_queue() == []


def test_run_url_is_deduplicated_on_rerun(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    assert sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok"])
    assert not sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok"])
    assert len(pipeline["uploads"]) == 1


def test_run_url_local_file_skips_download(pipeline, tmp_path):
    video = tmp_path / "My Podcast Ep 1.mp4"
    video.write_bytes(b"local")
    assert sched.run_url(str(video), targets=["tiktok"])
    assert pipeline["download"] == []
    post = load_posts()[0]
    assert post["source_url"].startswith("file:")
    assert Path(post["file_path"]).name.startswith("local_My_Podcast_Ep_1_")


def test_run_url_rejects_non_links(pipeline):
    assert not sched.run_url("not a link", targets=["tiktok"])
    assert pipeline["uploads"] == []


def test_run_url_reports_probe_errors(pipeline, monkeypatch, caplog):
    def bad_probe(url):
        raise ValueError(f"{url} is a playlist/channel - pass a link to a single video")
    monkeypatch.setattr(sched, "probe_source", bad_probe)
    assert not sched.run_url("https://youtube.com/@someone", targets=["tiktok"])
    assert "single video" in caplog.text


def test_run_url_clamps_clip_count(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok"], num_clips=50)
    assert len(pipeline["uploads"]) == 5


def test_run_url_dry_run_uploads_nothing(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    assert sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok"], dry_run=True)
    assert pipeline["uploads"] == [] and load_posts() == []


# ---------------------------------------------------------------- --review in the pipeline
def test_review_edits_reach_the_upload(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    monkeypatch.setattr(sched, "REVIEW", True)
    seen = []

    def fake_review(platform, title, hashtags, clip_path, *, preview, duration=None):
        seen.append((platform, title, duration, preview("Edited", "")))
        return ("Edited " + platform, "#mine")

    monkeypatch.setattr(sched, "review_post", fake_review)
    sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok", "youtube"])
    assert ("tiktok", "Edited tiktok\n\n#mine") in pipeline["uploads"]
    assert ("youtube", "Edited youtube") in pipeline["uploads"]
    assert seen[0][1] == "Best bit" and seen[0][2] == 30.0
    assert seen[0][3] == "Edited"                                  # cleared hashtags -> none
    assert seen[1][3].startswith("Title:       Edited")             # YouTube preview format
    assert {p["caption"] for p in load_posts() if p["platform"] == "tiktok"} == {"Edited tiktok\n\n#mine"}


def test_review_skip_records_nothing(pipeline, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    monkeypatch.setattr(sched, "REVIEW", True)
    monkeypatch.setattr(sched, "review_post", lambda *a, **k: None)
    assert not sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok"])
    assert pipeline["uploads"] == [] and load_posts() == []
    assert "Skipped tiktok" in caplog.text and "failed" not in caplog.text


def test_review_quit_stops_everything(pipeline, monkeypatch):
    monkeypatch.setattr(sched, "probe_source", _fake_probe)
    monkeypatch.setattr(sched, "REVIEW", True)

    def quit_(*a, **k):
        raise review.ReviewAbort()
    monkeypatch.setattr(sched, "review_post", quit_)
    with pytest.raises(review.ReviewAbort):
        sched.run_url("https://www.twitch.tv/videos/123", targets=["tiktok", "youtube"], num_clips=3)
    assert pipeline["uploads"] == []


def test_post_file_goes_through_review(pipeline, monkeypatch, tmp_path):
    clip = tmp_path / "ready_clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(sched, "REVIEW", True)
    monkeypatch.setattr(sched, "review_post", lambda p, t, h, c, **k: (t + "!", h))
    assert sched.run_post_file(str(clip), "My caption", "tiktok")
    assert pipeline["uploads"] == [("tiktok", "My caption!")]       # explicit caption: no hashtags
    assert load_posts()[0]["kind"] == "manual"


def test_post_file_without_caption_adds_hashtags(pipeline, tmp_path):
    clip = tmp_path / "ready_clip.mp4"
    clip.write_bytes(b"x")
    assert sched.run_post_file(str(clip), "", "tiktok")
    caption = pipeline["uploads"][0][1]
    assert caption.startswith("ready clip\n\n#")


def test_review_refuses_non_interactive(monkeypatch):
    monkeypatch.setattr(sched, "is_interactive", lambda: False)
    monkeypatch.setattr(sys, "argv", ["ttclips", "--review", "--url", "https://x.y/z"])
    with pytest.raises(SystemExit) as exc:
        sched.main()
    assert exc.value.code == 2


def test_main_turns_review_quit_into_clean_exit(monkeypatch):
    monkeypatch.setattr(sched, "is_interactive", lambda: True)
    monkeypatch.setattr(sched, "REVIEW", False)

    def quit_(*a, **k):
        raise review.ReviewAbort()
    monkeypatch.setattr(sched, "run_url", quit_)
    monkeypatch.setattr(sys, "argv", ["ttclips", "--review", "--url", "https://x.y/z"])
    with pytest.raises(SystemExit) as exc:
        sched.main()
    assert exc.value.code == 130


# ---------------------------------------------------------------- probe_source
class _FakeYDL:
    info: dict = {}

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        assert self.opts["noplaylist"] is True
        return self.info


@pytest.fixture
def fake_ytdlp(monkeypatch):
    mod = types.SimpleNamespace(YoutubeDL=_FakeYDL)
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return _FakeYDL


def test_probe_youtube_keeps_plain_id(fake_ytdlp):
    fake_ytdlp.info = {"id": "dQw4w9WgXcQ", "extractor_key": "Youtube", "title": "T",
                       "uploader": "Me", "duration": 212,
                       "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    src = downloader.probe_source("https://youtu.be/dQw4w9WgXcQ?list=PL1")
    assert src["video_id"] == "dQw4w9WgXcQ"           # matches the channel monitor's ids
    assert src["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert (src["channel"], src["duration"]) == ("Me", 212.0)


def test_probe_twitch_is_site_prefixed(fake_ytdlp):
    fake_ytdlp.info = {"id": "v2087654321", "extractor_key": "TwitchVod", "title": "Stream",
                       "uploader": "Streamer", "webpage_url": "https://www.twitch.tv/videos/2087654321"}
    src = downloader.probe_source("https://www.twitch.tv/videos/2087654321")
    assert src["video_id"] == "twitchvod_v2087654321"
    assert src["cache_id"] == "v2087654321"


def test_probe_twitch_clip_slug_is_sanitised(fake_ytdlp):
    fake_ytdlp.info = {"id": "Funny/Clip:Slug", "extractor_key": "TwitchClips"}
    src = downloader.probe_source("https://clips.twitch.tv/x")
    assert src["video_id"] == "twitchclips_Funny_Clip_Slug"
    assert src["title"] == "Funny/Clip:Slug"


def test_probe_rejects_playlists(fake_ytdlp):
    fake_ytdlp.info = {"_type": "playlist", "id": "PL", "entries": [{}]}
    with pytest.raises(ValueError, match="single video"):
        downloader.probe_source("https://www.youtube.com/playlist?list=PL")
