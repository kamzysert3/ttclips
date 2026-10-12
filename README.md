# ttclips — clip reposter for TikTok

Fully local, free pipeline: monitors YouTube channels, detects highlights (whisper
+ Gemini), crops clips to vertical 9:16 with **dead-center, word-synced highlight
captions**, and posts to TikTok through Playwright browser automation (cookie auth,
no API approval needed).

## How it works

```
YouTube RSS feeds ─────────▶ monitor.py (queue of new videos)
                                   │
                          yt-dlp + ffmpeg (download, merge + cache reuse)
                                   ▼
                          faster-whisper (transcription + word timestamps)
                                   ▼
                          Gemini (probe → fallback chain) highlight ranking
                                   ▼
                          FFmpeg (crop 9:16 + big centered karaoke captions)
                                   ▼
    2 pipelines:  repost clips        OR        generated original clips
             Gemini highlights  +   Gemini script → edge-tts narration →
             cut_and_crop_clip        stock b-roll + captions
                                      (gradient fallback)
                                   ▼
                          Playwright → TikTok upload (cookies)
```

8 posts/day schedule (`config/posting_window.json`):

| Time | Type |
|------|------|
| 08:00 | repost |
| 10:00 | repost |
| 12:00 | generated |
| 14:00 | repost |
| 16:00 | generated |
| 18:00 | repost |
| 20:00 | generated |
| 22:00 | repost |

## Setup (Windows)

1. **Python 3.10+** — verify with `python --version`.
2. `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
   - creates `.venv`, installs deps & Playwright chromium
   - installs ffmpeg via winget if missing and adds its bin dir to your PATH so
     yt-dlp's video+audio merge finds it
   - copies `.env.example` → `.env`
3. Edit **`.env`** → set `GEMINI_API_KEY` (free at https://aistudio.google.com/apikey)
   and `PEXELS_API_KEY` (free at https://www.pexels.com/api/ — powers stock
   backgrounds behind generated clips; leave blank and generated clips keep the
   gradient backplate).
4. **TikTok cookies**: log into tiktok.com in Chrome → use the *"Get cookies.txt
   LOCALLY"* extension → export for tiktok.com → save as `data/cookies.txt`.
   Re-export roughly monthly (sessions drift).

## Verify

```powershell
.\.venv\Scripts\python.exe -m src.scheduler --validate-session   # cookies OK?
.\.venv\Scripts\python.exe -m src.scheduler --monitor            # build watch queue
.\.venv\Scripts\python.exe -m src.scheduler --once --dry-run     # full pipeline, no upload
.\.venv\Scripts\python.exe -m src.scheduler --once --slot 12:00  # post one slot now
.\.venv\Scripts\python.exe -m src.scheduler --once --slot 12:00 --no-upload  # render only
.\.venv\Scripts\python.exe -m src.scheduler --post-file "data\clips\video.mp4"   # post an already-generated clip
.\.venv\Scripts\python.exe -m src.gemini  --probe                # ping Gemini model status
```

Closing flags: `--dry-run` = skip upload, `--no-upload` = render but don't post.
`--post-file <path>` (optional `--caption "..."`) uploads a clip that is already
generated, skipping the entire download/transcribe/rank/encode pipeline — handy
for retrying a stuck upload on a finished clip. On success it records a manual
entry in `data/posted.json`.

## Scheduled runs (Windows Task Scheduler)

| Task | Trigger | Action |
|------|---------|--------|
| ttclips-monitor | Daily 06:00 | refresh the watch queue (`--monitor`) |
| ttclips-poster | Hourly 07:00→23:00 | `--once` (posts only at configured slot times) |

The scheduler posts exactly once per slot per day; run `--catchup` after the
machine was off for a while to fill missed slots for today. `scripts/catchup.ps1`
wraps it.

## Configuration

- `config/channels.json` — channels to monitor. Fields per channel:
  `name`, `channel_id`, `enabled`, `keywords` (empty = accept all uploads),
  `max_moments` (how many clips to post per video, capped at 5).
- `config/posting_window.json` — the 8 daily slots (`time`, `type`, and a
  `hint` for generated inspiration).
- `config/tiktok.json` — browser/headless/delay/retry settings, default hashtags,
  caption template. Uploads run in an isolated subprocess with retries; waits grow
  exponentially (`retry_wait_initial_sec`, `retry_wait_backoff`,
  `retry_wait_max_sec`) after every failure. `user_agent` must stay a recent
  desktop Chrome UA (the library default Chrome/58 gets TCP-reset by TikTok's
  WAF); `upload_timeout_sec` gives page loads their budget.
- `.env` — secrets + models + styling:
  - `GEMINI_MODEL` + `GEMINI_FALLBACK_MODELS` (comma list) — tried in order; the
    runtime probes candidates at startup and promotes the healthiest model
  - `GEMINI_RETRY_TIMEOUT` — hard cap on retrying a stuck model
  - `WHISPER_MODEL` / `WHISPER_DEVICE`, `TTS_VOICE`, `COOKIES_FILE`
  - `PEXELS_API_KEY` — stock-video b-roll search for generated clips (free key)
  - caption knobs: `CAPTION_FONT_SIZE` (96), `CAPTION_ALIGNMENT` (5),
    `CAPTION_MARGINV`, `CAPTION_OUTLINE`, `CAPTION_SHADOW`,
    `CAPTION_WORDS_PER_LINE`, `HIGHLIGHT_COLOUR` (yellow), `IDLE_COLOUR` (grey)

## Captions

Captions are big (96pt), dead-centered, and karaoke-style: the word currently
being spoken is bold in the highlight colour while the rest of the line stays
grey. Repost clips use real whisper word timestamps; generated clips re-transcribe
their TTS narration with faster-whisper so the karaoke is word-synced too, in
exactly the same style as reposts.

## State

- `data/queue.json` — discovered videos awaiting processing
- `data/posted.json` — dedup history (never reposts the same moment twice)
- `data/downloads/` — merged mp4s (reused on retry, no re-download)
- `data/clips/` — rendered vertical clips
- `data/broll/` — cached stock backgrounds for generated clips
- `data/logs/ttclips.log` — full log

## Resilience

- **Gemini 503/429 overload**: the probe picks a healthy model at startup and the
  fallback chain walks through remaining models; retries use jittered backoff
  bounded by `GEMINI_RETRY_TIMEOUT`. Older flash models (2.x) return 404 for new
  free-tier keys — use the ones listed in `.env.example`.
- **YouTube bot-checks**: intermittent "sign in to confirm you're not a bot". The
  downloader fails loudly; merging is handled by the winget ffmpeg install.
- **Partial failures**: a failed Gemini post step doesn't waste a 150 MB download —
  the cached merged mp4 is reused.
- **B-roll (generated clips)**: backgrounds come from the free Pexels Videos API
  (`PEXELS_API_KEY`), searched with Gemini-chosen keywords, dimmed and cropped to
  9:16. If the search, download, or render fails the clip falls back to an
  animated gradient backplate, so a slot never dies.

## Notes & risks

- **Copyright**: only repost content you have rights to. Clipping public channels
  without permission can violate YouTube ToS / creators' rights. Prefer channels
  that allow embedding/repurposing, or lean on the generated pipeline.
- **TikTok bot detection**: keep `headless: false`; the human-like delays in
  `config/tiktok.json` help. If TikTok serves a CAPTCHA the run fails loudly.
- **UI changes**: selectors in the TikTok uploader may break; pin versions.
- **Free tier limits**: the Gemini free tier handles dozens of calls/day — fine for
  a few daily clips.