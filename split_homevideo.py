#!/usr/bin/env python3
"""
Split a home video into logical clips by reading the burned-in timestamp.

Usage:
    python3 split_homevideo.py <input.mp4> [--interval 10] [--gap 300] [--out-dir ./clips]

Arguments:
    --interval  Seconds between sampled frames (default: 10)
    --gap       Time gap (seconds) between consecutive timestamps that
                triggers a new clip, even on the same date (default: 3600)
    --mode      Clip grouping mode: scene, session, or daily (default)
    --out-dir   Output directory for clips (default: <input>_clips/)
    --crop      ffmpeg crop string "w:h:x:y" for timestamp region
                (default tuned for 640x480 with bottom-right overlay)
    --dry-run   Print split points without cutting
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# --- defaults ---
DEFAULT_INTERVAL = 10          # sample every N seconds
DEFAULT_GAP = 3600             # 1-hour camera-time gap = new clip (empirically tuned; see golden_labels analysis)
DEFAULT_CROP = "250:110:385:370"  # w:h:x:y for 640x480 bottom-right timestamp
DEFAULT_MODE = "daily"
DEFAULT_SCENE_THRESHOLD = 0.4
DEFAULT_BLACK_MIN_DURATION = 0.1
DEFAULT_FUSE_WINDOW = 5.0      # seconds within which a visual signal corroborates an OCR boundary
DEFAULT_MIN_CLIP_S = 120.0     # clips shorter than this are merged into prior clip; validated on golden set

_CACHE_FORMAT = 2              # increment when cache schema changes; forces re-scan on old caches
_VISUAL_CACHE_FORMAT = 1
_MIN_GAP_S = 60               # minimum camera-time jump to emit a boundary (internal, not user-tunable)

OCR_BIN = Path(__file__).parent / "ocr_timestamp"
VIDEO_TIMESCALE = 29970  # matches source tbn; same on all segs/concat to prevent PTS mis-scaling
MIN_BOUNDARY_SEG = 0.05  # s; boundary re-encodes shorter than ~1 frame (29.97fps≈0.033s)
                         # produce zero video frames and corrupt the concat — skip them.

# OCR preprocessing filter chain (applied after fps filter in bulk scan, standalone in refinement).
# yadif: deinterlaces VHS comb artifacts; format=gray: removes color noise Vision ignores anyway;
# scale 4×: more glyph detail than 3×; unsharp: crisp edges post-scale; eq: harden contrast.
_VF_PREPROCESS = "yadif,format=gray,scale=iw*4:ih*4:flags=lanczos,unsharp=5:5:2.0,eq=contrast=2.0:brightness=0.05"
FRAMES_PER_SAMPLE = 3  # frames extracted per interval window; majority vote → fewer misreads

# ------------------------------------------------------------------ #
# Boundary detection types
# ------------------------------------------------------------------ #

@dataclass
class Boundary:
    video_t:    float               # video-file position of boundary
    type:       str                 # 'gap' | 'large_gap'
    cam_before: datetime | None     # last valid timestamp before boundary
    cam_after:  datetime | None     # first valid timestamp after boundary
    cam_jump_s: float               # (cam_after - cam_before).total_seconds(); negative = backward
    prev_t:     float | None        # video_t of last valid sample before (for refine_split)
    prev_dt:    datetime | None     # datetime of last valid sample before (for refine_split)


# ------------------------------------------------------------------ #
# Timestamp parsing
# ------------------------------------------------------------------ #

DATE_PATTERN = re.compile(
    r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})"
)
TIME_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?", re.IGNORECASE
)


def parse_timestamp(text: str) -> datetime | None:
    """Parse '5:01 PM / 1/ 4/90' style OCR output (may have noisy newlines)."""
    # Normalize: collapse newlines + multiple spaces so multi-line OCR joins up
    flat = re.sub(r"[\n\r]+", " ", text)
    flat = re.sub(r" +", " ", flat).strip()
    date_m = DATE_PATTERN.search(flat)
    time_m = TIME_PATTERN.search(flat)
    if not date_m:
        return None
    month, day, year = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
    # Reject implausible dates (month > 12, day > 31)
    if month > 12 or day > 31:
        return None
    if year < 100:
        year += 1900 if year >= 80 else 2000
    # Reject implausible years for home VHS footage (1985–2005)
    if not (1985 <= year <= 2005):
        return None
    # Require a real time parse — defaulting to 00:00 on failure causes huge false jumps
    if not time_m:
        return None
    hour, minute = int(time_m.group(1)), int(time_m.group(2))
    ampm = (time_m.group(3) or "").upper()
    if not ampm:
        return None
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


# ------------------------------------------------------------------ #
# Frame extraction + OCR
# ------------------------------------------------------------------ #

def extract_frame(video: str, t: float, crop: str, tmpdir: str) -> str | None:
    """Extract one cropped frame to a BMP; return path or None on failure."""
    frame_path = os.path.join(tmpdir, f"frame_{t:.3f}.bmp")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-hwaccel", "videotoolbox",
        "-ss", str(t),
        "-i", video,
        "-vframes", "1",
        "-vf", f"crop={crop},{_VF_PREPROCESS}",
        "-update", "1",
        "-y", frame_path,
    ]
    ret = subprocess.run(cmd, capture_output=True)
    if ret.returncode != 0 or not os.path.exists(frame_path):
        return None
    return frame_path


def extract_all_frames(video: str, interval: int, crop: str, tmpdir: str) -> list[str]:
    """Single-pass: decode video sequentially at 1/interval fps, crop each frame."""
    out_pattern = os.path.join(tmpdir, "frame_%06d.bmp")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", video,
        "-vf", f"fps={FRAMES_PER_SAMPLE}/{interval},crop={crop},{_VF_PREPROCESS}",
        "-start_number", "0",
        "-y", out_pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(glob.glob(os.path.join(tmpdir, "frame_*.bmp")))


def frame_index(path: str) -> int:
    m = re.search(r"frame_(\d+)\.bmp", os.path.basename(path))
    assert m is not None, f"unexpected frame filename: {path}"
    return int(m.group(1))


def ocr_batch(paths: list[str]) -> dict[str, str]:
    """Run ocr_timestamp once over all paths; return {path: raw_text}."""
    if not paths:
        return {}
    result = subprocess.run(
        [str(OCR_BIN)] + paths,
        capture_output=True, text=True,
    )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" in line:
            p, _, text = line.partition("\t")
            out[p] = text
    return out


# ------------------------------------------------------------------ #
# Main logic
# ------------------------------------------------------------------ #

def get_duration(video: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def scan(
    video: str, interval: int, crop: str, cache_path: str | None = None
) -> list[tuple[float, datetime | None]]:
    """Sample frames every `interval` seconds; return (t, parsed_dt) list.

    If cache_path is given, load from it when it exists (and matches interval+crop),
    otherwise scan and save results there for fast re-runs with different --gap values.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if (cached.get("cache_format") == _CACHE_FORMAT
                and cached.get("interval") == interval
                and cached.get("crop") == crop
                and cached.get("vf_preprocess") == _VF_PREPROCESS
                and cached.get("frames_per_sample", 1) == FRAMES_PER_SAMPLE):
            print(f"  (loaded from cache: {cache_path})")
            return [
                (float(t), parse_timestamp(text) if text else None)
                for t, text in cached["samples"]
            ]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: single-pass extraction
        print(f"  Extracting frames (single pass, {FRAMES_PER_SAMPLE} frames/{interval}s)...", flush=True)
        frame_paths = extract_all_frames(video, interval, crop, tmpdir)
        print(f"  Extracted {len(frame_paths)} frames.", flush=True)

        # Phase 2: batch OCR
        print(f"  Running OCR on {len(frame_paths)} frames (batch)...", flush=True)
        ocr_results = ocr_batch(frame_paths)

        # Phase 3: group frames by interval window, majority-vote on OCR reading
        interval_frames: dict[float, list[str]] = {}
        for path in frame_paths:
            t = float((frame_index(path) // FRAMES_PER_SAMPLE) * interval)
            interval_frames.setdefault(t, []).append(path)

        # (t, datetime|None, raw_text|None) — raw_text is stored in cache so
        # parse_timestamp fixes propagate without re-scanning.
        results: dict[float, tuple[datetime | None, str | None]] = {}
        for t, paths in interval_frames.items():
            frame_data = [(p, ocr_results.get(p, "")) for p in paths]
            parsed = [(p, text, parse_timestamp(text)) for p, text in frame_data]
            valid = [(p, text, dt) for p, text, dt in parsed if dt is not None]
            if not valid:
                results[t] = (None, None)
            else:
                winner_dt = Counter(dt for _, _, dt in valid).most_common(1)[0][0]
                winner_text = next(text for _, text, dt in valid if dt == winner_dt)
                results[t] = (winner_dt, winner_text)

    sorted_results = sorted(results.items())
    samples = [(t, dt) for t, (dt, _) in sorted_results]

    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _CACHE_FORMAT,
                "interval": interval,
                "crop": crop,
                "vf_preprocess": _VF_PREPROCESS,
                "frames_per_sample": FRAMES_PER_SAMPLE,
                "samples": [(t, text) for t, (_, text) in sorted_results],
            }, f)
        print(f"  (scan cached to {cache_path})")

    return samples


