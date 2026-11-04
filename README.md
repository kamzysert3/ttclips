# ttclips

[![PyPI - Version](https://img.shields.io/pypi/v/ttclips.svg?logo=pypi&logoColor=white)](https://pypi.org/project/ttclips/)
[![PyPI - Python Versions](https://img.shields.io/pypi/pyversions/ttclips.svg?logo=python&logoColor=white)](https://pypi.org/project/ttclips/)
[![CI](https://img.shields.io/github/actions/workflow/status/kamzysert3/ttclips/ci.yml.svg?branch=main&logo=github)](https://github.com/kamzysert3/ttclips/actions?query=workflow%3ACI)
[![License](https://img.shields.io/github/license/kamzysert3/ttclips.svg)](https://github.com/kamzysert3/ttclips/blob/main/LICENSE)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-F0643F.svg?logo=github)](https://github.com/sponsors/kamzysert3)

Turn **your own YouTube channel** into TikTok content automatically. ttclips
monitors your channel(s), detects the highlight moments, cuts them into vertical
9:16 clips with dead-center, word-synced karaoke captions, and posts them to
TikTok **and/or YouTube Shorts** on a schedule — no TikTok API approval, no
Google Cloud project, no manual editing, fully local.

Point it at channels you own or have rights to: this is a repurposing tool for
creators, not a content scraper (see
[Legal & operational notes](#legal--operational-notes)).

- **Fully local and free** — no clip-hosting service, no paid APIs required.
- **AI-ranked highlights** — whisper transcription + Gemini highlight ranking.
- **Word-synced captions** — the active word is bold/highlighted in real time.
- **Cookie-based auth** — no TikTok developer approval / API key needed.
- **Two independent targets** — TikTok and YouTube Shorts, each with its own
  schedule tracking and dedup state; post to one, the other, or both.
- **Self-healing** — model fallback chains, cached downloads, graceful b-roll fallback.

---

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [How it works](#how-it-works)
- [Command-line reference](#command-line-reference)
- [Configuration](#configuration)
  - [Target channels (`config/channels.json`)](#target-channels)
  - [Posting window (`config/posting_window.json`)](#posting-window)
  - [Platforms (`config/platforms.json`)](#platforms)
  - [TikTok upload settings (`config/tiktok.json`)](#tiktok-upload-settings)
  - [YouTube Shorts settings (`config/youtube.json`)](#youtube-upload-settings)
  - [Environment variables (`.env`)](#environment-variables)
- [State and data](#state-and-data)
- [Automation (Windows Task Scheduler)](#automation)
- [Frequently asked questions](#faq)
- [Development](#development)
- [Releasing](#releasing)
- [Legal & operational notes](#legal--operational-notes)
- [Support the project](#support-the-project)
- [License](#license)

---

## Features

- **YouTube monitoring** — polls channel RSS feeds and queues new videos that
  match your keyword filters.
- **Highlight detection** — `faster-whisper` transcribes the audio; Gemini
  ranks the strongest 20–70 second moments with hooks, captions and hashtags.
- **Vertical clip production** — FFmpeg crops to 1080×1920 (9:16) and burns in
  big, centered, word-synced captions that highlight each spoken word as it
  happens.
- **Original content mode** — Gemini writes a script, `edge-tts` narrates it,
  stock b-roll (Pexels) or an animated gradient backs the clip.
- **Scheduled posting** — up to 8 posts/day at configurable times, with
  catch-up for missed slots.
- **Resilient uploads** — Playwright-driven TikTok *and* YouTube Studio uploads
  with human-like delays, exponential backoff retries, and an isolated
  subprocess so a crashed upload can never take the scheduler down.
- **Reusable state** — never re-downloads a video or reposts the same moment.

## Requirements

- **Python 3.10+**
- **ffmpeg + ffprobe** (required to merge streams and render clips):
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Alternative: set ``FFMPEG_BIN`` / ``FFPROBE_BIN`` in `.env` to point at
    your own binaries.
- **Playwright Chromium** — installed once, see [Installation](#installation).
- **A Gemini API key** — free at <https://aistudio.google.com/apikey>.
- **TikTok cookies** (only needed for the TikTok upload step) — see
  [Quickstart](#quickstart).
- **Google cookies** (only needed for the YouTube Shorts upload step) — see
  [Quickstart](#quickstart).

## Installation

```bash
pip install ttclips
```

Then install the Playwright browser (one-time):

```bash
playwright install chromium
```

> On Windows, `pip install` puts the `ttclips` command in
> `%LOCALAPPDATA%\Programs\Python\Python312\Scripts` (add it to `PATH` if the
> command isn't found).

### Where config and data live

`ttclips` resolves its working directory as follows:

1. `$TTCLIPS_HOME` if set, otherwise
2. the current working directory.

All `config/`, `data/` and `.env` paths are relative to that directory, so the
same installation can run multiple independent projects.

## Quickstart

### 1. Initialise a project folder

```bash
mkdir my-clips && cd my-clips
ttclips --init --yes
```

This creates `config/` (default channels, posting window, TikTok + YouTube
settings, platform defaults) and `.env.example`. The `--yes` flag acknowledges
that you own (or have rights to) the channels you will repost — see
[`docs/LEGAL.md`](https://github.com/kamzysert3/ttclips/blob/main/docs/LEGAL.md).

### 2. Configure secrets

```bash
cp .env.example .env    # Windows: copy .env.example .env
```

Edit `.env` and set at minimum:

```ini
GEMINI_API_KEY=your_gemini_api_key_here
```

Optionally set `PEXELS_API_KEY` (free at <https://www.pexels.com/api/>) to
power stock-video backgrounds for generated clips. Without it, generated clips
use an animated gradient background.

### 3. Provide TikTok cookies (for posting)

TikTok uploads use browser session cookies instead of an API key:

1. Log in to tiktok.com in Chrome.
2. Export cookies in Netscape format (e.g. the *"Get cookies.txt LOCALLY"*
   browser extension).
3. Save as `data/cookies.txt` in the project folder.

Re-export roughly monthly, as sessions drift.

### 4. Provide Google cookies (only for YouTube Shorts)

YouTube Shorts are posted through **YouTube Studio** with the same
cookie-auth approach:

1. Log in to <https://studio.youtube.com> in Chrome.
2. Export cookies in Netscape format (the *"Get cookies.txt LOCALLY"* extension
   works for Google as well).
3. Save as `data/youtube_cookies.txt` in the project folder.

No Google Cloud project, no OAuth app and no API audit are needed — Studio
uploads are ordinary uploads. See
[Why not the YouTube Data API?](#why-not-the-youtube-data-api).

### 5. Verify and run

```bash
ttclips --validate-session        # TikTok cookies OK?
ttclips --validate-youtube        # YouTube cookies OK?
ttclips --monitor                 # build the watch queue from YouTube feeds
ttclips --once --dry-run          # full pipeline, no upload
ttclips --once --slot 12:00       # post one slot now
ttclips --once --slot 12:00 --no-upload    # render only, don't post
ttclips --once --target youtube   # post the slot to YouTube Shorts instead
```

Pick the default target in `config/platforms.json` (`tiktok`, `youtube` or
`both`) or override it per run with `--target`.

## How it works

```
YouTube RSS feeds ─────────────────▶ monitor (queue of new videos)
                                     │
                          yt-dlp + ffmpeg (download, merge, cache)
                                     ▼
                          faster-whisper (transcription + word timestamps)
                                     ▼
                          Gemini (probe → fallback chain) highlight ranking
                                     ▼
                          FFmpeg (crop 9:16 + centered karaoke captions)
                                     ▼
   two pipelines:    repost clips            OR        generated original clips
              Gemini highlights        Gemini script → edge-tts narration →
              cut_and_crop_clip        stock b-roll + captions
                                        (gradient fallback)
                                     ▼
                          one clip, fanned out to the target platforms
                                     ▼
            Playwright → TikTok upload        Playwright → YouTube Studio upload
              (data/cookies.txt)                 (data/youtube_cookies.txt)
```

Default posting schedule (`config/posting_window.json`):

| Time  | Type      |
|-------|-----------|
| 08:00 | repost    |
| 10:00 | repost    |
| 12:00 | generated |
| 14:00 | repost    |
| 16:00 | generated |
| 18:00 | repost    |
| 20:00 | generated |
| 22:00 | repost    |

## Command-line reference

| Command | Description |
|---------|-------------|
| `ttclips` | Post the current/upcoming slot (if not already posted today) |
| `ttclips --init --yes` | Scaffold `config/` + `.env.example` into the project folder; `--yes` acknowledges you own the content you will repost |
| `ttclips --monitor` | Refresh the YouTube watch queue only |
| `ttclips --validate-session` | Check whether the TikTok cookie session is alive |
| `ttclips --validate-youtube` | Check whether the YouTube (Studio) cookie session is alive |
| `ttclips --once` | Post the current/upcoming slot, then exit |
| `ttclips --once --slot 12:00` | Post a specific slot |
| `ttclips --once --type generated` | Force a content type (`repost`/`generated`) |
| `ttclips --once --target youtube` | Post to YouTube Shorts (`tiktok`, `youtube` or `both`) |
| `ttclips --target both --catchup` | Fill every missed slot on both platforms |
| `ttclips --catchup` | Post all missed slots for today |
| `ttclips --dry-run` | Full pipeline, skip upload |
| `ttclips --no-upload` | Render the real 9:16 MP4 but don't post |
| `ttclips --post-file <path>` | Upload an already-rendered clip, skipping the pipeline |
| `ttclips --post-file <path> --caption "..."` | ...with a custom caption |
| `ttclips --version` | Print the installed version |
| `ttclips-gemini --probe` | Ping candidate Gemini models and report status |
| `ttclips-init --home <dir> --yes [--force]` | Scaffold config into a specific directory |
| `ttclips-youtube --probe` | Dump the YouTube Studio DOM (diagnostic, never posts) |
| `ttclips-youtube --validate-session` | Check the YouTube session from the terminal |

Posting flags compose: `--dry-run` skips the upload, `--no-upload` renders the
clip but doesn't post, and `--post-file` uploads an existing clip directly
(recording it in `data/posted.json` so it is never reposted).

## Configuration

### Target channels (`config/channels.json`)

Each channel entry:

| Field | Description |
|-------|-------------|
| `name` | Human-readable label (logging) |
| `channel_id` | YouTube channel ID |
| `enabled` | `true`/`false` to toggle a channel |
| `keywords` | Only accept uploads whose title matches at least one keyword; empty = accept all |
| `max_moments` | Max clips to post per video (1–5) |

Top-level options:

| Key | Description |
|-----|-------------|
| `excluded_keywords` | Titles containing these are skipped (`live`, `stream`, ...) |
| `min_duration_sec` / `max_duration_sec` | Accepted video length window |

### Posting window (`config/posting_window.json`)

A list of `{"time": "HH:MM", "type": "repost"|"generated", "hint": ""}` slots.
`hint` feeds the script prompt for generated-content slots. An optional
`"targets"` key overrides the platform(s) for that single slot
(`"targets": "youtube"`), e.g.:

```json
{ "time": "12:00", "type": "generated", "hint": "", "targets": "both" }
```

### Platforms (`config/platforms.json`)

| Key | Description |
|-----|-------------|
| `default_target` | `tiktok` (default), `youtube` or `both` |

The two platforms are **independent**: each keeps its own per-day slot
tracking and its own dedup records in `data/posted.json`, so a source video
posted to TikTok is still eligible for a YouTube Short. The CLI flag
`--target tiktok|youtube|both` overrides the config for a single run.

`default_target` is also the **queue policy**: a source video stays in
`data/queue.json` until every configured platform has it.

| `default_target` | Queue behaviour |
|------------------|-----------------|
| `tiktok` | the source leaves the queue after its TikTok posts (legacy behaviour) |
| `both` | the source stays queued until TikTok *and* YouTube have it |

To run the two platforms from **separate** scheduled tasks, set
`default_target` to `both` and give each task its own `--target`:

```powershell
# Task Scheduler -> ttclips-poster
ttclips --once --target tiktok
# Task Scheduler -> ttclips-poster-youtube
ttclips --once --target youtube
```

Both tasks then work off the same queue without evicting each other's work —
a one-off `--target` run never removes a source another platform still owes.

### TikTok upload settings (`config/tiktok.json`)

| Key | Description |
|-----|-------------|
| `headless` | Keep `false` — TikTok's bot detection is stricter headless |
| `min/max_action_delay_sec` | Human-like pause range between upload actions |
| `retries` | Upload attempts per clip |
| `retry_wait_initial_sec` / `retry_wait_backoff` / `retry_wait_max_sec` | Exponential backoff between attempts |
| `upload_timeout_sec` | Page-load budget |
| `user_agent` | Keep a recent desktop Chrome UA (stale UAs get reset by TikTok's WAF) |
| `default_hashtags` | Hashtags appended to every caption |

### YouTube Shorts settings (`config/youtube.json`)

| Key | Description |
|-----|-------------|
| `headless` | Keep `false` — Studio's upload dialog is flaky headless |
| `channel_id` | Optional `UC…` channel id; skips the "CREATE → Upload videos" clicks |
| `cookies_file` | Path to the exported Google cookies (`data/youtube_cookies.txt`) |
| `visibility` | `public` (default), `unlisted` or `private` |
| `made_for_kids` | `false` by default |
| `default_hashtags` | Hashtags added to every Short (`#Shorts` is appended automatically) |
| `include_source_link` | Adds the original YouTube URL to the description of reposts |
| `max_duration_sec` | `180` — the Shorts length limit |
| `min/max_action_delay_sec`, `retries`, `retry_wait_*`, `upload_timeout_sec`, `user_agent` | Same meaning as in `config/tiktok.json` |
| `selectors` | Override any Studio CSS selector when YouTube changes its markup |

`selectors` accepts a single CSS selector or a list of alternatives, e.g.:

```json
{ "selectors": { "post_button": ["ytcp-post-button", "button.post-button"] } }
```

When Studio changes its layout, run `ttclips-youtube --probe` — it writes
`data/cache/probe/{dom.html,elements.json,screen.png}` so the new selectors can
be picked without reading the code.

### Environment variables (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_API_KEY` | — | **Required.** Google AI Studio key |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Primary ranking/script model |
| `GEMINI_FALLBACK_MODELS` | `gemini-3.5-flash-lite,gemini-3.1-flash-lite` | Ordered fallbacks tried on 503/429 |
| `GEMINI_MAX_RETRIES` | `6` | Retries per model before switching |
| `GEMINI_RETRY_DELAY` | `3` | Initial backoff seconds |
| `GEMINI_RETRY_MAX_DELAY` | `45` | Backoff cap |
| `GEMINI_RETRY_TIMEOUT` | `90` | Hard cap on total retry time per model |
| `WHISPER_MODEL` | `small` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `WHISPER_DEVICE` | `auto` | Compute device |
| `TTS_VOICE` | `en-US-AvaNeural` | edge-tts narration voice |
| `PEXELS_API_KEY` | — | Optional stock b-roll background source |
| `FFMPEG_BIN` / `FFPROBE_BIN` | — | Optional explicit binary paths |
| `COOKIES_FILE` | `data/cookies.txt` | TikTok session cookies (Netscape format) |
| `YOUTUBE_COOKIES_FILE` | `data/youtube_cookies.txt` | Google session cookies (Netscape format) |
| `CAPTION_FONT_SIZE` | `96` | Caption size (pt) |
| `CAPTION_ALIGNMENT` | `5` | ASS alignment (`5` = middle-center) |
| `CAPTION_MARGINV` | `0` | Vertical margin |
| `CAPTION_OUTLINE` / `CAPTION_SHADOW` | `3` / `2` | Outline/shadow width |
| `CAPTION_WORDS_PER_LINE` | `6` | Max words per caption line |
| `HIGHLIGHT_COLOUR` | `0000FFFF` | Active-word colour (ASS BGR, default yellow) |
| `IDLE_COLOUR` | `00808080` | Rest-of-line colour (ASS BGR, light grey) |

## State and data

| Path | Content |
|------|---------|
| `data/queue.json` | Discovered videos awaiting processing |
| `data/posted.json` | Post history / dedup, per platform (never reposts the same moment twice) |
| `data/cookies.txt` | TikTok session cookies (git-ignored) |
| `data/youtube_cookies.txt` | Google session cookies (git-ignored) |
| `data/downloads/` | Merged MP4s (reused on retry — no re-download) |
| `data/clips/` | Rendered vertical clips |
| `data/cache/` | Cached transcripts + highlight rankings (so a second platform run is cheap) |
| `data/broll/` | Cached stock backgrounds for generated clips |
| `data/logs/ttclips.log` | Full log |

## Automation

For unattended daily operation on Windows, register two tasks in **Task
Scheduler** (set the task *Start in* folder to the project directory, or define
`TTCLIPS_HOME`):

| Task | Trigger | Action |
|------|---------|--------|
| `ttclips-monitor` | Daily 06:00 | `ttclips --monitor` |
| `ttclips-poster` | Hourly 07:00→23:00 | `ttclips --once` |
| `ttclips-poster-youtube` | Hourly 07:00→23:00 | `ttclips --once --target youtube` |

The scheduler posts each slot at most once per day **per platform**. After the
machine was off, run `ttclips --catchup` to fill the missed slots for today
(add `--target youtube` for the YouTube-only variant).

To let the two platform tasks share one queue, set `"default_target": "both"`
in `config/platforms.json` — see
[Platforms](#platforms).

## FAQ

<details>
<summary><b>Do I need a TikTok API key?</b></summary>

No. Uploads use Playwright browser automation with your exported session
cookies — no developer approval or API key is required.
</details>

<details>
<summary><b>Why not the YouTube Data API?</b></summary>

Since 28 July 2020, videos uploaded through the YouTube Data API by a project
that has **not** completed YouTube's compliance audit are locked as *private*
and cannot be published — not even by hand in Studio. Passing that audit takes
weeks and usually requires a Google Cloud project.

ttclips therefore drives **YouTube Studio** with Playwright, using the same
cookie-based approach as TikTok. A Studio upload is an ordinary upload: it is
public immediately and needs no Google Cloud project, no OAuth app and no API
review.
</details>

<details>
<summary><b>Do I need a Google Cloud project or API key for Shorts?</b></summary>

No. Only exported browser cookies for a logged-in `studio.youtube.com` session
(`data/youtube_cookies.txt`). There is no OAuth client, no quota and no audit.
</details>

<details>
<summary><b>How do TikTok and YouTube Shorts interact?</b></summary>

They are independent targets. Each platform tracks its own slots per day and
its own dedup records in `data/posted.json`, so a source video posted to
TikTok can still be posted as a Short, and a clip that failed on one platform
is retried on that platform only. `--target both` renders a clip once and
uploads it to both. A source only leaves the queue once **every** platform
listed in `config/platforms.json` has it.
</details>

<details>
<summary><b>The YouTube upload clicks the wrong button / can't find the fields.</b></summary>

YouTube Studio's markup changes from time to time. Run
`ttclips-youtube --probe` and inspect `data/cache/probe/elements.json` for the
current element names, then override the selector in `config/youtube.json`
under `"selectors"` — no code change needed.
</details>

<details>
<summary><b>Why does the upload fail or hang?</b></summary>

TikTok's bot detection is aggressive. Keep `headless: false` in
`config/tiktok.json`, use a recent desktop Chrome `user_agent`, and re-export
cookies monthly. Retries use exponential backoff; a CAPTCHA fails the run
loudly rather than silently posting. The same applies to YouTube Studio —
keep `headless: false` in `config/youtube.json` and re-export the Google
cookies when the session expires (`ttclips --validate-youtube`).
</details>

<details>
<summary><b>Why does it need ffmpeg?</b></summary>

ffmpeg merges YouTube video+audio streams, crops to 9:16, and burns in the
captions. `ffmpeg` plus `ffprobe` must resolve on `PATH` (or via
`FFMPEG_BIN`/`FFPROBE_BIN`).
</details>

<details>
<summary><b>Gemini keeps erroring (503 / 429 / high demand)?</b></summary>

The runtime probes candidate models at startup and promotes the healthiest one;
the fallback chain walks through the remaining models with jittered backoff
bounded by `GEMINI_RETRY_TIMEOUT`. Older `flash` models return 404 for new
free-tier keys — use the models listed in `.env.example`.
</details>

<details>
<summary><b>How do I uninstall?</b></summary>

```bash
pip uninstall ttclips
```

State under `data/` and `.env` are not removed.
</details>

## Development

```bash
git clone https://github.com/kamzysert3/ttclips.git
cd ttclips
python -m venv .venv
.\.venv\Scripts\activate        # Windows (PowerShell)
source .venv/bin/activate       # macOS/Linux
pip install -e .
playwright install chromium
```

One-command setup on Windows: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
(creates the venv, installs the package, Playwright, ffmpeg via winget, and
`.env` from the example).

There is no formal test suite yet; run `python -m ttclips --version` and a
`ttclips --once --dry-run` to smoke-test a local checkout. Three offline
checks ship with the repo and need no network, no cookies and no API keys:

```bash
.\.venv\Scripts\python.exe scripts\check_targets.py       # platform-aware state + metadata
.\.venv\Scripts\python.exe scripts\check_repost_flow.py   # cross-post fan-out with stubbed uploaders
.\.venv\Scripts\python.exe scripts\check_youtube_flow.py  # Studio automation against a mock DOM
```

Contributions are welcome via pull request — keep changes consistent with the
existing structure and style, and keep `pyproject.toml` + `ttclips/__init__.py`
versions in sync.

## Releasing

Pushing a version tag builds the distributions and publishes them to PyPI
automatically (GitHub Actions):

```bash
git add . && git commit -m "Release v1.1.0"
git tag v1.1.0
git push origin main --tags
```

The release pipeline verifies the tag matches the version in both
`pyproject.toml` and `ttclips/__init__.py`, builds the wheel + sdist, runs
`twine check`, and publishes via trusted publishing (OIDC) — no API tokens.

## Legal & operational notes

- **Own your content:** ttclips is a repurposing tool — configure only channels
  you own or have rights to repost. `ttclips --init --yes` records that
  acknowledgement. Clipping channels without permission can violate YouTube ToS
  and creators' rights; see
  [`docs/LEGAL.md`](https://github.com/kamzysert3/ttclips/blob/main/docs/LEGAL.md).
- **TikTok ToS:** browser automation to post content is personal-use and
  may not be permitted for commercial accounts — verify your own risk.
- **YouTube ToS:** posting to YouTube Studio is done with browser automation
  on your own account. Automated posting can breach YouTube's Terms of
  Service, and re-uploading content you do not own is a copyright violation.
- **UI changes:** TikTok's and YouTube Studio's uploader selectors can change;
  pin dependency versions, re-export cookies regularly and use the
  `selectors` overrides in the config files.
- **Free-tier limits:** the Gemini free tier handles dozens of calls/day —
  comfortable for a few daily clips.

## Support the project

If ttclips saves you time, consider sponsoring its development:

❤️ **[Sponsor ttclips on GitHub](https://github.com/sponsors/kamzysert3)**

Sponsorships fund maintenance, new features, and documentation.

## License

[MIT](https://github.com/kamzysert3/ttclips/blob/main/LICENSE) — see the
`LICENSE` file for full terms.