---
title: ttclips Privacy Policy
---

# ttclips Privacy Policy

_Last updated: 18 November 2026_

ttclips is an open-source desktop application that runs entirely on your own
computer. There is no ttclips server, account system or analytics. The
developer of ttclips does not receive, collect or store any of your data.

## What data ttclips processes

| Data | Why | Where it is stored |
|---|---|---|
| TikTok OAuth access and refresh tokens | To send clips to your TikTok account on your behalf | `data/tiktok_token.json` on your computer |
| Your TikTok display name / open ID (from `user.info.basic`) | To confirm which account authorized the app before uploading | In memory and local log files only |
| Video files you choose to process and the clips ttclips renders | To create and upload the clips | `data/` folders on your computer |
| YouTube channel IDs you configure | To find new videos from channels you own | Local config files |

## Who data is shared with

- **TikTok**: only the clips you upload, their captions and the OAuth
  tokens, sent directly to TikTok's official API
  (`open.tiktokapis.com`). TikTok's own
  [Privacy Policy](https://www.tiktok.com/legal/privacy-policy) applies there.
- **Optional third-party features**: if you turn on AI captions (Google
  Gemini) or stock B-roll (Pexels), the relevant text or search terms go to
  those services under their own policies. These features are off unless you
  add your own API keys, and they are separate from the TikTok integration.

ttclips never sells data or shares it with anyone else.

## Retention and deletion

Tokens stay until you delete `data/tiktok_token.json` or revoke access in
TikTok (**Settings → Security → Apps and services**). Clips stay until you
delete them. Uninstalling ttclips and deleting its folder removes everything.

## Children

ttclips is not directed at children under 13 (or the minimum age in your
country).

## Changes

Any change to this policy is published at this URL with a new "Last updated"
date.

## Contact

CONTACT_EMAIL
