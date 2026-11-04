"""Platform target resolution (TikTok / YouTube Shorts).

The two targets are independent: each platform keeps its own per-day slot
tracking and its own dedup records. A source video stays in the queue until
*every* configured platform has it, so posting to one platform never blocks
the other.
"""
from __future__ import annotations

import logging

from ttclips.config import load_platforms_config

log = logging.getLogger(__name__)

PLATFORMS = ("tiktok", "youtube")


def resolve_targets(override: str = "") -> list[str]:
    """Resolve the platforms to post to.

    Precedence: CLI ``--target`` override > ``config/platforms.json`` >
    ``tiktok``. ``both`` (or ``all``) expands to every supported platform.
    """
    raw = (override or "").strip()
    if not raw:
        raw = str(load_platforms_config().get("default_target", "tiktok") or "tiktok")
    targets: list[str] = []
    for part in raw.lower().replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part in ("all", "both"):
            for platform in PLATFORMS:
                if platform not in targets:
                    targets.append(platform)
            continue
        if part not in PLATFORMS:
            log.warning("Unknown platform '%s' (ignored) — known: %s",
                        part, ", ".join(PLATFORMS))
            continue
        if part not in targets:
            targets.append(part)
    return targets or ["tiktok"]


def configured_targets() -> list[str]:
    """The platform set that must be satisfied before a source leaves the queue.

    Read from ``config/platforms.json`` only — a one-off ``--target`` run must
    not remove a source that another platform still owes.
    """
    return resolve_targets("")
