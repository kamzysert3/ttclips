"""Caption phrasing/timing and smart-framing decisions (no ffmpeg, no cv2)."""
from __future__ import annotations

from ttclips import captions, framing


def _words(*items):
    return [{"word": w, "start": s, "end": e} for w, s, e in items]


# ---------------------------------------------------------------- captions
def test_clip_words_rebases_and_glues_subword_tokens():
    segs = [{"start": 10, "end": 12, "text": "", "words": [
        {"word": " It", "start": 10.0, "end": 10.2},
        {"word": "'s", "start": 10.2, "end": 10.3},
        {"word": " fine", "start": 10.4, "end": 10.8},
        {"word": " late", "start": 30.0, "end": 30.5},
    ]}]
    words = captions.clip_words(segs, 10.0, 12.0)
    assert [w["word"] for w in words] == ["It's", "fine"]
    assert words[0]["start"] == 0.0 and words[0]["end"] == 0.3


def test_phrases_are_short_uppercase_and_break_on_punctuation_and_pauses():
    style = captions.PRESETS["pop"]
    words = _words(("Oh,", 0.0, 0.2), ("my", 0.2, 0.4), ("God.", 0.4, 0.8),
                   ("There", 0.9, 1.1), ("has", 1.1, 1.3), ("to", 1.3, 1.4), ("be", 1.4, 1.6),
                   ("wait", 3.0, 3.3))
    phrases = captions.group_phrases(words, style)
    texts = [" ".join(w["text"] for w in p["words"]) for p in phrases]
    assert texts == ["OH MY GOD", "THERE HAS TO", "BE", "WAIT"]
    assert all(len(p["words"]) <= style.max_words for p in phrases)


def test_phrase_timing_never_overlaps_and_clears_after_a_pause():
    phrases = captions.group_phrases(
        _words(("a", 0.0, 0.3), ("b", 0.3, 0.6), ("c", 0.6, 0.9), ("d", 0.95, 1.2),
               ("e", 5.0, 5.3)), captions.PRESETS["pop"])
    for prev, nxt in zip(phrases, phrases[1:]):
        assert prev["end"] <= nxt["start"]
    # the phrase before the long pause lingers briefly, then clears
    assert phrases[-2]["end"] <= 1.2 + captions.LINGER + 0.01


def test_split_lines_balances_two_lines():
    assert captions.split_lines(["HI", "THERE"], 13) == [[0, 1]]
    assert captions.split_lines(["ABSOLUTELY", "INSANE", "PLAY"], 13) == [[0], [1, 2]]


def test_build_ass_has_hook_highlight_and_safe_zone_position():
    ass = captions.build_ass(
        _words(("Hello", 0.0, 0.4), ("world", 0.4, 0.9)),
        title="A {tricky} hook", duration=5.0)
    assert "Style: Caption,Montserrat Black" in ass
    assert ",Hook,," in ass and "{tricky}" not in ass  # braces can't inject ASS tags
    assert "\\pos(540,1305)" in ass                     # 68% down: clear of platform UI
    lines = [ln for ln in ass.splitlines() if ",Caption,," in ln]
    assert len(lines) == 2                               # one event per spoken word
    assert lines[0].split(",")[2] == lines[1].split(",")[1]  # contiguous, no flicker
    assert "\\c&H00E6FF&" in lines[0]


def test_style_presets_and_env_overrides():
    style = captions.load_style({"CAPTION_STYLE": "clean", "CAPTION_FONT_SIZE": "80",
                                 "HIGHLIGHT_COLOUR": "#00FF00", "CAPTION_Y": "0.5"})
    assert (style.size, style.uppercase, style.y) == (80, False, 0.5)
    assert captions._colour(style.highlight_colour) == "&H00FF00&"
    assert captions._colour("#FF8800") == "&H0088FF&"
    assert captions._colour("0000FFFF") == "&H00FFFF&"   # legacy ASS BGR still accepted
    assert captions.load_style({"CAPTION_STYLE": "nope"}) == captions.PRESETS["pop"]


