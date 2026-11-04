"""YouTube Shorts uploader: Playwright automation of YouTube Studio.

Uploads run in a child subprocess (``python -m ttclips.youtube_uploader
<video>``) so a Playwright/thread crash inside one attempt can never poison
the scheduler process.

Why the Studio UI and not the YouTube Data API: since 28 July 2020, videos
uploaded by an *unverified* API project are locked as private and cannot be
published (not even manually in Studio). Lifting that requires passing
YouTube's compliance audit. Driving the Studio UI performs a normal upload
and needs no Google Cloud project at all.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from ttclips.config import (CACHE_DIR, CLIPS_DIR, HOME, load_env,
                            load_youtube_config, YOUTUBE_COOKIES_FILE)
from ttclips.cookies import YOUTUBE_DOMAINS, cookies_to_storage_state
from ttclips.encoder import probe_video

log = logging.getLogger(__name__)
YT_CFG = load_youtube_config()
ENV = load_env()

HEADLESS = bool(YT_CFG.get("headless", False))
DELAY_MIN = float(YT_CFG.get("min_action_delay_sec", 6))
DELAY_MAX = float(YT_CFG.get("max_action_delay_sec", 22))
RETRIES = max(1, int(YT_CFG.get("retries", 2)))
UPLOAD_TIMEOUT = int(YT_CFG.get("upload_timeout_sec", 300))
UA = YT_CFG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
RETRY_WAIT_INITIAL = float(YT_CFG.get("retry_wait_initial_sec", 30))
RETRY_WAIT_BACKOFF = float(YT_CFG.get("retry_wait_backoff", 2.0))
RETRY_WAIT_MAX = float(YT_CFG.get("retry_wait_max_sec", 300))

CHANNEL_ID = str(YT_CFG.get("channel_id", "") or "").strip()
VISIBILITY = str(YT_CFG.get("visibility", "public")).strip().lower()
MADE_FOR_KIDS = bool(YT_CFG.get("made_for_kids", False))
CATEGORY_ID = str(YT_CFG.get("category_id", "27"))
DEFAULT_HASHTAGS = str(YT_CFG.get("default_hashtags", "#Shorts #fyp"))
APPEND_SHORTS_TAG = bool(YT_CFG.get("append_shorts_hashtag", True))
INCLUDE_SOURCE_LINK = bool(YT_CFG.get("include_source_link", True))
MAX_TITLE_LEN = int(YT_CFG.get("max_title_len", 100))
MAX_DESC_LEN = int(YT_CFG.get("max_description_len", 5000))
MAX_DURATION_SEC = float(YT_CFG.get("max_duration_sec", 180))

# ---------------------------------------------------------------------------
# Studio selectors. Studio is a React app whose markup changes regularly, so
# every selector is overridable from config/youtube.json -> "selectors".
# A value may be a single CSS selector (commas are allowed, Playwright
# supports comma-separated CSS) or a list of alternatives.

_DEFAULT_SELECTORS: dict[str, list[str]] = {
    "file_input": [
        "ytcp-file-selector input[type='file']",
        "input[type='file']",
    ],
    "title": [
        "ytcp-title-and-description-input input#textbox",
        "ytcp-title-input input",
        "input#textbox",
        "ytcp-text-field#title input",
        "input[aria-label*='title' i]",
    ],
    "description": [
        "ytcp-video-metadata-description textarea",
        "#description-container textarea",
        "ytcp-social-suggestions textarea",
        "textarea[aria-label*='description' i]",
        "ytcp-video-metadata-description [contenteditable='true']",
    ],
    "next_button": [
        "ytcp-button#next-button",
        "button#next-button",
        "ytcp-button:has-text('Next')",
        "button:has-text('Next')",
    ],
    "show_more_button": [
        "ytcp-button:has-text('Show more')",
        "button:has-text('Show more')",
    ],
    "made_for_kids_no": [
        "ytcp-radio-button:has-text('not made for kids')",
        "ytcp-radio-button[aria-label*='not made for kids' i]",
        "tp-yt-paper-radio-button[aria-label*='not made for kids' i]",
    ],
    "made_for_kids_yes": [
        "ytcp-radio-button:has-text('made for kids')",
        "ytcp-radio-button[aria-label*='made for kids' i]",
        "tp-yt-paper-radio-button[aria-label*='made for kids' i]",
    ],
    "visibility_public": [
        "ytcp-radio-button:has-text('Public')",
        "ytcp-radio-button[aria-label*='Public' i]",
    ],
    "visibility_unlisted": [
        "ytcp-radio-button:has-text('Unlisted')",
        "ytcp-radio-button[aria-label*='Unlisted' i]",
    ],
    "visibility_private": [
        "ytcp-radio-button:has-text('Private')",
        "ytcp-radio-button[aria-label*='Private' i]",
    ],
    "post_button": [
        "ytcp-post-button",
        "button.post-button",
        "ytcp-button#post-button",
        "button#post-button",
        "ytcp-button:has-text('Post')",
    ],
    "create_button": [
        "ytcp-button#create-button",
        "button[aria-label='Create']",
        "button:has-text('Create')",
    ],
    "upload_videos_item": [
        "ytcp-ve-navigation-item:has-text('Upload videos')",
        "tp-yt-paper-item:has-text('Upload videos')",
        "[role='menuitem']:has-text('Upload videos')",
    ],
    "success_toast": [
        "ytcp-video-posted-toast",
        "[aria-label*='Video posted' i]",
        "[role='status']:has-text('posted')",
        "[role='alert']:has-text('posted')",
    ],
    "error_message": [
        "ytcp-error-message",
        "[role='alert'][aria-label*='error' i]",
        "ytcp-inline-error-message",
    ],
}


def _selector_list(value) -> list[str]:
    """Normalise a configured selector value into a list of CSS selectors."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _build_selectors() -> dict[str, list[str]]:
    selectors = {k: list(v) for k, v in _DEFAULT_SELECTORS.items()}
    for key, value in (YT_CFG.get("selectors") or {}).items():
        alts = _selector_list(value)
        if alts:
            selectors[key] = alts
    return selectors


