"""Short-form caption rendering: whisper words -> animated ASS subtitles.

What makes these read as "edited" rather than auto-generated:

* short punchy phrases (2-3 words) broken on pauses and punctuation, never
  a wall of text; long phrases wrap into two balanced lines
* a heavy display font (bundled Montserrat Black), thick outline, soft shadow
* the spoken word is highlighted and pops; each phrase pops in
* phrases hold through tiny gaps (no flicker) and clear during real pauses
* placed in the platform-safe lower-middle band, clear of the UI chrome
* an optional hook card pinned at the top for the first seconds

Everything is tunable through ``.env`` (see ``.env.example``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from ttclips.config import load_env

_ENV = load_env()

FONTS_DIR = Path(__file__).parent / "assets"


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Montserrat Black"
    size: int = 92
    uppercase: bool = True
    max_words: int = 3          # words per phrase
    max_line_chars: int = 13    # characters per line before wrapping / shrinking
    max_lines: int = 2
    y: float = 0.68             # caption centre, fraction of frame height
    outline: float = 7
    shadow: float = 3
    text_colour: str = "#FFFFFF"
    highlight_colour: str = "#FFE600"
    outline_colour: str = "#000000"
    highlight: bool = True      # colour the word being spoken
    word_pop: int = 110         # % scale of the spoken word (100 = off)
    phrase_pop: bool = True     # phrases scale in instead of blinking on
    strip_punctuation: bool = True


PRESETS: dict[str, CaptionStyle] = {
    # Bold, uppercase, 2-3 words, spoken word pops in colour.
    "pop": CaptionStyle(),
    # One huge word at a time.
    "word": CaptionStyle(size=124, max_words=1, max_line_chars=10, max_lines=1,
                         highlight=False, word_pop=100, outline=9),
    # Calmer sentence-case lines for talky / educational content.
    "clean": CaptionStyle(size=68, uppercase=False, max_words=5, max_line_chars=20,
                          outline=5, shadow=2, word_pop=100, strip_punctuation=False,
                          highlight_colour="#5CE1FF"),
}

# Phrase timing (seconds)
PAUSE_BREAK = 0.45     # a silence this long always starts a new phrase
MAX_PHRASE_SEC = 2.4
LINGER = 0.30          # how long a phrase stays after its last word
LEAD_IN = 0.04         # show a phrase slightly before the word lands
MIN_WORD_SEC = 0.08


def _colour(value: str) -> str:
    """'#RRGGBB' (or legacy ASS 'AABBGGRR'/'BBGGRR' hex) -> ASS '&HBBGGRR&'."""
    v = str(value).strip()
    if v.startswith("#") and len(v) == 7:
        return f"&H{v[5:7]}{v[3:5]}{v[1:3]}&".upper()
    v = v.lstrip("&").lstrip("Hh").rstrip("&")
    return f"&H{v[-6:].upper().zfill(6)}&"


def _flag(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_style(env: dict | None = None) -> CaptionStyle:
    """Preset (CAPTION_STYLE) with individual .env overrides applied."""
    env = _ENV if env is None else env
    style = PRESETS.get(env.get("CAPTION_STYLE", "pop").strip().lower(), PRESETS["pop"])
    casts = {
        "font": ("CAPTION_FONT", str), "size": ("CAPTION_FONT_SIZE", int),
        "uppercase": ("CAPTION_UPPERCASE", _flag), "max_words": ("CAPTION_MAX_WORDS", int),
        "max_line_chars": ("CAPTION_MAX_LINE_CHARS", int), "y": ("CAPTION_Y", float),
        "outline": ("CAPTION_OUTLINE", float), "shadow": ("CAPTION_SHADOW", float),
        "text_colour": ("CAPTION_COLOUR", str), "highlight_colour": ("HIGHLIGHT_COLOUR", str),
        "outline_colour": ("CAPTION_OUTLINE_COLOUR", str),
        "highlight": ("CAPTION_HIGHLIGHT", _flag), "word_pop": ("CAPTION_WORD_POP", int),
    }
    overrides = {}
    for attr, (key, cast) in casts.items():
        raw = env.get(key, "")
        if raw == "":
            continue
        try:
            overrides[attr] = cast(raw)
        except ValueError:
            continue
    return replace(style, **overrides) if overrides else style


HOOK_SECONDS = float(_ENV.get("HOOK_SECONDS", "3.0"))  # 0 disables the hook card
HOOK_Y = float(_ENV.get("HOOK_Y", "0.15"))


# ---------------------------------------------------------------- words
_TRAIL_PUNCT = ".,;:!?…"


def clip_words(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Whisper word timestamps inside the clip window, rebased to t=0.

    Sub-word tokens whisper emits without a leading space ("'s", "-year") are
    glued back onto the previous word.
    """
    out: list[dict] = []
    for seg in segments:
        for w in seg.get("words") or []:
            raw = str(w.get("word", ""))
            text = raw.strip()
            if not text:
                continue
            start = max(float(w["start"]), clip_start)
            end = min(float(w["end"]), clip_end)
            if end - start < 0.03:
                continue
            start, end = start - clip_start, end - clip_start
            glue = out and not raw[:1].isspace() and (text[0] in "'’-" or not text[0].isalnum())
            if glue and start - out[-1]["end"] < 0.3:
                out[-1]["word"] += text
                out[-1]["end"] = round(end, 3)
                continue
            out.append({"word": text, "start": round(start, 3), "end": round(end, 3)})
    return out


