"""TikTok uploads through the official Content Posting API.

The sanctioned alternative to the cookie/Playwright backend in
:mod:`ttclips.uploader`. Enable it with ``"backend": "api"`` in
``config/tiktok.json``. Two modes (``api_mode``):

* ``inbox`` (default) — the clip is sent to your TikTok inbox as a draft
  (scope ``video.upload``). You tap the notification and publish it from the
  app. Works without TikTok's app audit and keeps a human in the loop.
* ``direct`` — posts straight to the profile (scope ``video.publish``).
  Until your app passes TikTok's audit, direct posts are forced to
  ``SELF_ONLY`` and the account itself must be set to private.

Setup (one-time):

1. Create an app at https://developers.tiktok.com/, add *Login Kit* (Desktop)
   and *Content Posting API*. Register the redirect URI
   ``http://localhost:*/callback/`` (TikTok supports wildcard ports for
   desktop apps).
2. Put ``TIKTOK_CLIENT_KEY`` and ``TIKTOK_CLIENT_SECRET`` in ``.env``.
3. Run ``ttclips-upload --auth`` once; a browser opens for consent and the
   tokens are saved to ``token_file`` (refreshed automatically afterwards).

The client secret lives on your own machine: ttclips is self-hosted and
single-user, so you register your *own* TikTok app.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

from ttclips.config import DATA_DIR, HOME, load_env, load_tiktok_config

log = logging.getLogger(__name__)

API_BASE = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{API_BASE}/v2/oauth/token/"
USER_INFO_URL = f"{API_BASE}/v2/user/info/"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
DIRECT_INIT_PATH = "/v2/post/publish/video/init/"
INBOX_INIT_PATH = "/v2/post/publish/inbox/video/init/"
STATUS_PATH = "/v2/post/publish/status/fetch/"
REDIRECT_PATH = "/callback/"

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_FINAL_CHUNK = 128 * MB
MAX_CHUNKS = 1000
MAX_VIDEO_BYTES = 4 * 1024 * MB
DEFAULT_CHUNK = 10 * MB
MAX_TITLE_RUNES = 2200

MODES = ("inbox", "direct")
SCOPES = {
    "inbox": ["user.info.basic", "video.upload"],
    "direct": ["user.info.basic", "video.publish"],
}
# config/tiktok.json "visibility" (shared with the browser backend) -> API value
VISIBILITY_TO_PRIVACY = {
    "everyone": "PUBLIC_TO_EVERYONE",
    "public": "PUBLIC_TO_EVERYONE",
    "friends": "MUTUAL_FOLLOW_FRIENDS",
    "followers": "FOLLOWER_OF_CREATOR",
    "private": "SELF_ONLY",
    "only_me": "SELF_ONLY",
    "self_only": "SELF_ONLY",
}
RETRIABLE_HTTP = {429, 500, 502, 503, 504}
TERMINAL_OK = {"inbox": "SEND_TO_USER_INBOX", "direct": "PUBLISH_COMPLETE"}
# Error codes whose fix is on the user's side: explain, never retry.
ACTIONABLE = {
    "unaudited_client_can_only_post_to_private_accounts":
        "Unaudited TikTok apps can only direct-post to a PRIVATE account. Set the "
        "account to private, switch api_mode to 'inbox', or get the app audited.",
    "spam_risk_too_many_posts":
        "TikTok's daily post cap for this account is reached; try again tomorrow.",
    "spam_risk_user_banned_from_posting": "This TikTok account is banned from posting.",
    "reached_active_user_cap": "The TikTok app's daily active publishing-user quota is reached.",
    "scope_not_authorized":
        "The token lacks the scope this api_mode needs. Re-run: ttclips-upload --auth",
    "access_token_invalid": "The TikTok token is invalid. Re-run: ttclips-upload --auth",
    "privacy_level_option_mismatch":
        "privacy_level is not one the account allows (see creator_info).",
}


class TikTokAPIError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, status: int = 0, log_id: str = ""):
        self.code = code or "unknown_error"
        self.status = status
        self.log_id = log_id
        hint = ACTIONABLE.get(self.code, "")
        text = f"{self.code}: {message}".rstrip(": ")
        if hint:
            text += f" — {hint}"
        if log_id:
            text += f" (log_id {log_id})"
        super().__init__(text)

    @property
    def retriable(self) -> bool:
        return self.status in RETRIABLE_HTTP or self.code in ("rate_limit_exceeded", "internal_error")


# ---------------------------------------------------------------------------
# Settings

@dataclass
class Settings:
    client_key: str
    client_secret: str
    mode: str = "inbox"
    token_file: Path = DATA_DIR / "tiktok_token.json"
    privacy_level: str = "PUBLIC_TO_EVERYONE"
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False
    chunk_size: int = DEFAULT_CHUNK
    status_timeout_sec: float = 300.0
    status_poll_sec: float = 5.0
    retries: int = 4
    redirect_port: int = 0  # 0 = random free port (register a wildcard-port URI)

    @classmethod
    def load(cls, cfg: dict | None = None, env: dict | None = None) -> "Settings":
        cfg = load_tiktok_config() if cfg is None else cfg
        env = load_env() if env is None else env
        api = cfg.get("api") or {}

        def pick(key, default=None):
            return api.get(key, cfg.get(key, default))

        mode = str(pick("api_mode", "inbox") or "inbox").strip().lower()
        if mode not in MODES:
            log.warning("Unknown TikTok api_mode '%s'; using inbox", mode)
            mode = "inbox"
        privacy = str(pick("privacy_level", "") or "").strip().upper()
        if not privacy:
            vis = str(cfg.get("visibility", "everyone")).strip().lower()
            privacy = VISIBILITY_TO_PRIVACY.get(vis, "PUBLIC_TO_EVERYONE")
        raw_token = str(pick("token_file", "") or "").strip()
        token = Path(raw_token).expanduser() if raw_token else DATA_DIR / "tiktok_token.json"
        if not token.is_absolute():
            token = HOME / token
        return cls(
            client_key=str(env.get("TIKTOK_CLIENT_KEY") or os.environ.get("TIKTOK_CLIENT_KEY", "")).strip(),
            client_secret=str(env.get("TIKTOK_CLIENT_SECRET") or os.environ.get("TIKTOK_CLIENT_SECRET", "")).strip(),
            mode=mode,
            token_file=token,
            privacy_level=privacy,
            disable_comment=bool(pick("disable_comment", False)),
            disable_duet=bool(pick("disable_duet", False)),
            disable_stitch=bool(pick("disable_stitch", False)),
            chunk_size=int(float(pick("chunk_size_mb", DEFAULT_CHUNK / MB)) * MB),
            status_timeout_sec=float(pick("status_timeout_sec", 300)),
            status_poll_sec=float(pick("status_poll_sec", 5)),
            retries=max(1, int(pick("api_retries", 4))),
            redirect_port=int(pick("redirect_port", 0) or 0),
        )

    def require_client(self) -> None:
        if not self.client_key or not self.client_secret:
            raise TikTokAPIError(
                "missing_client_credentials",
                "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env "
                "(from your app at developers.tiktok.com)")


# ---------------------------------------------------------------------------
# PKCE + chunk planning (pure helpers)

def make_pkce() -> tuple[str, str]:
    """``(verifier, challenge)``. TikTok's desktop flow wants the challenge as
    the **hex** SHA-256 of the verifier — not base64url like RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, hashlib.sha256(verifier.encode("ascii")).hexdigest()


