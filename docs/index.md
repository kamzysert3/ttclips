---
title: ttclips — turn your own YouTube channel into TikTok clips
---

# ttclips

**A desktop app that turns your own YouTube videos into vertical, captioned
clips and sends them to TikTok as drafts — locally, on your own computer.**

ttclips is open-source software for creators. Point it at a YouTube channel you
own; it finds the strongest moments, cuts them into 9:16 clips with face-aware
framing and word-synced captions, then hands each clip to TikTok through the
official **Content Posting API**. Nothing is hosted: there is no ttclips server
and no account to create.

## How it works with TikTok

1. You authorise ttclips once with **Login Kit** in your browser. The token is
   stored on your machine only.
2. ttclips renders a clip locally (9:16, captions burned in).
3. With the **`video.upload`** scope it uploads the clip to your own TikTok
   inbox as a draft.
4. You open TikTok, review the draft, and tap **Post** yourself.

ttclips never publishes to your profile directly and never touches another
person's account.

## Install

```bash
pip install ttclips
playwright install chromium
ttclips --init --yes
```

Requires Python 3.10+ and ffmpeg. Full instructions and configuration are in the
[README](https://github.com/kamzysert3/ttclips#readme).

## Links

- **Source code & documentation:** <https://github.com/kamzysert3/ttclips>
- **Package:** <https://pypi.org/project/ttclips/>
- [Terms of Service](terms.html)
- [Privacy Policy](privacy.html)
- [Legal & operational notes](LEGAL.html)

## Contact

benjester262@gmail.com