def filter_ocr_outliers(
    samples: list[tuple[float, datetime | None]],
    max_drift_s: float = 900,
    max_run: int = 3,
) -> list[tuple[float, datetime]]:
    """
    Remove isolated OCR misreads from the valid-reading list.

    A reading is kept if it is consistent (within max_drift_s) with EITHER its
    immediate OR extended (up to max_run steps) prev/next neighbor.  A real clip
    boundary fails the immediate-prev check but passes immediate-next.  A
    consecutive misread run fails all neighbors within the window and is dropped.
    """
    valid = [(t, dt) for t, dt in samples if dt is not None]
    if len(valid) < 3:
        return valid

    def drift(t_a: float, dt_a: datetime, t_b: float, dt_b: datetime) -> float:
        cam_adv = (dt_b - dt_a).total_seconds()
        vid_adv = t_b - t_a
        return abs(cam_adv - vid_adv)

    kept: list[tuple[float, datetime]] = [valid[0]]
    for i in range(1, len(valid) - 1):
        t, dt = valid[i]
        ok_prev = drift(*valid[i - 1], t, dt) < max_drift_s
        ok_next = drift(t, dt, *valid[i + 1]) < max_drift_s
        if ok_prev or ok_next:
            kept.append((t, dt))
            continue
        # Immediate neighbors inconsistent. Search extended window (handles
        # consecutive misread runs): drop only if nothing within max_run steps
        # is consistent either direction.
        far_prev = any(
            drift(*valid[j], t, dt) < max_drift_s
            for j in range(max(0, i - 1 - max_run), i - 1)
        )
        far_next = any(
            drift(t, dt, *valid[j]) < max_drift_s
            for j in range(i + 2, min(len(valid), i + 2 + max_run))
        )
        if far_prev or far_next:
            kept.append((t, dt))
            continue
        # Nothing in the extended window is consistent with this reading.
        # Check if any before-window reading is consistent with any after-window
        # reading — if so, this reading is noise in a bridgeable sequence and
        # should be dropped. If no such bridge exists, it may be a real boundary.
        look_back = range(max(0, i - 1 - max_run), i)
        look_fwd = range(i + 1, min(len(valid), i + 2 + max_run))
        bridgeable = any(
            drift(*valid[j], *valid[k]) < max_drift_s
            for j in look_back
            for k in look_fwd
        )
        if not bridgeable:
            kept.append((t, dt))
    kept.append(valid[-1])
    return kept