def test_words_from_lines_spreads_time_over_words():
    words = captions.words_from_lines([{"start": 0.0, "end": 2.0, "text": "one three"}])
    assert [w["word"] for w in words] == ["one", "three"]
    assert words[0]["end"] == words[1]["start"] and words[1]["end"] == 2.0


def test_snap_bounds_moves_onto_word_edges_only():
    segs = [{"start": 9, "end": 21, "text": "", "words": [
        {"word": " before", "start": 9.0, "end": 9.6},
        {"word": " Hello", "start": 10.3, "end": 10.7},
        {"word": " there", "start": 19.6, "end": 20.2},
        {"word": " next", "start": 20.3, "end": 20.8},
    ]}]
    start, end = captions.snap_bounds(segs, 10.0, 20.0)
    assert start == 10.18          # just before "Hello"
    assert 20.2 < end <= 20.3      # finishes "there", stops before "next"
    assert captions.snap_bounds([{"start": 0, "end": 5, "text": "x", "words": None}], 1, 4) == (1, 4)


# ---------------------------------------------------------------- framing
def _face(cx, cy=0.4, h=0.4):
    return (cx, cy, h * 0.45, h)


def test_vertical_source_is_left_alone():
    plan = framing.decide([[_face(0.5)]] * 8, [], 2.0, 1080, 1920, 1080, 1920)
    assert plan.layout == "crop"


def test_talking_head_is_tracked_and_held_steady():
    samples = [[_face(0.30 + 0.005 * (i % 2))] for i in range(40)]
    plan = framing.decide(samples, [], 10.0, 1920, 1080, 1080, 1920)
    assert plan.layout == "track"
    positions = {p for _, p in plan.keyframes}
    assert len(positions) == 1            # jitter inside the deadzone: no pan at all
    assert 0.1 < positions.pop() < 0.3    # crop sits left, on the face


def test_tracking_snaps_at_scene_cuts_instead_of_sliding():
    samples = [[_face(0.25)]] * 20 + [[_face(0.75)]] * 20
    plan = framing.decide(samples, [5.0], 10.0, 1920, 1080, 1080, 1920)
    at_cut = [p for t, p in plan.keyframes if t == 5.0]
    assert len(at_cut) == 2 and at_cut[0] < 0.3 < 0.7 < at_cut[1]
    expr = framing._pan_expr(plan.keyframes, 2000)
    assert "gte(t,5.0)" in expr and "*(t-" not in expr   # two holds, no ramp


def test_slow_drift_becomes_a_smooth_pan():
    samples = [[_face(0.3 + 0.4 * i / 39)] for i in range(40)]
    plan = framing.decide(samples, [], 10.0, 1920, 1080, 1080, 1920)
    xs = [p for _, p in plan.keyframes]
    assert xs == sorted(xs) and xs[-1] - xs[0] > 0.3
    assert "*(t-" in framing._pan_expr(plan.keyframes, 2000)


def test_corner_facecam_gets_the_stacked_layout():
    samples = [[_face(0.86, 0.25, h=0.2)] for _ in range(40)]
    plan = framing.decide(samples, [], 10.0, 1280, 720, 1080, 1920)
    assert plan.layout == "stack"
    x, y, w, h = plan.facecam
    assert x <= 0.86 <= x + w and y <= 0.25 <= y + h and x + w <= 1.0
    graph = framing.build_video_filter(plan, 1280, 720)
    assert "vstack" in graph and graph.endswith("[framed]")
    assert framing.caption_anchor(plan) is not None


def test_no_face_falls_back_and_the_most_prominent_face_wins():
    assert framing.decide([[]] * 20, [], 5.0, 1920, 1080, 1080, 1920).layout == "crop"
    big, small = _face(0.3, h=0.4), _face(0.8, h=0.1)
    track = framing._dominant_track([[small, big]] * 10)
    assert all(f == big for f in track)


def test_forced_layouts_and_blur_graph():
    plan = framing.decide([], [], 5.0, 1920, 1080, 1080, 1920, layout="blur")
    assert plan.layout == "blur"
    assert "gblur" in framing.build_video_filter(plan, 1920, 1080)
    crop = framing.build_video_filter(framing.FramePlan("crop"), 1920, 1080)
    assert "crop=1080:1920" in crop
