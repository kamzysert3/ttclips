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

**Description** (114/120):

```
Desktop tool for clippers: turns streams and videos you're licensed to clip into captioned vertical TikTok drafts.
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

## 3. "How each product and scope works" (942/1000)

```
ttclips is a local desktop app for creators and clippers. It watches YouTube channels the user owns or is licensed to clip (e.g. a creator's paid clipping campaign), cuts highlights into vertical 9:16 clips with captions, then sends them to TikTok with the official Content Posting API.

Login Kit (Desktop): OAuth in the system browser, redirect to http://localhost:<port>/callback/. No website is involved. Tokens are stored locally on the user's machine and refreshed automatically.

user.info.basic: one GET /v2/user/info/ call to confirm which TikTok account authorized the app before any draft is sent.

video.upload: after a clip renders locally, ttclips calls /v2/post/publish/inbox/video/init/, uploads in chunks, then polls /v2/post/publish/status/fetch/ until SEND_TO_USER_INBOX. The clip becomes a draft in the creator inbox; the creator reviews and taps Post. ttclips never publishes directly and does not request video.publish.
```

Keep this text and the demo video in lockstep — every sentence above should be
visible on screen at some point.

---

## 4. Terms & privacy URLs

The form rejects blank URLs, and reviewers do open them. The pages are written
and committed: `docs/terms.md`, `docs/privacy.md`, `docs/_config.yml`.

1. Push `main`.
2. GitHub → **Settings → Pages** → Source: *Deploy from a branch* → Branch
   `main`, folder `/docs` → Save.
3. Wait for the build, then open both URLs in an **incognito** window before
   submitting:

```
https://kamzysert3.github.io/ttclips/terms.html
https://kamzysert3.github.io/ttclips/privacy.html
```

Jekyll renders each `.md` to `.html` with `jekyll-theme-minimal`; no extra
build step is needed. If you later host on a custom domain, update both URLs in
the portal **and** the links in `README.md`.

Fallback if Pages is unavailable: the `github.com/.../blob/main/docs/...`
links work but have been rejected before as "not an official policy page".
`docs/LEGAL.md` is neither a ToS nor a privacy policy — do not submit it.

### Web/Desktop URL property

Enter `https://kamzysert3.github.io/ttclips/` (served by `docs/index.md`).

Verification token is already in the build: `docs/_includes/head-custom.html`
holds the `tiktok-developers-site-verification` meta tag, which
`jekyll-theme-minimal` injects into every page's `<head>`. After Pages builds,
open the URL and view source to confirm the tag is present, then click
**Verify** in the portal. Leave the tag in place — TikTok re-checks it.

The redirect URI stays `http://localhost:*/callback/`; do not attach a web
redirect to this domain.

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
   Say what it is — a clipping tool for content you own or are licensed to clip
   (creator clipping campaigns) — and point at `docs/LEGAL.md` / `terms.html`
   if asked. Demo with a creator whose campaign you are actually in, or your own
   channel.