def find_all_boundaries(
    samples: list[tuple[float, datetime | None]],
    min_gap_s: int = _MIN_GAP_S,
    gap_s: int = DEFAULT_GAP,
) -> list["Boundary"]:
    """
    Stage 1: emit every candidate boundary at a low detection floor (min_gap_s).

    Returns Boundary objects sorted by video_t. Type 'large_gap' when the camera
    jump exceeds gap_s or is backward; 'gap' for smaller detected pauses.
    """
    clean = filter_ocr_outliers(samples)
    boundaries: list[Boundary] = []
    prev: tuple[float, datetime] | None = None
    for t, dt in clean:
        if prev is not None:
            prev_t, prev_dt = prev
            video_advance = t - prev_t
            cam_advance = (dt - prev_dt).total_seconds()
            jumped_forward = cam_advance > video_advance + min_gap_s
            jumped_backward = cam_advance < -1800
            if jumped_forward or jumped_backward:
                is_large = cam_advance > video_advance + gap_s or jumped_backward
                boundaries.append(Boundary(
                    video_t=t,
                    type="large_gap" if is_large else "gap",
                    cam_before=prev_dt,
                    cam_after=dt,
                    cam_jump_s=cam_advance,
                    prev_t=prev_t,
                    prev_dt=prev_dt,
                ))
        prev = (t, dt)
    return boundaries


def merge_short_clips(cuts: list[float], min_clip_s: float = DEFAULT_MIN_CLIP_S) -> list[float]:
    """
    Drop any cut that would produce a clip shorter than min_clip_s, merging it
    into the prior clip.

    A single hallucinated OCR reading (e.g. one bad frame reads "1999" or a
    wrong date) creates a jump-in boundary immediately followed by a revert-out
    boundary a few seconds later — both real per their own logic, but together
    they bracket a near-zero-length spurious clip. This collapse is often only
    visible after refine_split narrows each coarse boundary down to its precise
    1s transition point, so this must run on final splits, not just coarse cuts.
    """
    merged = [cuts[0]]
    for t in cuts[1:]:
        if t - merged[-1] < min_clip_s:
            continue
        merged.append(t)
    return merged