def words_from_lines(lines: list[dict]) -> list[dict]:
    """Fake word timings for text that only has line-level timing.

    Each line's duration is shared between its words by length, so the same
    renderer (and look) works without real word timestamps.
    """
    out: list[dict] = []
    for line in lines:
        toks = str(line.get("text", "")).split()
        if not toks:
            continue
        start, end = float(line["start"]), float(line["end"])
        total = sum(len(t) + 1 for t in toks)
        t = start
        for tok in toks:
            dur = (end - start) * (len(tok) + 1) / total
            out.append({"word": tok, "start": round(t, 3), "end": round(t + dur, 3)})
            t += dur
    return out


def _display(word: str, style: CaptionStyle) -> str:
    text = word.strip()
    if style.strip_punctuation:
        text = text.strip("\"“”()[]").rstrip(".,;:…")
        text = re.sub(r"^[-–—]+|[-–—]+$", "", text)
    return text.upper() if style.uppercase else text


def group_phrases(words: list[dict], style: CaptionStyle) -> list[dict]:
    """Group words into caption phrases ``{start, end, words:[{text,start,end}]}``.

    A phrase ends at the word cap, at sentence punctuation, on a pause, when
    it would overflow its lines, or when it has been on screen long enough.
    """
    budget = style.max_line_chars * style.max_lines
    phrases: list[list[dict]] = []
    buf: list[dict] = []
    for w in words:
        text = _display(w["word"], style)
        if not text:
            continue
        item = {"text": text, "start": w["start"], "end": max(w["end"], w["start"] + MIN_WORD_SEC),
                "raw": w["word"].strip()}
        if buf:
            prev = buf[-1]
            chars = sum(len(b["text"]) + 1 for b in buf) + len(text)
            ended = prev["raw"][-1:] in ".!?…" or (prev["raw"][-1:] in ",;:" and len(buf) >= 2)
            if (len(buf) >= style.max_words or ended or chars > budget
                    or item["start"] - prev["end"] > PAUSE_BREAK
                    or item["end"] - buf[0]["start"] > MAX_PHRASE_SEC):
                phrases.append(buf)
                buf = []
        buf.append(item)
    if buf:
        phrases.append(buf)

    out = []
    for i, ph in enumerate(phrases):
        start = max(0.0, ph[0]["start"] - LEAD_IN)
        if out:
            start = max(start, min(out[-1]["end"], ph[0]["start"]))
        end = ph[-1]["end"] + LINGER
        if i + 1 < len(phrases):
            end = min(end, max(ph[-1]["end"], phrases[i + 1][0]["start"] - LEAD_IN))
        out.append({"start": round(start, 2), "end": round(max(end, start + MIN_WORD_SEC), 2),
                    "words": ph})
    return out


def split_lines(texts: list[str], max_chars: int, max_lines: int = 2) -> list[list[int]]:
    """Word indices per line: one line if it fits, else the most balanced 2."""
    idx = list(range(len(texts)))
    if max_lines < 2 or len(texts) < 2 or len(" ".join(texts)) <= max_chars:
        return [idx]
    best, best_cost = 1, None
    for cut in range(1, len(texts)):
        a, b = len(" ".join(texts[:cut])), len(" ".join(texts[cut:]))
        cost = max(a, b) + (0.5 if a > b else 0)  # ties: prefer the shorter top line
        if best_cost is None or cost < best_cost:
            best, best_cost = cut, cost
    return [idx[:best], idx[best:]]


