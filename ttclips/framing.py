"""Smart 9:16 framing: face-aware reframing of landscape footage.

Instead of blindly centre-cropping, the clip window is sampled a few times a
second, faces are detected, and one of these layouts is chosen:

``track``  the speaker fills the frame; the crop follows them with a smoothed,
           shot-aware virtual camera (holds still when they barely move,
           snaps on scene cuts instead of sliding across them).
``stack``  gameplay with a small corner facecam: facecam on top, gameplay
           underneath (the classic streamer short).
``blur``   whole picture kept, floated over a blurred copy of itself.
``crop``   plain centre crop (already-vertical sources, or nothing to follow).

``plan_framing`` only *decides*; ``build_video_filter`` turns the decision
into an ffmpeg filter chain. OpenCV is optional — without it (or when
analysis fails) everything degrades to the fallback layout.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ttclips.binutils import ffmpeg_bin
from ttclips.config import load_env

log = logging.getLogger(__name__)
_ENV = load_env()

LAYOUTS = ("auto", "track", "stack", "blur", "crop")
CLIP_LAYOUT = _ENV.get("CLIP_LAYOUT", "auto").strip().lower()
# Layout used by "auto" when there is no face worth following.
CLIP_FALLBACK_LAYOUT = _ENV.get("CLIP_FALLBACK_LAYOUT", "crop").strip().lower()
# Share of the output height given to the facecam in the stacked layout.
STACK_SPLIT = float(_ENV.get("STACK_SPLIT", "0.36"))

ASSETS_DIR = Path(__file__).parent / "assets"
YUNET_MODEL = ASSETS_DIR / "face_detection_yunet_2023mar.onnx"

ANALYSIS_FPS = 4.0
ANALYSIS_WIDTH = 480
SCENE_THRESHOLD = 0.32  # ffmpeg scene score that counts as a hard cut

# Faces smaller than this (fraction of frame height) are background noise.
MIN_FACE_H = 0.06
# A face must be visible in at least this share of samples to drive framing.
MIN_PRESENCE = 0.35
# Facecam heuristics: small, parked, away from the middle of the frame.
FACECAM_MAX_H = 0.30
FACECAM_MAX_JITTER = 0.035
FACECAM_MIN_OFFSET = 0.17
# Virtual camera: ignore subject drift below this share of the source width.
PAN_DEADZONE = 0.05
SMOOTH_SECONDS = 1.6


@dataclass
class FramePlan:
    layout: str = "crop"
    # track: [(t_seconds, crop_left as a 0..1 fraction of the free travel)]
    keyframes: list[tuple[float, float]] = field(default_factory=list)
    # stack: facecam rectangle (x, y, w, h) as fractions of the source frame
    facecam: tuple[float, float, float, float] | None = None
    # stack: horizontal centre (0..1) of the gameplay crop
    game_center: float = 0.5
    reason: str = ""


# ---------------------------------------------------------------- sampling
def _sample_frames(source: Path, start: float, duration: float, width: int, height: int):
    """Yield BGR frames (numpy arrays) of the clip window at ANALYSIS_FPS."""
    import numpy as np

    cmd = [
        ffmpeg_bin() or "ffmpeg", "-v", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-an", "-vf", f"fps={ANALYSIS_FPS},scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    size = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def _scene_cuts(source: Path, start: float, duration: float) -> list[float]:
    """Frame-accurate hard-cut times (seconds from clip start)."""
    cmd = [
        ffmpeg_bin() or "ffmpeg", "-v", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-an", "-vf",
        f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',metadata=print:file=-",
        "-f", "null", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    cuts = []
    for line in out.splitlines():
        if "pts_time:" in line:
            try:
                t = float(line.rsplit("pts_time:", 1)[1].split()[0])
            except (ValueError, IndexError):
                continue
            if 0.2 < t < duration - 0.2:
                cuts.append(t)
    return sorted(cuts)


def _make_detector(width: int, height: int):
    """Return ``detect(frame) -> [(cx, cy, w, h)]`` in 0..1 frame fractions."""
    import cv2

    try:
        cv2.setLogLevel(2)  # errors only: silence dnn backend chatter
    except Exception:
        pass
    if YUNET_MODEL.exists() and hasattr(cv2, "FaceDetectorYN"):
        try:
            yn = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (width, height), 0.7, 0.3, 50)

            def detect_yunet(frame):
                _, faces = yn.detect(frame)
                if faces is None:
                    return []
                return [((f[0] + f[2] / 2) / width, (f[1] + f[3] / 2) / height,
                         f[2] / width, f[3] / height) for f in faces]

            return detect_yunet
        except Exception as e:  # pragma: no cover - depends on the cv2 build
            log.debug("YuNet unavailable (%s) — using Haar cascade", e)

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect_haar(frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = cascade.detectMultiScale(gray, 1.1, 6, minSize=(24, 24))
        return [((x + w / 2) / width, (y + h / 2) / height, w / width, h / height)
                for (x, y, w, h) in found]

    return detect_haar


# ---------------------------------------------------------------- pure logic
def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _dominant_track(samples: list[list[tuple]]) -> list[tuple | None]:
    """Pick, per sample, the face belonging to the most prominent person.

    Faces are greedily linked across samples by proximity; the track with the
    largest accumulated area wins. Returns one face (or None) per sample.
    """
    tracks: list[dict] = []
    for i, faces in enumerate(samples):
        for f in faces:
            if f[3] < MIN_FACE_H:
                continue
            best = None
            for tr in tracks:
                last_i, last = tr["last_i"], tr["last"]
                if last_i == i or i - last_i > 3 * ANALYSIS_FPS:
                    continue
                dist = abs(f[0] - last[0]) + abs(f[1] - last[1])
                if dist < max(0.12, 1.2 * last[2]) and (best is None or dist < best[0]):
                    best = (dist, tr)
            if best:
                tr = best[1]
            else:
                tr = {"hits": {}, "score": 0.0}
                tracks.append(tr)
            tr["hits"][i] = f
            tr["last_i"], tr["last"] = i, f
            tr["score"] += f[2] * f[3]
    if not tracks:
        return [None] * len(samples)
    top = max(tracks, key=lambda tr: tr["score"])
    return [top["hits"].get(i) for i in range(len(samples))]


def _shots(n: int, cuts: list[float]) -> list[tuple[int, int]]:
    """Split sample indices [0, n) into shots at the given cut times."""
    bounds = [0]
    for c in cuts:
        idx = int(c * ANALYSIS_FPS + 0.999)
        if bounds[-1] < idx < n:
            bounds.append(idx)
    bounds.append(n)
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def _fill_gaps(values: list[float | None], default: float) -> list[float]:
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    if not known:
        return [default] * len(values)
    out = []
    k = 0
    for i in range(len(values)):
        while k + 1 < len(known) and known[k + 1][0] <= i:
            k += 1
        i0, v0 = known[k]
        if i <= i0 or k + 1 >= len(known):
            out.append(v0)
        else:
            i1, v1 = known[k + 1]
            out.append(v0 + (v1 - v0) * (i - i0) / (i1 - i0))
    return out


def _smooth(values: list[float], window: int) -> list[float]:
    if window <= 1 or len(values) < 3:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - half): i + half + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker on (t, x): drop keyframes a straight pan covers."""
    if len(points) <= 2:
        return points
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, worst_d = 0, 0.0
    for i in range(1, len(points) - 1):
        t, x = points[i]
        line = x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x0
        if abs(x - line) > worst_d:
            worst, worst_d = i, abs(x - line)
    if worst_d <= tol:
        return [points[0], points[-1]]
    return _simplify(points[: worst + 1], tol)[:-1] + _simplify(points[worst:], tol)