def _collapse_revert_phantoms(
    cuts: list[float],
    boundary_map: dict[float, "Boundary"],
    min_phantom_s: float = DEFAULT_MIN_CLIP_S,
) -> list[float]:
    """
    Collapse phantom clips in daily mode: a short clip whose bounding boundaries
    have opposite-sign cam_jump_s values is an OCR misread in a transition zone —
    the camera jumped forward to a hallucinated date then immediately reverted.

    Same-date violations (A→B→A with B being a misread year) and near-same-date
    violations (A→B→C where B is a wildly wrong month/year and A≈C) both trigger
    because in every such case one jump is large-positive and the next is
    large-negative. Genuine short clips on a new date are always bracketed by
    same-sign (both positive) jumps.
    """
    if len(cuts) <= 2:
        return cuts
    result = [cuts[0]]
    i = 1
    while i < len(cuts):
        if i + 1 < len(cuts):
            b1 = boundary_map.get(cuts[i])
            b2 = boundary_map.get(cuts[i + 1])
            clip_dur = cuts[i + 1] - cuts[i]
            if (b1 is not None and b2 is not None
                    and clip_dur < min_phantom_s
                    and b1.cam_jump_s * b2.cam_jump_s < 0):  # opposite-sign jumps
                i += 2  # skip both jump-in and revert-out cuts
                continue
        result.append(cuts[i])
        i += 1
    return result


def group_clips(boundaries: list["Boundary"], mode: str, gap_s: int) -> list[float]:
    """
    Stage 2: decide which boundaries become cut points based on mode.

    Returns list[float] of video_t values with 0.0 prepended.
      scene   — all boundaries (gap + large_gap)
      session — large_gap only
      daily   — only confirmed date changes or backward jumps; unknown-date
                boundaries are skipped (avoids splitting same-day footage)
    """
    cuts = [0.0]
    for b in boundaries:
        if mode == "scene":
            cuts.append(b.video_t)
        elif mode == "session":
            if b.type == "large_gap":
                cuts.append(b.video_t)
        elif mode == "daily":
            date_change = (
                b.cam_before is not None and b.cam_after is not None
                and b.cam_before.date() != b.cam_after.date()
            )
            if date_change:
                cuts.append(b.video_t)
    return cuts


_SCENE_RE = re.compile(r"Parsed_showinfo.*pts_time:([\d.]+)")
_BLACK_RE = re.compile(r"Parsed_blackdetect.*black_start:([\d.]+)")


