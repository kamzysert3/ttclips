from __future__ import annotations

import pytest

import ttclips.platforms as platforms
from ttclips import scaffold
from tests.conftest import REPO_ROOT


@pytest.mark.parametrize("override,cfg,expected", [
    ("", {}, ["tiktok"]),
    ("", {"default_target": "youtube"}, ["youtube"]),
    ("", {"default_target": "both"}, ["tiktok", "youtube"]),
    ("all", {"default_target": "youtube"}, ["tiktok", "youtube"]),
    ("youtube, tiktok", {}, ["youtube", "tiktok"]),
    ("tiktok;tiktok", {}, ["tiktok"]),
    ("instagram", {}, ["tiktok"]),            # unknown -> fallback
    ("instagram,youtube", {}, ["youtube"]),   # unknown dropped
    ("  YouTube ", {}, ["youtube"]),
])
def test_resolve_targets(monkeypatch, override, cfg, expected):
    monkeypatch.setattr(platforms, "load_platforms_config", lambda: cfg)
    assert platforms.resolve_targets(override) == expected


def test_configured_targets_ignores_cli_override(monkeypatch):
    monkeypatch.setattr(platforms, "load_platforms_config", lambda: {"default_target": "both"})
    assert platforms.configured_targets() == ["tiktok", "youtube"]


# ---------------------------------------------------------------- scaffold
def test_ownership_ack_required(capsys):
    assert scaffold.require_ownership_ack(False) is False
    assert "requires --yes" in capsys.readouterr().err
    assert scaffold.require_ownership_ack(True) is True


def test_legal_doc_linked_from_notice_exists():
    assert "docs/LEGAL.md" in scaffold.OWNERSHIP_NOTICE
    legal = REPO_ROOT / "docs" / "LEGAL.md"
    assert legal.exists(), "OWNERSHIP_NOTICE links to docs/LEGAL.md — keep it in the repo"
    assert "YouTube Shorts" in legal.read_text(encoding="utf-8")


def test_init_home_creates_and_is_idempotent(tmp_path):
    created = scaffold.init_home(tmp_path)
    names = {p.name for p in created}
    assert set(scaffold.CONFIG_FILES) | {".env.example"} == names
    (tmp_path / "config" / "channels.json").write_text("[]", encoding="utf-8")
    assert scaffold.init_home(tmp_path) == []
    assert (tmp_path / "config" / "channels.json").read_text(encoding="utf-8") == "[]"
    assert len(scaffold.init_home(tmp_path, force=True)) == len(names)


def test_templates_are_valid_json():
    import json
    for name in scaffold.CONFIG_FILES:
        json.loads((scaffold.TEMPLATES_DIR / name).read_text(encoding="utf-8"))