def build_auth_url(client_key: str, redirect_uri: str, scopes: list[str],
                   state: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "client_key": client_key,
        "scope": ",".join(scopes),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTH_URL}?{query}"


def plan_chunks(video_size: int, chunk_size: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """``(chunk_size, total_chunk_count)`` following TikTok's transfer rules.

    Chunks are 5–64 MB; the count is the size divided by the chunk size
    *rounded down*, so the final chunk absorbs the remainder (up to 128 MB).
    Files no bigger than one chunk go up in a single request.
    """
    if video_size <= 0:
        raise ValueError("video is empty")
    if video_size > MAX_VIDEO_BYTES:
        raise ValueError("TikTok accepts videos up to 4 GB")
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, int(chunk_size)))
    if video_size <= chunk:
        return video_size, 1
    count = video_size // chunk
    while count > MAX_CHUNKS or video_size - chunk * (count - 1) > MAX_FINAL_CHUNK:
        chunk = min(MAX_CHUNK, chunk * 2)
        count = video_size // chunk
    return chunk, count


def chunk_ranges(video_size: int, chunk_size: int, count: int) -> list[tuple[int, int]]:
    """Inclusive ``(first_byte, last_byte)`` per chunk; the last takes the rest."""
    ranges = []
    for i in range(count):
        start = i * chunk_size
        end = video_size - 1 if i == count - 1 else start + chunk_size - 1
        ranges.append((start, end))
    return ranges


