# Legal & operational notes

ttclips automates the creation and posting of short-form video clips. This
document keeps the rules for using it responsibly. It is **not legal advice** —
when in doubt, consult a lawyer or avoid the use case.

## Configure only content you own or have rights to

ttclips repurposes videos from YouTube channels into TikTok clips and YouTube
Shorts. It must
only be pointed at channels **you own** or for which you **have explicit
permission** to clip and repost.

- `ttclips --init --yes` records your acknowledgement of this requirement.
- Adding a channel to `config/channels.json` is a claim that you own it or
  hold the rights to its content.

Reposting content you do not own can violate:

- **YouTube's Terms of Service** — automated downloading may breach YouTube's
  restrictions on scraping/downloading content.
- **TikTok's Terms of Service** — repurposing others' content without rights
  is a copyright violation and ground for account suspension.
- **Creators' rights** — clips of others' work may infringe copyright and
  personality/publicity rights, regardless of attribution.

### Safe use cases

- Your own YouTube channel → your own TikTok account.
- Content you commissioned, licensed, or hold a written license for.
- Content from creators who explicitly grant permission for repurposing.

## TikTok Terms of Service

ttclips can post to TikTok two ways (`backend` in `config/tiktok.json`):

- **`api` (recommended)** — TikTok's official Content Posting API through
  your own registered app. In `inbox` mode clips arrive as drafts that you
  publish yourself in the app. In `direct` mode, unaudited apps can only post
  privately to a private account. Follow TikTok's Content Sharing Guidelines.
- **`browser`** — Playwright browser automation using your exported session
  cookies. The points below apply to this backend.

- TikTok's Terms of Service may prohibit automated posting and third-party
  automation of accounts.
- Use only **your own TikTok account**, and accept that it can be rate-limited,
  restricted, or suspended.
- Keep `headless: false` and a recent desktop user-agent to reduce bot
  detection; a CAPTCHA fails loudly rather than silently posting.
- Re-export `data/cookies.txt` roughly monthly — sessions drift.
- Verify your own risk, especially for commercial accounts.

## YouTube Shorts posting

ttclips can post Shorts two ways (`backend` in `config/youtube.json`):

- **`api` (recommended)** — the official YouTube Data API v3 with your own
  OAuth client. This is the sanctioned upload path. Note: until your Google
  Cloud project passes YouTube's API compliance audit, API uploads are
  locked to **private**.
- **`studio`** — Playwright automation of YouTube Studio with exported
  session cookies. YouTube's Terms of Service may prohibit automated access;
  use only your own channel and accept the risk of restrictions.

## YouTube & downloading

- Only download videos you are entitled to download under the content's
  license and YouTube's ToS.
- The tool is intended for creators repurposing their **own** material — it is
  not a "scraper" product.

## Captions, AI, and generated content

- The generated pipeline creates original scripts via an AI model
  (Gemini) and `edge-tts` narration. AI-assisted output does not transfer
  rights in underlying content; you are still responsible for what you post.
- Review generated captions and hashtags before posting; AI output can be
  inaccurate.

## No warranty

ttclips is provided under the MIT License **without warranty of any kind**.
You are solely responsible for the content you post and for complying with all
applicable laws, platform terms, and third-party rights.

---

Full license: [`LICENSE`](../LICENSE)