"""FFmpeg encoder: 9:16 vertical clip production with burnt-in captions."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from src.binutils import ffmpeg_bin, ffprobe_bin
from src.clipper import transcribe
from src.config import load_env

log = logging.getLogger(__name__)

_ENV = load_env()

# ---------------------------------------------------------------------------
# Caption style (big, dead-center, word-synced highlight). Override any of
# these via .env, e.g.  CAPTION_FONT_SIZE=110  HIGHLIGHT_COLOUR=0000FFFF
CAPTION_FONT_SIZE = int(_ENV.get("CAPTION_FONT_SIZE", "96"))
CAPTION_ALIGNMENT = int(_ENV.get("CAPTION_ALIGNMENT", "5"))  # 5 = middle-center
CAPTION_MARGINV = int(_ENV.get("CAPTION_MARGINV", "0"))
CAPTION_OUTLINE = int(_ENV.get("CAPTION_OUTLINE", "3"))
CAPTION_SHADOW = int(_ENV.get("CAPTION_SHADOW", "2"))
CAPTION_WORDS_PER_LINE = int(_ENV.get("CAPTION_WORDS_PER_LINE", "6"))
HIGHLIGHT_COLOUR = _ENV.get("HIGHLIGHT_COLOUR", "0000FFFF")  # ASS BGR: yellow
IDLE_COLOUR = _ENV.get("IDLE_COLOUR", "00808080")  # ASS BGR: light grey

# ---------------------------------------------------------------------------
# Binary resolution (moved to src.binutils): env override -> winget Gyan build
# -> PATH. Missing binaries fail loudly with a clear message.
def _ffmpeg() -> str:
    path = ffmpeg_bin()
    if not path:
        raise RuntimeError(
            "ffmpeg not found. Install it (e.g. `winget install Gyan.FFmpeg`) "
            "or set FFMPEG_BIN in .env."
        )
    return path


def _ffprobe() -> str:
    path = ffprobe_bin()
    if not path:
        raise RuntimeError(
            "ffprobe not found. Install it (e.g. `winget install Gyan.FFmpeg`) "
            "or set FFPROBE_BIN in .env."
        )
    return path


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output([
            _ffprobe(), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], stderr=subprocess.DEVNULL, text=True)
        return float(out.strip())
    except Exception:
        return 0.0


def _safe_path(p: Path) -> str:
    """Return a /-separated path string safe for ffmpeg subtitle filters on Windows."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log.debug("ffmpeg cmd: %s", " ".join(cmd[:8]) + " ...")
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        log.error("ffmpeg failed (exit %s):\n%s", proc.returncode, stderr[-3000:])
        raise RuntimeError(
            f"ffmpeg exit {proc.returncode} for {cmd[0]}: {stderr[-1500:]}"
        )
    return proc