SELECTORS = _build_selectors()


# ---------------------------------------------------------------------------
# Cookies

def cookies_path() -> Path:
    """Resolve the cookie file: env override -> config -> default location."""
    raw = str(ENV.get("YOUTUBE_COOKIES_FILE") or YT_CFG.get("cookies_file") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = HOME / path
        return path
    return YOUTUBE_COOKIES_FILE


def _validate_cookies() -> Path:
    path = cookies_path()
    if not path.exists() or path.stat().st_size < 100:
        raise FileNotFoundError(
            f"YouTube cookies file not found or too small: {path}\n"
            "Export cookies from a logged-in Chrome session:\n"
            "  1. Install 'Get cookies.txt LOCALLY' browser extension\n"
            "  2. Open studio.youtube.com (logged in)\n"
            "  3. Export cookies to Netscape format\n"
            "  4. Save as data/youtube_cookies.txt in the project root"
        )
    state = cookies_to_storage_state(path, YOUTUBE_DOMAINS)
    if not state["cookies"]:
        raise ValueError(
            f"No google.com/youtube.com cookies found in {path}.\n"
            "Re-export the cookies while logged into studio.youtube.com."
        )
    return path


# ---------------------------------------------------------------------------
# Metadata

def build_short_metadata(title: str, hashtags: str = "", source_url: str = "") -> dict:
    """Build the YouTube snippet for a Short.

    Returns ``{"title": str, "description": str, "tags": list[str]}`` with the
    title/description trimmed to YouTube's limits.
    """
    clean_title = " ".join(str(title or "").strip().split()) or "Short"
    if len(clean_title) > MAX_TITLE_LEN:
        clean_title = clean_title[:MAX_TITLE_LEN].rstrip()

    tags: list[str] = []
    for raw in str(hashtags or "").split():
        tag = raw.lstrip("#").strip()
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    if APPEND_SHORTS_TAG and not any(t.lower() == "shorts" for t in tags):
        tags.insert(0, "Shorts")

    parts = [clean_title]
    hashtags_text = " ".join(f"#{t}" for t in tags).strip()
    if hashtags_text:
        parts.append(hashtags_text)
    if INCLUDE_SOURCE_LINK and source_url:
        parts.append(f"Original: {source_url}")
    description = "\n\n".join(parts)
    if len(description) > MAX_DESC_LEN:
        description = description[:MAX_DESC_LEN].rstrip()

    # snippet.tags is capped at 500 characters in total.
    trimmed: list[str] = []
    used = 0
    for tag in tags:
        if used + len(tag) > 500:
            break
        trimmed.append(tag)
        used += len(tag)
    return {"title": clean_title, "description": description, "tags": trimmed}


def _default_hashtags() -> str:
    return DEFAULT_HASHTAGS


def validate_short(video_path: str) -> tuple[bool, str]:
    """Pre-flight check that a clip qualifies as a YouTube Short.

    A Short must be vertical (height > width) and at most
    ``max_duration_sec`` long (180 s). Returns ``(ok, reason)``.
    """
    path = Path(video_path)
    if not path.exists():
        return False, f"file not found: {path}"
    info = probe_video(path)
    duration = float(info.get("duration") or 0.0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if duration <= 0:
        log.warning("YouTube :: could not read the duration of %s - skipping the format check",
                    path.name)
        return True, ""
    if duration > MAX_DURATION_SEC:
        return False, (f"{path.name} is {duration:.1f}s long - YouTube Shorts must be "
                       f"{MAX_DURATION_SEC:.0f}s or shorter")
    if width and height and height <= width:
        return False, (f"{path.name} is {width}x{height} - Shorts must be vertical "
                       f"(height > width)")
    log.info("YouTube :: pre-flight ok - %s %dx%d, %.1fs", path.name, width, height, duration)
    return True, ""


# ---------------------------------------------------------------------------
# Playwright helpers

def _first_attached(page, selectors: list[str]):
    """Return the first locator that resolves to an attached element."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _first_visible(page, selectors: list[str], timeout_ms: int = 15000):
    """Poll until one of the selectors resolves to a visible element."""
    deadline = time.time() + max(0, timeout_ms) / 1000
    while True:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(0.4)


def _fill_field(page, selectors: list[str], value: str, timeout_ms: int = 15000) -> bool:
    loc = _first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        loc.fill(value)
        return True
    except Exception:
        pass
    try:
        loc.click()
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(value)
        return True
    except Exception:
        return False


def _click_any(page, selectors: list[str], timeout_ms: int = 15000) -> bool:
    loc = _first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        loc.click(timeout=6000)
        return True
    except Exception:
        pass
    try:
        loc.click(force=True, timeout=6000)
        return True
    except Exception:
        return False


def _human_delay(min_sec: float = DELAY_MIN, max_sec: float = DELAY_MAX) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff before the given attempt (1-based)."""
    wait = RETRY_WAIT_INITIAL * (RETRY_WAIT_BACKOFF ** (attempt - 1))
    return min(wait, RETRY_WAIT_MAX) * random.uniform(0.8, 1.2)


# ---------------------------------------------------------------------------
# Upload flow

def _goto_upload(page) -> None:
    url = (f"https://studio.youtube.com/channel/{CHANNEL_ID}/upload"
           if CHANNEL_ID else "https://studio.youtube.com/")
    log.info("YouTube :: navigating to %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=UPLOAD_TIMEOUT * 1000)
    time.sleep(4)
    if "accounts.google.com" in page.url or "/signin" in page.url:
        raise RuntimeError(
            "YouTube session expired or not signed in — re-export cookies "
            "from a logged-in studio.youtube.com session"
        )
    if CHANNEL_ID:
        return
    if not _click_any(page, SELECTORS["create_button"], 12000):
        raise RuntimeError(
            "Could not reach the YouTube Studio upload dialog (CREATE button "
            "not found) — set channel_id in config/youtube.json"
        )
    time.sleep(1.5)
    if not _click_any(page, SELECTORS["upload_videos_item"], 12000):
        raise RuntimeError("Could not open 'Upload videos' in YouTube Studio")
    time.sleep(3)


def _stage_file(page, video_path: str) -> None:
    loc = _first_attached(page, SELECTORS["file_input"])
    if loc is None:
        loc = _first_visible(page, SELECTORS["file_input"], 20000)
    if loc is None:
        raise RuntimeError(
            "YouTube Studio upload dialog not found (no file input) — run "
            "python -m ttclips.youtube_uploader --probe to inspect the DOM"
        )
    loc.set_input_files(str(video_path))
    log.info("YouTube :: staged %s", Path(video_path).name)
    time.sleep(3)


def _advance(page, max_steps: int = 4) -> None:
    """Click through the 'Next' steps of the upload dialog."""
    for _ in range(max_steps):
        loc = _first_visible(page, SELECTORS["next_button"], 3000)
        if loc is None:
            break
        try:
            loc.click(timeout=6000)
        except Exception:
            break
        time.sleep(2.5)


def _set_made_for_kids(page, made_for_kids: bool) -> None:
    key = "made_for_kids_yes" if made_for_kids else "made_for_kids_no"
    if _click_any(page, SELECTORS[key], 8000):
        log.info("YouTube :: made for kids = %s", made_for_kids)
    else:
        log.warning("YouTube :: 'Made for kids' option not found — keeping default")


def _set_visibility(page, visibility: str) -> None:
    key = f"visibility_{visibility}"
    selectors = SELECTORS.get(key) or SELECTORS["visibility_public"]
    if _click_any(page, selectors, 8000):
        log.info("YouTube :: visibility = %s", visibility)
    else:
        log.warning("YouTube :: visibility option %r not found — keeping default", visibility)


def _extract_video_id(href: str) -> str:
    if not href:
        return ""
    m = re.search(r"[?&]v=([\w-]{11})", href)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([\w-]{11})", href)
    if m:
        return m.group(1)
    return ""


def _grab_video_id(page) -> str:
    """Best-effort lookup of the id of the video we just posted."""
    for sel in ("a[href*='watch?v=']", "a[href*='youtu.be/']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                vid = _extract_video_id(loc.get_attribute("href") or "")
                if vid:
                    return vid
        except Exception:
            continue
    if not CHANNEL_ID:
        return ""
    try:
        page.goto(
            f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/upload?filter=shorts",
            wait_until="domcontentloaded", timeout=60000,
        )
        time.sleep(4)
        hrefs = page.eval_on_selector_all(
            "a[href*='watch?v=']", "els => els.map(e => e.getAttribute('href'))")
        for href in hrefs or []:
            vid = _extract_video_id(href or "")
            if vid:
                return vid
    except Exception as e:
        log.warning("YouTube :: could not resolve the posted video id: %s", e)
    return ""


def _wait_for_post_result(page, timeout_sec: int = 120) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        err = _first_visible(page, SELECTORS["error_message"], 200)
        if err is not None:
            try:
                message = err.inner_text()[:200]
            except Exception:
                message = "unknown error"
            raise RuntimeError(f"YouTube Studio reported an error: {message}")
        if _first_visible(page, SELECTORS["success_toast"], 200) is not None:
            return True
        if "/upload" not in page.url:
            return True
        time.sleep(1)
    log.warning("YouTube :: no confirmation seen after posting — assuming success")
    return True


def _upload_once(video_path: str, title: str, description: str, *,
                 visibility: str = "", made_for_kids: bool = False) -> str:
    """Run exactly one in-process upload. Returns the video URL (may be '')."""
    from playwright.sync_api import sync_playwright

    cookies = cookies_path()
    state = cookies_to_storage_state(cookies, YOUTUBE_DOMAINS)
    vis = (visibility or VISIBILITY or "public").lower()
    kids = MADE_FOR_KIDS if made_for_kids is None else bool(made_for_kids)

    ok, reason = validate_short(video_path)
    if not ok:
        raise RuntimeError(f"Not a valid YouTube Short: {reason}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            storage_state=state,
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        page.set_default_timeout(UPLOAD_TIMEOUT * 1000)
        try:
            _goto_upload(page)
            _stage_file(page, video_path)
            if not _fill_field(page, SELECTORS["title"], title, 20000):
                log.warning("YouTube :: title field not found")
            if not _fill_field(page, SELECTORS["description"], description, 8000):
                _click_any(page, SELECTORS["show_more_button"], 3000)
                _fill_field(page, SELECTORS["description"], description, 8000)
            _advance(page)
            _set_made_for_kids(page, kids)
            _set_visibility(page, vis)
            if not _click_any(page, SELECTORS["post_button"], 20000):
                raise RuntimeError("Could not find the 'Post' button in YouTube Studio")
            _wait_for_post_result(page)
            video_id = _grab_video_id(page)
            return f"https://youtu.be/{video_id}" if video_id else ""
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def _run_upload_child(video_path: str, title: str, description: str,
                       visibility: str, made_for_kids: bool, cookies: str) -> tuple[bool, str]:
    """Spawn a fresh python process that performs one upload."""
    payload = json.dumps({
        "title": title,
        "description": description,
        "visibility": visibility,
        "made_for_kids": made_for_kids,
        "cookies": cookies,
    })
    cmd = [sys.executable, "-m", "ttclips.youtube_uploader", str(video_path), "--input-json"]
    extra: dict = {}
    if os.name == "nt":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    log.info("YouTube upload subprocess: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=UPLOAD_TIMEOUT + 300,
            **extra,
        )
    except subprocess.TimeoutExpired:
        log.warning("YouTube upload child timed out after %ss", UPLOAD_TIMEOUT + 300)
        return False, ""
    for line in (proc.stdout or "").splitlines():
        log.info("[youtube] %s", line.rstrip())
    if proc.returncode != 0:
        tail = [l.strip() for l in (proc.stderr or "").splitlines() if l.strip()][-4:]
        log.warning("YouTube upload child exited %s: %s",
                    proc.returncode, " | ".join(tail) if tail else "no stderr")
        return False, ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT:"):
            rest = line[len("RESULT:"):].strip()
            if rest.startswith("ok"):
                return True, rest[2:].strip()
            return False, ""
    return False, ""


def upload_short(video_path: str, title: str, description: str, *,
                 visibility: str = "", made_for_kids: bool = False,
                 cookies: str = "") -> tuple[bool, str]:
    """Upload one vertical clip to YouTube as a Short.

    Each attempt runs in a fresh subprocess so a Playwright crash in one
    attempt can never poison the scheduler process. Returns
    ``(success, video_url)`` — the URL may be empty when YouTube did not
    expose the id of the posted video.
    """
    _validate_cookies()
    video_path = str(video_path)
    title = " ".join(str(title or "").split())
    description = str(description or "")

    ok, reason = validate_short(video_path)
    if not ok:
        log.error("YouTube upload skipped: %s", reason)
        return False, ""

    log.info("Uploading %s to YouTube (title %d chars)...",
             Path(video_path).name, len(title))
    _human_delay()
    for attempt in range(1, RETRIES + 1):
        try:
            ok, url = _run_upload_child(
                video_path, title, description,
                visibility or VISIBILITY, made_for_kids, cookies,
            )
            if ok:
                log.info("YouTube upload succeeded (attempt %d/%d) %s",
                         attempt, RETRIES, url or "")
                return True, url
        except Exception as e:
            log.warning("YouTube upload attempt %d/%d failed: %s", attempt, RETRIES, e)
        if attempt < RETRIES:
            wait = _backoff_delay(attempt)
            log.info("Waiting %.0fs before next attempt (backoff %d/%d)...",
                     wait, attempt, RETRIES - 1)
            time.sleep(wait)
    log.error("All YouTube upload attempts failed for %s", video_path)
    return False, ""


def validate_session() -> bool:
    """Check that the YouTube cookie session is usable. True when signed in."""
    _validate_cookies()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed — skipping session check")
        return True
    log.info("Validating YouTube session via Playwright ...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=cookies_to_storage_state(cookies_path(), YOUTUBE_DOMAINS),
            user_agent=UA,
        )
        page = context.new_page()
        page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        url = page.url
        logged_in = "accounts.google.com" not in url and "studio.youtube.com" in url
        context.close()
        browser.close()
        log.info("YouTube session valid: %s (final url: %s)", logged_in, url)
        return logged_in


# ---------------------------------------------------------------------------
# Probe (diagnostic, never posts)

_PROBE_JS = """() => {
  const out = [];
  const els = document.querySelectorAll(
    'input, textarea, button, ytcp-button, ytcp-radio-button, ' +
    'tp-yt-paper-radio-button, [role="radio"], [role="button"], ' +
    '[contenteditable="true"], a[href*="watch?v="]'
  );
  for (const el of els) {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName,
      id: el.id || '',
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      aria: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (el.innerText || '').trim().slice(0, 100),
      visible: !!(r.width && r.height),
    });
  }
  return out;
}"""


def run_probe(probe_video: str = "", stage: bool = False) -> int:
    """Diagnostic (no post): open the upload dialog and dump the DOM."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright is not installed")
        return 1

    video = Path(probe_video) if probe_video else None
    if video is None:
        clips = sorted(CLIPS_DIR.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not clips:
            log.error("No mp4 in %s — pass --probe-video ...", CLIPS_DIR)
            return 1
        video = clips[0]
    if not video.exists():
        log.error("Probe video not found: %s", video)
        return 1
    _validate_cookies()

    probe_dir = CACHE_DIR / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            storage_state=cookies_to_storage_state(cookies_path(), YOUTUBE_DOMAINS),
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        page.set_default_timeout(UPLOAD_TIMEOUT * 1000)
        try:
            log.info("Probe :: navigating to the upload page")
            _goto_upload(page)
            if stage:
                log.info("Probe :: staging %s (creates a draft, never posts)", video.name)
                _stage_file(page, str(video))
            page.screenshot(path=str(probe_dir / "screen.png"), full_page=True)
            (probe_dir / "dom.html").write_text(page.content(), encoding="utf-8")
            (probe_dir / "elements.json").write_text(
                json.dumps(page.evaluate(_PROBE_JS), indent=2, ensure_ascii=False),
                encoding="utf-8")
            (probe_dir / "summary.json").write_text(
                json.dumps({
                    "url": page.url,
                    "title": page.title(),
                    "video": video.name,
                    "staged": stage,
                }, indent=2), encoding="utf-8")
            log.info("Probe :: dumps written to %s", probe_dir)
        finally:
            context.close()
            browser.close()
    return 0


def main() -> None:
    """Child-process entrypoint: upload one clip, exit 0 on success."""
    import argparse

    parser = argparse.ArgumentParser(description="Upload one clip to YouTube Shorts")
    parser.add_argument("video", nargs="?", default="", help="Path to the mp4 to upload")
    parser.add_argument("--input-json", action="store_true",
                        help="Read {title, description, visibility, made_for_kids, cookies} JSON from stdin")
    parser.add_argument("--title", type=str, default="", help="Video title")
    parser.add_argument("--description", type=str, default="", help="Video description")
    parser.add_argument("--visibility", type=str, default="", help="public|unlisted|private")
    parser.add_argument("--made-for-kids", action="store_true", help="Flag as made for kids")
    parser.add_argument("--probe", action="store_true",
                        help="Diagnostic only: open the upload dialog and dump the DOM (never posts)")
    parser.add_argument("--probe-video", type=str, default="",
                        help="mp4 to stage during --probe (default: latest clip in data/clips)")
    parser.add_argument("--probe-stage", action="store_true",
                        help="With --probe, also select the file (starts a draft upload, never posts)")
    parser.add_argument("--validate-session", action="store_true",
                        help="Check whether the YouTube cookie session is alive")
    args = parser.parse_args()

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        )

    if args.validate_session:
        try:
            valid = validate_session()
        except Exception as e:
            log.error("YouTube session check failed: %s", e)
            sys.exit(1)
        sys.exit(0 if valid else 1)

    if args.probe:
        try:
            sys.exit(run_probe(args.probe_video, args.probe_stage))
        except Exception as e:
            log.error("Probe failed: %s", e)
            sys.exit(1)

    if args.input_json:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as e:
            log.error("Bad JSON on stdin: %s", e)
            sys.exit(2)
    else:
        payload = {}

    title = payload.get("title", args.title) or args.title
    description = payload.get("description", args.description) or args.description
    visibility = payload.get("visibility", args.visibility) or args.visibility
    made_for_kids = bool(payload.get("made_for_kids", args.made_for_kids))
    cookies = payload.get("cookies", "")

    path = Path(args.video)
    if not path.exists():
        log.error("Video not found: %s", path)
        sys.exit(2)
    try:
        _validate_cookies()
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)
    url = _upload_once(str(path), title, description,
                       visibility=visibility, made_for_kids=made_for_kids)
    print(f"RESULT: ok {url}".rstrip(), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