def detect_visual_boundaries(
    video: str,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    black_min_duration: float = DEFAULT_BLACK_MIN_DURATION,
    cache_path: str | None = None,
) -> tuple[list[float], list[float]]:
    """
    Single ffmpeg decode pass: detect scene cuts and black frames independent of OCR.

    These are corroborating signals for fuse_boundaries() — an OCR-detected jump that
    coincides with a real scene cut or black frame (camera off/on) is far more likely
    to be a genuine boundary than an isolated OCR misread.

    Returns (scene_cut_times, black_frame_times), both sorted lists of video_t.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if (cached.get("cache_format") == _VISUAL_CACHE_FORMAT
                and cached.get("scene_threshold") == scene_threshold
                and cached.get("black_min_duration") == black_min_duration):
            return cached["scene_cuts"], cached["black_frames"]

    cmd = [
        "ffmpeg", "-loglevel", "info",
        "-i", video,
        "-vf", f"blackdetect=d={black_min_duration}:pic_th=0.98,"
               f"select='gt(scene,{scene_threshold})',showinfo",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)

    scene_cuts: list[float] = []
    black_frames: list[float] = []
    for line in r.stderr.splitlines():
        m = _SCENE_RE.search(line)
        if m:
            scene_cuts.append(float(m.group(1)))
            continue
        m = _BLACK_RE.search(line)
        if m:
            black_frames.append(float(m.group(1)))

    scene_cuts.sort()
    black_frames.sort()

    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _VISUAL_CACHE_FORMAT,
                "scene_threshold": scene_threshold,
                "black_min_duration": black_min_duration,
                "scene_cuts": scene_cuts,
                "black_frames": black_frames,
            }, f)

    return scene_cuts, black_frames


def fuse_boundaries(
    boundaries: list["Boundary"],
    scene_cuts: list[float],
    black_frames: list[float],
    window_s: float = DEFAULT_FUSE_WINDOW,
) -> list["Boundary"]:
    """
    Two-of-three voting: an OCR-detected boundary is kept only if corroborated by an
    independent visual signal (scene cut or black frame) somewhere in the actual
    transition window. OCR-only boundaries with no visual corroboration are dropped
    as likely misreads.

    b.video_t is the first OCR sample AFTER the jump, not the transition point itself
    — with OCR success well under 100%, the true cut can sit anywhere back to b.prev_t
    (the last confirmed sample before the jump). Search [prev_t, video_t] padded by
    window_s on both ends, rather than a fixed window around video_t alone.
    """
    visual_times = sorted(scene_cuts + black_frames)
    confirmed = []
    for b in boundaries:
        lo = (b.prev_t if b.prev_t is not None else b.video_t - window_s) - window_s
        hi = b.video_t + window_s
        if any(lo <= vt <= hi for vt in visual_times):
            confirmed.append(b)
    return confirmed


def refine_split(
    video: str,
    coarse_t: float,
    prev_t: float,
    prev_dt: datetime,
    gap_s: int,
    crop: str,
    tmpdir: str,
    visual_times: list[float] | None = None,
) -> tuple[float, str]:
    """
    Dense 1s scan of [prev_t+1, coarse_t) to find the transition point.

    Tracks the last frame confirming the OLD session and cuts there + 1s,
    rather than at the first NEW-session frame. This handles OCR returning None
    during the actual switch (camera motion, power-on noise) — those ambiguous
    frames belong to the old clip's tail, not the new clip's head.

    If the entire window is an OCR dead zone (all None) and visual_times is
    provided, falls back to the earliest visual signal (scene cut / black frame)
    in [prev_t, coarse_t] as the anchor rather than returning coarse_t unchanged.

    Returns (refined_t, method) where method is 'ocr', 'visual', or 'coarse'.
    """
    window = list(range(int(prev_t) + 1, int(coarse_t)))
    if not window:
        return coarse_t, "coarse"

    # Extract all frames in the refinement window in parallel, then batch OCR
    workers = (os.cpu_count() or 4) * 2
    paths: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_t = {
            executor.submit(extract_frame, video, float(t), crop, tmpdir): t
            for t in window
        }
        for future in as_completed(future_to_t):
            paths[future_to_t[future]] = future.result()

    valid_paths = [p for t in window if (p := paths.get(t)) is not None]
    ocr_results = ocr_batch(valid_paths)

    last_old_t: float = prev_t
    for t in window:
        path = paths.get(t)
        if path is None:
            continue
        text = ocr_results.get(path, "")
        dt = parse_timestamp(text) if text else None
        if dt is None:
            continue
        cam_advance = (dt - prev_dt).total_seconds()
        video_advance = float(t) - prev_t
        if cam_advance > video_advance + gap_s or cam_advance < -1800:
            return float(last_old_t) + 1.0, "ocr"
        else:
            last_old_t = t

    # Dense scan found nothing (OCR dead zone). Fall back to the earliest visual
    # signal in [prev_t, coarse_t] — VHS head-switch noise that killed OCR also
    # typically produces a black frame or scene cut at the true boundary.
    if visual_times:
        anchors = [vt for vt in visual_times if prev_t <= vt <= coarse_t]
        if anchors:
            return min(anchors), "visual"

    return coarse_t, "coarse"


def snap_to_keyframe(video: str, t: float, look_back: float = 30.0) -> float:
    """Return the last keyframe pts <= t by scanning [t-look_back, t+5]."""
    start = max(0.0, t - look_back)
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", f"{start:.3f}%+{look_back + 5:.0f}",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv",
        video,
    ], capture_output=True, text=True)
    best = start
    for line in r.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 3 and "K" in parts[-1]:
            try:
                pts = float(parts[1])
                if pts <= t:
                    best = pts
            except ValueError:
                pass
    return best


def snap_to_keyframe_forward(video: str, t: float, look_ahead: float = 30.0) -> float:
    """Return the first keyframe pts >= t."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", f"{t:.3f}%+{look_ahead:.0f}",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv",
        video,
    ], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 3 and "K" in parts[-1]:
            try:
                pts = float(parts[1])
                if pts >= t:
                    return pts
            except ValueError:
                pass
    return t


