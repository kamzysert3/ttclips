"""Verify the release tag matches the declared version before publishing.

Usage:
    python .github/scripts/check_release.py v0.1.0

Exits non-zero when the Git tag, `pyproject.toml` and `ttclips/__init__.py`
disagree, so a mismatched tag can never make it to PyPI.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    raw_tag = sys.argv[1] if len(sys.argv) > 1 else ""
    tag = raw_tag.removeprefix("v")

    with (ROOT / "pyproject.toml").open("rb") as fh:
        pyproject_version = tomllib.load(fh)["project"]["version"]

    init_text = (ROOT / "ttclips" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init_text)
    init_version = match.group(1) if match else ""

    problems = []
    if tag != pyproject_version:
        problems.append(f"Git tag '{raw_tag}' != pyproject.toml version '{pyproject_version}'")
    if tag != init_version:
        problems.append(f"Git tag '{raw_tag}' != ttclips/__init__.py __version__ '{init_version}'")
    if pyproject_version != init_version:
        problems.append(f"pyproject.toml '{pyproject_version}' != ttclips/__init__.py '{init_version}'")

    if problems:
        print("Version mismatch(es):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: tag {raw_tag} == pyproject.toml {pyproject_version} == __init__.py {init_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())