"""TikTok Content Posting API backend, against an in-process fake TikTok."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

import ttclips.tiktok_api as tt

MB = tt.MB


# ---------------------------------------------------------------- pure helpers
def test_pkce_challenge_is_hex_sha256():
    verifier, challenge = tt.make_pkce()
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    assert challenge == hashlib.sha256(verifier.encode()).hexdigest()


def test_auth_url_params():
    url = tt.build_auth_url("ck", "http://localhost:5000/callback/",
                            ["user.info.basic", "video.upload"], "st", "ch")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith(tt.AUTH_URL)
    assert q == {"client_key": "ck", "scope": "user.info.basic,video.upload",
                 "response_type": "code", "redirect_uri": "http://localhost:5000/callback/",
                 "state": "st", "code_challenge": "ch", "code_challenge_method": "S256"}


@pytest.mark.parametrize("size,expected", [
    (1 * MB, (1 * MB, 1)),            # under 5 MB: one request, chunk = whole file
    (7 * MB, (7 * MB, 1)),            # fits in one chunk
    (25 * MB, (10 * MB, 2)),          # count rounds DOWN; last chunk takes 15 MB
    (30 * MB, (10 * MB, 3)),
])
def test_plan_chunks(size, expected):
    assert tt.plan_chunks(size, 10 * MB) == expected


def test_plan_chunks_clamps_and_limits():
    assert tt.plan_chunks(20 * MB, 1 * MB) == (5 * MB, 4)        # min 5 MB chunk
    chunk, count = tt.plan_chunks(4 * 1024 * MB, 10 * MB)
    assert MB * 5 <= chunk <= tt.MAX_CHUNK and count <= tt.MAX_CHUNKS
    assert 4 * 1024 * MB - chunk * (count - 1) <= tt.MAX_FINAL_CHUNK
    with pytest.raises(ValueError):
        tt.plan_chunks(4 * 1024 * MB + 1)
    with pytest.raises(ValueError):
        tt.plan_chunks(0)


def test_chunk_ranges_cover_file_exactly():
    size = 25 * MB + 123
    chunk, count = tt.plan_chunks(size, 10 * MB)
    ranges = tt.chunk_ranges(size, chunk, count)
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    for (a0, a1), (b0, _) in zip(ranges, ranges[1:]):
        assert b0 == a1 + 1


def test_truncate_runes_counts_utf16():
    assert tt.truncate_runes("abc", 2) == "ab"
    assert tt.truncate_runes("😀😀", 3) == "😀"          # emoji = 2 UTF-16 units
    assert tt.truncate_runes("short") == "short"


def test_resolve_privacy():
    assert tt.resolve_privacy("PUBLIC_TO_EVERYONE",
                              ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]) == "PUBLIC_TO_EVERYONE"
    assert tt.resolve_privacy("PUBLIC_TO_EVERYONE",
                              ["FOLLOWER_OF_CREATOR", "SELF_ONLY"]) == "SELF_ONLY"


def test_settings_load_maps_visibility_and_api_block(tmp_path):
    s = tt.Settings.load({"visibility": "followers", "api": {"api_mode": "direct",
                          "token_file": str(tmp_path / "t.json")}},
                         {"TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s"})
    assert (s.mode, s.privacy_level, s.client_key) == ("direct", "FOLLOWER_OF_CREATOR", "k")
    assert s.token_file == tmp_path / "t.json"
    assert tt.Settings.load({"api": {"api_mode": "bogus"}}, {}).mode == "inbox"
    assert tt.Settings.load({"api": {"privacy_level": "self_only"}}, {}).privacy_level == "SELF_ONLY"


def test_error_hint_is_actionable():
    e = tt.TikTokAPIError("unaudited_client_can_only_post_to_private_accounts", "x", status=403)
    assert "inbox" in str(e) and not e.retriable
    assert tt.TikTokAPIError("rate_limit_exceeded", status=429).retriable


# ---------------------------------------------------------------- fake TikTok
class FakeTikTok:
    """Minimal in-memory TikTok: token, creator_info, init, chunk PUT, status."""

    def __init__(self, mode="inbox"):
        self.mode = mode
        self.calls: list[tuple[str, str]] = []
        self.init_body = None
        self.puts: list[dict] = []
        self.received = 0
        self.statuses = ["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX" if mode == "inbox"
                         else "PUBLISH_COMPLETE"]
        self.creator = {"creator_username": "me", "creator_nickname": "Me",
                        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                                                  "SELF_ONLY"],
                        "comment_disabled": False, "duet_disabled": True,
                        "stitch_disabled": False, "max_video_post_duration_sec": 600}
        self.fail_next: dict[str, list] = {}   # path -> [(status, code), ...]
        self.token_requests: list[dict] = []
        self.post_ids = [7300000000000000001]

    @staticmethod
    def ok(data):
        return httpx.Response(200, json={"data": data, "error": {"code": "ok", "message": ""}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        queued = self.fail_next.get(path)
        if queued:
            status, code = queued.pop(0)
            return httpx.Response(status, json={"data": {}, "error": {
                "code": code, "message": "boom", "log_id": "L1"}})
        if path == "/v2/oauth/token/":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            n = len(self.token_requests)
            return httpx.Response(200, json={
                "access_token": f"at{n}", "refresh_token": f"rt{n}", "open_id": "o",
                "scope": "user.info.basic,video.upload", "expires_in": 7200,
                "refresh_expires_in": 15552000, "token_type": "Bearer"})
        assert request.headers.get("Authorization", "").startswith("Bearer ") or request.method == "PUT"
        if path == "/v2/user/info/":
            return self.ok({"user": {"open_id": "o", "display_name": "Me"}})
        if path == tt.CREATOR_INFO_PATH:
            return self.ok(self.creator)
        if path in (tt.INBOX_INIT_PATH, tt.DIRECT_INIT_PATH):
            self.init_body = json.loads(request.content)
            return self.ok({"publish_id": "v_pub_1", "upload_url": "https://upload.fake/u?x=1"})
        if request.method == "PUT":
            self.puts.append(dict(request.headers))
            self.received += len(request.content)
            total = self.init_body["source_info"]["video_size"]
            return httpx.Response(201 if self.received >= total else 206)
        if path == tt.STATUS_PATH:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            data = {"status": status, "uploaded_bytes": self.received}
            if status == "FAILED":
                data["fail_reason"] = "duration_check_failed"
            if status == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = self.post_ids
            return self.ok(data)
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def fake(tmp_path):
    return FakeTikTok()


def _client(fake, tmp_path, mode="inbox", token=None, **overrides):
    fake.mode = mode
    if mode == "direct":
        fake.statuses = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    settings = tt.Settings(client_key="ck", client_secret="cs", mode=mode,
                           token_file=tmp_path / "token.json", status_poll_sec=0,
                           **overrides)
    token = token if token is not None else {
        "access_token": "at0", "refresh_token": "rt0",
        "expires_at": time.time() + 3600, "refresh_expires_at": time.time() + 86400}
    if token:
        settings.token_file.write_text(json.dumps(token), encoding="utf-8")
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return tt.TikTokClient(settings, http, sleep=lambda s: None)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\1" * (12 * MB + 7))
    return p


# ---------------------------------------------------------------- flows
def test_inbox_upload_end_to_end(fake, tmp_path, video):
    client = _client(fake, tmp_path)
    ok, remote = tt.upload_clip(str(video), "caption #fyp", client=client)
    assert (ok, remote) == (True, "publish_id:v_pub_1")
    assert "post_info" not in fake.init_body           # inbox init takes source_info only
    src = fake.init_body["source_info"]
    assert src == {"source": "FILE_UPLOAD", "video_size": video.stat().st_size,
                   "chunk_size": 10 * MB, "total_chunk_count": 1}
    assert fake.received == video.stat().st_size
    assert fake.puts[0]["content-range"] == f"bytes 0-{video.stat().st_size - 1}/{video.stat().st_size}"
    assert (("POST", tt.CREATOR_INFO_PATH) not in fake.calls)


def test_multi_chunk_ranges_sent_in_order(fake, tmp_path, video):
    client = _client(fake, tmp_path, chunk_size=5 * MB)
    assert tt.upload_clip(str(video), "c", client=client)[0]
    size = video.stat().st_size
    assert [h["content-range"] for h in fake.puts] == [
        f"bytes 0-{5 * MB - 1}/{size}", f"bytes {5 * MB}-{size - 1}/{size}"]


def test_direct_upload_respects_creator_settings(fake, tmp_path, video):
    client = _client(fake, tmp_path, mode="direct", privacy_level="FOLLOWER_OF_CREATOR")
    ok, url = tt.upload_clip(str(video), "  hello  ", is_aigc=True, client=client)
    assert ok and url == "https://www.tiktok.com/@me/video/7300000000000000001"
    info = fake.init_body["post_info"]
    assert info["privacy_level"] == "SELF_ONLY"        # not offered -> safe fallback
    assert info["disable_duet"] is True                # creator disabled it
    assert info["disable_comment"] is False
    assert info["is_aigc"] is True
    assert info["title"] == "hello"


def test_retries_rate_limit_then_succeeds(fake, tmp_path, video):
    fake.fail_next[tt.INBOX_INIT_PATH] = [(429, "rate_limit_exceeded")]
    assert tt.upload_clip(str(video), "c", client=_client(fake, tmp_path))[0]
    assert fake.calls.count(("POST", tt.INBOX_INIT_PATH)) == 2


def test_unaudited_error_fails_fast_with_hint(fake, tmp_path, video, caplog):
    fake.fail_next[tt.DIRECT_INIT_PATH] = [(403, "unaudited_client_can_only_post_to_private_accounts")]
    assert tt.upload_clip(str(video), "c", client=_client(fake, tmp_path, mode="direct")) == (False, "")
    assert fake.calls.count(("POST", tt.DIRECT_INIT_PATH)) == 1
    assert "private" in caplog.text.lower() and "inbox" in caplog.text


def test_chunk_put_retries_server_errors(fake, tmp_path, video):
    original = fake.handler
    hits = {"n": 0}

    def flaky(request):
        if request.method == "PUT" and hits["n"] == 0:
            hits["n"] += 1
            return httpx.Response(503)
        return original(request)

    client = _client(fake, tmp_path)
    client.http = httpx.Client(transport=httpx.MockTransport(flaky))
    client.tokens.http = client.http
    assert tt.upload_clip(str(video), "c", client=client)[0]
    assert hits["n"] == 1


def test_failed_status_reports_reason(fake, tmp_path, video, caplog):
    fake.statuses = ["FAILED"]
    client = _client(fake, tmp_path)
    fake.statuses = ["FAILED"]
    assert tt.upload_clip(str(video), "c", client=client) == (False, "")
    assert "duration_check_failed" in caplog.text


def test_status_timeout_counts_as_posted(fake, tmp_path, video, caplog):
    client = _client(fake, tmp_path, status_timeout_sec=0)
    fake.statuses = ["PROCESSING_UPLOAD"]
    ok, remote = tt.upload_clip(str(video), "c", client=client)
    assert ok and remote == "publish_id:v_pub_1"
    assert "avoid a duplicate" in caplog.text


def test_missing_video(fake, tmp_path):
    assert tt.upload_clip(str(tmp_path / "nope.mp4"), "c", client=_client(fake, tmp_path)) == (False, "")


# ---------------------------------------------------------------- tokens
def test_expiring_token_is_refreshed_and_rotated(fake, tmp_path, video):
    client = _client(fake, tmp_path, token={
        "access_token": "old", "refresh_token": "rt_old",
        "expires_at": time.time() + 10, "refresh_expires_at": time.time() + 86400})
    assert tt.upload_clip(str(video), "c", client=client)[0]
    assert fake.token_requests[0]["grant_type"] == "refresh_token"
    assert fake.token_requests[0]["refresh_token"] == "rt_old"
    saved = json.loads((tmp_path / "token.json").read_text())
    assert saved["refresh_token"] == "rt1"             # rotated value persisted
    assert saved["expires_at"] > time.time() + 7000


def test_no_token_tells_user_to_auth(fake, tmp_path, video, caplog):
    client = _client(fake, tmp_path, token={})
    assert tt.upload_clip(str(video), "c", client=client) == (False, "")
    assert "ttclips-upload --auth" in caplog.text


def test_expired_refresh_token(fake, tmp_path):
    client = _client(fake, tmp_path, token={
        "access_token": "a", "refresh_token": "r", "expires_at": 0, "refresh_expires_at": 1})
    with pytest.raises(tt.TikTokAPIError, match="refresh_expired"):
        client.tokens.access_token()


def test_exchange_code_sends_pkce_verifier(fake, tmp_path):
    client = _client(fake, tmp_path, token={})
    client.tokens.exchange_code("CODE", "http://localhost:1/callback/", "VERIFIER")
    form = fake.token_requests[0]
    assert form["grant_type"] == "authorization_code"
    assert (form["code"], form["code_verifier"], form["client_secret"]) == ("CODE", "VERIFIER", "cs")


def test_validate_session(fake, tmp_path):
    assert tt.validate_session(_client(fake, tmp_path, mode="direct"))
    fake.fail_next["/v2/user/info/"] = [(401, "access_token_invalid")]
    assert not tt.validate_session(_client(fake, tmp_path))


# ---------------------------------------------------------------- oauth redirect
def _hit(url):
    time.sleep(0.2)
    try:
        httpx.get(url, timeout=5)
    except httpx.HTTPError:
        pass


def test_redirect_server_returns_code():
    server = tt.RedirectServer(0)
    assert server.redirect_uri == f"http://localhost:{server.port}/callback/"
    threading.Thread(target=_hit, args=(server.redirect_uri.replace("localhost", "127.0.0.1") + "?code=abc&state=s1",)).start()
    assert server.wait_for_code("s1", timeout=10) == "abc"


def test_redirect_server_rejects_state_mismatch():
    server = tt.RedirectServer(0)
    threading.Thread(target=_hit, args=(server.redirect_uri.replace("localhost", "127.0.0.1") + "?code=abc&state=evil",)).start()
    with pytest.raises(tt.TikTokAPIError, match="state_mismatch"):
        server.wait_for_code("s1", timeout=10)


def test_redirect_server_surfaces_denial():
    server = tt.RedirectServer(0)
    threading.Thread(target=_hit, args=(
        server.redirect_uri.replace("localhost", "127.0.0.1") + "?error=access_denied&error_description=nope&state=s1",)).start()
    with pytest.raises(tt.TikTokAPIError, match="access_denied"):
        server.wait_for_code("s1", timeout=10)


# ---------------------------------------------------------------- integration
def test_uploader_dispatches_to_api(monkeypatch, tmp_path):
    import ttclips.uploader as up
    seen = {}

    def fake_upload(path, caption, *, is_aigc=False):
        seen.update(path=path, caption=caption, is_aigc=is_aigc)
        return True, "publish_id:x"

    monkeypatch.setattr(up, "BACKEND", "api")
    monkeypatch.setattr(tt, "upload_clip", fake_upload)
    monkeypatch.setattr(up, "_validate_cookies", lambda: pytest.fail("API needs no cookies"))
    assert up.publish_clip("v.mp4", "cap", is_aigc=True) == (True, "publish_id:x")
    assert seen == {"path": "v.mp4", "caption": "cap", "is_aigc": True}
    assert up.upload_clip("v.mp4", "cap") is True


def test_scheduler_labels_generated_clips_as_aigc(monkeypatch, state_files, tmp_path):
    import ttclips.scheduler as sched
    calls = []
    monkeypatch.setattr(sched, "publish_clip",
                        lambda p, c, *, is_aigc=False: calls.append(is_aigc) or (True, "rid"))
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    assert sched._publish("tiktok", clip, title="t", hashtags="#a", kind="generated")[0]
    assert sched._publish("tiktok", clip, title="t", hashtags="#a", kind="repost",
                          source_url="u")[0]
    assert calls == [True, False]
    from ttclips.state import load_posts
    assert [p["remote_id"] for p in load_posts()] == ["rid", "rid"]