def camera_path(centers: list[float | None], cuts: list[float], duration: float,
                crop_frac: float) -> list[tuple[float, float]]:
    """Turn per-sample subject centres (0..1 of source width) into keyframes.

    Returns ``[(t, pos)]`` where ``pos`` (0..1) is the crop's position along
    its free horizontal travel. Each shot is handled on its own so the camera
    never slides across a cut; two keyframes share a timestamp at every cut.
    """
    n = len(centers)
    if n == 0 or crop_frac >= 0.999:
        return [(0.0, 0.5)]
    travel = 1.0 - crop_frac
    keyframes: list[tuple[float, float]] = []
    carry = 0.5  # subject centre inherited by shots with no detection
    shots = _shots(n, cuts)
    for a, b in shots:
        vals = centers[a:b]
        if all(v is None for v in vals):
            path = [carry] * (b - a)
        else:
            path = _smooth(_fill_gaps(vals, carry), int(SMOOTH_SECONDS * ANALYSIS_FPS) | 1)
        if max(path) - min(path) < PAN_DEADZONE:
            path = [_median(path)] * len(path)
        carry = path[-1]
        t_start = 0.0 if a == 0 else next((c for c in cuts if int(c * ANALYSIS_FPS + 0.999) == a),
                                          a / ANALYSIS_FPS)
        t_end = duration if b == n else next((c for c in cuts if int(c * ANALYSIS_FPS + 0.999) == b),
                                             b / ANALYSIS_FPS)
        pts = []
        for j, cx in enumerate(path):
            t = min(max((a + j) / ANALYSIS_FPS, t_start), t_end)
            pos = min(1.0, max(0.0, (cx - crop_frac / 2) / travel))
            pts.append((t, pos))
        pts[0] = (t_start, pts[0][1])
        pts.append((t_end, pts[-1][1]))
        pts = [p for i, p in enumerate(pts) if i == 0 or p[0] > pts[i - 1][0] or i == len(pts) - 1]
        keyframes.extend(_simplify(pts, 0.012))
    return [(round(t, 3), round(p, 4)) for t, p in keyframes]