def _ffmpeg_copy_seg(video: str, seg_start: float, seg_end: float, out: str):
    subprocess.run([
        "ffmpeg", "-loglevel", "error",
        "-ss", f"{seg_start:.3f}",
        "-i", video,
        "-t", f"{seg_end - seg_start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy",
        "-video_track_timescale", str(VIDEO_TIMESCALE),
        "-avoid_negative_ts", "make_zero",
        "-y", out,
    ], check=True)


def _ffmpeg_encode_seg(video: str, seg_start: float, seg_end: float, out: str, crf: int):
    subprocess.run([
        "ffmpeg", "-loglevel", "error",
        "-ss", f"{seg_start:.3f}",
        "-i", video,
        "-t", f"{seg_end - seg_start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-video_track_timescale", str(VIDEO_TIMESCALE),
        "-avoid_negative_ts", "make_zero",
        "-y", out,
    ], check=True)


def cut_clip_with_boundary_encode(
    video: str,
    start: float,
    end: float,
    exact_start: float | None,
    exact_end: float | None,
    out_path: str,
    crf: int = 18,
):
    """
    Cut a clip, re-encoding only the small boundary segments around non-keyframe
    split points. Stream-copies everything else.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        segs: list[str] = []

        # Leading boundary: re-encode [exact_start, kf_after].
        # Body always starts on kf_after (a keyframe) so its stream-copy is clean.
        body_start = start
        if exact_start is not None:
            kf_after = snap_to_keyframe_forward(video, exact_start)
            body_start = kf_after
            if kf_after - exact_start >= MIN_BOUNDARY_SEG:
                seg = os.path.join(tmpdir, "seg_0_lead.mp4")
                _ffmpeg_encode_seg(video, exact_start, kf_after, seg, crf)
                segs.append(seg)
            # else: sub-frame gap — re-encoding it yields ZERO video frames
            # (libx264 over <1 frame), which corrupts the concat. Drop the
            # <1-frame remainder; body starts on the keyframe.

        # Trailing boundary: re-encode [kf_before, exact_end]; body ends on kf_before.
        body_end = end
        trail_seg: str | None = None
        if exact_end is not None:
            kf_before = snap_to_keyframe(video, exact_end)
            body_end = kf_before
            if exact_end - kf_before >= MIN_BOUNDARY_SEG:
                trail_seg = os.path.join(tmpdir, "seg_2_trail.mp4")
                _ffmpeg_encode_seg(video, kf_before, exact_end, trail_seg, crf)
            # else: sub-frame gap — skip (same zero-frame hazard as above).

        # Body: stream copy [body_start, body_end]
        if body_end > body_start:
            seg = os.path.join(tmpdir, "seg_1_body.mp4")
            _ffmpeg_copy_seg(video, body_start, body_end, seg)
            segs.append(seg)

        if trail_seg:
            segs.append(trail_seg)

        if not segs:
            _ffmpeg_copy_seg(video, start, end, out_path)
            return

        if len(segs) == 1:
            shutil.move(segs[0], out_path)
            return

        list_path = os.path.join(tmpdir, "concat_list.txt")
        with open(list_path, "w") as f:
            for seg in segs:
                f.write(f"file '{seg}'\n")
        subprocess.run([
            "ffmpeg", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-video_track_timescale", str(VIDEO_TIMESCALE),
            "-fflags", "+genpts",
            "-y", out_path,
        ], check=True)


def _jumped(t_a: float, dt_a: datetime, t_b: float, dt_b: datetime) -> bool:
    video_advance = t_b - t_a
    cam_advance = (dt_b - dt_a).total_seconds()
    return cam_advance > video_advance + _MIN_GAP_S or cam_advance < -1800


def _reading_confirmed(filtered: list[tuple[float, datetime]], i: int) -> bool:
    """
    True unless filtered[i] is itself a misread: an isolated bad OCR reading
    (e.g. one frame hallucinates a wrong date/year) creates a jump-in boundary
    immediately followed by a revert-out boundary, while its neighbors on
    either side remain consistent with each other. merge_short_clips()
    collapses the resulting near-zero clip, but the bad reading can still be
    the nearest valid sample to a clip's start — checking that it's sandwiched
    between two mutually consistent neighbors catches this without re-touching
    filter_ocr_outliers, which is tuned for boundary detection, not label
    selection. A reading that jumps from its prior but is NOT later reverted
    (a real session boundary) is left alone.

    With no prior reading (i == 0) there's no "jump-in" to check. Forward
    jumps are trusted by default (the common case: camera was paused/off,
    a real session boundary). Backward jumps are treated as suspect even
    without prior context — clocks don't normally run backward, so this is
    more likely an OCR misread or clock reset than a legitimate boundary.
    """
    if i + 1 >= len(filtered):
        return True
    t, dt = filtered[i]
    t_next, dt_next = filtered[i + 1]
    if not _jumped(t, dt, t_next, dt_next):
        return True
    if i == 0:
        return not (dt_next - dt).total_seconds() < -1800
    t_prev, dt_prev = filtered[i - 1]
    jumped_in = _jumped(t_prev, dt_prev, t, dt)
    neighbors_consistent = not _jumped(t_prev, dt_prev, t_next, dt_next)
    return not (jumped_in and neighbors_consistent)


def _label_for(filtered: list[tuple[float, datetime]], start: float, mode: str = "session") -> str:
    """Return a label string for the clip starting at `start` seconds."""
    fallback: datetime | None = None
    for i, (t, dt) in enumerate(filtered):
        if t >= start:
            if fallback is None:
                fallback = dt
            if not _reading_confirmed(filtered, i):
                continue
            if mode == "daily":
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d_%H%M")
    if fallback is not None:
        if mode == "daily":
            return fallback.strftime("%Y-%m-%d")
        return fallback.strftime("%Y-%m-%d_%H%M")
    return f"{int(start):05d}s"


def split_video(
    video: str, splits: list[float], out_dir: str, filtered: list[tuple[float, datetime]], mode: str = "session"
):
    os.makedirs(out_dir, exist_ok=True)
    duration = get_duration(video)
    stem = Path(video).stem

    # Remove this stem's clips from a prior run. Labels are derived from OCR, so a
    # re-run can rename clips; without this, renamed outputs orphan the old files.
    stale = glob.glob(os.path.join(out_dir, f"{stem}_clip*.mp4"))
    for old in stale:
        os.remove(old)
    if stale:
        print(f"  (removed {len(stale)} clip(s) from a previous run)")

    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        label = _label_for(filtered, start, mode)
        out_path = os.path.join(out_dir, f"{stem}_clip{idx+1:02d}_{label}.mp4")
        exact_start = start if idx > 0 else None
        exact_end = splits[idx + 1] if idx + 1 < len(splits) else None
        print(f"  clip {idx+1:02d}: {start:.1f}s → {end:.1f}s  ({label})  → {out_path}")
        cut_clip_with_boundary_encode(video, start, end, exact_start, exact_end, out_path)


# ------------------------------------------------------------------ #

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input video file")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP,
                    help="Camera timestamp gap (seconds) that triggers a new clip")
    ap.add_argument("--mode", choices=["scene", "session", "daily"], default=DEFAULT_MODE,
                    help="Clip grouping mode (default: daily)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--crop", default=DEFAULT_CROP,
                    help="ffmpeg crop 'w:h:x:y' for timestamp region")
    ap.add_argument("--cache", default=None,
                    help="JSON file to cache OCR scan results (saves time on re-runs)")
    ap.add_argument("--visual-cache", default=None,
                    help="JSON file to cache scene-cut/black-frame detection (saves time on re-runs)")
    ap.add_argument("--enable-visual-fusion", action="store_true", default=True,
                    help="Run visual boundary detection (scene cuts + black frames) to anchor cuts "
                         "in OCR dead zones where every frame returns None (default: on)")
    ap.add_argument("--no-visual-fusion", dest="enable_visual_fusion", action="store_false",
                    help="Disable visual boundary detection (faster; may misplace cuts at VHS head-switch noise zones)")
    ap.add_argument("--fuse-window", type=float, default=DEFAULT_FUSE_WINDOW,
                    help="Seconds of padding around [prev_t, video_t] to search for a corroborating visual signal")
    ap.add_argument("--min-clip", type=float, default=DEFAULT_MIN_CLIP_S,
                    help="Clips shorter than this (seconds) are merged into the prior clip")
    ap.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD,
                    help="ffmpeg scene-change detection threshold (0-1)")
    ap.add_argument("--black-min-duration", type=float, default=DEFAULT_BLACK_MIN_DURATION,
                    help="Minimum duration (s) of a black frame run to count as a boundary signal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    video = os.path.abspath(args.input)
    if not os.path.exists(video):
        sys.exit(f"File not found: {video}")

    out_dir = args.out_dir or (Path(video).stem + "_clips")
    out_dir = os.path.abspath(out_dir)

    cache = args.cache or (Path(video).stem + "_ocr_cache.json")
    visual_cache = args.visual_cache or (Path(video).stem + "_visual_cache.json")

    if not OCR_BIN.exists():
        sys.exit(f"ocr_timestamp binary not found at {OCR_BIN}. Run: swiftc -O ocr_timestamp.swift -o ocr_timestamp")

    print(f"Scanning: {video}")
    print(f"  interval={args.interval}s  gap={args.gap}s  mode={args.mode}  crop={args.crop}")
    samples = scan(video, args.interval, args.crop, cache_path=cache)

    valid = [(t, dt) for t, dt in samples if dt]
    print(f"\nOCR success: {len(valid)}/{len(samples)} frames")
    if valid:
        print(f"  date range: {valid[0][1].date()} → {valid[-1][1].date()}")

    boundaries = find_all_boundaries(samples, gap_s=args.gap)

    visual_times: list[float] = []
    if args.enable_visual_fusion:
        print("\nDetecting visual boundaries (scene cuts + black frames)...")
        scene_cuts, black_frames = detect_visual_boundaries(
            video, args.scene_threshold, args.black_min_duration, cache_path=visual_cache
        )
        print(f"  scene cuts: {len(scene_cuts)}  black frames: {len(black_frames)}")
        visual_times = sorted(scene_cuts + black_frames)
        before = len(boundaries)
        boundaries = fuse_boundaries(boundaries, scene_cuts, black_frames, args.fuse_window)
        print(
            f"  confirmed {len(boundaries)}/{before} OCR boundary(ies) "
            f"(visual corroboration within ±{args.fuse_window:.0f}s)"
        )

    cut_ts = group_clips(boundaries, args.mode, args.gap)
    duration = get_duration(video)

    # Build lookup: video_t → Boundary for refinement decisions and phantom collapse
    boundary_map = {b.video_t: b for b in boundaries}

    if args.mode == "daily":
        cut_ts = _collapse_revert_phantoms(cut_ts, boundary_map)

    filtered: list[tuple[float, datetime]] = filter_ocr_outliers(samples)

    effective_min_clip = 0.0 if args.mode == "daily" else args.min_clip

    if args.dry_run:
        splits: list[float] = [0.0] + list(cut_ts[1:])
        before_merge = len(splits)
        splits = merge_short_clips(splits, effective_min_clip)
        if len(splits) < before_merge:
            n = before_merge - len(splits)
            print(f"\nMerged {n} clip(s) shorter than {effective_min_clip:.0f}s into their neighbor")
        print(f"\nFound {len(splits)} clip(s) (boundary times ±{args.interval}s, not yet refined):")
        for idx, start in enumerate(splits):
            end = splits[idx + 1] if idx + 1 < len(splits) else duration
            dur = end - start
            label = _label_for(filtered, start, args.mode)
            b = boundary_map.get(start)
            btype = f" [{b.type}]" if b else ""
            print(f"  {idx+1:3d}. {start:7.0f}s → {end:7.0f}s  ({dur/60:5.1f} min)  {label}{btype}")
        print("\nDry run — not cutting.")
        return

    large_gap_count = sum(1 for vt in cut_ts[1:] if boundary_map.get(vt) and boundary_map[vt].type == "large_gap")
    print(f"\nRefining {large_gap_count} large_gap boundary(ies) at 1s resolution...")
    with tempfile.TemporaryDirectory() as tmpdir:
        splits = [0.0]
        for vt in cut_ts[1:]:
            b = boundary_map.get(vt)
            if b and b.type == "large_gap" and b.prev_t is not None and b.prev_dt is not None:
                refined_t, method = refine_split(
                    video, vt, b.prev_t, b.prev_dt, args.gap, args.crop, tmpdir, visual_times
                )
                saved = vt - refined_t
                method_tag = f" [{method}]" if method != "ocr" else ""
                print(f"  coarse={vt:.0f}s → refined={refined_t:.0f}s  (saved {saved:.0f}s){method_tag}")
                splits.append(refined_t)
            else:
                splits.append(vt)

    before_merge = len(splits)
    splits = merge_short_clips(splits, effective_min_clip)
    if len(splits) < before_merge:
        n = before_merge - len(splits)
        print(f"\nMerged {n} clip(s) shorter than {effective_min_clip:.0f}s into their neighbor")

    print(f"\nFound {len(splits)} clip(s):")
    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        dur = end - start
        label = _label_for(filtered, start, args.mode)
        print(f"  {idx+1:3d}. {start:7.0f}s → {end:7.0f}s  ({dur/60:5.1f} min)  {label}")

    print(f"\nCutting into {out_dir}/")
    split_video(video, splits, out_dir, filtered, args.mode)
    print("Done.")


if __name__ == "__main__":
    main()
