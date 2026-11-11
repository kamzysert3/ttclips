"""Interactive caption review before each upload (``ttclips --review``).

For every clip and platform the user sees the title and hashtags pre-filled,
edits them in place (Enter keeps the line), checks a preview of exactly what
will be posted, then presses Enter to upload:

    ── Review · TikTok ──────────────────────────────
    Clip  abc123_10_40.mp4  (30.0s)
    Title     › This is the best moment ever
    Hashtags  › #funny #fyp #viral
    ── Will post ──
    This is the best moment ever

    #funny #fyp #viral
    (48/2200 chars)
    [Enter] upload  [e] edit  [p] play clip  [s] skip  [q] quit ›

In-place editing uses prompt_toolkit when it is installed (ships with
ttclips); otherwise each field shows its current value and an empty answer
keeps it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

SKIPPED = "__skipped__"  # remote_id sentinel: the user skipped this upload

PLATFORM_LABELS = {"tiktok": "TikTok", "youtube": "YouTube Shorts"}
CAPTION_LIMITS = {"tiktok": 2200, "youtube": 100}  # tiktok: caption; youtube: title


class ReviewAbort(KeyboardInterrupt):
    """The user chose to quit from the review prompt. Subclasses
    KeyboardInterrupt so no ``except Exception`` in the pipeline swallows it."""


def is_interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def _prefilled_input(label: str, value: str) -> str:
    """Edit *value* in place. Falls back to "empty keeps it" without prompt_toolkit."""
    try:
        from prompt_toolkit import prompt
    except ImportError:
        print(f"{label}{value}")
        answer = input(f"{' ' * (len(label) - 2)}› ")
        if answer.strip() == "-":
            return ""
        return answer if answer.strip() else value
    return prompt(label, default=value)


def play_clip(path: Path) -> None:
    """Open the clip in the system's default video player."""
    path = Path(path)
    if not path.exists():
        print(f"  (clip not found: {path})")
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        print(f"  (could not open player: {e})")


def _clip_label(path: Path, duration: float | None) -> str:
    name = Path(path).name
    return f"{name}  ({duration:.1f}s)" if duration else name


def review_post(platform: str, title: str, hashtags: str, clip_path: Path, *,
                preview: Callable[[str, str], str],
                duration: float | None = None,
                edit: Callable[[str, str], str] = _prefilled_input,
                ask: Callable[[str], str] = input,
                out: Callable[[str], None] = print,
                player: Callable[[Path], None] = play_clip) -> tuple[str, str] | None:
    """Let the user edit and approve one upload.

    ``preview(title, hashtags)`` renders exactly what will be posted. Returns
    the approved ``(title, hashtags)``, ``None`` to skip this upload, or
    raises :class:`ReviewAbort` to stop the whole run.
    """
    label = PLATFORM_LABELS.get(platform, platform)
    limit = CAPTION_LIMITS.get(platform)
    out("")
    out(f"── Review · {label} " + "─" * max(4, 46 - len(label)))
    out(f"Clip  {_clip_label(clip_path, duration)}")
    editing = True
    while True:
        if editing:
            title = " ".join(edit("Title     › ", title).split())
            hashtags = " ".join(edit("Hashtags  › ", hashtags).split())
            editing = False
        rendered = preview(title, hashtags)
        out("── Will post ──")
        out(rendered)
        if platform == "tiktok" and limit:
            n = len(rendered)
            out(f"({n}/{limit} chars{' — TOO LONG, will be cut' if n > limit else ''})")
        elif platform == "youtube" and limit and len(title) > limit:
            out(f"(title is {len(title)}/{limit} chars and will be cut)")
        choice = ask("[Enter] upload  [e] edit  [p] play clip  [s] skip  [q] quit › ")
        choice = choice.strip().lower()
        if choice == "":
            return title, hashtags
        if choice in ("e", "edit"):
            editing = True
        elif choice in ("p", "play"):
            player(clip_path)
        elif choice in ("s", "skip"):
            out(f"Skipped {label}.")
            return None
        elif choice in ("q", "quit"):
            raise ReviewAbort()
        else:
            out("Press Enter to upload, or e / p / s / q.")
