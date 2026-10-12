"""TikTok uploader: Playwright browser automation with cookie-based auth.

Uploads run in a child subprocess (``python -m src.uploader <video>``) so a
Playwright/thread crash inside one attempt can never poison the scheduler
process — tiktok_uploader never stops its internal sync_playwright loop, and
a second in-process call after a failure raises the "Sync API inside the
asyncio loop" error.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import random
import subprocess
import sys
import time
from pathlib import Path

from src.config import CLIPS_DIR, COOKIES_FILE, load_env, load_tiktok_config

log = logging.getLogger(__name__)
TK_CFG = load_tiktok_config()
ENV = load_env()

DELAY_MIN = TK_CFG.get("min_action_delay_sec", 6)
DELAY_MAX = TK_CFG.get("max_action_delay_sec", 22)
RETRIES = max(1, int(TK_CFG.get("retries", 2)))
UPLOAD_TIMEOUT = int(TK_CFG.get("upload_timeout_sec", 180))
UA = TK_CFG.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
RETRY_WAIT_INITIAL = float(TK_CFG.get("retry_wait_initial_sec", 30))
RETRY_WAIT_BACKOFF = float(TK_CFG.get("retry_wait_backoff", 2.0))
RETRY_WAIT_MAX = float(TK_CFG.get("retry_wait_max_sec", 300))

# ---------------------------------------------------------------------------
# Probe scaffolding: reach the upload form, type a hashtag, dump the DOM so we
# can find TikTok's real suggestion-popover markup. Never posts.
PROBE_DIR = Path(__file__).resolve().parent.parent / "data" / "probe"

_PROBE_JS = """() => {
  const cap = document.querySelector('[contenteditable="true"]');
  const cR = cap ? cap.getBoundingClientRect() : null;
  const out = [];
  const els = document.querySelectorAll('*');
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    if (!el.getBoundingClientRect) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const role = el.getAttribute('role');
    const cls0 = (typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '');
    const low = cls0.toLowerCase();
    const txt = (el.innerText || '').trim();
    const smell = /mention|suggest|option|listbox|menu|auto|popup|panel/.test(low)
      || (role && /listbox|menu|option|combobox/.test(role));
    const hash = txt.indexOf('#') === 0;
    if (!smell && !hash) continue;
    let dist = 1e9;
    let distY = 1e9;
    if (cR) {
      const ex = r.left + r.width / 2;
      const ey = r.top + r.height / 2;
      dist = Math.hypot(ex - (cR.left + cR.width / 2), ey - (cR.top + cR.height / 2));
      distY = ey - (cR.top + cR.height / 2);
    }
    out.push({
      tag: el.tagName,
      role: role || '',
      cls: cls0.slice(0, 120),
      text: txt.length > 120 ? txt.slice(0, 120) + '...' : txt,
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
      dist: Math.round(dist), distY: Math.round(distY),
    });
    if (out.length > 600) break;
  }
  return out;
}"""

# ---------------------------------------------------------------------------
# Idempotent patch for the vendored tiktok_uploader library.
#
# 1. coachmark/tooltip dismissal (TikTok's one-time "new tools" popover blocks
#    the post flow and makes the uploader hang),
# 2. caption typing that accepts ONLY exact-match hashtag suggestions so real
#    tags become clickable chips (plain-typed #words are never linkified);
#    suggestions that don't match the typed tag are committed as plain text.
VENDOR_PATCH_MARKER = "# ttclips-patch:v4"

_VENDOR_PATCH_FUNCS = '''# ttclips-patch:v4
def _set_plain_description(page, description: str) -> None:
    """ttclips: type the caption, accepting ONLY the exact hashtag
    suggestion (first popover option whose .hash-tag-topic equals the typed
    tag) so real tags become clickable chips; others stay plain."""
    if description is None:
        return
    description = description.encode("utf-8", "ignore").decode("utf-8")
    try:
        desc_locator = page.locator(f"xpath={config.selectors.upload.description}")
        desc_locator.wait_for(state="visible", timeout=config.implicit_wait * 1000)
        desc_locator.click()
        _clear(desc_locator)
        time.sleep(1)
        mention_box = page.locator(f"xpath={config.selectors.upload.mention_box}")
        for word in description.split(" "):
            if not word:
                continue
            if word[0] != "#":
                desc_locator.press_sequentially(word + " ", delay=20)
                continue
            tag = word[1:]
            desc_locator.press_sequentially(word, delay=80)
            time.sleep(1.2)
            accepted = False
            try:
                mention_box.wait_for(state="visible", timeout=config.add_hashtag_wait * 1000)
                time.sleep(0.8)
                option = mention_box.locator("div[role='option']").first
                topic = option.locator("span.hash-tag-topic").first
                topic_txt = topic.inner_text().strip()
                if topic_txt.lstrip("#").strip().lower() == tag.lower():
                    print("ttclips: chip", word)
                    option.click()
                    time.sleep(0.4)
                    accepted = True
                print("ttclips: plain (first option:", topic_txt, ") ", word)
            except Exception:
                print("ttclips: plain (no popover) ", word)
            if not accepted:
                desc_locator.press(" ")
        time.sleep(0.5)
    except Exception as exception:
        print("Failed to set description: ", exception)
        _clear(desc_locator)
        desc_locator.fill(description)


def _dismiss_coachmarks(page) -> None:
    """ttclips: dismiss TikTok 'new tools'/tutorial overlays that block the form."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.8)
    except Exception:
        pass
    for sel in (
        'button[aria-label*="close" i]',
        'button[aria-label*="dismiss" i]',
        'button[aria-label*="Skip" i]',
        '[data-e2e="close_button"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1200):
                loc.click()
                time.sleep(0.5)
        except Exception:
            pass
    try:
        page.evaluate(
            "() => { document.querySelectorAll("
            "'[class*=\\"coachmark\\"], [class*=\\"Coachmark\\"], "
            "[class*=\\"tooltip\\"]'"
            ").forEach(el => el.remove()); }"
        )
        time.sleep(0.3)
    except Exception:
        pass


'''

_VENDOR_PATCH_EDITS = [
    # Coachmark dismissal right after the file is processed.
    (
        "    _set_video(page, path=path, num_retries=num_retries, **kwargs)\n",
        "    _set_video(page, path=path, num_retries=num_retries, **kwargs)\n"
        "    _dismiss_coachmarks(page)\n",
    ),
    # Verbatim caption typing instead of the hashtag-autocomplete flow.
    (
        "    _set_description(page, description)\n",
        "    _set_plain_description(page, description)\n",
    ),
    # One more coachmark sweep right before clicking Post.
    (
        "    _post_video(page)\n",
        "    _dismiss_coachmarks(page)\n    _post_video(page)\n",
    ),
    # Insert the two helper functions just before the interactivity step.
    (
        "def _set_interactivity(\n",
        _VENDOR_PATCH_FUNCS + "def _set_interactivity(\n",
    ),
]


def _ensure_vendor_patch() -> None:
    """Idempotently apply the fixes to the vendored tiktok_uploader.

    Normalizes any prior ttclips patch (v1/v2) back to pristine, applies the
    v3 edits, then reloads. Re-applies after any reinstall; aborts loudly if
    an anchor goes missing so we never upload from a silently-corrupted
    library.
    """
    from tiktok_uploader import upload as tup_upload

    upload_path = Path(tup_upload.__file__)
    source = upload_path.read_text(encoding="utf-8")
    if VENDOR_PATCH_MARKER in source:
        return
    if re.search(r"# ttclips-patch:v\d+", source):
        source = re.sub(
            r"(?s)# ttclips-patch:v\d+\n.*?(?=def _set_interactivity\()",
            "",
            source,
            count=1,
        )
        source = source.replace(
            "    _set_video(page, path=path, num_retries=num_retries, **kwargs)\n"
            "    _dismiss_coachmarks(page)\n",
            "    _set_video(page, path=path, num_retries=num_retries, **kwargs)\n",
            1,
        )
        source = source.replace(
            "    _set_plain_description(page, description)\n",
            "    _set_description(page, description)\n",
            1,
        )
        source = source.replace(
            "    _dismiss_coachmarks(page)\n    _post_video(page)\n",
            "    _post_video(page)\n",
            1,
        )
    for old, new in _VENDOR_PATCH_EDITS:
        if old not in source:
            raise RuntimeError(
                f"Vendor patch anchor missing in {upload_path.name}: {old!r}"
            )
        source = source.replace(old, new, 1)
    upload_path.write_text(source, encoding="utf-8")
    importlib.reload(tup_upload)
    log.info("Applied tiktok_uploader patch v4 (%s)", upload_path)


def _human_delay(min_sec: float = DELAY_MIN, max_sec: float = DELAY_MAX) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff before the given attempt (1-based).

    Grows on every failure and never exceeds RETRY_WAIT_MAX; adds +/-20% jitter.
    """
    wait = RETRY_WAIT_INITIAL * (RETRY_WAIT_BACKOFF ** (attempt - 1))
    wait = min(wait, RETRY_WAIT_MAX) * random.uniform(0.8, 1.2)
    return wait


def _validate_cookies() -> Path:
    cp = Path(COOKIES_FILE)
    if not cp.exists() or cp.stat().st_size < 100:
        raise FileNotFoundError(
            f"Cookies file not found or too small: {cp}\n"
            "Export cookies from a logged-in Chrome session:\n"
            "  1. Install 'Get cookies.txt LOCALLY' browser extension\n"
            "  2. Open TikTok.com (logged in)\n"
            "  3. Export cookies to Netscape format\n"
            "  4. Save as data/cookies.txt in the project root"
        )
    return cp


def _cookies_to_storage_state(cookies_path: Path) -> dict:
    """Convert a Netscape-format cookies.txt into Playwright storageState JSON.

    Fields per line (tab-separated):
        domain  includeSubdomains  path  secure  expires  name  value
    A line may start with ``#HttpOnly_`` before the domain (httpOnly cookie).
    """
    cookies = []
    for line in cookies_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        if "tiktok.com" not in domain:
            continue
        try:
            expires_sec = int(round(float(expires)))
        except ValueError:
            expires_sec = -1
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": expires_sec if expires_sec > 0 else -1,
            "httpOnly": http_only,
            "secure": secure.lower() == "true",
            "sameSite": "Lax",
        })
    state = {"cookies": cookies, "origins": []}
    log.info("Parsed %d TikTok cookies from %s", len(cookies), cookies_path.name)
    return state


def _upload_once(video_path: str, caption: str, cookies_file: str = "") -> bool:
    """Run exactly one in-process upload. Returns True on success."""
    _ensure_vendor_patch()
    from tiktok_uploader import config
    from tiktok_uploader.upload import upload_video

    # tiktok_uploader's config.toml uses a 30s implicit_wait which the heavy
    # TikTok pages exceed, and ships a Chrome/58 user_agent from 2017 that
    # TikTok's WAF answers with a TCP reset (net::ERR_CONNECTION_RESET). Give
    # the page loads the configured budget and use a modern UA.
    config.implicit_wait = UPLOAD_TIMEOUT
    if UA:
        config.disguising.user_agent = UA
    # Validate cookies on the creator-center upload page instead of the heavy
    # infinite-feed homepage; same auth checks, far faster load.
    config.paths.main = "https://www.tiktok.com/creator-center/upload?lang=en"

    failed = upload_video(
        str(video_path),
        description=caption,
        cookies=cookies_file or str(COOKIES_FILE),
        headless=TK_CFG.get("headless", False),
    )
    return len(failed) == 0


def _run_upload_child(video_path: str, caption: str, cookies: str) -> bool:
    """Spawn a fresh python process that performs one upload."""
    payload = json.dumps({"caption": caption, "cookies": cookies})
    cmd = [sys.executable, "-m", "src.uploader", str(video_path), "--input-json"]
    extra: dict = {}
    if os.name == "nt":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    log.info("Upload subprocess: %s", " ".join(cmd))
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
        log.warning("Upload child timed out after %ss", UPLOAD_TIMEOUT + 300)
        return False
    for line in (proc.stdout or "").splitlines():
        log.info("[upload] %s", line.rstrip())
    if proc.returncode != 0:
        tail = [l.strip() for l in (proc.stderr or "").splitlines() if l.strip()][-4:]
        log.warning("Upload child exited %s: %s",
                    proc.returncode, " | ".join(tail) if tail else "no stderr")
        return False
    return True


def upload_clip(video_path: str, caption: str, *, cookies: str = "") -> bool:
    """Upload a single video clip to TikTok.

    Each attempt runs in a fresh subprocess so a Playwright/thread crash in
    one attempt can never poison the scheduler process. Returns True on success.
    """
    _validate_cookies()
    cookies_file = cookies or str(COOKIES_FILE)
    video_path = str(video_path)
    caption = " ".join(caption.strip().split())
    log.info("Uploading %s (caption %d chars)...", Path(video_path).name, len(caption))

    _human_delay()
    for attempt in range(1, RETRIES + 1):
        try:
            if _run_upload_child(video_path, caption, cookies_file):
                log.info("Upload succeeded (attempt %d/%d)", attempt, RETRIES)
                return True
        except Exception as e:
            log.warning("Upload attempt %d/%d failed: %s", attempt, RETRIES, e)
        if attempt < RETRIES:
            wait = _backoff_delay(attempt)
            log.info("Waiting %.0fs before next attempt (backoff %d/%d)...",
                     wait, attempt, RETRIES - 1)
            time.sleep(wait)
    log.error("All upload attempts failed for %s", video_path)
    return False


def run_probe(probe_tag: str = "fyp", video_arg: str = "", accept_hint: bool = False) -> int:
    """Diagnostic (no post): reach the upload form, type a hashtag, dump DOM.

    With accept_hint, also clicks the best-match suggestion (like the real
    patch) and dumps the post-accept caption DOM. Never clicks Post; safe.
    """
    _ensure_vendor_patch()
    from playwright.sync_api import sync_playwright
    from tiktok_uploader import config as tk_cfg
    from tiktok_uploader.upload import _clear, _dismiss_coachmarks, _go_to_upload, _set_video

    tk_cfg.implicit_wait = UPLOAD_TIMEOUT
    if UA:
        tk_cfg.disguising.user_agent = UA
    tk_cfg.paths.main = "https://www.tiktok.com/creator-center/upload?lang=en"

    if video_arg:
        video = Path(video_arg)
    else:
        clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not clips:
            log.error("No mp4 in %s — pass --probe-video ...", CLIPS_DIR)
            return 1
        video = clips[0]
    if not video.exists():
        log.error("Probe video not found: %s", video)
        return 1
    _validate_cookies()

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=TK_CFG.get("headless", False))
        context = browser.new_context(
            storage_state=_cookies_to_storage_state(Path(COOKIES_FILE)),
            user_agent=UA,
        )
        page = context.new_page()
        try:
            log.info("Probe :: navigating to upload page")
            _go_to_upload(page)
            log.info("Probe :: staging video %s", video.name)
            _set_video(page, path=str(video), num_retries=2)
            _dismiss_coachmarks(page)
            desc = page.locator(f"xpath={tk_cfg.selectors.upload.description}")
            desc.wait_for(state="visible", timeout=tk_cfg.implicit_wait * 1000)
            desc.click()
            _clear(desc)
            time.sleep(1)
            tag = "#" + probe_tag.strip().lstrip("#")
            log.info("Probe :: typing %s into caption box", tag)
            desc.press_sequentially(tag, delay=80)
            time.sleep(2.5)
            page.screenshot(path=str(PROBE_DIR / "screen.png"), full_page=True)
            (PROBE_DIR / "summary.json").write_text(
                json.dumps({
                    "url": page.url,
                    "title": page.title(),
                    "probe_tag": tag,
                    "video": video.name,
                }, indent=2), encoding="utf-8")
            (PROBE_DIR / "candidates.json").write_text(
                json.dumps(page.evaluate(_PROBE_JS), indent=2, ensure_ascii=False),
                encoding="utf-8")
            caption_html = page.evaluate(
                "() => { const el = document.querySelector('[contenteditable=\"true\"]'); "
                "return el ? el.innerHTML : null; }"
            )
            (PROBE_DIR / "caption_innerhtml.html").write_text(
                caption_html or "", encoding="utf-8")
            if accept_hint:
                try:
                    box = page.locator("xpath=//div[contains(@class, 'mention-list-popover')]")
                    box.wait_for(state="visible", timeout=int(tk_cfg.add_hashtag_wait * 1000))
                    time.sleep(0.8)
                    option = box.locator("div[role='option']").first
                    topic_txt = option.locator("span.hash-tag-topic").first.inner_text().strip()
                    log.info("Probe :: first suggestion topic: %s", topic_txt)
                    if topic_txt.lstrip("#").strip().lower() == tag[1:].lower():
                        option.click()
                        time.sleep(0.8)
                        (PROBE_DIR / "caption_after_accept.html").write_text(
                            page.evaluate(
                                "() => { const el = document.querySelector('[contenteditable=\"true\"]'); "
                                "return el ? el.innerHTML : null; }"
                            ) or "", encoding="utf-8")
                        page.screenshot(path=str(PROBE_DIR / "screen_after_accept.png"), full_page=True)
                        log.info("Probe :: accepted chip; wrote caption_after_accept.html")
                    else:
                        log.info("Probe :: first option mismatch; no accept")
                except Exception as exc:
                    log.warning("Probe :: accept step skipped: %s", exc)
            log.info("Probe :: dumps written to %s", PROBE_DIR)
        finally:
            context.close()
            browser.close()
    return 0


def main() -> None:
    """Child-process entrypoint: upload one clip, exit 0 on success."""
    import argparse

    parser = argparse.ArgumentParser(description="Upload one clip to TikTok")
    parser.add_argument("video", nargs="?", default="", help="Path to the mp4 to upload")
    parser.add_argument("--input-json", action="store_true",
                        help="Read {caption, cookies} JSON from stdin")
    parser.add_argument("--probe", action="store_true",
                        help="Diagnostic only: reach the upload form, type a hashtag, dump DOM (never posts)")
    parser.add_argument("--probe-tag", type=str, default="fyp",
                        help="Hashtag to type during --probe (default: fyp)")
    parser.add_argument("--probe-video", type=str, default="",
                        help="mp4 to stage during --probe (default: latest clip in data/clips)")
    parser.add_argument("--probe-accept", action="store_true",
                        help="With --probe, also click the exact-match suggestion and dump the caption DOM after accept")
    args = parser.parse_args()

    if args.probe:
        sys.exit(run_probe(args.probe_tag, args.probe_video, args.probe_accept))

    if args.input_json:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as e:
            log.error("Bad JSON on stdin: %s", e)
            sys.exit(2)
    else:
        payload = {}
    caption = payload.get("caption", "")
    cookies = payload.get("cookies", "") or str(COOKIES_FILE)
    print(f"Child received caption ({len(caption)} chars)", flush=True)

    path = Path(args.video)
    if not path.exists():
        log.error("Video not found: %s", path)
        sys.exit(2)
    _validate_cookies()
    ok = _upload_once(str(path), caption, cookies)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()


def validate_session() -> bool:
    """Check if TikTok session cookies are usable. Returns True if page loads correctly."""
    _validate_cookies()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed — skipping session check")
        return True
    log.info("Validating TikTok session via Playwright ...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=_cookies_to_storage_state(COOKIES_FILE),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.tiktok.com/creator-center/upload", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        url = page.url
        logged_in = "login" not in url and "tiktok.com" in url
        context.close()
        browser.close()
        log.info("Session valid: %s (final url: %s)", logged_in, url)
        return logged_in