"""Scaffold a ttclips home directory: ``config/`` + ``.env.example``.

Default templates ship inside the package (``ttclips/templates``) so an
installed CLI can bootstrap a fresh working folder:

    ttclips --init --yes               # uses $TTCLIPS_HOME or the CWD
    TTCLIPS_HOME=/path/to/proj ttclips --init --yes
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ttclips.config import HOME

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

CONFIG_FILES = ("channels.json", "posting_window.json", "tiktok.json")

OWNERSHIP_NOTICE = (
    "ttclips posts content to TikTok automatically. Only configure channels you "
    "OWN or have rights to repost. Clipping content you don't own may violate "
    "YouTube's Terms of Service, TikTok's Terms of Service, and creators' "
    "rights. See https://github.com/kamzysert3/ttclips/blob/main/docs/LEGAL.md"
)

ACKNOWLEDGEMENT = (
    "Acknowledged: you own (or have rights to) the channels you configure. "
    "See docs/LEGAL.md before adding new channels."
)


def require_ownership_ack(yes: bool) -> bool:
    """Ownership guardrail for ``--init``.

    Prints the ownership notice; scaffolding proceeds only when ``yes`` is
    True. Returns whether scaffolding may continue.
    """
    print(OWNERSHIP_NOTICE)
    if not yes:
        print("Aborting: --init requires --yes to acknowledge content ownership.", file=sys.stderr)
        return False
    print(ACKNOWLEDGEMENT)
    return True


def init_home(home: Path | None = None, *, force: bool = False) -> list[Path]:
    """Copy default config + ``.env.example`` into *home*.

    Returns the list of paths that were created. Existing files are left
    untouched unless ``force`` is set, so re-running is safe.
    """
    target = Path(home) if home is not None else HOME
    config_dir = target / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    for name in CONFIG_FILES:
        src = TEMPLATES_DIR / name
        dst = config_dir / name
        if dst.exists() and not force:
            continue
        shutil.copyfile(src, dst)
        created.append(dst)

    env_example = target / ".env.example"
    if not env_example.exists() or force:
        shutil.copyfile(TEMPLATES_DIR / "env.example", env_example)
        created.append(env_example)

    return created


def main() -> None:  # pragma: no cover - thin CLI wrapper
    import argparse

    parser = argparse.ArgumentParser(prog="ttclips-init", description="Scaffold ttclips config files")
    parser.add_argument("--home", type=str, default="", help="Target directory (default: $TTCLIPS_HOME or CWD)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--yes", action="store_true",
                        help="Acknowledge you own (or have rights to) the content you will repost")
    args = parser.parse_args()

    if not require_ownership_ack(args.yes):
        sys.exit(1)
    created = init_home(args.home or None, force=args.force)
    for path in created:
        print(f"created {path}")
    if not created:
        print("nothing to do (files already exist; use --force to overwrite)")


if __name__ == "__main__":
    main()
