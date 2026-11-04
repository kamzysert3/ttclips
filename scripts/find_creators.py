#!/usr/bin/env python3
"""Discover YouTube creators for outreach.

Pulls candidate channels for a search term — subscriber count, upload
activity, and whether they already link TikTok (plus other socials) —
so you can shortlist creators to contact instead of hunting by hand.

Usage:
    python scripts/find_creators.py "faceless finance" --min-subs 10000
    python scripts/find_creators.py "ai news" --skip-tiktok --csv creators.csv

Requires yt-dlp (already a ttclips dependency) and network access to YouTube.

Limitations:
  * TikTok detection scans the channel homepage HTML + channel description.
    A link that lives only behind the About/"links" tab may be missed.
  * A subscriber count of "?" means the channel page did not expose it.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    sys.exit("yt-dlp is required: pip install yt-dlp (it ships with ttclips)")

try:
    from yt_dlp.networking import Request
except Exception:  # older yt-dlp
    from yt_dlp.utils import sanitized_Request as Request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

SOCIAL_PATTERNS = {
    "tiktok": re.compile(r"tiktok\.com", re.I),
    "instagram": re.compile(r"instagram\.com", re.I),
    "x": re.compile(r"(?:twitter\.com|x\.com)/[A-Za-z0-9_]", re.I),
    "linktree": re.compile(r"linktr\.ee", re.I),
}

TAB_SUFFIX = re.compile(r"\s+-\s+(Videos|Home|Shorts|Playlists|Posts)$")


def _human(n) -> str:
    if n is None:
        return "?"
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}".replace(".0", "")
    return str(n)


def search_channels(query: str, count: int) -> list[dict]:
    """Unique channels from a YouTube search, in result order."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "ignoreerrors": True}
    with YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{count}:{query}", download=False) or {}
    out, seen = [], set()
    for e in res.get("entries") or []:
        if not e:
            continue
        cid = e.get("channel_id") or ""
        curl = e.get("channel_url") or e.get("uploader_url") or ""
        if not curl and cid:
            curl = f"https://www.youtube.com/channel/{cid}"
        if not curl:
            continue
        curl = curl.rstrip("/")
        if curl in seen:
            continue
        seen.add(curl)
        name = (e.get("channel") or e.get("uploader") or "(unknown)").strip()
        out.append({"name": name, "url": curl, "id": cid})
    return out


