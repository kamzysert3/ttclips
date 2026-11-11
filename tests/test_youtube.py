"""YouTube metadata + the official Data API backend (network fully stubbed)."""
from __future__ import annotations

import pytest

pytest.importorskip("googleapiclient")
import httplib2  # noqa: E402  (ships with google-api-python-client)
from googleapiclient.errors import HttpError  # noqa: E402

import ttclips.youtube_api as api  # noqa: E402
import ttclips.youtube_uploader as yu  # noqa: E402


# ---------------------------------------------------------------- metadata
def test_short_metadata_adds_shorts_tag_and_trims(monkeypatch):
    monkeypatch.setattr(yu, "INCLUDE_SOURCE_LINK", True)
    meta = yu.build_short_metadata("  A   " + "x" * 200, "#fun #Fun #Shorts", "https://y/1")
    assert len(meta["title"]) <= yu.MAX_TITLE_LEN
    assert meta["tags"][0].lower() == "shorts" or "Shorts" in meta["tags"]
    assert [t.lower() for t in meta["tags"]].count("fun") == 1
    assert meta["description"].endswith("Original: https://y/1")


def test_short_metadata_tag_budget():
    meta = yu.build_short_metadata("t", " ".join(f"#{'t' * 40}{i}" for i in range(30)))
    assert sum(len(t) for t in meta["tags"]) <= 500


# ---------------------------------------------------------------- api body
def test_build_body_defaults_and_invalid_visibility():
    body = api.build_body("T" * 150, "d", tags=["Shorts"], visibility="loud")
    assert len(body["snippet"]["title"]) == 100
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert api.build_body("t", "d")["status"]["privacyStatus"] == "public"


# ---------------------------------------------------------------- api upload
class _Progress:
    def progress(self):
        return 0.5


class _Request:
    """Replays a script of next_chunk() outcomes."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Service:
    def __init__(self, request):
        self.request = request
        self.inserted = None

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted = body
        return self.request


def _http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


@pytest.fixture
def video(tmp_path, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * 1024)
    return path


def test_api_upload_success_with_progress(video):
    req = _Request([(_Progress(), None),
                    (None, {"id": "abc", "status": {"privacyStatus": "public"}})])
    svc = _Service(req)
    ok, url = api.upload_short(str(video), "Title", "Desc", tags=["Shorts"], service=svc)
    assert (ok, url) == (True, "https://youtube.com/shorts/abc")
    assert svc.inserted["snippet"]["tags"] == ["Shorts"]


def test_api_upload_retries_transient_errors(video):
    req = _Request([_http_error(503), ConnectionError("reset"),
                    (None, {"id": "xyz", "status": {}})])
    ok, url = api.upload_short(str(video), "t", "d", service=_Service(req), retries=3)
    assert ok and url.endswith("xyz")
    assert req.calls == 3


def test_api_upload_gives_up_after_retries(video):
    req = _Request([_http_error(500)] * 3)
    assert api.upload_short(str(video), "t", "d", service=_Service(req), retries=2) == (False, "")


def test_api_upload_fails_fast_on_client_error(video):
    req = _Request([_http_error(403)])
    assert api.upload_short(str(video), "t", "d", service=_Service(req)) == (False, "")
    assert req.calls == 1


def test_api_upload_warns_when_locked_private(video, caplog):
    req = _Request([(None, {"id": "p", "status": {"privacyStatus": "private"}})])
    ok, _ = api.upload_short(str(video), "t", "d", visibility="public", service=_Service(req))
    assert ok
    assert "locked" in caplog.text


def test_missing_token_gives_actionable_error(monkeypatch, tmp_path):
    cfg = {"token_file": str(tmp_path / "none.json")}
    with pytest.raises(api.YouTubeAPIError, match="ttclips-youtube --auth"):
        api.get_credentials(interactive=False, cfg=cfg)


def test_interactive_auth_without_client_secret(tmp_path):
    cfg = {"token_file": str(tmp_path / "t.json"),
           "client_secrets_file": str(tmp_path / "missing.json")}
    with pytest.raises(api.YouTubeAPIError, match="OAuth client file not found"):
        api.get_credentials(interactive=True, cfg=cfg)


# ---------------------------------------------------------------- dispatch
def test_uploader_dispatches_to_api_backend(monkeypatch, video):
    calls = {}

    def fake_api_upload(path, title, description, **kw):
        calls.update(path=path, title=title, **kw)
        return True, "https://youtube.com/shorts/q"

    monkeypatch.setattr(yu, "BACKEND", "api")
    monkeypatch.setattr(yu, "validate_short", lambda p: (True, ""))
    monkeypatch.setattr(api, "upload_short", fake_api_upload)
    monkeypatch.setattr(yu, "_validate_cookies",
                        lambda: pytest.fail("API backend must not need cookies"))

    ok, url = yu.upload_short(str(video), "  spaced   title ", "d", tags=["Shorts"])
    assert ok and url.endswith("/q")
    assert calls["title"] == "spaced title"
    assert calls["tags"] == ["Shorts"]


def test_uploader_api_backend_still_validates_clip(monkeypatch, video):
    monkeypatch.setattr(yu, "BACKEND", "api")
    monkeypatch.setattr(yu, "validate_short", lambda p: (False, "too long"))
    monkeypatch.setattr(api, "upload_short", lambda *a, **k: pytest.fail("should not upload"))
    assert yu.upload_short(str(video), "t", "d") == (False, "")
