"""Clip extraction: faster-whisper transcription + Gemini highlight ranking."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ttclips.config import load_env
from ttclips.gemini import call_with_model_retry, resolve_models

log = logging.getLogger(__name__)
ENV = load_env()
WHISPER_MODEL = ENV.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = ENV.get("WHISPER_DEVICE", "auto")

_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Loading faster-whisper model '%s' (device=%s) ...", WHISPER_MODEL, WHISPER_DEVICE)
        _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type="int8")
    return _whisper_model


def transcribe(video_path: Path, *, word_timestamps: bool = True) -> list[dict]:
    """Return whisper segments as [{start, end, text, words?}] with second precision.

    ``words`` (when enabled) is a list of {"word", "start", "end"} used by the
    encoder for word-synced caption burning.
    """
    model = _get_whisper()
    segments, info = model.transcribe(
        str(video_path),
        word_timestamps=word_timestamps,
        vad_filter=True,
        beam_size=1,
    )
    out = []
    for seg in segments:
        words = None
        if word_timestamps and seg.words:
            words = [
                {"word": w.word, "start": round(float(w.start), 2), "end": round(float(w.end), 2)}
                for w in seg.words
            ]
        out.append({
            "start": round(float(seg.start), 2),
            "end": round(float(seg.end), 2),
            "text": seg.text.strip(),
            "words": words,
        })
    log.info("Transcribed %s -> %d segments (~%.0fs audio)", video_path.name, len(out), info.duration or 0)
    return out


def _transcript_text(segments: list[dict], min_gap: float = 2.0) -> str:
    """Compact transcript with merge of tiny gaps to bound token usage."""
    merged = []
    for s in segments:
        if merged and s["start"] - merged[-1]["end"] <= min_gap:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
            merged[-1]["text"] += " " + s["text"]
        else:
            merged.append(dict(s))
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in merged]
    return "\n".join(lines)


HIGHLIGHT_PROMPT = """You are a short-form content editor for TikTok.
Below is a timestamped transcript of a video. Pick the strongest moments to
repurpose as short vertical clips. A strong moment usually: is self-contained
(podcast/explainer/quote style), has a hook within the first 3 seconds, delivers
one clear idea, and ends cleanly.

Constraints:
- Each highlight must be between 20 and 70 seconds long.
- Start/end values MUST come from actual transcript timestamps.
- Highlights must not overlap with each other.
- Return ONLY valid JSON matching this schema:
{"highlights": [{"start": <float>, "end": <float>, "score": <int 0-100>,
 "hook": "<one catchy opening sentence>", "title": "<short TikTok caption>",
 "hashtags": ["tag1", "tag2"], "reason": "<one line why it is viral>"}]}
- Each highlight must include up to 4 relevant hashtags: lowercase, no "#",
  no duplicates, matched to the clip's topic/niche.
- Pick at most {max_h} highlights, ranked by score descending.

Transcript:
"""


def _sanitize_hashtags(raw, limit: int = 4) -> list[str]:
    """Normalize LLM hashtags: strip '#', drop empties, dedupe case-insensitively."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        tag = str(item).lstrip("#").strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def rank_highlights(segments: list[dict], num_clips: int = 3) -> list[dict]:
    """Rank transcript moments via Gemini.  Returns highlight dicts."""
    num_clips = max(1, int(num_clips))
    text = _transcript_text(segments)
    # Bound token input ~ 60k chars; chunk the transcript if longer.
    text = text[:60000]
    prompt = HIGHLIGHT_PROMPT.replace("{max_h}", str(num_clips)) + text

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("google-genai is not installed.  pip install google-genai")

    key = ENV.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing from .env — see .env.example")

    client = genai.Client(api_key=key)
    models = resolve_models()
    resp = call_with_model_retry(
        lambda model: client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.4,
            ),
        ),
        models,
    )
    raw = resp.text or "{}"
    data = json.loads(raw)
    highlights = data.get("highlights", [])
    # Validate & sanitize
    valid = []
    for h in highlights:
        try:
            start = float(h.get("start"))
            end = float(h.get("end"))
        except (TypeError, ValueError):
            continue
        if end - start < 20 or end - start > 70:
            continue
        if start < 0 or end <= start:
            continue
        h["start"], h["end"] = start, end
        h["hashtags"] = _sanitize_hashtags(h.get("hashtags"))
        valid.append(h)
    valid.sort(key=lambda x: x.get("score", 0), reverse=True)
    log.info("Gemini ranked %d valid highlights", len(valid))
    return valid[:num_clips]


def pick_best_highlight(segments: list[dict]) -> dict | None:
    """Rank then return the single best highlight (None if nothing found)."""
    highlights = rank_highlights(segments, num_clips=1)
    return highlights[0] if highlights else None