def fetch_channel(url: str, want_activity: bool) -> dict:
    """Channel metadata in a single call: subs, description (+ newest upload).

    ``want_activity`` runs the newest entry fully to get its ``upload_date``;
    without it the entries stay flat and the call is much cheaper.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "extract_flat": False if want_activity else "in_playlist",
            "playlistend": 1}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{url}/videos", download=False) or {}

    name = info.get("channel") or info.get("uploader") or info.get("title") or url
    name = TAB_SUFFIX.sub("", name).strip()

    last = None
    for e in info.get("entries") or []:
        if e and e.get("upload_date"):
            d = e["upload_date"]  # YYYYMMDD
            last = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            break

    return {"name": name,
            "url": info.get("channel_url") or info.get("uploader_url") or url,
            "subs": info.get("channel_follower_count"),
            "description": info.get("description") or "",
            "last_upload": last}


def fetch_home_html(url: str) -> str:
    """Raw channel homepage HTML (catches header link chips like TikTok)."""
    resp = None
    raw = b""
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            req = Request(url + "/", headers={"User-Agent": UA,
                                              "Cookie": "SOCS=CAI",
                                              "Accept-Language": "en-US"})
            resp = ydl.urlopen(req)
            raw = resp.read()
    except Exception:
        return ""
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    return raw.decode("utf-8", "replace")


def detect_socials(*texts: str) -> set[str]:
    blob = " ".join(t for t in texts if t)
    return {k for k, pat in SOCIAL_PATTERNS.items() if pat.search(blob)}


def analyze(cand: dict, args: argparse.Namespace) -> tuple[dict | None, str]:
    """Fetch + filter one channel. Returns (row, skip_reason)."""
    try:
        info = fetch_channel(cand["url"], want_activity=not args.no_activity)
    except Exception as e:
        return None, f"ERROR {e}"

    if not args.no_activity and not info["last_upload"]:
        return None, "no uploads, skipping"

    subs = info["subs"]
    if args.min_subs and (subs or 0) < args.min_subs:
        return None, f"subs {_human(subs)} < min, skipping"
    if args.max_subs and subs and subs > args.max_subs:
        return None, f"subs {_human(subs)} > max, skipping"

    socials = detect_socials(info["description"], fetch_home_html(cand["url"]))
    if args.skip_tiktok and "tiktok" in socials:
        return None, "already has TikTok, skipping"

    info["socials"] = ",".join(sorted(socials)) or "-"
    info["has_tiktok"] = "tiktok" in socials
    return info, (f"subs={_human(subs)} last={info['last_upload'] or 'n/a'} "
                  f"socials={info['socials']}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="find_creators",
        description="Find YouTube creators to outreach (subs / activity / TikTok check)")
    ap.add_argument("query", help='YouTube search term, e.g. "faceless finance"')
    ap.add_argument("--search", type=int, default=50,
                    help="videos to pull from YouTube search (default 50)")
    ap.add_argument("--channels", type=int, default=15,
                    help="max channels to analyze in detail (default 15)")
    ap.add_argument("--min-subs", type=int, default=0, help="minimum subscribers")
    ap.add_argument("--max-subs", type=int, default=0, help="maximum subscribers (0 = no cap)")
    ap.add_argument("--skip-tiktok", action="store_true",
                    help="drop channels that already link TikTok")
    ap.add_argument("--no-activity", action="store_true",
                    help="skip the last-upload check (faster)")
    ap.add_argument("--jobs", type=int, default=3,
                    help="parallel requests to YouTube (default 3)")
    ap.add_argument("--csv", default="", help="also write results to this CSV path")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cands = search_channels(args.query, args.search)
    if not cands:
        sys.exit(f"No channels found for query: {args.query!r}")
    todo = cands[: args.channels]
    print(f"Found {len(cands)} unique channels; analyzing first {len(todo)} ...",
          file=sys.stderr)

    rows: list[dict] = []
    stats = {"no_videos": 0, "subs": 0, "tiktok": 0, "errors": 0}
    reason_kind = {"ERROR": "errors", "no uploads": "no_videos",
                   "subs": "subs", "already has TikTok": "tiktok"}

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(analyze, c, args): (i, c) for i, c in enumerate(todo, 1)}
        for fut in as_completed(futures):
            i, c = futures[fut]
            row, reason = fut.result()
            label = f"[{i}/{len(todo)}] {c['name']}"
            print(f"{label}: {reason}", file=sys.stderr)
            if row is None:
                for prefix, stat_key in reason_kind.items():
                    if reason.startswith(prefix):
                        stats[stat_key] += 1
                        break
            else:
                rows.append(row)

    rows.sort(key=lambda r: (r["subs"] or -1), reverse=True)

    if rows:
        name_w = min(max(len(r["name"]) for r in rows), 34)
        print(f"\n{'SUBS':<8} {'LAST':<10} {'TIKTOK':<7} "
              f"{'CHANNEL':<{name_w}}  URL")
        print(f"{'-' * 8} {'-' * 10} {'-' * 7} {'-' * name_w}  {'-' * 30}")
        for r in rows:
            name = r["name"] if len(r["name"]) <= name_w else r["name"][: name_w - 3] + "..."
            print(f"{_human(r['subs']):<8} {r['last_upload'] or 'n/a':<10} "
                  f"{'yes' if r['has_tiktok'] else 'no':<7} "
                  f"{name:<{name_w}}  {r['url']}")

    skipped = len(todo) - len(rows)
    print(f"\n{len(rows)} candidates from {len(todo)} analyzed "
          f"({skipped} skipped: {stats['subs']} subs out of range, "
          f"{stats['no_videos']} no uploads, {stats['tiktok']} has TikTok, "
          f"{stats['errors']} errors)")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "name", "url", "subs", "last_upload",
                "has_tiktok", "socials", "description"])
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
        print(f"CSV written: {args.csv}")


if __name__ == "__main__":
    main()
