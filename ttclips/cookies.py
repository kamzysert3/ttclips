"""Cookie parsing: Netscape ``cookies.txt`` -> Playwright ``storageState``.

Shared by the TikTok and YouTube uploaders. Each platform filters the
exported cookies by domain, so one Chrome export can serve both (or two
separate exports can be used — see ``config/youtube.json``).

Format (tab-separated), one cookie per line::

    domain  includeSubdomains  path  secure  expires  name  value

A line may start with ``#HttpOnly_`` before the domain (httpOnly cookie).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

TIKTOK_DOMAINS = ("tiktok.com",)
YOUTUBE_DOMAINS = ("google.com", "youtube.com", "googleusercontent.com")


def cookies_to_storage_state(
    cookies_path: Path,
    domains: tuple[str, ...] = TIKTOK_DOMAINS,
) -> dict:
    """Convert a Netscape-format cookies.txt into Playwright storageState JSON.

    Only cookies whose domain contains one of *domains* are kept. Returns
    ``{"cookies": [...], "origins": []}``.
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
        if not any(d in domain for d in domains):
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
    log.info(
        "Parsed %d cookies (%s) from %s",
        len(cookies), "+".join(domains), cookies_path.name,
    )
    return state
