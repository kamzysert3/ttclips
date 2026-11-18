---
title: ttclips — Privacy Policy
---

# ttclips — Privacy Policy

**Last updated:** 18 November 2026

ttclips is local desktop software. **The ttclips project does not operate a
server, does not have a user database, and never receives your videos, tokens,
cookies or TikTok data.** Everything below describes what the Software does on
your own machine and which third parties it contacts directly, at your request.

This policy belongs to the [Terms of Service](terms.html).

## 1. Summary

| Question | Answer |
|---|---|
| Do you collect my data? | No. There is no backend and no telemetry. |
| Where does my data live? | On your computer, in the folder you run ttclips from. |
| Who else sees it? | Only the services you connect: TikTok, YouTube/Google, and optionally Gemini, Pexels and Edge TTS. |
| Can I delete it? | Yes — delete `data/` and `.env`, or uninstall. |

## 2. Data the Software handles on your machine

- **TikTok authorisation data** — after you approve the app in your browser,
  ttclips stores the OAuth access and refresh tokens in
  `data/tiktok_token.json`. They are used to call TikTok's API on your behalf
  and are refreshed automatically.
- **Your TikTok account identity** — with the `user.info.basic` scope, ttclips
  retrieves your TikTok username, display name and avatar once per run so it can
  log which account a draft was sent to. This is stored only in your local log
  file (`data/logs/ttclips.log`).
- **Video and audio** — the source videos you choose, the rendered clips, the
  transcripts and the highlight rankings are written to `data/downloads/`,
  `data/clips/` and `data/cache/`.
- **Browser session cookies (optional)** — if you use the browser-based upload
  backends instead of the official APIs, you provide exported cookies
  (`data/cookies.txt`, `data/youtube_cookies.txt`). These are read locally and
  are never transmitted anywhere except to the platform they belong to.
- **API keys** — keys for Gemini, Pexels or your own TikTok app are read from
  your `.env` file.

All of the above stays on your device unless a step below sends it to a third
party. Nothing is sent to the ttclips project.

## 3. Data sent to TikTok

When you use the official API backend:

- **Authorisation:** your browser is sent to TikTok to approve the scopes you
  requested (`user.info.basic` and `video.upload`). TikTok returns a token to
  `http://localhost:<port>/callback/` on your machine.
- **Account lookup:** one request to TikTok's `GET /v2/user/info/` returns your
  username, display name and avatar.
- **Video upload:** the clip you chose to post, its caption and hashtags are
  uploaded to TikTok's Content Posting API in chunks. In inbox mode the result
  is a draft in your own TikTok inbox, which you publish yourself.

TikTok's handling of that data is governed by
[TikTok's Privacy Policy](https://www.tiktok.com/legal/privacy-policy) and
TikTok's Developer Terms. ttclips does not post anything you did not ask it to
post, and does not access TikTok data belonging to anyone but the account you
authorised.

## 4. Other third parties (only when you enable or use those features)

- **Google Gemini API** — transcripts of your source video (and, for generated
  clips, a topic hint) are sent to Google to rank highlights or write a script.
  Requires your own `GEMINI_API_KEY`.
- **Pexels API** — search terms are sent to Pexels to fetch stock b-roll.
  Requires your own `PEXELS_API_KEY`. Without it, clips use a local gradient.
- **Microsoft Edge TTS** — the narration text for generated clips is sent to
  Microsoft's text-to-speech endpoint.
- **YouTube** — channel RSS feeds are public; downloads and Shorts uploads go to
  YouTube under your own account and cookies.

Each of these is governed by that provider's own privacy policy. You can avoid
Gemini/Pexels/Edge TTS entirely by using only the repost pipeline.

## 5. Data the project does not collect

No analytics, no crash reporting, no usage statistics, no advertising
identifiers, no device fingerprinting. The Software makes no request to any
ttclips-operated endpoint, because none exists.

## 6. Retention and deletion

Data is kept on your machine until you remove it:

- delete `data/tiktok_token.json` to revoke the local token, and revoke the app
  in TikTok's own settings (**Settings and privacy → Security and permissions →
  Manage app permissions**);
- delete `data/clips/`, `data/downloads/`, `data/cache/` and `data/logs/` to
  remove media and logs;
- uninstalling the package does not delete `data/` or `.env` — delete them
  yourself.

## 7. Security

Tokens and cookies are stored as plain files in your project folder, protected
by your operating system's file permissions. Keep that folder private, do not
commit it to version control (the shipped `.gitignore` excludes `data/` and
`.env`), and re-export cookies when they expire. No method of storage is
perfectly secure; use the Software on a machine you control.

## 8. Children

The Software is not directed at children under 13, and you must meet the minimum
age required by the platforms you connect.

## 9. International transfers

When your machine calls TikTok, Google, Microsoft or Pexels, your data is
processed in the countries where those providers operate. Those transfers are
made by those providers under their own policies, not by the ttclips project.

## 10. Changes

If this policy changes, the "last updated" date above changes and the current
version stays published at this URL. Material changes will also be noted in the
project's release notes.

## 11. Contact

Privacy questions: **benjester262@gmail.com**