def _facecam_rect(face: tuple[float, float, float, float], src_aspect: float,
                  panel_aspect: float) -> tuple[float, float, float, float]:
    """Rectangle (x, y, w, h fractions) around a parked facecam face.

    Sized from the face (head + shoulders) and shaped to the panel's aspect so
    it scales without distortion; shifted to stay inside the frame.
    """
    cx, cy, _fw, fh = face
    h = min(1.0, fh * 2.5)
    w = h * panel_aspect / src_aspect
    if w > 1.0:
        w, h = 1.0, src_aspect / panel_aspect
    x = min(max(cx - w / 2, 0.0), 1.0 - w)
    y = min(max(cy - h * 0.46, 0.0), 1.0 - h)
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


def _border(strength, lo: int, hi: int, span: tuple[int, int], near_hi: bool) -> int | None:
    """Best static line between *lo* and *hi* along axis 1 of *strength*.

    ``strength[a, b]`` is how persistent the edge at position ``b`` is on
    row/column ``a``. A border must be present on most of ``span``. When
    several qualify, the one closest to the face wins.
    """
    a0, a1 = max(0, span[0]), min(strength.shape[0], span[1])
    lo, hi = max(0, lo), min(strength.shape[1], hi)
    if a1 - a0 < 4 or hi <= lo:
        return None
    cover = (strength[a0:a1, lo:hi] > 5.0).mean(axis=0)
    good = [i for i, c in enumerate(cover) if c >= 0.72]
    if not good:
        return None
    return lo + (good[-1] if near_hi else good[0])


