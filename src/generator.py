"""Original content generation: Gemini script -> edge-tts narration MP3."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from src.config import DOWNLOADS_DIR, load_env
from src.encoder import probe_duration
from src.gemini import call_with_model_retry, resolve_models

log = logging.getLogger(__name__)
ENV = load_env()
TTS_VOICE = ENV.get("TTS_VOICE", "en-US-AvaNeural")

TMP_DIR = DOWNLOADS_DIR / "generated"
TMP_DIR.mkdir(parents=True, exist_ok=True)

SCRIPT_HINT_PROMPT = """You write short, punchy TikTok scripts for a
voice-over TikTok account (original content, 45-70 seconds when spoken).

Topic/hint: {hint}

Rules:
- Spoken script, 130-170 words. One clear idea, strong hook in the first line.
- Casual but credible tone. No "subscribe" bashing, no cringe.
- End with a takeaway or a question to drive comments.
- Also return a short TikTok caption (no hashtags, <=80 chars) and 4 relevant
  hashtags (without #).
- Also return 3 stock-video search keywords for b-roll background footage.
  Keep them short (2-4 words each), generic enough that a stock library has
  them (e.g. "gaming setup", "city night lights", "esports crowd").

Return ONLY JSON:
{{"script": "<full spoken text>", "caption": "<caption>", "hashtags": ["tag1",
"tag2", "tag3", "tag4"], "keywords": ["kw1", "kw2", "kw3"]}}
"""


def build_script(hint: str) -> dict:
    """Ask Gemini for a spoken script + caption + hashtags."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("google-genai not installed")
    key = ENV.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing from .env")
    client = genai.Client(api_key=key)
    models = resolve_models()
    resp = call_with_model_retry(
        lambda model: client.models.generate_content(
            model=model,
            contents=SCRIPT_HINT_PROMPT.format(hint=hint or "a trending niche topic"),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7,
            ),
        ),
        models,
    )
    data = json.loads(resp.text or "{}")
    script = (data.get("script") or "").strip()
    if not script:
        raise RuntimeError("Gemini returned an empty script")
    log.info("Generated script (%d chars) for hint: %s", len(script), hint)
    return {
        "script": script,
        "caption": (data.get("caption") or script[:80]).strip(),
        "hashtags": [h.lstrip("#") for h in data.get("hashtags", [])][:4],
        "keywords": [k.strip(".#") for k in data.get("keywords", []) if k.strip()][:5],
    }


def narration_mp3(script: str, label: str) -> Path:
    """Generate an MP3 narration with edge-tts and return its path."""
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts not installed.  pip install edge-tts")
    stem = (label.replace(" ", "_").replace("/", "_").replace(":", "_")[:40] or "voiceover")
    stem = f"{stem}_{datetime.now().strftime('%Y%m%d')}"
    path = TMP_DIR / f"{stem}.mp3"

    async def _save() -> None:
        comm = edge_tts.Communicate(script, TTS_VOICE)
        await comm.save(str(path))

    asyncio.run(_save())
    log.info("Narration saved: %s", path)
    return path


def generate_original(hint: str, label: str = "") -> dict:
    """Full original-content step: returns dict with script/caption/paths."""
    result = build_script(hint)
    nar_path = narration_mp3(result["script"], label or hint)
    result["narration_path"] = str(nar_path)
    result["duration_sec"] = probe_duration(nar_path)
    return result