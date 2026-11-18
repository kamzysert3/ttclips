"""YouTube Shorts uploads through the official YouTube Data API v3.

This is the sanctioned alternative to the Studio/Playwright backend in
:mod:`ttclips.youtube_uploader`. Enable it with ``"backend": "api"`` in
``config/youtube.json``.

Setup (one-time):

1. In Google Cloud Console, create a project, enable *YouTube Data API v3*
   and create an OAuth client of type **Desktop app**.
2. Download the client JSON to ``config/client_secret.json`` (or point
   ``client_secrets_file`` at it).
3. ``pip install "ttclips[api]"`` then run ``ttclips-youtube --auth`` once.
   A browser window opens; the refresh token is saved to ``token_file``.

Caveat: until the Google Cloud project passes YouTube's API compliance
audit, every video it uploads is locked to **private**. Audited projects
upload normally.
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from ttclips.config import DATA_DIR, HOME, load_youtube_config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
RETRIABLE_STATUS = {429, 500, 502, 503, 504}
CHUNK_SIZE = 8 * 1024 * 1024
VALID_VISIBILITY = ("public", "unlisted", "private")

INSTALL_HINT = (
    "The YouTube API backend needs extra packages:\n"
    "    pip install \"ttclips[api]\""
)


class YouTubeAPIError(RuntimeError):
    """Raised for configuration/auth problems the user must fix."""


# ---------------------------------------------------------------------------
# Config

def _resolve(raw: str, default: Path) -> Path:
    raw = str(raw or "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else HOME / path


def client_secrets_path(cfg: dict | None = None) -> Path:
    cfg = load_youtube_config() if cfg is None else cfg
    return _resolve(cfg.get("client_secrets_file", ""), HOME / "config" / "client_secret.json")


def token_path(cfg: dict | None = None) -> Path:
    cfg = load_youtube_config() if cfg is None else cfg
    return _resolve(cfg.get("token_file", ""), DATA_DIR / "youtube_token.json")


def build_body(title: str, description: str, *, tags: list[str] | None = None,
               visibility: str = "public", made_for_kids: bool = False,
               category_id: str = "27") -> dict:
    """The ``videos.insert`` request body."""
    visibility = str(visibility or "public").strip().lower()
    if visibility not in VALID_VISIBILITY:
        log.warning("Unknown visibility '%s'; using private", visibility)
        visibility = "private"
    return {
        "snippet": {
            "title": str(title or "Short")[:100],
            "description": str(description or "")[:5000],
            "tags": list(tags or []),
            "categoryId": str(category_id or "27"),
        },
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }


# ---------------------------------------------------------------------------
# Auth

def _imports():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise YouTubeAPIError(INSTALL_HINT) from e
    return Request, Credentials, InstalledAppFlow, build, HttpError, MediaFileUpload


def get_credentials(*, interactive: bool = False, cfg: dict | None = None):
    """Load (and refresh) stored OAuth credentials.

    With ``interactive=True`` a missing or revoked token triggers the browser
    consent flow; otherwise a :class:`YouTubeAPIError` explains how to run it.
    """
    Request, Credentials, InstalledAppFlow, *_ = _imports()
    tok = token_path(cfg)
    creds = None
    if tok.exists():
        creds = Credentials.from_authorized_user_file(str(tok), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tok.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:  # refresh token revoked / expired
            log.warning("Refreshing the YouTube token failed: %s", e)
            creds = None
    if not interactive:
        raise YouTubeAPIError(
            f"No valid YouTube API token at {tok}. Run: ttclips-youtube --auth")
    secrets = client_secrets_path(cfg)
    if not secrets.exists():
        raise YouTubeAPIError(
            f"OAuth client file not found: {secrets}\n"
            "Create a 'Desktop app' OAuth client in Google Cloud Console "
            "(YouTube Data API v3 enabled) and save its JSON there.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text(creds.to_json(), encoding="utf-8")
    log.info("Saved YouTube API token to %s", tok)
    return creds


def _service(creds):
    build = _imports()[3]
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Upload

def _backoff(attempt: int, base: float = 2.0, cap: float = 64.0) -> float:
    return min(cap, base ** attempt) + random.uniform(0, 1)


def upload_short(video_path: str, title: str, description: str, *,
                 tags: list[str] | None = None, visibility: str = "public",
                 made_for_kids: bool = False, category_id: str = "27",
                 retries: int = 5, service=None) -> tuple[bool, str]:
    """Resumable upload of one Short. Returns ``(success, video_url)``.

    ``service`` can be injected (tests); otherwise one is built from the
    stored OAuth token.
    """
    _, _, _, _, HttpError, MediaFileUpload = _imports()
    if service is None:
        service = _service(get_credentials())
    body = build_body(title, description, tags=tags, visibility=visibility,
                      made_for_kids=made_for_kids, category_id=category_id)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                            chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    attempt = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status is not None:
                log.info("YouTube API upload %d%%", int(status.progress() * 100))
        except HttpError as e:
            code = getattr(getattr(e, "resp", None), "status", 0)
            if code not in RETRIABLE_STATUS or attempt >= retries:
                log.error("YouTube API upload failed (HTTP %s): %s", code, e)
                return False, ""
            attempt += 1
            wait = _backoff(attempt)
            log.warning("YouTube API HTTP %s; retry %d/%d in %.0fs", code, attempt, retries, wait)
            time.sleep(wait)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt >= retries:
                log.error("YouTube API upload failed: %s", e)
                return False, ""
            attempt += 1
            wait = _backoff(attempt)
            log.warning("YouTube API network error (%s); retry %d/%d in %.0fs",
                        e, attempt, retries, wait)
            time.sleep(wait)

    video_id = (response or {}).get("id", "")
    if not video_id:
        log.error("YouTube API returned no video id: %s", response)
        return False, ""
    privacy = (response.get("status") or {}).get("privacyStatus", "")
    if visibility != "private" and privacy == "private":
        log.warning("YouTube locked %s to private. Unaudited API projects can only "
                    "upload private videos; see docs/LEGAL.md.", video_id)
    url = f"https://youtube.com/shorts/{video_id}"
    log.info("YouTube API upload succeeded: %s", url)
    return True, url


def validate_session() -> bool:
    """True when the stored token works (calls ``channels.list mine=true``)."""
    try:
        service = _service(get_credentials())
        resp = service.channels().list(part="snippet", mine=True).execute()
    except YouTubeAPIError as e:
        log.error("%s", e)
        return False
    except Exception as e:
        log.error("YouTube API session check failed: %s", e)
        return False
    items = resp.get("items") or []
    if not items:
        log.error("Token is valid but no YouTube channel is attached to this account")
        return False
    log.info("YouTube API session valid for channel: %s",
             items[0].get("snippet", {}).get("title", "?"))
    return True