def find_cam_box(edges, face: tuple[float, float, float, float]):
    """Locate the facecam overlay's rectangle around a parked face.

    ``edges`` is ``(dx, dy)``: time-averaged signed pixel gradients. Moving
    gameplay averages out to ~0 while the overlay's fixed border survives, so
    the nearest full-length static line on each side of the face is the
    border. Sides with no line fall back to the frame edge (overlay flush
    with it) or a head-and-shoulders guess. Returns (x0, y0, x1, y1) fractions.
    """
    import numpy as np

    dx, dy = np.abs(edges[0]), np.abs(edges[1])
    H, W = dx.shape[0], dy.shape[1]
    cx, cy, fw, fh = face[0] * W, face[1] * H, face[2] * W, face[3] * H
    rows = (int(cy - fh * 0.9), int(cy + fh * 0.9))
    cols = (int(cx - fw * 0.9), int(cx + fw * 0.9))

    def side(found, guess, limit):
        if found is not None:
            return float(found)
        return float(limit) if abs(guess - limit) < 0.6 * max(fw, fh) else float(guess)

    left = _border(dx, int(cx - 3.5 * fw), int(cx - 0.75 * fw), rows, near_hi=True)
    right = _border(dx, int(cx + 0.75 * fw), int(cx + 3.5 * fw), rows, near_hi=False)
    top = _border(dy.T, int(cy - 2.5 * fh), int(cy - 0.75 * fh), cols, near_hi=True)
    bottom = _border(dy.T, int(cy + 0.9 * fh), int(cy + 3.5 * fh), cols, near_hi=False)
    x0 = side(left, max(0.0, cx - 1.5 * fw), 0) + 2
    x1 = side(right, min(W, cx + 1.5 * fw), W) - 2
    y0 = side(top, max(0.0, cy - 1.1 * fh), 0) + 2
    y1 = side(bottom, min(H, cy + 1.7 * fh), H) - 2
    if x1 - x0 < 1.2 * fw or y1 - y0 < 1.2 * fh:
        return None
    return (x0 / W, y0 / H, x1 / W, y1 / H)