def resolve_privacy(wanted: str, options: list[str]) -> str:
    """Use the configured privacy level when the account allows it, otherwise
    fall back to SELF_ONLY (always offered) rather than sending a value TikTok
    will reject."""
    if wanted in options:
        return wanted
    fallback = "SELF_ONLY" if "SELF_ONLY" in options or not options else options[-1]
    log.warning("TikTok privacy '%s' not allowed for this account (allowed: %s); using %s",
                wanted, ", ".join(options) or "none reported", fallback)
    return fallback


def truncate_runes(text: str, limit: int = MAX_TITLE_RUNES) -> str:
    """Trim to *limit* UTF-16 code units (TikTok counts titles that way)."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: limit * 2].decode("utf-16-le", errors="ignore").rstrip()


# ---------------------------------------------------------------------------
# Tokens

class TokenStore:
    """Stores the OAuth token JSON and refreshes it before expiry. TikTok
    rotates refresh tokens, so every refresh response is persisted."""

    REFRESH_MARGIN_SEC = 300

    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings = settings
        self.http = http
        self.path = settings.token_file

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("TikTok token file %s is unreadable", self.path)
            return {}

    def save(self, token: dict) -> dict:
        now = time.time()
        token = dict(token)
        if "expires_in" in token:
            token["expires_at"] = now + float(token.pop("expires_in"))
        if "refresh_expires_in" in token:
            token["refresh_expires_at"] = now + float(token.pop("refresh_expires_in"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return token

    def _token_request(self, data: dict) -> dict:
        self.settings.require_client()
        payload = {"client_key": self.settings.client_key,
                   "client_secret": self.settings.client_secret, **data}
        resp = self.http.post(TOKEN_URL, data=payload,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            body = resp.json()
        except ValueError:
            raise TikTokAPIError("bad_token_response", resp.text[:200], status=resp.status_code)
        if resp.status_code >= 400 or "access_token" not in body:
            raise TikTokAPIError(str(body.get("error") or "token_error"),
                                 str(body.get("error_description") or ""),
                                 status=resp.status_code, log_id=str(body.get("log_id") or ""))
        return body

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict:
        return self.save(self._token_request({
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code_verifier": verifier,
        }))

    def access_token(self) -> str:
        token = self.load()
        if not token.get("access_token"):
            raise TikTokAPIError("not_authorized",
                                 f"No TikTok token at {self.path}. Run: ttclips-upload --auth")
        now = time.time()
        if float(token.get("expires_at", 0)) - now > self.REFRESH_MARGIN_SEC:
            return token["access_token"]
        if not token.get("refresh_token") or float(token.get("refresh_expires_at", now + 1)) <= now:
            raise TikTokAPIError("refresh_expired",
                                 "TikTok refresh token expired. Run: ttclips-upload --auth")
        log.info("Refreshing TikTok access token")
        token = self.save(self._token_request({
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
        }))
        return token["access_token"]


class RedirectServer:
    """One-shot localhost server that receives TikTok's OAuth redirect.

    Binds in ``__init__`` so the real port is known before the authorize URL
    is built (``port=0`` picks a free port; register the redirect URI with a
    wildcard port, ``http://localhost:*/callback/``).
    """

    def __init__(self, port: int = 0, host: str = "localhost"):
        self.result: dict = {}
        self._done = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib API
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.rstrip("/") != REDIRECT_PATH.rstrip("/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                outer.result = dict(urllib.parse.parse_qsl(parsed.query))
                ok = "code" in outer.result
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = ("ttclips is connected to TikTok. You can close this tab." if ok
                       else "TikTok authorization failed. Check the terminal.")
                self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
                outer._done.set()

            def log_message(self, *args):  # keep stderr quiet
                pass

        self._server = http.server.HTTPServer((host, port), Handler)
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://localhost:{self.port}{REDIRECT_PATH}"

    def wait_for_code(self, expected_state: str, timeout: float = 300) -> str:
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        try:
            if not self._done.wait(timeout):
                raise TikTokAPIError("auth_timeout", "No redirect from TikTok within the time limit")
        finally:
            self._server.shutdown()
            self._server.server_close()
        if self.result.get("error"):
            raise TikTokAPIError(self.result["error"], self.result.get("error_description", ""))
        if self.result.get("state") != expected_state:
            raise TikTokAPIError("state_mismatch", "OAuth state did not match; aborting")
        return self.result["code"]


def authorize(settings: Settings | None = None, *, open_browser: bool = True,
              timeout: float = 300) -> dict:
    """Interactive one-time consent flow (``ttclips-upload --auth``)."""
    settings = settings or Settings.load()
    settings.require_client()
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    server = RedirectServer(settings.redirect_port)
    url = build_auth_url(settings.client_key, server.redirect_uri, SCOPES[settings.mode],
                         state, challenge)
    print(f"Opening TikTok to authorize ttclips ({settings.mode} mode):\n  {url}")
    if open_browser:
        webbrowser.open(url)
    code = server.wait_for_code(state, timeout)
    with httpx.Client(timeout=30) as http_client:
        token = TokenStore(settings, http_client).exchange_code(code, server.redirect_uri, verifier)
    log.info("Saved TikTok token to %s (scopes: %s)", settings.token_file, token.get("scope", "?"))
    return token


# ---------------------------------------------------------------------------
# Client

def _backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** attempt) + random.uniform(0, 1)


class TikTokClient:
    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None,
                 sleep=time.sleep):
        self.settings = settings or Settings.load()
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self.tokens = TokenStore(self.settings, self.http)
        self.sleep = sleep

    # -- JSON API -----------------------------------------------------------
    def _post(self, path: str, body: dict | None = None) -> dict:
        for attempt in range(self.settings.retries + 1):
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}",
                       "Content-Type": "application/json; charset=UTF-8"}
            try:
                resp = self.http.post(f"{API_BASE}{path}", json=body or {}, headers=headers)
                err = self._error_from(resp)
            except httpx.TransportError as e:
                err = TikTokAPIError("network_error", str(e), status=503)
                resp = None
            if err is None:
                return resp.json().get("data") or {}
            if not err.retriable or attempt >= self.settings.retries:
                raise err
            wait = _backoff(attempt + 1)
            log.warning("TikTok API %s; retry %d/%d in %.0fs", err.code, attempt + 1,
                        self.settings.retries, wait)
            self.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    def _error_from(resp: httpx.Response) -> TikTokAPIError | None:
        try:
            body = resp.json()
        except ValueError:
            return TikTokAPIError("bad_response", resp.text[:200], status=resp.status_code)
        error = body.get("error") or {}
        code = str(error.get("code") or ("ok" if resp.status_code < 400 else "http_error"))
        if code == "ok" and resp.status_code < 400:
            return None
        return TikTokAPIError(code, str(error.get("message") or ""), status=resp.status_code,
                              log_id=str(error.get("log_id") or error.get("logid") or ""))

    # -- endpoints ----------------------------------------------------------
    def user_info(self) -> dict:
        resp = self.http.get(USER_INFO_URL, params={"fields": "open_id,display_name"},
                             headers={"Authorization": f"Bearer {self.tokens.access_token()}"})
        err = self._error_from(resp)
        if err:
            raise err
        return (resp.json().get("data") or {}).get("user") or {}

    def creator_info(self) -> dict:
        return self._post(CREATOR_INFO_PATH)

    def init_upload(self, video_size: int, *, caption: str = "", is_aigc: bool = False,
                    creator: dict | None = None) -> tuple[dict, int, int]:
        chunk, count = plan_chunks(video_size, self.settings.chunk_size)
        source = {"source": "FILE_UPLOAD", "video_size": video_size,
                  "chunk_size": chunk, "total_chunk_count": count}
        if self.settings.mode == "inbox":
            data = self._post(INBOX_INIT_PATH, {"source_info": source})
        else:
            creator = creator if creator is not None else self.creator_info()
            data = self._post(DIRECT_INIT_PATH, {
                "post_info": self.post_info(caption, creator, is_aigc=is_aigc),
                "source_info": source,
            })
        if not data.get("publish_id") or not data.get("upload_url"):
            raise TikTokAPIError("bad_init_response", json.dumps(data)[:200])
        return data, chunk, count

    def post_info(self, caption: str, creator: dict, *, is_aigc: bool = False) -> dict:
        """Direct-post metadata. Never loosens what the creator disabled in
        their own TikTok settings."""
        return {
            "title": truncate_runes(caption.strip()),
            "privacy_level": resolve_privacy(self.settings.privacy_level,
                                             list(creator.get("privacy_level_options") or [])),
            "disable_comment": self.settings.disable_comment or bool(creator.get("comment_disabled")),
            "disable_duet": self.settings.disable_duet or bool(creator.get("duet_disabled")),
            "disable_stitch": self.settings.disable_stitch or bool(creator.get("stitch_disabled")),
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": bool(is_aigc),
        }

    def upload_file(self, upload_url: str, path: Path, chunk: int, count: int) -> None:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            for idx, (start, end) in enumerate(chunk_ranges(size, chunk, count), 1):
                fh.seek(start)
                data = fh.read(end - start + 1)
                headers = {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                           "Content-Range": f"bytes {start}-{end}/{size}"}
                for attempt in range(self.settings.retries + 1):
                    try:
                        resp = self.http.put(upload_url, content=data, headers=headers)
                        status = resp.status_code
                    except httpx.TransportError as e:
                        status, resp = 0, None
                        log.warning("Chunk %d/%d network error: %s", idx, count, e)
                    if status in (200, 201, 206):
                        break
                    if status and status not in RETRIABLE_HTTP:
                        raise TikTokAPIError("upload_failed",
                                             f"chunk {idx}/{count} HTTP {status}: "
                                             f"{resp.text[:200] if resp is not None else ''}",
                                             status=status)
                    if attempt >= self.settings.retries:
                        raise TikTokAPIError("upload_failed", f"chunk {idx}/{count} gave up",
                                             status=status)
                    self.sleep(_backoff(attempt + 1))
                log.info("TikTok upload chunk %d/%d done", idx, count)

    def wait_for_status(self, publish_id: str) -> dict:
        """Poll until the post reaches a terminal state for this mode."""
        target = TERMINAL_OK[self.settings.mode]
        deadline = time.monotonic() + self.settings.status_timeout_sec
        last = {}
        while True:
            last = self._post(STATUS_PATH, {"publish_id": publish_id})
            status = last.get("status", "")
            if status == target or status == "PUBLISH_COMPLETE":
                return last
            if status == "FAILED":
                raise TikTokAPIError(str(last.get("fail_reason") or "publish_failed"),
                                     "TikTok rejected the post")
            if time.monotonic() >= deadline:
                last["_timed_out"] = True
                return last
            self.sleep(self.settings.status_poll_sec)  # status endpoint: 30 req/min


def upload_clip(video_path: str, caption: str, *, is_aigc: bool = False,
                client: TikTokClient | None = None) -> tuple[bool, str]:
    """Upload one clip. Returns ``(success, remote_id)``.

    ``remote_id`` is the public post URL when TikTok reports one (direct
    mode), otherwise ``publish_id:<id>``. If TikTok is still processing when
    ``status_timeout_sec`` runs out the upload is treated as done (the bytes
    are with TikTok) so the scheduler never double-posts the clip.
    """
    path = Path(video_path)
    if not path.exists():
        log.error("Video not found: %s", path)
        return False, ""
    try:
        client = client or TikTokClient()
        creator = client.creator_info() if client.settings.mode == "direct" else None
        if creator and creator.get("max_video_post_duration_sec"):
            log.info("TikTok creator %s allows up to %ss videos",
                     creator.get("creator_username", "?"), creator["max_video_post_duration_sec"])
        data, chunk, count = client.init_upload(path.stat().st_size, caption=caption,
                                                is_aigc=is_aigc, creator=creator)
        publish_id = data["publish_id"]
        log.info("TikTok %s upload started (publish_id %s, %d chunk(s))",
                 client.settings.mode, publish_id, count)
        client.upload_file(data["upload_url"], path, chunk, count)
        status = client.wait_for_status(publish_id)
    except (TikTokAPIError, ValueError, OSError) as e:
        log.error("TikTok API upload failed: %s", e)
        return False, ""

    if status.get("_timed_out"):
        log.warning("TikTok still processing %s after %.0fs (last status %s); "
                    "recording it as posted to avoid a duplicate.",
                    publish_id, client.settings.status_timeout_sec, status.get("status", "?"))
    if client.settings.mode == "inbox":
        log.info("TikTok draft sent to your inbox — open TikTok to publish it (publish_id %s)",
                 publish_id)
        return True, f"publish_id:{publish_id}"
    post_ids = status.get("publicaly_available_post_id") or []  # sic — TikTok's spelling
    if post_ids:
        user = (creator or {}).get("creator_username", "")
        url = (f"https://www.tiktok.com/@{user}/video/{post_ids[0]}" if user
               else f"https://www.tiktok.com/video/{post_ids[0]}")
        log.info("TikTok post published: %s", url)
        return True, url
    log.info("TikTok post accepted (publish_id %s). Unaudited apps post as private.", publish_id)
    return True, f"publish_id:{publish_id}"


def validate_session(client: TikTokClient | None = None) -> bool:
    """True when the stored token works for the configured mode."""
    try:
        client = client or TikTokClient()
        user = client.user_info()
        if client.settings.mode == "direct":
            info = client.creator_info()
            log.info("TikTok direct posting as @%s; allowed privacy: %s",
                     info.get("creator_username", "?"),
                     ", ".join(info.get("privacy_level_options") or []))
        log.info("TikTok API session valid for %s (%s mode)",
                 user.get("display_name", "?"), client.settings.mode)
        return True
    except (TikTokAPIError, httpx.HTTPError) as e:
        log.error("TikTok API session check failed: %s", e)
        return False
