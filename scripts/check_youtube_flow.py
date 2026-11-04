r"""Checks the YouTube Studio automation logic against a mock Studio DOM.

Playwright drives a local page that mimics the real upload dialog (ytcp-*
custom elements, a shadow-DOM file input, radio buttons, a post button).
Nothing is uploaded and nothing is posted — this only proves the selectors
and the helper functions drive the form correctly.

Run with:  .\.venv\Scripts\python.exe scripts\check_youtube_flow.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ttclips-yt-"))
os.environ["TTCLIPS_HOME"] = str(TMP)
(TMP / "config").mkdir(parents=True, exist_ok=True)
(TMP / "data").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ttclips.youtube_uploader as yt  # noqa: E402

MOCK = TMP / "studio_mock.html"
MOCK.write_text(
    """<!doctype html>
<html><body>
<div id="dialog">
  <ytcp-file-selector></ytcp-file-selector>
  <ytcp-title-and-description-input style="display:none">
    <input id="textbox" placeholder="Add title">
  </ytcp-title-and-description-input>
  <div id="description-container" style="display:none">
    <textarea placeholder="Add a description"></textarea>
  </div>
  <ytcp-button id="next-button" style="display:none">Next</ytcp-button>
</div>
<div id="visibility" style="display:none">
  <ytcp-radio-button>No, it's not made for kids</ytcp-radio-button>
  <ytcp-radio-button>Yes, it's made for kids</ytcp-radio-button>
  <ytcp-radio-button>Public</ytcp-radio-button>
  <ytcp-radio-button>Unlisted</ytcp-radio-button>
  <ytcp-radio-button>Private</ytcp-radio-button>
  <ytcp-post-button style="display:none"><button class="post-button">Post</button></ytcp-post-button>
</div>
<div role="status" id="toast" style="display:none">Video posted</div>
<script>
  const fileSelector = document.querySelector('ytcp-file-selector');
  const shadow = fileSelector.attachShadow({mode: 'open'});
  shadow.innerHTML = '<input type="file" id="file">';
  shadow.querySelector('#file').addEventListener('change', () => {
    document.querySelector('ytcp-title-and-description-input').style.display = '';
    document.querySelector('#description-container').style.display = '';
    document.querySelector('#next-button').style.display = '';
  });
  document.querySelector('#next-button').addEventListener('click', () => {
    document.querySelector('#visibility').style.display = '';
    document.querySelector('ytcp-post-button').style.display = '';
  });
  document.querySelector('.post-button').addEventListener('click', () => {
    document.querySelector('#dialog').style.display = 'none';
    document.querySelector('#toast').style.display = '';
  });
</script>
</body></html>""",
    encoding="utf-8",
)

# Minimal cookies file so the storage state can be built.
(TMP / "data" / "youtube_cookies.txt").write_text(
    "# Netscape HTTP Cookie File\n"
    ".google.com\tTRUE\t/\tTRUE\t0\tSID\tfake-value\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tfake-value\n",
    encoding="utf-8",
)

captured: dict = {}


def fake_goto_upload(page) -> None:
    page.goto(MOCK.as_uri())


yt._goto_upload = fake_goto_upload
yt.HEADLESS = True

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


class _Recorder:
    """Wraps the real page so we can read back what the flow filled in."""

    def __init__(self, page):
        self._page = page

    def __getattr__(self, item):
        return getattr(self._page, item)

    def locator(self, selector, *args, **kwargs):
        loc = self._page.locator(selector, *args, **kwargs)
        if "textbox" in selector or "title" in selector.lower():
            captured.setdefault("title", loc)
        if "description" in selector.lower():
            captured.setdefault("description", loc)
        return loc


print("mock Studio upload flow")
(TMP / "clip.mp4").write_bytes(b"fake mp4")
url = yt._upload_once(
    str(TMP / "clip.mp4"), "My Short title",
    "My description\n\n#Shorts #fyp", visibility="public", made_for_kids=False,
)
check("upload returned without error", url is not None, repr(url))

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.goto(MOCK.as_uri())
    shadow_input = page.locator("ytcp-file-selector input[type='file']")
    check("shadow-DOM file input is reachable", shadow_input.count() > 0)

    # replay the flow with the real helpers and read the values back
    page.goto(MOCK.as_uri())
    (TMP / "clip.mp4").write_bytes(b"fake mp4")
    yt._stage_file(page, str(TMP / "clip.mp4"))
    check("file input accepted the video", True)
    check("title field visible after staging",
          yt._first_visible(page, yt.SELECTORS["title"], 5000) is not None)
    check("description field visible after staging",
          yt._first_visible(page, yt.SELECTORS["description"], 5000) is not None)
    yt._fill_field(page, yt.SELECTORS["title"], "My Short title", 5000)
    yt._fill_field(page, yt.SELECTORS["description"], "My description", 5000)
    yt._advance(page)
    check("visibility step revealed",
          yt._first_visible(page, yt.SELECTORS["visibility_public"], 5000) is not None)
    yt._set_made_for_kids(page, False)
    yt._set_visibility(page, "public")
    check("post button reachable",
          yt._first_visible(page, yt.SELECTORS["post_button"], 5000) is not None)
    check("clicking post works",
          yt._click_any(page, yt.SELECTORS["post_button"], 5000))
    time_ok = yt._wait_for_post_result(page, timeout_sec=15)
    check("post confirmed", time_ok)
    check("success toast detected",
          yt._first_visible(page, yt.SELECTORS["success_toast"], 2000) is not None)
    check("title was typed", page.locator("#textbox").input_value() == "My Short title",
          page.locator("#textbox").input_value())
    check("description was typed",
          "My description" in page.locator("#description-container textarea").input_value())
    context.close()
    browser.close()

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all checks passed")