# ---------------------------------------------------------------- ASS generation
def _ass_header(w: int = 1080, h: int = 1920) -> str:
    return (
        "[Script Info]\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
f"Style: Default,Arial,{CAPTION_FONT_SIZE},&H00FFFFFF,&H00FFFF00,"
        "&H00000000,&H80000000,"
        "0,0,0,0,100,100,1,0,1,"
        f"{CAPTION_OUTLINE},{CAPTION_SHADOW},"
        f"{CAPTION_ALIGNMENT},30,30,{CAPTION_MARGINV},1\n"
        f"Style: Title,Arial,64,&H00FFFFFF,&H0000FF00,&H00000000,&H00000000,"
        "1,0,0,0,100,100,0,0,1,2,0,8,30,30,40,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _ass_color(value) -> str:
    """Normalize a colour value (with or without '&H') to ASS form '&HBBGGRR&'."""
    v = str(value).strip()
    if v.startswith("&"):
        v = v[2:] if len(v) > 1 and v[1].lower() == "h" else v[1:]
    return f"&H{v}&"


def _bundle_words(buf_words: list[dict]) -> dict:
    toks = [w["word"].strip() for w in buf_words if w["word"].strip()]
    return {
        "start": buf_words[0]["start"],
        "end": buf_words[-1]["end"],
        "text": " ".join(toks),
        "words": buf_words,
    }


def _words_to_caption_lines(
    words: list[dict],
    words_per_line: int = CAPTION_WORDS_PER_LINE,
    max_duration: float = 4.0,
) -> list[dict]:
    """Group word dicts into caption line cues [{start, end, text, words}]."""
    if not words:
        return []
    lines = []
    buf_words = []
    buf_start = None
    for w in words:
        if buf_start is None:
            buf_start = w["start"]
        buf_words.append(w)
        duration = w["end"] - buf_start
        if len(buf_words) >= words_per_line or duration >= max_duration:
            lines.append(_bundle_words(buf_words))
            buf_words = []
            buf_start = None
    if buf_words:
        lines.append(_bundle_words(buf_words))
    return lines


def _clip_words(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Flatten whisper word timestamps inside the clip window, rebased to t=0."""
    out = []
    for s in segments:
        for w in s.get("words") or []:
            ws = max(float(w["start"]), clip_start)
            we = min(float(w["end"]), clip_end)
            if we - ws < 0.05:
                continue
            out.append({
                "word": w["word"],
                "start": round(ws - clip_start, 2),
                "end": round(we - clip_start, 2),
            })
    return out


def _line_to_word_events(line: dict) -> list[tuple[float, float, str]]:
    """One Dialogue per spoken word: whole line laid out identically, active
    word bold+highlight colour, rest idle grey."""
    words = line.get("words") or []
    toks = [w["word"].strip() for w in words if w["word"].strip()]
    if not toks:
        return []
    hl, idle = _ass_color(HIGHLIGHT_COLOUR), _ass_color(IDLE_COLOUR)
    events = []
    for i, tok in enumerate(toks):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        t0 = max(line["start"], w["start"])
        t1 = min(line["end"], nxt["start"] if nxt else line["end"])
        if t1 - t0 < 0.05:
            t1 = min(line["end"], t0 + 0.05)
        pieces = []
        for j, tok in enumerate(toks):
            esc = _escape_ass(tok)
            if j == i:
                pieces.append(f"{{\\c{hl}\\b1}}{esc}{{\\c{idle}\\b0}}")
            else:
                pieces.append(esc)
        text = " ".join(pieces)
        events.append((round(t0, 2), round(t1, 2), text))
    return events


def build_ass(caption_lines: list[dict], title: str = "", w: int = 1080, h: int = 1920) -> str:
    """Return a complete .ass file content string."""
    out = _ass_header(w, h)
    if title:
        # Show title first 3 seconds, centered top
        title_end = min(3.0, caption_lines[0]["start"] if caption_lines else 3.0)
        out += f"Dialogue: 0,{_fmt_ts(0)},{_fmt_ts(title_end)},Title,,0,0,0,,{_escape_ass(title)}\n"
    for line in caption_lines:
        events = _line_to_word_events(line)
        if not events:
            events = [(line["start"], line["end"], line["text"])]
        for t0, t1, text in events:
            out += f"Dialogue: 0,{_fmt_ts(t0)},{_fmt_ts(t1)},Default,,0,0,0,,{text}\n"
    return out


def _segments_to_caption_lines(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Filter segments to the clip window and rebase timestamps to t=0.

    Carries rebased per-word timestamps (``words``) for word-synced captions.
    """
    out = []
    for s in segments:
        s_start = max(float(s["start"]), clip_start)
        s_end = min(float(s["end"]), clip_end)
        if s_end - s_start < 0.3:
            continue
        words = None
        if s.get("words"):
            words = []
            for w in s["words"]:
                ws = max(float(w["start"]), clip_start)
                we = min(float(w["end"]), clip_end)
                if we - ws < 0.05:
                    continue
                words.append({
                    "word": w["word"],
                    "start": round(ws - clip_start, 2),
                    "end": round(we - clip_start, 2),
                })
        out.append({
            "start": round(s_start - clip_start, 2),
            "end": round(s_end - clip_start, 2),
            "text": s["text"],
            "words": words,
        })
    return out


# ---------------------------------------------------------------- reproducible clip production
def cut_and_crop_clip(
    source: Path,
    clip_start: float,
    clip_end: float,
    segments: list[dict],
    out: Path,
    title: str = "",
    *,
    aspect_w: int = 1080,
    aspect_h: int = 1920,
) -> Path:
    """Trim *source* to [clip_start, clip_end], crop to 9:16, burn captions.

    ``segments`` should be the full-video whisper segment list; timestamps are
    clipped and rebased automatically.
    """
    if not source.exists():
        raise FileNotFoundError(source)
    words = _clip_words(segments, clip_start, clip_end)
    caption_lines = _words_to_caption_lines(words)
    if not caption_lines:
        caption_lines = _segments_to_caption_lines(segments, clip_start, clip_end)
    ass_content = build_ass(caption_lines, title=title, w=aspect_w, h=aspect_h)

    ass_path = out.parent / (out.stem + ".ass")
    ass_path.write_text(ass_content, encoding="utf-8-sig")
    vf = (
        f"scale={aspect_w}:{aspect_h}:force_original_aspect_ratio=increase,"
        f"crop={aspect_w}:{aspect_h},setsar=1,"
        f"subtitles='{_safe_path(ass_path)}'"
    )
    duration = clip_end - clip_start
    cmd = [
        _ffmpeg(), "-y",
        "-ss", str(clip_start), "-t", str(duration),
        "-i", str(source),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    _run(cmd)
    log.info("Clip produced: %s", out)
    return out


# ---------------------------------------------------------------- generated clip
def _script_to_timed_lines(script: str, duration: float) -> list[dict]:
    """Approximate sentence-level timings for a narration script (fallback only)."""
    import re as _re
    sentences = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', script) if s.strip()]
    if not sentences:
        return [{"start": 0.0, "end": duration, "text": script[:80]}]
    total_chars = sum(len(s) for s in sentences)
    lines = []
    t = 0.0
    for s in sentences:
        dur = (len(s) / total_chars) * duration if total_chars else duration / len(sentences)
        dur = max(dur, 0.8)
        lines.append({"start": round(t, 2), "end": round(t + dur, 2), "text": s})
        t += dur
    if lines:
        lines[-1]["end"] = round(duration, 2)
    return lines


def make_generated_clip(
    narration_mp3: Path,
    script: str,
    title: str,
    out: Path,
    duration_sec: float | None = None,
    background: Path | None = None,
) -> Path:
    """Create a vertical 1080x1920 clip with captions over stock b-roll.

    ``title`` is written to the .ass. ``script`` is only used as a fallback
    caption source when the narration cannot be transcribed. Prefers the given
    ``background`` b-roll clip (dimmed, 9:16, narration audio only); falls back
    to an animated gradient backplate when no background is available or
    rendering it fails.
    """
    if duration_sec is None:
        duration_sec = probe_duration(narration_mp3) or 45.0

    ass_path = out.parent / (out.stem + ".ass")
    segments = transcribe(narration_mp3)
    words = _clip_words(segments, 0.0, duration_sec)
    caption_lines = _words_to_caption_lines(words)
    if not caption_lines:
        caption_lines = _segments_to_caption_lines(segments, 0.0, duration_sec)
        for line in caption_lines:
            line["words"] = None
    if not caption_lines:
        lines = _script_to_timed_lines(script, duration_sec)
        for line in lines:
            line["words"] = None
        caption_lines = lines
    ass_content = build_ass(caption_lines, title=title, w=1080, h=1920)
    ass_path.write_text(ass_content, encoding="utf-8-sig")

    if background is not None and background.exists():
        try:
            return _render_with_background(narration_mp3, ass_path, out, duration_sec, background)
        except RuntimeError as e:
            log.warning("B-roll render failed — falling back to gradient: %s", e)

    return _render_gradient(narration_mp3, ass_path, out, duration_sec)


def _render_with_background(
    narration_mp3: Path, ass_path: Path, out: Path, duration_sec: float, background: Path
) -> Path:
    """Mute the b-roll, crop to 9:16, dim it, burn captions, mux narration."""
    vf = (
        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,setsar=1,"
        f"eq=brightness=-0.25,"
        f"subtitles='{_safe_path(ass_path)}'[v]"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-stream_loop", "-1", "-i", str(background),
        "-i", str(narration_mp3),
        "-filter_complex", vf,
        "-map", "[v]", "-map", "1:a",
        "-t", f"{duration_sec:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(cmd)
    log.info("Generated clip (b-roll): %s", out)
    return out


def _render_gradient(narration_mp3: Path, ass_path: Path, out: Path, duration_sec: float) -> Path:
    """Fallback backplate: animated gradients, then plain colour."""
    grad_input = (
        f"gradients=s=1080x1920:c0=0x1e3c72:c1=0x2a5298:c2=0x0f2027:"
        f"speed=0.04:d={duration_sec}:r=30"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", grad_input,
        "-i", str(narration_mp3),
        "-filter_complex",
        f"[0:v]subtitles='{_safe_path(ass_path)}'[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    try:
        _run(cmd)
        log.info("Generated clip: %s", out)
        return out
    except RuntimeError:
        log.warning("gradients filter unavailable, using solid colour fallback")

    cmd_fb = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", f"color=c=0x1e3c72:s=1080x1920:d={duration_sec},fps=30",
        "-i", str(narration_mp3),
        "-vf", f"subtitles='{_safe_path(ass_path)}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(cmd_fb)
    log.info("Generated clip (fallback colour): %s", out)
    return out