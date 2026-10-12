"""Scaffold a ttclips home directory: ``config/`` + ``.env.example``.

Default templates ship inside the package (``ttclips/templates``) so an
installed CLI can bootstrap a fresh working folder:

    ttclips --init                        # uses $TTCLIPS_HOME or the CWD
    TTCLIPS_HOME=/path/to/proj ttclips --init
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ttclips.config import HOME

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

CONFIG_FILES = ("channels.json", "posting_window.json", "tiktok.json")


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
    args = parser.parse_args()

    created = init_home(args.home or None, force=args.force)
    for path in created:
        print(f"created {path}")
    if not created:
        print("nothing to do (files already exist; use --force to overwrite)")


if __name__ == "__main__":
    main()