# ---------------------------------------------------------------- ASS output
def _ts(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _header(style: CaptionStyle, w: int, h: int) -> str:
    text, outline = _colour(style.text_colour)[:-1], _colour(style.outline_colour)[:-1]
    cap = (f"Style: Caption,{style.font},{style.size},{text.replace('&H', '&H00')},"
           f"{text.replace('&H', '&H00')},{outline.replace('&H', '&H00')},&H96000000,"
           f"-1,0,0,0,100,100,1,0,1,{style.outline:g},{style.shadow:g},5,60,60,0,1")
    hook = (f"Style: Hook,{style.font},58,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,"
            f"-1,0,0,0,100,100,0,0,3,16,0,5,80,80,0,1")
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"{cap}\n{hook}\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )


def _phrase_events(phrase: dict, style: CaptionStyle, w: int, h: int, y: float) -> list[str]:
    words = phrase["words"]
    texts = [x["text"] for x in words]
    lines = split_lines(texts, style.max_line_chars, style.max_lines)
    longest = max(len(" ".join(texts[i] for i in ln)) for ln in lines)
    # Shrink (never wrap further) when a line is still too long.
    fit = min(100, int(100 * style.max_line_chars / longest)) if longest > style.max_line_chars else 100
    fit = max(fit, 55)
    pos = f"\\an5\\pos({w // 2},{int(h * y)})"
    hl = _colour(style.highlight_colour)
    base = _colour(style.text_colour)

    def body(active: int, pop: bool) -> str:
        rendered = []
        for ln in lines:
            parts = []
            for i in ln:
                t = _esc(texts[i])
                on, off = "", ""
                if i == active and style.highlight:
                    on, off = f"\\c{hl}", f"\\c{base}"
                if i == active and pop and style.word_pop != 100:
                    big = int(fit * style.word_pop / 100)
                    on += f"\\t(0,70,\\fscx{big}\\fscy{big})"
                    off += f"\\fscx{fit}\\fscy{fit}"
                parts.append(f"{{{on}}}{t}{{{off}}}" if on else t)
            rendered.append(" ".join(parts))
        return "\\N".join(rendered)

    # One event per spoken word: identical layout, different active word.
    spans = []
    for i, word in enumerate(words):
        t0 = phrase["start"] if i == 0 else max(phrase["start"], word["start"])
        t1 = phrase["end"] if i == len(words) - 1 else min(phrase["end"], words[i + 1]["start"])
        if spans and t0 < spans[-1][1]:
            t0 = spans[-1][1]
        if t1 - t0 < 0.02:
            continue
        spans.append((t0, t1, i))

    events = []
    for n, (t0, t1, i) in enumerate(spans):
        intro = ""
        if n == 0 and style.phrase_pop:
            lo = int(fit * 0.82)
            intro = f"\\fscx{lo}\\fscy{lo}\\t(0,90,\\fscx{fit}\\fscy{fit})"
        elif fit != 100:
            intro = f"\\fscx{fit}\\fscy{fit}"
        # The whole phrase is still scaling in during its first event, so the
        # first word is only coloured; later words get their own pop.
        text = f"{{{pos}{intro}}}{body(i, pop=n > 0)}"
        events.append(f"Dialogue: 1,{_ts(t0)},{_ts(t1)},Caption,,0,0,0,,{text}")
    return events


def _hook_event(title: str, w: int, h: int, duration: float) -> str | None:
    text = " ".join(str(title).split())
    if not text or HOOK_SECONDS <= 0:
        return None
    # Wrap to <= 3 lines of ~22 chars so the box stays inside the safe area.
    lines, cur = [], ""
    for tok in text.split():
        if cur and len(cur) + 1 + len(tok) > 22:
            lines.append(cur)
            cur = tok
        else:
            cur = f"{cur} {tok}".strip()
    lines.append(cur)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1].rstrip(".,!?") + "…"
    end = min(HOOK_SECONDS, max(0.5, duration))
    tags = (f"\\an5\\pos({w // 2},{int(h * HOOK_Y)})\\fad(120,220)"
            f"\\fscx90\\fscy90\\t(0,140,\\fscx100\\fscy100)")
    body = "\\N".join(_esc(x) for x in lines)
    return f"Dialogue: 2,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{{{tags}}}{body}"


def build_ass(words: list[dict], *, title: str = "", duration: float = 0.0,
              w: int = 1080, h: int = 1920, style: CaptionStyle | None = None,
              y: float | None = None) -> str:
    """Complete .ass document for a clip's words (already rebased to t=0)."""
    style = style or load_style()
    y = style.y if y is None else y
    out = [_header(style, w, h).rstrip("\n")]
    hook = _hook_event(title, w, h, duration or (words[-1]["end"] if words else 3.0))
    if hook:
        out.append(hook)
    for phrase in group_phrases(words, style):
        out.extend(_phrase_events(phrase, style, w, h, y))
    return "\n".join(out) + "\n"


def snap_bounds(segments: list[dict], start: float, end: float,
                *, lead: float = 0.12, tail: float = 0.30) -> tuple[float, float]:
    """Nudge clip bounds onto word boundaries so speech is never cut mid-word.

    The start moves to just before the first word that is (mostly) inside the
    window; the end to just after the last one, without spilling into the
    next word. Bounds move by at most ~1s; without word timestamps they are
    returned unchanged.
    """
    words = [w for s in segments for w in (s.get("words") or [])]
    if not words:
        return start, end
    new_start, new_end = start, end
    inside = [w for w in words
              if (float(w["start"]) + float(w["end"])) / 2 >= start
              and (float(w["start"]) + float(w["end"])) / 2 <= end]
    if not inside:
        return start, end
    first, last = inside[0], inside[-1]
    if abs(float(first["start"]) - start) <= 1.0:
        new_start = max(0.0, float(first["start"]) - lead)
    if abs(float(last["end"]) - end) <= 1.0:
        new_end = float(last["end"]) + tail
        after = [float(w["start"]) for w in words if float(w["start"]) >= float(last["end"]) - 0.01
                 and w is not last]
        if after:
            new_end = min(new_end, max(float(last["end"]) + 0.05, min(after) - 0.03))
    if new_end - new_start < 1.0:
        return start, end
    return round(new_start, 3), round(new_end, 3)
