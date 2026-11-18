# TikTok app review — submission pack

Paste-ready values for the Developer Portal form at
<https://developers.tiktok.com/apps/> (app: **ttclips**). Everything here
matches what the code actually does: desktop OAuth on `localhost`, the official
Content Posting API in **inbox** mode, scopes `user.info.basic` + `video.upload`.
See `ttclips/tiktok_api.py`.

> Do **not** request `video.publish` (direct post). You are not demoing direct
> posting, and an undemonstrated scope is the fastest rejection there is.

---

## 0. Before you touch the form

| Check | Value / action |
|---|---|
| Environment | Switch to **Sandbox** and add your own TikTok account as a test user |
| Client key/secret | Sandbox app's key + secret → `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` in `.env` |
| Backend | `config/tiktok.json` → `"backend": "api"`, `"api": { "api_mode": "inbox" }` |
| Redirect URI | Register `http://localhost:*/callback/` (wildcard port = what `tiktok_api.py` uses) |
| Icon | `assets/icon-1024.png` — already 1024×1024, 23 KB, PNG ✅ |
| Legal pages | See [§4](#4-terms--privacy-urls) — the form will not submit without them |
| Demo video | See [§5](#5-demo-video-shot-list) — must be recorded in sandbox |

---

## 1. App details

| Field | Value |
|---|---|
| **App name** | `ttclips` (7/50) |
| **Category** | `Photo & Video` |
| **Platforms** | ✅ **Desktop** only — leave Web / Android / iOS unchecked |
| **App icon** | `assets/icon-1024.png` |

**Description** (112/120):

```
Desktop app that turns your own YouTube videos into vertical captioned clips and sends them to TikTok as drafts.
```

**Terms of Service URL** / **Privacy Policy URL**: see [§4](#4-terms--privacy-urls).

---

## 2. Products & scopes

**Add products:** `Login Kit`, `Content Posting API`.
Remove everything else (no Share Kit, no Display API, no Research API — unused).

**Add scopes:** `user.info.basic`, `video.upload`.

| Scope | Where in the code | What it does |
|---|---|---|
| `user.info.basic` | `TikTokClient.user_info()` | one `GET /v2/user/info/` per run to confirm which account authorized the app |
| `video.upload` | `TikTokClient.upload()` | inbox draft: `/v2/post/publish/inbox/video/init/` → chunked `PUT` → poll `/v2/post/publish/status/fetch/` until `SEND_TO_USER_INBOX` |

---

## 3. "How each product and scope works" (863/1000)

```
ttclips is a local desktop app for creators. It watches YouTube channels the user owns, cuts highlights into vertical 9:16 clips with captions, then sends them to TikTok with the official Content Posting API.

Login Kit (Desktop): OAuth in the system browser, redirect to http://localhost:<port>/callback/. No website is involved. Tokens are stored locally on the user's machine and refreshed automatically.

user.info.basic: one GET /v2/user/info/ call to confirm which TikTok account authorized the app before any draft is sent.

video.upload: after a clip renders locally, ttclips calls /v2/post/publish/inbox/video/init/, uploads in chunks, then polls /v2/post/publish/status/fetch/ until SEND_TO_USER_INBOX. The clip becomes a draft in the creator inbox; the creator reviews and taps Post. ttclips never publishes directly and does not request video.publish.
```

Keep this text and the demo video in lockstep — every sentence above should be
visible on screen at some point.

---

## 4. Terms & privacy URLs

The form rejects blank URLs, and reviewers do open them. Two options:

**A. GitHub Pages (recommended)** — enable Pages on `kamzysert3/ttclips`
(branch `main`, folder `/docs`), then commit `docs/terms.md` + `docs/privacy.md`
and use:

```
https://kamzysert3.github.io/ttclips/terms.html
https://kamzysert3.github.io/ttclips/privacy.html
```

Enable Pages → source `main` → `/docs`. A `_config.yml` with
`theme: jekyll-theme-minimal` renders the markdown to HTML at `.html` paths.

**B. Raw GitHub links (weaker)** — `docs/LEGAL.md` exists but is not a
ToS/privacy policy, and `github.com/.../blob/...` links have been rejected
before as "not an official policy page". Only use these if A is impossible.

What the privacy policy must say for this app to pass review:

- data processed: OAuth tokens, the video files the user selects, the TikTok
  username returned by `user.info.basic`
- where it is stored: `data/tiktok_token.json` and `data/clips/` **on the
  user's own machine** — there is no ttclips server
- who it is sent to: TikTok's API only (plus Gemini/Pexels if the user enabled
  those features — those are unrelated to the TikTok integration)
- retention: tokens until deleted / refresh, clips until the user deletes them
- contact email + "last updated" date

---

## 5. Demo video shot list

Record in **Sandbox**, 1080p, ≤50 MB, `mp4`. Show the terminal and the real
UIs — no slides-only walkthrough, no cuts that hide a step.

| Time | On screen |
|---|---|
| 0:00–0:10 | `ttclips --version` in a terminal; briefly show the README so the reviewer sees what the app is |
| 0:10–0:35 | `ttclips-upload --auth` → browser opens TikTok consent screen **listing `user.info.basic` and `video.upload`** → click *Authorize* → terminal: `Saved TikTok token … (scopes: user.info.basic,video.upload)` |
| 0:35–0:50 | `ttclips --validate-session` → prints the authorized `@username` (this is the `user.info.basic` proof) |
| 0:50–1:40 | `ttclips --once --slot 12:00 --review` → clip renders (9:16 + captions visible), review screen shows caption/hashtags, press Enter |
| 1:40–2:10 | Upload logs: init → chunk progress → `SEND_TO_USER_INBOX` + `publish_id` |
| 2:10–2:35 | Open the TikTok app → **Inbox / notifications** → the new draft → show the clip and its caption |
| 2:35–2:50 | Tap **Post** → clip appears on the profile |

Show the sandbox client key region of the portal if you like, but **never show
the client secret or the token file contents**.

---

## 6. Rejection risks specific to this app

1. **Demo uses cookies, not the API.** `"backend": "browser"` posts via
   Playwright — that is not the integration under review. Record with
   `"backend": "api"` or the reviewer sees no API calls at all.
2. **Scope/behaviour mismatch.** Requesting `video.publish` while only showing
   inbox drafts = "scope not demonstrated". Add it only after you build and film
   direct posting (which needs the account private pre-audit).
3. **Legal URLs 404 or generic.** Host them before submitting; re-check both
   links in an incognito window.
4. **Desktop vs Web.** Leaving **Web** checked drags in domain verification and
   a demo video whose domain must match the URL property. Desktop only.
5. **Unused products.** Every product you leave attached must be demonstrated —
   remove Share Kit / Display API even if they were added earlier.
6. **Marketing copy.** "Turns YouTube into TikTok content" reads like a scraper.
   Keep the description on *your own* videos (as above) and point at
   `docs/LEGAL.md` if asked.