def fit_cam_rect(box: tuple[float, float, float, float], face: tuple[float, float, float, float],
                 src_aspect: float, panel_aspect: float) -> tuple[float, float, float, float]:
    """Largest panel-shaped rectangle inside the facecam *box*, framed on the face."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    want_h = w * src_aspect / panel_aspect  # height (as a fraction) matching full width
    if want_h <= h:
        ry = min(max(face[1] - want_h * 0.45, y0), y1 - want_h)
        return (round(x0, 4), round(ry, 4), round(w, 4), round(want_h, 4))
    want_w = h * panel_aspect / src_aspect
    rx = min(max(face[0] - want_w / 2, x0), x1 - want_w)
    return (round(rx, 4), round(y0, 4), round(want_w, 4), round(h, 4))


def decide(samples: list[list[tuple]], cuts: list[float], duration: float,
           src_w: int, src_h: int, out_w: int, out_h: int,
           layout: str = "auto", edges=None) -> FramePlan:
    """Choose a layout from detected faces (pure function; unit-tested)."""
    fallback = CLIP_FALLBACK_LAYOUT if CLIP_FALLBACK_LAYOUT in ("crop", "blur") else "crop"
    src_aspect = src_w / src_h
    crop_frac = (out_w / out_h) / src_aspect  # share of source width a 9:16 crop keeps
    if crop_frac >= 0.97:
        return FramePlan("crop", reason="source is already vertical")
    if layout in ("crop", "blur"):
        return FramePlan(layout, reason="forced")

    # Follow the most prominent person *per shot*: after a cut it is usually
    # someone (or somewhere) else.
    track: list[tuple | None] = []
    for a, b in _shots(len(samples), cuts):
        track.extend(_dominant_track(samples[a:b]))
    hits = [f for f in track if f is not None]
    presence = len(hits) / len(track) if track else 0.0
    if presence < MIN_PRESENCE:
        if layout == "auto":
            return FramePlan(fallback, reason=f"no steady face ({presence:.0%} of samples)")
        if layout == "stack":
            return FramePlan(fallback, reason="stack needs a facecam; none found")
        return FramePlan("track", keyframes=[(0.0, 0.5)], reason="no face; centred")

    med = tuple(_median([f[k] for f in hits]) for k in range(4))
    jitter = max(_median([abs(f[0] - med[0]) for f in hits]),
                 _median([abs(f[1] - med[1]) for f in hits]))
    off_centre = max(abs(med[0] - 0.5), abs(med[1] - 0.5))
    is_facecam = (med[3] <= FACECAM_MAX_H and jitter <= FACECAM_MAX_JITTER
                  and off_centre >= FACECAM_MIN_OFFSET and presence >= 0.6)

    if layout == "stack" or (layout == "auto" and is_facecam):
        panel_aspect = out_w / (out_h * STACK_SPLIT)
        box = find_cam_box(edges, med) if edges is not None else None
        rect = (fit_cam_rect(box, med, src_aspect, panel_aspect) if box
                else _facecam_rect(med, src_aspect, panel_aspect))
        cam_lo, cam_hi = (box[0], box[2]) if box else (rect[0], rect[0] + rect[2])
        # Gameplay panel: centre crop, nudged away from the facecam if needed.
        game_frac = (out_w / (out_h * (1 - STACK_SPLIT))) / src_aspect
        game_center = 0.5
        if game_frac < 1.0:
            lo, hi = 0.5 - game_frac / 2, 0.5 + game_frac / 2
            if cam_lo < hi and cam_hi > lo:  # facecam bleeds into the centre crop
                if med[0] >= 0.5:
                    game_center = max(game_frac / 2, cam_lo - game_frac / 2)
                else:
                    game_center = min(1 - game_frac / 2, cam_hi + game_frac / 2)
        return FramePlan("stack", facecam=rect, game_center=round(game_center, 4),
                         reason=f"facecam at ({med[0]:.2f},{med[1]:.2f}), face {med[3]:.0%} tall")

    centers = [f[0] if f is not None else None for f in track]
    keys = camera_path(centers, cuts, duration, crop_frac)
    moving = len({p for _, p in keys}) > 1
    return FramePlan("track", keyframes=keys,
                     reason=f"face in {presence:.0%} of samples, "
                            f"{'panning' if moving else 'static'} over {len(cuts) + 1} shot(s)")


# ---------------------------------------------------------------- entry points
def plan_framing(source: Path, start: float, duration: float, src_w: int, src_h: int,
                 out_w: int = 1080, out_h: int = 1920, layout: str | None = None) -> FramePlan:
    """Analyse the clip window and return how to frame it. Never raises."""
    layout = (layout or CLIP_LAYOUT).lower()
    if layout not in LAYOUTS:
        log.warning("Unknown CLIP_LAYOUT %r — using auto", layout)
        layout = "auto"
    if not src_w or not src_h:
        return FramePlan("crop", reason="unknown source size")
    if layout in ("crop", "blur") or (out_w / out_h) / (src_w / src_h) >= 0.97:
        return decide([], [], duration, src_w, src_h, out_w, out_h, layout)
    try:
        import cv2  # noqa: F401
    except ImportError:
        log.warning("opencv not installed — smart framing disabled "
                    "(pip install opencv-python-headless)")
        return FramePlan(CLIP_FALLBACK_LAYOUT if CLIP_FALLBACK_LAYOUT == "blur" else "crop",
                         reason="opencv missing")
    try:
        aw = ANALYSIS_WIDTH
        ah = max(2, int(round(aw * src_h / src_w / 2)) * 2)
        detect = _make_detector(aw, ah)
        import numpy as np
        samples, sum_dx, sum_dy = [], 0.0, 0.0
        for frame in _sample_frames(source, start, duration, aw, ah):
            samples.append(detect(frame))
            gray = frame.mean(axis=2, dtype=np.float32)
            sum_dx = sum_dx + np.diff(gray, axis=1, append=gray[:, -1:])
            sum_dy = sum_dy + np.diff(gray, axis=0, append=gray[-1:, :])
        if not samples:
            return FramePlan("crop", reason="no frames decoded")
        cuts = _scene_cuts(source, start, duration)
        plan = decide(samples, cuts, duration, src_w, src_h, out_w, out_h, layout,
                      edges=(sum_dx / len(samples), sum_dy / len(samples)))
    except Exception as e:
        log.warning("Framing analysis failed (%s) — centre crop", e)
        return FramePlan("crop", reason="analysis failed")
    log.info("Framing: %s (%s)", plan.layout, plan.reason)
    return plan


def _pan_expr(keyframes: list[tuple[float, float]], travel_px: int) -> str:
    """ffmpeg expression for the crop's x over time (piecewise linear)."""
    if not keyframes:
        return str(travel_px // 2)
    px = [(t, round(p * travel_px, 1)) for t, p in keyframes]
    if len({x for _, x in px}) == 1:
        return str(int(px[0][1]))
    terms = [f"lt(t,{px[0][0]})*{px[0][1]}"] if px[0][0] > 0 else []
    for (t0, x0), (t1, x1) in zip(px, px[1:]):
        if t1 <= t0:
            continue  # zero-length segment = hard cut
        if x0 == x1:
            terms.append(f"gte(t,{t0})*lt(t,{t1})*{x0}")
        else:
            slope = round((x1 - x0) / (t1 - t0), 3)
            terms.append(f"gte(t,{t0})*lt(t,{t1})*({x0}+{slope}*(t-{t0}))")
    terms.append(f"gte(t,{px[-1][0]})*{px[-1][1]}")
    return "+".join(terms)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def build_video_filter(plan: FramePlan, src_w: int, src_h: int,
                       out_w: int = 1080, out_h: int = 1920,
                       src: str = "[0:v]", dst: str = "[framed]") -> str:
    """filter_complex fragment mapping *src* to a framed out_w x out_h *dst*."""
    cover = (f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={out_w}:{out_h}")
    if plan.layout == "track" and src_w and src_h:
        scaled_w = _even(src_w * out_h / src_h)
        travel = scaled_w - out_w
        if travel > 0:
            x = _pan_expr(plan.keyframes, travel)
            sharpen = ",unsharp=5:5:0.5:5:5:0.0" if out_h / src_h > 1.5 else ""
            return (f"{src}scale={scaled_w}:{out_h}:flags=lanczos,"
                    f"crop={out_w}:{out_h}:x='{x}':y=0{sharpen},setsar=1{dst}")
    if plan.layout == "stack" and plan.facecam and src_w and src_h:
        top_h = _even(out_h * STACK_SPLIT)
        bot_h = out_h - top_h
        fx, fy, fw, fh = plan.facecam
        cw, ch = _even(fw * src_w), _even(fh * src_h)
        cx = int(min(max(fx * src_w, 0), src_w - cw))
        cy = int(min(max(fy * src_h, 0), src_h - ch))
        game_w = _even(src_w * bot_h / src_h)
        gx = int(min(max(plan.game_center * game_w - out_w / 2, 0), max(0, game_w - out_w)))
        return (
            f"{src}split=2[fr_a][fr_b];"
            f"[fr_a]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={out_w}:{top_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={out_w}:{top_h}[fr_cam];"
            f"[fr_b]scale={game_w}:{bot_h}:flags=lanczos,"
            f"crop={min(out_w, game_w)}:{bot_h}:{gx}:0,"
            f"scale={out_w}:{bot_h}:force_original_aspect_ratio=increase,crop={out_w}:{bot_h}[fr_game];"
            f"[fr_cam][fr_game]vstack=inputs=2,"
            f"drawbox=x=0:y={top_h - 3}:w={out_w}:h=6:color=black@0.9:t=fill,setsar=1{dst}"
        )
    if plan.layout == "blur":
        return (
            f"{src}split=2[fr_a][fr_b];"
            f"[fr_a]scale={out_w // 4}:{out_h // 4}:force_original_aspect_ratio=increase,"
            f"crop={out_w // 4}:{out_h // 4},gblur=sigma=12,eq=brightness=-0.12:saturation=1.15,"
            f"scale={out_w}:{out_h}:flags=bicubic[fr_bg];"
            f"[fr_b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fr_fg];"
            f"[fr_bg][fr_fg]overlay=(W-w)/2:(H-h)/2,setsar=1{dst}"
        )
    return f"{src}{cover},setsar=1{dst}"


def caption_anchor(plan: FramePlan) -> float | None:
    """Preferred caption centre (fraction of height) for a layout, if any.

    The stacked layout puts captions on the seam between facecam and gameplay.
    """
    if plan.layout == "stack":
        return STACK_SPLIT + 0.035
    return None
