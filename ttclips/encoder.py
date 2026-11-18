"""FFmpeg encoder: 9:16 vertical clip production with burnt-in captions."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from ttclips.binutils import ffmpeg_bin, ffprobe_bin
from ttclips import captions, framing
from ttclips.clipper import transcribe
from ttclips.config import load_env

log = logging.getLogger(__name__)

_ENV = load_env()

# Encode quality. CRF 18 is visually lossless on a phone; platforms re-encode
# uploads, so feeding them a clean master matters. The preset only trades
# encode time for file size at a given CRF, so the default stays quick.
VIDEO_CRF = _ENV.get("VIDEO_CRF", "18")
VIDEO_PRESET = _ENV.get("VIDEO_PRESET", "veryfast")
MAX_FPS = int(_ENV.get("VIDEO_MAX_FPS", "60"))
_OFF = ("0", "false", "no", "off")
# Loudness-normalise speech to the level short-form platforms target.
AUDIO_NORMALIZE = _ENV.get("AUDIO_NORMALIZE", "1").strip().lower() not in _OFF
# Trim clip bounds onto word boundaries so speech is never cut mid-word.
SNAP_TO_WORDS = _ENV.get("SNAP_TO_WORDS", "1").strip().lower() not in _OFF

# ---------------------------------------------------------------------------
# Binary resolution (moved to ttclips.binutils): env override -> winget Gyan build
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


def probe_video(path: Path) -> dict:
    """Return ``{"duration", "width", "height", "fps"}`` for a file.

    Missing binaries or unreadable files yield zeros rather than raising —
    callers decide whether that is fatal.
    """
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0}
    try:
        out = subprocess.check_output([
            _ffprobe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
            "-of", "json", str(path),
        ], stderr=subprocess.DEVNULL, text=True)
        data = json.loads(out or "{}")
        stream = (data.get("streams") or [{}])[0]
        info["width"] = int(stream.get("width") or 0)
        info["height"] = int(stream.get("height") or 0)
        info["duration"] = float(data.get("format", {}).get("duration") or 0.0)
        num, _, den = str(stream.get("avg_frame_rate") or "0/1").partition("/")
        info["fps"] = float(num) / float(den) if float(den or 0) else 0.0
    except Exception:
        pass
    return info


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


# ---------------------------------------------------------------- shared pieces
def _subtitles_filter(ass_path: Path) -> str:
    """`subtitles` filter that can also see the bundled caption font."""
    return (f"subtitles=filename='{_safe_path(ass_path)}'"
            f":fontsdir='{_safe_path(captions.FONTS_DIR)}'")


def _video_args(fps: float = 0.0) -> list[str]:
    out_fps = max(24, min(MAX_FPS, round(fps))) if fps and fps > 1 else 30
    return [
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-r", str(out_fps), "-g", str(out_fps * 2),
        "-maxrate", "14M", "-bufsize", "28M",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-movflags", "+faststart",
    ]


def _audio_filter(duration: float) -> str:
    """Loudness normalisation plus click-free edges."""
    chain = []
    if AUDIO_NORMALIZE:
        chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    chain.append("afade=t=in:d=0.04")
    if duration > 0.5:
        chain.append(f"afade=t=out:st={max(0.0, duration - 0.12):.3f}:d=0.12")
    return ",".join(chain)


_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def _write_ass(ass_path: Path, words: list[dict], title: str, duration: float,
               w: int, h: int, y: float | None = None) -> None:
    ass_path.write_text(
        captions.build_ass(words, title=title, duration=duration, w=w, h=h, y=y),
        encoding="utf-8-sig")


def _segment_lines(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Segment-level cues inside the clip window, rebased to t=0 (no word timing)."""
    out = []
    for s in segments:
        s_start = max(float(s["start"]), clip_start)
        s_end = min(float(s["end"]), clip_end)
        if s_end - s_start >= 0.3:
            out.append({"start": s_start - clip_start, "end": s_end - clip_start,
                        "text": s["text"]})
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
    layout: str | None = None,
) -> Path:
    """Trim *source* to [clip_start, clip_end], reframe to 9:16, burn captions.

    ``segments`` should be the full-video whisper segment list; timestamps are
    clipped and rebased automatically. Framing is face-aware (see
    ``ttclips.framing``); ``layout`` overrides ``CLIP_LAYOUT`` for this clip.
    """
    if not source.exists():
        raise FileNotFoundError(source)
    if SNAP_TO_WORDS:
        clip_start, clip_end = captions.snap_bounds(segments, clip_start, clip_end)
    duration = clip_end - clip_start

    info = probe_video(source)
    plan = framing.plan_framing(source, clip_start, duration, info["width"], info["height"],
                                aspect_w, aspect_h, layout)

    words = captions.clip_words(segments, clip_start, clip_end)
    if not words:
        words = captions.words_from_lines(_segment_lines(segments, clip_start, clip_end))
    ass_path = out.parent / (out.stem + ".ass")
    _write_ass(ass_path, words, title, duration, aspect_w, aspect_h,
               y=framing.caption_anchor(plan))

    graph = (
        framing.build_video_filter(plan, info["width"], info["height"], aspect_w, aspect_h)
        + f";[framed]{_subtitles_filter(ass_path)}[v]"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-ss", f"{clip_start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-af", _audio_filter(duration),
        *_video_args(info["fps"]), *_AUDIO_ARGS,
        str(out),
    ]
    _run(cmd)
    log.info("Clip produced: %s (%s layout)", out, plan.layout)
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
        lines.append({"start": round(t, 2), "end": round(t + dur, 2), "text": s})
        t += dur
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

    ``title`` becomes the hook card. ``script`` is only used as a fallback
    caption source when the narration cannot be transcribed. Prefers the given
    ``background`` b-roll clip (dimmed, 9:16, narration audio only); falls back
    to an animated gradient backplate when no background is available or
    rendering it fails.
    """
    if duration_sec is None:
        duration_sec = probe_duration(narration_mp3) or 45.0

    ass_path = out.parent / (out.stem + ".ass")
    segments = transcribe(narration_mp3)
    words = captions.clip_words(segments, 0.0, duration_sec)
    if not words:
        words = captions.words_from_lines(
            _segment_lines(segments, 0.0, duration_sec)
            or _script_to_timed_lines(script, duration_sec))
    _write_ass(ass_path, words, title, duration_sec, 1080, 1920)

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
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,setsar=1,"
        "eq=brightness=-0.16:contrast=1.05:saturation=1.1,vignette=PI/5,"
        f"{_subtitles_filter(ass_path)}[v]"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-stream_loop", "-1", "-i", str(background),
        "-i", str(narration_mp3),
        "-filter_complex", vf,
        "-map", "[v]", "-map", "1:a",
        "-af", _audio_filter(duration_sec),
        "-t", f"{duration_sec:.3f}",
        *_video_args(30), *_AUDIO_ARGS,
        str(out),
    ]
    _run(cmd)
    log.info("Generated clip (b-roll): %s", out)
    return out


def _render_gradient(narration_mp3: Path, ass_path: Path, out: Path, duration_sec: float) -> Path:
    """Fallback backplate: animated gradients, then plain colour."""
    tail = ["-af", _audio_filter(duration_sec), *_video_args(30), *_AUDIO_ARGS,
            "-shortest", str(out)]
    grad_input = (
        f"gradients=s=1080x1920:c0=0x1e3c72:c1=0x2a5298:c2=0x0f2027:"
        f"speed=0.04:d={duration_sec}:r=30"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", grad_input,
        "-i", str(narration_mp3),
        "-filter_complex", f"[0:v]{_subtitles_filter(ass_path)}[v]",
        "-map", "[v]", "-map", "1:a",
        *tail,
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
        "-vf", _subtitles_filter(ass_path),
        *tail,
    ]
    _run(cmd_fb)
    log.info("Generated clip (fallback colour): %s", out)
    return out
