"""Test isolation: every ttclips path is resolved from $TTCLIPS_HOME at import
time, so point it at a throwaway directory *before* anything imports ttclips.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="ttclips-tests-"))
os.environ["TTCLIPS_HOME"] = str(_SESSION_HOME)
(_SESSION_HOME / "config").mkdir(exist_ok=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def state_files(tmp_path, monkeypatch):
    """Redirect state + queue + lock files into a per-test directory."""
    import ttclips.state as state

    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "posted.json")
    monkeypatch.setattr(state, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(state, "LOCK_FILE", tmp_path / ".state.lock")
    return tmp_path
