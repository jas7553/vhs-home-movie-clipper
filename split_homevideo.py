#!/usr/bin/env python3
"""
Split a home video into logical clips by reading the burned-in timestamp.

Usage:
    python3 split_homevideo.py <input.mp4> [--interval 10] [--gap 3600] [--out-dir ./clips]

Arguments:
    --interval  Seconds between sampled frames (default: 10)
    --gap       Time gap (seconds) between consecutive timestamps that
                triggers a new clip, even on the same date (default: 3600)
    --mode      Clip grouping mode: scene, session, or daily (default)
    --out-dir   Output directory for clips (default: <input>_clips/)
    --crop      ffmpeg crop string "w:h:x:y" for timestamp region
                (default: auto-detected per tape via calibration pass)
    --dry-run   Print split points without cutting
"""

import argparse
import glob
import json
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol

# --- defaults ---
DEFAULT_INTERVAL = 10          # sample every N seconds
DEFAULT_GAP = 3600             # 1-hour camera-time gap = new clip (empirically tuned)

DEFAULT_CROP = "560:130:40:350"   # w:h:x:y; full bottom band, 640x480 source
                                  # (was 250:110:385:370 — right-anchored, clipped off-center overlays)
DEFAULT_MODE = "daily"
DEFAULT_SCENE_THRESHOLD = 0.4
DEFAULT_BLACK_MIN_DURATION = 0.1
DEFAULT_FUSE_WINDOW = 5.0      # seconds within which a visual signal corroborates an OCR boundary
DEFAULT_MIN_CLIP_S = 120.0     # clips shorter than this are merged into prior clip; validated on golden set
ARTIFACT_MIN_S = 3.0           # hard floor applied in all modes; catches refinement-collision slivers
SPLICE_DEAD_ZONE_MAX_S = 120.0 # None-span up to this = Splice Dead Zone (visual anchor applies);
                               # wider = Long Dead Zone, falls back to coarse_t (ADR 0001, out of scope)

# Scene-snap (ultra-refinement, pass 3): after OCR refinement places a cut, snap it onto a
# precise shot-change frame. v2 anchor rule (finding 003):
#   - Detect in [t-context_s, t+after_s] so AdaptiveDetector sees burst-end cuts past t.
#   - Two cuts bracketing a short span → noise burst; anchor to LATER cut (end-of-burst, ADR 0001).
#   - Single cut ≤ t → clean content change; snap backward (removes old-session tail).
#   - Neither → no-op (VHS pause/resume often has no visual discontinuity).
SCENE_SNAP_CONTEXT_S = 20.0    # decode this much before t so AdaptiveDetector has rolling context
SCENE_SNAP_ACCEPT_S = 3.0      # accept a shot cut only within [t-this, t+after_s]; decoy guard
SCENE_SNAP_AFTER_S = 2.0       # extend detection past t to catch burst-end cuts (noise splices)
SCENE_SNAP_BURST_MAX_S = 6.0   # two close cuts within this span → noise burst, not two scenes
SCENE_SNAP_ADAPTIVE_THRESHOLD = 3.0
SCENE_SNAP_MIN_SCENE_LEN = 8   # frames

_CALIB_CACHE_FORMAT = 1        # increment to force re-calibration on all tapes
_CALIB_N_SAMPLES = 20         # frames sampled during calibration to verify OCR yield
_CACHE_FORMAT = 5              # increment when cache schema changes; forces re-scan on old caches
                              # (5: date-only readings accepted — _vote_bucket now stores date-only
                              #  text that v4 discarded, so old caches must be regenerated)
                              # (4: crop-primary scan with preprocessing fallback — supersedes v3 all-preprocessed)
_VISUAL_CACHE_FORMAT = 1
_MIN_GAP_S = 60               # minimum camera-time jump to emit a boundary (internal, not user-tunable)

OCR_BIN = Path(__file__).parent / "ocr_timestamp"
VIDEO_TIMESCALE = 29970  # matches source tbn; same on all segs/concat to prevent PTS mis-scaling
MIN_BOUNDARY_SEG = 0.05  # s; boundary re-encodes shorter than ~1 frame (29.97fps≈0.033s)
                         # produce zero video frames and corrupt the concat — skip them.

# OCR preprocessing filter chain — FALLBACK ONLY, not primary.
# yadif: deinterlaces VHS comb artifacts; format=gray: removes color noise Vision ignores anyway;
# scale 4×: more glyph detail than 3×; unsharp: crisp edges post-scale; eq: harden contrast.
# Measured on Converse 1990.mp4 (150 frames spread across the file): crop-only OCR parses
# 67% of frames, this chain only 45% — the unsharp+contrast=2.0 blows out the timestamp on a
# large class of (brighter/lower-contrast) frames, turning readable footage into all-None
# "dead zones". So scan() runs crop-only first and only applies this chain to buckets crop-only
# could not read (it uniquely recovers a few % that crop-only misses). See scan().
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
    prev_t:     float | None        # video_t of last valid sample before (for refinement)
    prev_dt:    datetime | None     # datetime of last valid sample before (for refinement)


class RefinementResult(NamedTuple):
    t: float
    method: str
    detail: str

RefinementStrategy = Callable[[str, Boundary], RefinementResult]


class Reading(NamedTuple):
    """One refinement-scan frame: the parsed timestamp (or None) plus the raw OCR
    text. Refinement keeps the raw text so it can classify garbled gap frames whose
    date is partially legible but rejected by the strict parser (see
    _gap_date_class / _place_content_aware)."""
    dt:  datetime | None
    raw: str


OcrFn = Callable[[list[int]], dict[int, Reading]]


class PlacementPolicy(Protocol):
    def place(self, boundary: Boundary, ocr_fn: OcrFn) -> RefinementResult: ...


@dataclass
class PipelineConfig:
    video: str
    interval: int = DEFAULT_INTERVAL
    gap: int = DEFAULT_GAP
    crop: str = DEFAULT_CROP
    mode: str = DEFAULT_MODE
    cache: str | None = None
    visual_cache: str | None = None
    enable_visual_fusion: bool = False
    enable_scene_snap: bool = False
    no_visual_anchor: bool = False
    fuse_window: float = DEFAULT_FUSE_WINDOW
    min_clip: float = DEFAULT_MIN_CLIP_S
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD
    black_min_duration: float = DEFAULT_BLACK_MIN_DURATION
    dry_run: bool = False


class PipelineResult(NamedTuple):
    splits: list[float]
    filtered: list[tuple[float, datetime]]
    boundary_map: dict[float, Boundary]
    phase_times: dict[str, float]


# ------------------------------------------------------------------ #
# Timestamp parsing
# ------------------------------------------------------------------ #

DATE_PATTERN = re.compile(
    r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})"
)
WORD_MONTH_PATTERN = re.compile(
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[.,:\-]?\s*(\d{1,2})\s+(\d{2,4})",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
TIME_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM|am|pm)?", re.IGNORECASE
)


def _parse_time_only(text: str) -> tuple[int, int] | None:
    """(hour24, minute) if text has a meridian time but no parseable date; else None."""
    flat = re.sub(r"[\n\r]+", " ", text)
    flat = re.sub(r" +", " ", flat).strip()
    if DATE_PATTERN.search(flat) or WORD_MONTH_PATTERN.search(flat):
        return None
    time_m = TIME_PATTERN.search(flat)
    if not time_m:
        return None
    ampm = (time_m.group(3) or "").upper()
    if not ampm:
        return None
    hour, minute = int(time_m.group(1)), int(time_m.group(2))
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def parse_timestamp(text: str) -> datetime | None:
    """Parse '5:01 PM / 1/ 4/90' style OCR output (may have noisy newlines)."""
    # Normalize: collapse newlines + multiple spaces so multi-line OCR joins up
    flat = re.sub(r"[\n\r]+", " ", text)
    flat = re.sub(r" +", " ", flat).strip()
    date_m = DATE_PATTERN.search(flat)
    wm_m = None if date_m else WORD_MONTH_PATTERN.search(flat)
    time_m = TIME_PATTERN.search(flat)
    if not date_m and not wm_m:
        return None
    if date_m:
        month, day, year = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
        # Reject implausible dates (month > 12, day > 31)
        if month > 12 or day > 31:
            return None
    else:
        assert wm_m is not None
        month = _MONTH_MAP[wm_m.group(1).upper()]
        day, year = int(wm_m.group(2)), int(wm_m.group(3))
    if year < 100:
        year += 1900 if year >= 80 else 2000
    # Reject implausible years for home VHS footage (1985–2005)
    if not (1985 <= year <= 2005):
        return None
    # Time is optional. A fully-qualified time (H:MM AM/PM) is used as-is; when
    # the time is absent, or its meridian is missing, fall back to midnight and
    # keep the date. Date-only readings are real on this footage: the camcorder
    # overlay can be set to show the date without a time, producing long spans
    # (e.g. 8/27, 9/2, 10/6 over 36 min) where every frame is date-only.
    # Rejecting them (the old behavior) made those spans invisible to boundary
    # detection, collapsing several real date changes into one clip — cross-date
    # contamination, a correctness failure. The "00:00 causes huge false jumps"
    # hazard the old guard worried about is absorbed downstream: filter_ocr_
    # outliers drops a lone midnight reading sitting among timed frames (its
    # drift to both timed neighbors is large while they bridge each other), and
    # daily-mode grouping cuts only on date changes, so an intra-day midnight
    # jump never becomes a clip boundary.
    hour, minute = 0, 0
    if time_m:
        ampm = (time_m.group(3) or "").upper()
        if ampm:
            hour, minute = int(time_m.group(1)), int(time_m.group(2))
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

def extract_frame(video: str, t: float, crop: str, tmpdir: str, preprocess: bool = False) -> str | None:
    """Extract one cropped frame to a BMP; return path or None on failure.

    Default is crop-only: it has a higher OCR yield than the _VF_PREPROCESS chain
    (67% vs 45% on the test file), so it is the primary path everywhere — including
    refinement, which calls this without overriding the default. preprocess=True is
    the fallback applied only to frames crop-only cannot read (see scan()).
    """
    vf = f"crop={crop},{_VF_PREPROCESS}" if preprocess else f"crop={crop}"
    frame_path = os.path.join(tmpdir, f"frame_{t:.3f}.bmp")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-hwaccel", "videotoolbox",
        "-ss", str(t),
        "-i", video,
        "-vframes", "1",
        "-vf", vf,
        "-update", "1",
        "-y", frame_path,
    ]
    ret = subprocess.run(cmd, capture_output=True)
    if ret.returncode != 0 or not os.path.exists(frame_path):
        return None
    return frame_path


def extract_all_frames(
    video: str, interval: int, crop: str, tmpdir: str, preprocess: bool = False
) -> list[str]:
    """Single-pass: decode video sequentially at 1/interval fps, crop each frame.

    Default crop-only (the higher-yield primary path); preprocess=True appends
    _VF_PREPROCESS for the scan() fallback pass.
    """
    vf = f"fps={FRAMES_PER_SAMPLE}/{interval},crop={crop}"
    if preprocess:
        vf += f",{_VF_PREPROCESS}"
    out_pattern = os.path.join(tmpdir, "frame_%06d.bmp")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", video,
        "-vf", vf,
        "-start_number", "0",
        "-y", out_pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(glob.glob(os.path.join(tmpdir, "frame_*.bmp")))


def frame_index(path: str) -> int:
    m = re.search(r"frame_(\d+)\.bmp", os.path.basename(path))
    assert m is not None, f"unexpected frame filename: {path}"
    return int(m.group(1))


def _bucket_frames(paths: list[str]) -> dict[int, list[str]]:
    """Group extracted frames into interval windows by frame index (FRAMES_PER_SAMPLE per window)."""
    buckets: dict[int, list[str]] = {}
    for path in paths:
        buckets.setdefault(frame_index(path) // FRAMES_PER_SAMPLE, []).append(path)
    return buckets


def _vote_bucket(paths: list[str], ocr: dict[str, str]) -> tuple[datetime | None, str | None]:
    """Majority-vote parseable readings; fall back to time-only text.

    Returns (dated_dt, text) when a dated reading wins, (None, time_only_text) when
    the window holds only time-only overlays, or (None, None) if unreadable.
    """
    parsed = [(p, text, parse_timestamp(text)) for p in paths for text in (ocr.get(p, ""),)]
    valid = [(p, text, dt) for p, text, dt in parsed if dt is not None]
    if valid:
        winner_dt = Counter(dt for _, _, dt in valid).most_common(1)[0][0]
        winner_text = next(text for _, text, dt in valid if dt == winner_dt)
        return winner_dt, winner_text
    for _, text, _ in parsed:
        if text and _parse_time_only(text) is not None:
            return None, text
    return None, None


def ocr_batch(paths: list[str]) -> dict[str, str]:
    """Run ocr_timestamp in parallel chunks; return {path: raw_text}."""
    if not paths:
        return {}
    n_workers = min(os.cpu_count() or 4, 8)
    chunk_size = max(1, math.ceil(len(paths) / n_workers))
    chunks = [paths[i:i + chunk_size] for i in range(0, len(paths), chunk_size)]

    def run_chunk(chunk: list[str]) -> dict[str, str]:
        result = subprocess.run([str(OCR_BIN)] + chunk, capture_output=True, text=True)
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "\t" in line:
                p, _, text = line.partition("\t")
                out[p] = text
        return out

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        merged: dict[str, str] = {}
        for chunk_result in pool.map(run_chunk, chunks):
            merged.update(chunk_result)
    return merged


# ------------------------------------------------------------------ #
# Main logic
# ------------------------------------------------------------------ #

def _get_video_dimensions(video: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video,
    ], capture_output=True, text=True, check=True)
    line = r.stdout.strip().splitlines()[0]
    w, h = line.split(",")
    return int(w), int(h)


def _bottom_band_crop(w: int, h: int) -> str:
    """Wide bottom-band crop proportionally scaled from the 640×480 reference.

    Reference crop 560:130:40:350 on 640×480 covers the bottom 130px starting
    at x=40 — wide enough to capture left/center/right overlay positions.
    Scales linearly for other frame dimensions.
    """
    crop_h = max(60, round(h * 130 / 480))
    crop_w = max(100, round(w * 560 / 640))
    crop_x = round(w * 40 / 640)
    crop_y = h - crop_h
    crop_x = max(0, min(crop_x, w - crop_w))
    return f"{crop_w}:{crop_h}:{crop_x}:{crop_y}"


def calibrate(video: str, cache_path: str | None = None) -> str:
    """Auto-detect overlay crop for the given tape. Returns a crop string.

    Derives a wide bottom-band crop scaled to the tape's frame dimensions,
    then verifies it against a spread of sampled frames. Falls back to
    DEFAULT_CROP if dimensions cannot be probed. Result is cached per tape.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("calib_format") == _CALIB_CACHE_FORMAT:
            print(f"  (loaded from calib cache: {cache_path})")
            return str(cached["crop"])

    try:
        w, h = _get_video_dimensions(video)
    except (subprocess.CalledProcessError, ValueError):
        print(f"  calib warn=dimension-probe-failed fallback={DEFAULT_CROP}")
        return DEFAULT_CROP

    crop = _bottom_band_crop(w, h)

    duration = get_duration(video)
    step = duration / (_CALIB_N_SAMPLES + 1)
    sample_times = [step * (i + 1) for i in range(_CALIB_N_SAMPLES)]

    with tempfile.TemporaryDirectory() as tmpdir:
        paths = [p for t in sample_times if (p := extract_frame(video, t, crop, tmpdir)) is not None]
        raw = ocr_batch(paths) if paths else {}

    hits = sum(
        1 for text in raw.values()
        if parse_timestamp(text) is not None or _parse_time_only(text) is not None
    )
    yield_pct = hits / max(1, len(paths))
    print(f"calib frame={w}x{h} crop={crop} sample={len(paths)} hits={hits} yield={yield_pct:.0%}")

    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"calib_format": _CALIB_CACHE_FORMAT, "crop": crop, "w": w, "h": h}, f)
        print(f"  (calib cached to {cache_path})")

    return crop


def get_duration(video: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def fill_timeonly_dates(
    raw: list[tuple[float, datetime | None, str | None]],
) -> list[tuple[float, datetime | None]]:
    """Infer dates for time-only OCR readings (AM/PM time with no date in overlay).

    Applies the chronological-tape assumption: a time-only reading inherits the date
    of its nearest dated predecessor.  When the camera clock wraps backward by more
    than 12 camera-hours the running date increments by one day, so overnight spans
    that bridge midnight split correctly across dates.  If no predecessor exists the
    nearest dated successor's date is used.  When neither side has a dated reading
    the entry stays None (unresolvable — the fail-safe from the issue spec).
    """
    n = len(raw)
    result: list[tuple[float, datetime | None]] = [(t, dt) for t, dt, _ in raw]
    last_effective: datetime | None = None

    for k in range(n):
        t_k, dt_k, text_k = raw[k]
        if dt_k is not None:
            last_effective = dt_k
            continue
        if not text_k:
            continue
        hm = _parse_time_only(text_k)
        if hm is None:
            continue
        h, m = hm
        if last_effective is not None:
            prev_min = last_effective.hour * 60 + last_effective.minute
            cur_min = h * 60 + m
            d = last_effective.date()
            if cur_min < prev_min - 12 * 60:
                d = d + timedelta(days=1)
            dt_filled = datetime(d.year, d.month, d.day, h, m)
        else:
            succ: datetime | None = None
            for j in range(k + 1, n):
                if raw[j][1] is not None:
                    succ = raw[j][1]
                    break
            if succ is None:
                continue
            dt_filled = datetime(succ.year, succ.month, succ.day, h, m)
        result[k] = (t_k, dt_filled)
        last_effective = dt_filled

    return result


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
            return fill_timeonly_dates([
                (float(t), parse_timestamp(text) if text else None, text or None)
                for t, text in cached["samples"]
            ])

    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: crop-only extraction (PRIMARY — higher OCR yield than preprocessing; see _VF_PREPROCESS).
        crop_dir = os.path.join(tmpdir, "crop")
        os.makedirs(crop_dir)
        print(f"  Extracting frames (crop-only, {FRAMES_PER_SAMPLE} frames/{interval}s)...", flush=True)
        crop_paths = extract_all_frames(video, interval, crop, crop_dir, preprocess=False)
        print(f"  Extracted {len(crop_paths)} frames. Running OCR (batch)...", flush=True)
        crop_ocr = ocr_batch(crop_paths)
        crop_buckets = _bucket_frames(crop_paths)

        # Per-window majority vote; remember which windows crop-only could not read.
        readings: dict[int, tuple[datetime, str | None]] = {}
        timeonly_map: dict[int, str] = {}
        unsolved: list[int] = []
        for bk, paths in crop_buckets.items():
            dt, text = _vote_bucket(paths, crop_ocr)
            if dt is not None:
                readings[bk] = (dt, text)
            elif text is not None:
                timeonly_map[bk] = text
            else:
                unsolved.append(bk)

        # Phase 2: preprocessing FALLBACK — second pass, used only for windows crop-only failed on.
        # Preprocessing hurts most frames but uniquely recovers some; restrict it to true gaps.
        if unsolved:
            pp_dir = os.path.join(tmpdir, "pp")
            os.makedirs(pp_dir)
            print(f"  {len(unsolved)} windows unread; preprocessing fallback pass...", flush=True)
            pp_paths = extract_all_frames(video, interval, crop, pp_dir, preprocess=True)
            # Index alignment between passes holds only if both yield the same frame count.
            if len(pp_paths) == len(crop_paths):
                pp_ocr = ocr_batch(pp_paths)
                pp_buckets = _bucket_frames(pp_paths)
                for bk in unsolved:
                    dt, text = _vote_bucket(pp_buckets.get(bk, []), pp_ocr)
                    if dt is not None:
                        readings[bk] = (dt, text)
                    elif text is not None:
                        timeonly_map[bk] = text
            else:
                print(f"  (skipped fallback: frame-count mismatch "
                      f"{len(pp_paths)} != {len(crop_paths)})", flush=True)

        # Assemble results keyed by t_last_frame of each window — the actual video time of the
        # last extracted frame in the window (a true upper bound on when any event was observed),
        # not the window-start label. raw_text is cached so parser fixes propagate without re-scan.
        results: dict[float, tuple[datetime | None, str | None]] = {}
        for bk, paths in crop_buckets.items():
            t_last = float(max(frame_index(p) for p in paths)) * interval / FRAMES_PER_SAMPLE
            if bk in readings:
                results[t_last] = readings[bk]
            elif bk in timeonly_map:
                results[t_last] = (None, timeonly_map[bk])
            else:
                results[t_last] = (None, None)

    sorted_results = sorted(results.items())

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

    return fill_timeonly_dates([(t, dt, text) for t, (dt, text) in sorted_results])


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
    samples: list[tuple[float, datetime]],
    gap_s: int = DEFAULT_GAP,
) -> list["Boundary"]:
    """
    Stage 3: emit every candidate boundary at a low detection floor (_MIN_GAP_S).

    Accepts pre-filtered samples (no None entries) — call filter_ocr_outliers first.
    Returns Boundary objects sorted by video_t. Type 'large_gap' when the camera
    jump exceeds gap_s or is backward; 'gap' for smaller detected pauses.
    """
    boundaries: list[Boundary] = []
    prev: tuple[float, datetime] | None = None
    for t, dt in samples:
        if prev is not None:
            prev_t, prev_dt = prev
            video_advance = t - prev_t
            cam_advance = (dt - prev_dt).total_seconds()
            jumped_forward = cam_advance > video_advance + _MIN_GAP_S
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


def drop_date_islands(
    samples: list[tuple[float, datetime | None]],
) -> list[tuple[float, datetime | None]]:
    """Drop 'date islands' — a dated reading whose date differs from BOTH nearest dated neighbours.

    Operates on raw samples (None entries for failed OCR windows are skipped when
    searching for neighbours). This allows the filter to catch misreads that are
    surrounded only by None gaps — which would survive if we ran only after
    None-stripping, since they'd appear isolated with no neighbours to compare.

    A real recording session is a contiguous run of same-date readings. A single
    isolated reading of a date that neither the previous nor the next *dated*
    reading shares is an OCR misread (wrong day/month/year). Removing these here,
    before boundary detection, stops them from creating spurious boundaries —
    crucially including a misread that lands exactly on a real session change,
    which would otherwise look like a phantom and cause two real sessions to merge.

    Replaces the old cut-level phantom collapse: unlike that heuristic (which
    keyed on opposite-sign camera jumps + short duration), this never merges a
    genuine short session — e.g. an out-of-order date run on a re-recorded tape —
    into its neighbour. A real session has >= 2 consecutive *dated* readings and
    so is never an island.
    """
    dated = [(i, dt) for i, (_, dt) in enumerate(samples) if dt is not None]
    if len(dated) < 3:
        return samples
    drop: set[int] = set()
    for j in range(1, len(dated) - 1):
        idx, dt = dated[j]
        d = dt.date()
        if d != dated[j - 1][1].date() and d != dated[j + 1][1].date():
            drop.add(idx)
    return [s for i, s in enumerate(samples) if i not in drop]


def drop_digit_drop_runs(
    samples: list[tuple[float, "datetime | None"]],
) -> list[tuple[float, "datetime | None"]]:
    """Drop multi-window digit-drop misreads that survive drop_date_islands.

    Word-month OCR (Style B, e.g. NOV. 26 1992) sometimes drops the tens digit
    of the day: NOV. 26 → NOV 6, NOV. 27 → NOV 2. When such a misread spans
    ≥2 consecutive windows it looks like a genuine session (the ≥2 rule in
    drop_date_islands) and survives the island filter.

    A run is flagged as a digit-drop misread when all three hold:
      1. The run is bracketed on both sides by the same outer date.
      2. The run's date has the same month and year as the outer date.
      3. The outer day is ≥10 and either its ones-digit or its tens-digit
         equals the run's day (e.g. outer=26 → ones=6 ✓; outer=27 → tens=2 ✓).

    Genuine out-of-order sessions (different month, different year, or ones-digit
    mismatch) are not dropped — the 9/01-between-3/25-and-4/08 case survives.
    None entries are skipped when forming runs, matching drop_date_islands semantics.
    """
    dated = [(i, dt) for i, (_, dt) in enumerate(samples) if dt is not None]
    if len(dated) < 3:
        return samples

    # Group consecutive same-date readings into runs (None gaps are invisible here).
    runs: list[tuple[date, list[int]]] = []
    cur_date = dated[0][1].date()
    cur_idx = [dated[0][0]]
    for idx, dt in dated[1:]:
        d = dt.date()
        if d == cur_date:
            cur_idx.append(idx)
        else:
            runs.append((cur_date, cur_idx))
            cur_date = d
            cur_idx = [idx]
    runs.append((cur_date, cur_idx))

    if len(runs) < 3:
        return samples

    drop: set[int] = set()
    for r in range(1, len(runs) - 1):
        run_date, run_indices = runs[r]
        left_date = runs[r - 1][0]
        right_date = runs[r + 1][0]
        if left_date != right_date:
            continue
        outer = left_date
        if (
            run_date.month == outer.month
            and run_date.year == outer.year
            and outer.day >= 10
            and (
                outer.day % 10 == run_date.day  # drop tens digit: 26 → 6
                or outer.day // 10 == run_date.day  # drop ones digit: 27 → 2
            )
        ):
            drop.update(run_indices)

    return [s for i, s in enumerate(samples) if i not in drop]


def drop_year_misread_runs(
    samples: list[tuple[float, datetime | None]],
) -> list[tuple[float, datetime | None]]:
    """Drop contiguous blocks of runs that are in-range year misreads.

    A maximal span of one or more adjacent runs whose years all differ from the
    bracketing real runs' year, while sharing the same month, is a year-misread
    cluster (e.g. 1990-04-29 / [1991-04-29, 1991-04-28] / 1990-04-29).  The
    outer runs must share the same year.  Genuine New-Year boundaries always
    co-change month, so same-month context safely constrains the drop.
    """
    dated = [(i, dt) for i, (_, dt) in enumerate(samples) if dt is not None]
    if len(dated) < 3:
        return samples

    runs: list[tuple[date, list[int]]] = []
    cur_date = dated[0][1].date()
    cur_indices = [dated[0][0]]
    for idx, dt in dated[1:]:
        d = dt.date()
        if d == cur_date:
            cur_indices.append(idx)
        else:
            runs.append((cur_date, cur_indices))
            cur_date = d
            cur_indices = [idx]
    runs.append((cur_date, cur_indices))

    if len(runs) < 3:
        return samples

    drop: set[int] = set()
    r = 1
    while r < len(runs) - 1:
        left_date = runs[r - 1][0]
        # Advance block_end over all consecutive runs with year≠left_year and month==left_month
        block_end = r
        while (
            block_end < len(runs) - 1
            and runs[block_end][0].year != left_date.year
            and runs[block_end][0].month == left_date.month
        ):
            block_end += 1
        if block_end > r:
            right_date = runs[block_end][0]
            if right_date.year == left_date.year and right_date.month == left_date.month:
                for inner_r in range(r, block_end):
                    drop.update(runs[inner_r][1])
                r = block_end + 1
                continue
        r += 1

    return [s for i, s in enumerate(samples) if i not in drop]


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


def group_clips(boundaries: list["Boundary"], mode: str) -> list[float]:
    """
    Stage 4: decide which boundaries become cut points based on mode.

    Returns list[float] of video_t values with 0.0 prepended.
      scene   — all boundaries (gap + large_gap)
      session — large_gap only
      daily   — only confirmed date changes; unknown-date boundaries are skipped
                (avoids splitting same-day footage; backward jumps are cut only
                when they cross a date boundary)

    Daily mode cuts on every date change in tape order, so a date that recurs
    later on the tape (out-of-order re-recording) yields a separate clip each
    time — it is not regrouped with its earlier occurrence. OCR misreads that
    would create spurious date changes are removed upstream by drop_date_islands,
    so no cut-level phantom collapse is needed here.
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
    cached: dict | None = None
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if (cached.get("cache_format") == _VISUAL_CACHE_FORMAT
                and cached.get("scene_threshold") == scene_threshold
                and cached.get("black_min_duration") == black_min_duration):
            print(f"  (loaded from visual cache: {cache_path})")
            return cached["scene_cuts"], cached["black_frames"]
        # Cache present but stale: say why before paying for a full re-decode.
        why = []
        if cached.get("cache_format") != _VISUAL_CACHE_FORMAT:
            why.append(f"cache_format={cached.get('cache_format')}!={_VISUAL_CACHE_FORMAT}")
        if cached.get("scene_threshold") != scene_threshold:
            why.append(f"scene_threshold={cached.get('scene_threshold')}!={scene_threshold}")
        if cached.get("black_min_duration") != black_min_duration:
            why.append(f"black_min_duration={cached.get('black_min_duration')}!={black_min_duration}")
        print(f"  (visual cache miss: {'; '.join(why) or 'unknown'})")

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


def _extract_and_ocr_window(
    video: str, times: list[int], crop: str, tmpdir: str, workers: int,
) -> tuple[dict[int, str | None], dict[str, str]]:
    """Extract frames for the given timestamps in parallel, OCR the valid ones."""
    paths: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_t = {
            executor.submit(extract_frame, video, float(t), crop, tmpdir): t
            for t in times
        }
        for future in as_completed(future_to_t):
            paths[future_to_t[future]] = future.result()
    valid_paths = [p for t in times if (p := paths.get(t)) is not None]
    return paths, ocr_batch(valid_paths)


def _scan_for_transition(
    times: list[int],
    readings: dict[int, Reading],
    prev_dt: datetime,
    prev_t: float,
    gap_s: int,
    expected_new_date: date | None = None,
) -> tuple[bool, float, float | None]:
    """Walk frame timestamps in order. Return (any_ocr, last_old_t, first_new_t).
    first_new_t is None if no new-session frame was found.

    Scans all frames rather than stopping at the first apparent jump, so that a
    single garbled frame misread as the new date (a false positive) is rejected
    when the very next legible frame reverts to the old session.  The returned
    first_new_t is the first *sustained* new-session reading — one not immediately
    followed by an old-session reading.

    expected_new_date: when set (from the coarse boundary's cam_after date after
    island filtering), frames that look 'new' but whose date doesn't match are
    treated as intermediate content and kept with the outgoing clip.  This prevents
    isolated intermediate-date footage (e.g. 4 frames of 5/12 between 5/09 and
    5/19 sessions) from being misidentified as the new session start."""
    any_ocr = False
    last_old_t: float = prev_t
    candidate_new_t: float | None = None
    for t in times:
        reading = readings.get(t)
        dt = reading.dt if reading is not None else None
        if dt is None:
            continue
        any_ocr = True
        cam_advance = (dt - prev_dt).total_seconds()
        video_advance = float(t) - prev_t
        is_new = cam_advance > video_advance + gap_s or cam_advance < -1800
        if is_new:
            if expected_new_date is not None and dt.date() != expected_new_date:
                # Intermediate date: looks new relative to old session but doesn't
                # match the expected boundary target.  Keep with outgoing clip.
                candidate_new_t = None
                last_old_t = float(t)
            elif candidate_new_t is None:
                candidate_new_t = float(t)
        else:
            # Old-session frame after a candidate jump → false positive; reset.
            candidate_new_t = None
            last_old_t = float(t)
    return any_ocr, last_old_t, candidate_new_t


# Lenient date-field extractors for garbled gap frames. The strict parser
# (parse_timestamp) rejects these — missing/garbled month, doubled slash, etc. —
# but the day immediately before the two-digit year and the leading month before
# the first slash survive OCR mangling well enough to tell old-date from new-date
# content. The year may OCR as 7x/8x/9x, hence the [789]\d class.
_DAY_BEFORE_YEAR = re.compile(r"(\d{1,2})\s*[/.\s\-]\s*([789]\d)(?!\d)")
_MONTH_LEAD = re.compile(r"(?<!\d)(\d{1,2})\s*/")


def _lenient_days(raw: str) -> set[int]:
    out: set[int] = set()
    for m in _DAY_BEFORE_YEAR.finditer(raw):
        d = int(m.group(1))
        if d > 31:                       # e.g. "074/90" -> "74"; retry trailing digit -> 4
            d = int(m.group(1)[-1])
        if 1 <= d <= 31:
            out.add(d)
    return out


def _lenient_months(raw: str) -> set[int]:
    out: set[int] = set()
    for m in _MONTH_LEAD.finditer(raw):
        mo = int(m.group(1))
        if mo > 12:
            mo = int(m.group(1)[-1])
        if 1 <= mo <= 12:
            out.add(mo)
    return out


def _gap_date_class(raw: str, old_dt: datetime, new_dt: datetime) -> str:
    """Classify a garbled gap frame as 'old', 'new', or 'noise' by which session's
    date its recoverable digits match. Only fields that *differ* between the two
    sessions discriminate; a field shared by both (e.g. same day) is ignored."""
    days = _lenient_days(raw)
    months = _lenient_months(raw)
    new_hit = (new_dt.day in days and new_dt.day != old_dt.day) \
        or (new_dt.month in months and new_dt.month != old_dt.month)
    old_hit = (old_dt.day in days and old_dt.day != new_dt.day) \
        or (old_dt.month in months and old_dt.month != new_dt.month)
    if new_hit and not old_hit:
        return "new"
    if old_hit and not new_hit:
        return "old"
    return "noise"


def _place_content_aware(
    window: list[int],
    readings: dict[int, Reading],
    last_old_t: float,
    first_new_t: float,
    old_dt: datetime,
    new_dt: datetime | None,
    visual_times: list[float] | None = None,
) -> float:
    """Decide the cut across the unreadable gap between the last confirmed old-session
    frame and the first confirmed new-session frame.

    The gap is one of three things and the correct cut differs (REQUIREMENTS L23/L25):
      - garbled NEW-date footage  -> cut at its start, so none leaks into the old clip;
      - garbled OLD-date footage  -> keep it with the old clip (ADR-0001);
      - a head-switch noise burst -> anchor to last visual event in the burst, or
                                     cut at last_old_t+1 when no signal exists (L23).
    Classify each gap frame by its recoverable date digits. Old content extends the
    confirmed-old run through the last 'old'-classified frame; the cut lands on the
    first 'new'-classified frame after that. When no date content is visible in the
    gap (pure noise), apply visual-anchor or last_old_t+1 rather than the
    end-of-gap placement — the end-of-gap heuristic causes tail leaks when OCR misses
    the early new-session frames at the start of a noise burst."""
    same_date_fallback = max(last_old_t + 1.0, first_new_t - 1.0)
    if new_dt is None or new_dt.date() == old_dt.date():
        return same_date_fallback
    gap = [t for t in window if last_old_t < t < first_new_t]
    classes = {
        t: _gap_date_class(readings[t].raw, old_dt, new_dt)
        for t in gap if t in readings
    }
    last_old_garble = max(
        (t for t in gap if classes.get(t) == "old"), default=None,
    )
    new_frames = [
        t for t in gap
        if (last_old_garble is None or t > last_old_garble) and classes.get(t) == "new"
    ]
    if new_frames:
        return float(min(new_frames))
    if last_old_garble is not None:
        # Confirmed old garble in gap: keep the whole ambiguous span with the old
        # clip (ADR-0001), so new clip starts just before the first confirmed new frame.
        return max(last_old_t + 1.0, first_new_t - 1.0)
    # Pure noise: no date digits recoverable from any gap frame.  Visual anchor marks
    # the end of a head-switch noise burst (ADR-0001); absent that, cut at
    # last_old_t+1 so no unclassified content leaks into the outgoing clip (L23).
    if visual_times:
        anchors = [vt for vt in visual_times if last_old_t < vt < first_new_t]
        if anchors:
            return max(anchors)
    return last_old_t + 1.0


def make_ocr_fn(video: str, crop: str, tmpdir: str, workers: int) -> OcrFn:
    """Production OcrFn: extracts frames via ffmpeg, parses timestamps via OCR binary."""
    def ocr_fn(times: list[int]) -> dict[int, Reading]:
        paths, raw = _extract_and_ocr_window(video, times, crop, tmpdir, workers)
        out: dict[int, Reading] = {}
        for t in times:
            text = raw.get(p, "") if (p := paths.get(t)) is not None else ""
            out[t] = Reading(parse_timestamp(text) if p is not None else None, text)
        return out
    return ocr_fn


class LongDeadZonePolicy:
    """Refinement for boundaries with span >= SPLICE_DEAD_ZONE_MAX_S.

    Two-pass hierarchical scan: coarse sub-sample (~50 pts) to bracket the
    transition, then dense 1s scan only within [last_old, first_new].
    If coarse is all-None (true LDZ), skips dense scan and falls back to coarse_t.
    If coarse is all-old, scans the short tail after the last coarse sample.
    """

    def __init__(self, gap_s: int, interval: int) -> None:
        self._gap_s = gap_s
        self._interval = interval

    def place(self, boundary: Boundary, ocr_fn: OcrFn) -> RefinementResult:
        coarse_t = boundary.video_t
        prev_t = boundary.prev_t
        prev_dt = boundary.prev_dt
        assert prev_t is not None and prev_dt is not None
        span = coarse_t - prev_t

        expected_new_date = boundary.cam_after.date() if boundary.cam_after else None
        window = list(range(int(prev_t) + 1, int(coarse_t) + self._interval))
        step = max(2, len(window) // 50)
        coarse_times = window[::step]
        readings: dict[int, Reading] = dict(ocr_fn(coarse_times))
        any_ocr_c, last_old_c, first_new_c = _scan_for_transition(
            coarse_times, readings, prev_dt, prev_t, self._gap_s, expected_new_date,
        )
        if first_new_c is not None:
            lo, hi = int(last_old_c), int(first_new_c)
            dense_times = [t for t in range(lo, hi + 1) if t not in readings]
            if dense_times:
                readings.update(ocr_fn(dense_times))
        elif any_ocr_c:
            tail = [t for t in window if t > coarse_times[-1]]
            if tail:
                readings.update(ocr_fn(tail))
        # else: all-None coarse → true LDZ; fall through to coarse_t fallback

        any_ocr, last_old_t, first_new_t = _scan_for_transition(
            window, readings, prev_dt, prev_t, self._gap_s, expected_new_date,
        )
        if first_new_t is not None:
            new_dt = readings[int(first_new_t)].dt
            cut = _place_content_aware(window, readings, last_old_t, first_new_t, prev_dt, new_dt)
            return RefinementResult(cut, "ocr", "")
        detail = f"LDZ {span:.0f}s" if not any_ocr else "all-old-in-window"

        return RefinementResult(coarse_t, "coarse", detail)


class ShortSpanPolicy:
    """Refinement for boundaries with span < SPLICE_DEAD_ZONE_MAX_S.

    Single dense scan of the full window. Handles three outcomes in priority order:
    1. OCR transition found → content-aware cut across the gap (_place_content_aware):
       at the first garbled-new frame, else end-of-gap when the gap is old/noise.
    2. Garbled new-session OCR (no clean new frame at all) → cut just after last old.
    3. All-None (Splice Dead Zone) → anchor to last visual event, or coarse_t fallback.
    """

    def __init__(self, gap_s: int, interval: int, visual_times: list[float] | None) -> None:
        self._gap_s = gap_s
        self._interval = interval
        self._visual_times = visual_times

    def place(self, boundary: Boundary, ocr_fn: OcrFn) -> RefinementResult:
        coarse_t = boundary.video_t
        prev_t = boundary.prev_t
        prev_dt = boundary.prev_dt
        assert prev_t is not None and prev_dt is not None
        span = coarse_t - prev_t

        expected_new_date = boundary.cam_after.date() if boundary.cam_after else None
        window = list(range(int(prev_t) + 1, int(coarse_t) + self._interval))
        readings = ocr_fn(window)
        any_ocr, last_old_t, first_new_t = _scan_for_transition(
            window, readings, prev_dt, prev_t, self._gap_s, expected_new_date,
        )

        if first_new_t is not None:
            new_dt = readings[int(first_new_t)].dt
            cut = _place_content_aware(
                window, readings, last_old_t, first_new_t, prev_dt, new_dt,
                self._visual_times,
            )
            return RefinementResult(cut, "ocr", "")

        if any_ocr:
            # Dense scan confirmed old-session content but no clean new-session frame.
            # When cam_after confirms a real date change, coarse_t is the t_last of the
            # first new-session coarse bucket and may already contain new-date frames —
            # returning coarse_t would leak them into the outgoing clip.
            # Search for the first garbled frame after last_old_t that contains new-date
            # digit evidence; treat it as the upper bound for _place_content_aware so the
            # gap before it is classified (old garble stays with old clip per ADR-0001).
            # If no such frame exists, cut just after the last confirmed old-session frame.
            # Without cam_after (no confirmed new date) fall through to coarse_t — the
            # boundary may be spurious and we have no placement evidence.
            cam_after = boundary.cam_after
            if cam_after is not None and prev_dt.date() != cam_after.date():
                first_garbled_new = next(
                    (t for t in window if t > last_old_t
                     and readings[t].raw
                     and _gap_date_class(readings[t].raw, prev_dt, cam_after) == "new"),
                    None,
                )
                if first_garbled_new is not None:
                    cut = _place_content_aware(
                        window, readings, last_old_t, float(first_garbled_new),
                        prev_dt, cam_after, self._visual_times,
                    )
                else:
                    cut = last_old_t + 1.0
                return RefinementResult(cut, "ocr", "garbled-boundary")
            # any_ocr=True but no confirmed new date (cam_after absent or same-date):
            # boundary may be spurious; return coarse_t as safe fallback.
            return RefinementResult(coarse_t, "coarse", "all-old-in-window")

        # Pure OCR dead zone (Splice Dead Zone): anchor to LAST visual event.
        if self._visual_times:
            anchors = [vt for vt in self._visual_times if prev_t <= vt < coarse_t]
            if anchors:
                return RefinementResult(max(anchors), "visual", "")

        return RefinementResult(coarse_t, "coarse", f"SDZ {span:.0f}s no-anchor")


def ocr_refinement(
    gap_s: int,
    crop: str,
    tmpdir: str,
    interval: int,
    visual_times: list[float] | None,
) -> RefinementStrategy:
    """Dense OCR-based refinement strategy. Returns a callable over (video, boundary)."""
    ldz   = LongDeadZonePolicy(gap_s, interval)
    short = ShortSpanPolicy(gap_s, interval, visual_times)

    def refine(video: str, boundary: Boundary) -> RefinementResult:
        coarse_t = boundary.video_t
        prev_t = boundary.prev_t
        prev_dt = boundary.prev_dt
        if prev_t is None or prev_dt is None:
            return RefinementResult(coarse_t, "coarse", "no-prev")
        if int(coarse_t) + interval <= int(prev_t) + 1:
            return RefinementResult(coarse_t, "coarse", "empty-window")
        workers = (os.cpu_count() or 4) * 2
        ocr_fn = make_ocr_fn(video, crop, tmpdir, workers)
        span = coarse_t - prev_t
        policy: PlacementPolicy = ldz if span >= SPLICE_DEAD_ZONE_MAX_S else short
        return policy.place(boundary, ocr_fn)

    return refine


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


_PYSCENEDETECT_WARNED = False


def snap_to_scene_cut(
    video: str,
    t: float,
    prev_t: float | None,
    context_s: float = SCENE_SNAP_CONTEXT_S,
    accept_s: float = SCENE_SNAP_ACCEPT_S,
    after_s: float = SCENE_SNAP_AFTER_S,
) -> tuple[float, float | None]:
    """
    Snap an OCR-refined cut time `t` onto a precise shot-change frame using the v2 anchor rule
    (finding 003). Returns (snapped_t, offset) where offset is snapped_t - t (negative = backward,
    positive = forward for burst-end), or None if nothing was snapped.

    Detection window: [t-context_s, t+after_s] — wide enough for AdaptiveDetector rolling context
    and to capture burst-end cuts that land just past t. Accept window: [max(prev_t, t-accept_s),
    t+after_s] — tight to block decoy mid-clip cuts.

    Branch logic:
      1. Two cuts bracketing a short span (≤BURST_MAX_S) → noise burst; anchor to LATER cut so
         the new clip starts after the burst (ADR 0001). May move cut forward.
      2. Single cut ≤ t → clean content change; snap backward (removes old-session tail).
      3. Neither → no-op.
    """
    global _PYSCENEDETECT_WARNED
    try:
        from scenedetect import AdaptiveDetector, detect
    except ImportError:
        if not _PYSCENEDETECT_WARNED:
            print("  (scene-snap disabled: pyscenedetect not installed — pip install scenedetect)")
            _PYSCENEDETECT_WARNED = True
        return t, None

    lo = max(0.0, t - context_s)
    hi = t + after_s
    accept_lo = max(lo, t - accept_s)
    if prev_t is not None:
        accept_lo = max(accept_lo, prev_t)
    if accept_lo >= hi:
        return t, None

    det = AdaptiveDetector(
        adaptive_threshold=SCENE_SNAP_ADAPTIVE_THRESHOLD,
        min_scene_len=SCENE_SNAP_MIN_SCENE_LEN,
    )
    scenes = detect(video, det, start_time=lo, end_time=hi)
    # Each scene's start (after the first) is a detected shot-change frame.
    cuts = [s[0].get_seconds() for s in scenes[1:]]
    accepted = [c for c in cuts if accept_lo <= c <= hi]

    before = [c for c in accepted if c <= t]
    after  = [c for c in accepted if c > t]

    if before and after:
        burst_start = max(before)
        burst_end   = min(after)
        if burst_end - burst_start <= SCENE_SNAP_BURST_MAX_S:
            # Noise burst: anchor to end so new clip starts clean (ADR 0001).
            return burst_end, burst_end - t

    if before:
        # Clean content change: snap backward to remove old-session tail.
        snapped = max(before)
        return snapped, snapped - t

    return t, None


def _ffmpeg_copy_seg(video: str, seg_start: float, seg_end: float, out: str):
    subprocess.run([
        "ffmpeg", "-loglevel", "error",
        "-ss", f"{seg_start:.3f}",
        "-i", video,
        "-t", f"{seg_end - seg_start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy",
        "-video_track_timescale", str(VIDEO_TIMESCALE),
        "-fflags", "+igndts",  # recompute DTS from PTS; prevents non-monotonic DTS from VFR source regions
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
        "-bf", "0",  # no B-frames → DTS=PTS at seg tail; prevents backward DTS at concat seam
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
        # Note: 3-seg clips (lead+body+trail) produce ~3-23 decoder-layer DTS warnings
        # ("non monotonic") in the first ~0.4s when decoded with `ffmpeg -f null -`.
        # Root cause: VFR source PTS irregularities in the stream-copied body propagate
        # through +igndts into the decoder's DTS sequence at the lead→body seam.
        # Container DTS is clean (ffprobe finds 0 non-monotonic events); players are
        # unaffected. Accepted as benign — see docs/adr/0003.
        subprocess.run([
            "ffmpeg", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-video_track_timescale", str(VIDEO_TIMESCALE),
            "-fflags", "+igndts",  # ignore input DTS; recompute from PTS across the concat seam
            "-avoid_negative_ts", "make_zero",
            "-muxpreload", "0",
            "-muxdelay", "0",
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


def _label_for(
    filtered: list[tuple[float, datetime]],
    start: float,
    mode: str = DEFAULT_MODE,
    boundary_map: dict[float, Boundary] | None = None,
) -> str:
    """Return a label string for the clip starting at `start` seconds."""
    # Refined cuts can land one sample before the first new-session frame, leaving
    # the nearest filtered reading still in cam_before's date (old-session tail).
    # Skip any reading whose date matches cam_before of the boundary at this cut.
    boundary = boundary_map.get(start) if boundary_map else None
    fallback: datetime | None = None
    for i, (t, dt) in enumerate(filtered):
        if t >= start:
            if fallback is None:
                fallback = dt
            if not _reading_confirmed(filtered, i):
                continue
            if boundary and boundary.cam_before is not None and dt.date() == boundary.cam_before.date():
                continue
            if mode == "daily":
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d_%H%M")
    if fallback is not None:  # pragma: no cover
        if mode == "daily":
            return fallback.strftime("%Y-%m-%d")
        return fallback.strftime("%Y-%m-%d_%H%M")
    return f"{int(start):05d}s"


def split_video(
    video: str, splits: list[float], out_dir: str, filtered: list[tuple[float, datetime]],
    mode: str = DEFAULT_MODE, boundary_map: dict[float, Boundary] | None = None
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
        print(f"stale_removed count={len(stale)}")

    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        label = _label_for(filtered, start, mode, boundary_map)
        out_path = os.path.join(out_dir, f"{stem}_clip{idx+1:02d}_{label}.mp4")
        exact_start = start if idx > 0 else None
        exact_end = splits[idx + 1] if idx + 1 < len(splits) else None
        print(f"cutting clip={idx+1:02d} start={start:.1f} end={end:.1f} date={label} out={out_path}")
        cut_clip_with_boundary_encode(video, start, end, exact_start, exact_end, out_path)


def run(config: PipelineConfig) -> PipelineResult:
    """Execute the full detection + refinement pipeline. Caller owns cutting."""
    t0 = time.perf_counter()
    phase_times: dict[str, float] = {}

    print(f"scan file={config.video} interval={config.interval} gap={config.gap} mode={config.mode} crop={config.crop}")
    samples = scan(config.video, config.interval, config.crop, cache_path=config.cache)
    phase_times["scan"] = time.perf_counter() - t0

    valid = [(t, dt) for t, dt in samples if dt]
    date_range = f" date_range={valid[0][1].date()}:{valid[-1][1].date()}" if valid else ""
    print(f"ocr ocr_success={len(valid)} ocr_total={len(samples)}{date_range}")

    samples = drop_date_islands(samples)
    samples = drop_year_misread_runs(samples)
    samples = drop_digit_drop_runs(samples)
    filtered: list[tuple[float, datetime]] = filter_ocr_outliers(samples)
    boundaries = find_all_boundaries(filtered, gap_s=config.gap)

    visual_times: list[float] = []
    if not config.dry_run and not config.no_visual_anchor:
        t_vis = time.perf_counter()
        scene_cuts, black_frames = detect_visual_boundaries(
            config.video, config.scene_threshold, config.black_min_duration,
            cache_path=config.visual_cache,
        )
        visual_times = sorted(scene_cuts + black_frames)
        phase_times["visual"] = time.perf_counter() - t_vis
        print(f"visual scene_cuts={len(scene_cuts)} black_frames={len(black_frames)}")
        if config.enable_visual_fusion:
            before = len(boundaries)
            boundaries = fuse_boundaries(boundaries, scene_cuts, black_frames, config.fuse_window)
            print(f"visual_fusion confirmed={len(boundaries)} total={before} window={config.fuse_window:.0f}")

    cut_ts = group_clips(boundaries, config.mode)
    boundary_map: dict[float, Boundary] = {b.video_t: b for b in boundaries}
    duration = get_duration(config.video)
    effective_min_clip = ARTIFACT_MIN_S if config.mode == "daily" else config.min_clip

    if config.dry_run:
        splits: list[float] = [0.0] + list(cut_ts[1:])
        before_merge = len(splits)
        splits = merge_short_clips(splits, effective_min_clip)
        if len(splits) < before_merge:
            print(f"merge_short merged={before_merge - len(splits)} min_clip={effective_min_clip:.0f}")
        print(f"clips count={len(splits)} note=coarse_unrefined interval_error=+-{config.interval}s")
        prev_label: str | None = None
        prev_idx: int | None = None
        for idx, start in enumerate(splits):
            end = splits[idx + 1] if idx + 1 < len(splits) else duration
            dur = end - start
            label = _label_for(filtered, start, config.mode, boundary_map)
            b = boundary_map.get(start)
            btype = f" btype={b.type}" if b else ""
            print(f"clip {idx+1:02d} start={start:.0f} end={end:.0f}"
                  f" dur_s={dur:.0f} dur_min={dur/60:.1f} date={label}{btype}")
            if config.mode == "daily" and prev_label == label and prev_idx is not None:
                prev_dur = start - splits[prev_idx]
                print(f"warn same_date_adjacent clips={prev_idx+1},{idx+1} date={label}"
                      f" durations={prev_dur:.0f}s,{dur:.0f}s")
            prev_label = label
            prev_idx = idx
        print("dry_run=true")
        return PipelineResult(splits, filtered, boundary_map, phase_times)

    refine_count = sum(
        1 for vt in cut_ts[1:]
        if (b := boundary_map.get(vt)) and b.prev_t is not None and b.prev_dt is not None
    )
    print(f"refine count={refine_count}")
    t_refine = time.perf_counter()
    refined_boundary_map: dict[float, Boundary] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        strategy = ocr_refinement(config.gap, config.crop, tmpdir, config.interval, visual_times)
        splits = [0.0]
        for vt in cut_ts[1:]:
            b = boundary_map.get(vt)
            if b and b.prev_t is not None and b.prev_dt is not None:
                t_b = time.perf_counter()
                rr = strategy(config.video, b)
                cut_t, method = rr.t, rr.method
                snap_note = ""
                if config.enable_scene_snap:
                    snapped, off = snap_to_scene_cut(config.video, rr.t, b.prev_t)
                    if off is not None:
                        snap_note = f" snap={off:+.2f}s"
                        cut_t, method = snapped, "snap"
                elapsed_b = time.perf_counter() - t_b
                reason = f" reason={rr.detail}" if rr.detail else ""
                print(
                    f"boundary coarse={vt:.0f} refined={cut_t:.0f} saved={vt - cut_t:.0f}"
                    f" win={int(vt - b.prev_t)} cam_jump={b.cam_jump_s:+.0f}"
                    f" date={b.prev_dt.strftime('%Y-%m-%d')} elapsed={elapsed_b:.1f}"
                    f" method={method}{snap_note}{reason}"
                )
                splits.append(cut_t)
                refined_boundary_map[cut_t] = b
            else:
                splits.append(vt)
                if b:
                    refined_boundary_map[vt] = b
    phase_times["refine"] = time.perf_counter() - t_refine

    before_merge = len(splits)
    splits = merge_short_clips(splits, effective_min_clip)
    if len(splits) < before_merge:
        print(f"merge_short merged={before_merge - len(splits)} min_clip={effective_min_clip:.0f}")

    print(f"clips count={len(splits)}")
    last_label: str | None = None
    last_idx: int | None = None
    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        dur = end - start
        label = _label_for(filtered, start, config.mode, refined_boundary_map)
        print(f"clip {idx+1:02d} start={start:.0f} end={end:.1f} dur_s={dur:.0f} dur_min={dur/60:.1f} date={label}")
        if config.mode == "daily" and last_label == label and last_idx is not None:
            prev_dur = start - splits[last_idx]
            print(f"warn same_date_adjacent clips={last_idx+1},{idx+1} date={label}"
                  f" durations={prev_dur:.0f}s,{dur:.0f}s")
        last_label = label
        last_idx = idx

    return PipelineResult(splits, filtered, refined_boundary_map, phase_times)


# ------------------------------------------------------------------ #

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input video file")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP,
                    help="Camera timestamp gap (seconds) that triggers a new clip")
    ap.add_argument("--mode", choices=["scene", "session", "daily"], default=DEFAULT_MODE,
                    help="Clip grouping mode (default: daily)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--crop", default=None,
                    help="ffmpeg crop 'w:h:x:y' for timestamp region "
                         "(default: auto-detected via calibration pass)")
    ap.add_argument("--calib-cache", default=None,
                    help="JSON file to cache per-tape calibration result (crop auto-detection)")
    ap.add_argument("--cache", default=None,
                    help="JSON file to cache OCR scan results (saves time on re-runs)")
    ap.add_argument("--visual-cache", default=None,
                    help="JSON file to cache scene-cut/black-frame detection (saves time on re-runs)")
    ap.add_argument("--enable-visual-fusion", action="store_true", default=False,
                    help="Drop OCR boundaries lacking visual corroboration (scene cut or black frame). "
                         "Off by default — VHS pause/resume often has no visual discontinuity so "
                         "this filter would delete real boundaries.")
    ap.add_argument("--enable-scene-snap", action="store_true", default=False,
                    help="Ultra-refinement (pass 3): snap each OCR-refined cut backward onto a precise "
                         "shot-change frame within 3s, if one exists. Tightens session boundaries where "
                         "OCR placement leaks a few seconds of the old session into the next clip. "
                         "Monotonic-safe (only tightens; no-op where no shot change exists). "
                         "Off by default pending placement measurement.")
    ap.add_argument("--no-visual-anchor", action="store_true", default=False,
                    help="Skip visual detection entirely (disables splice dead-zone anchoring; faster, "
                         "but cuts at splice boundaries may be misplaced)")
    ap.add_argument("--fuse-window", type=float, default=DEFAULT_FUSE_WINDOW,
                    help="Seconds of padding around [prev_t, video_t] to search for a corroborating visual signal")
    ap.add_argument("--min-clip", type=float, default=DEFAULT_MIN_CLIP_S,
                    help="Clips shorter than this (seconds) are merged into the prior clip "
                         "(ignored in daily mode, which uses a 3s floor to catch only refinement slivers)")
    ap.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD,
                    help="ffmpeg scene-change detection threshold (0-1)")
    ap.add_argument("--black-min-duration", type=float, default=DEFAULT_BLACK_MIN_DURATION,
                    help="Minimum duration (s) of a black frame run to count as a boundary signal")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-cut", action="store_true",
                    help="Run full refinement (including scene-snap) but skip the actual cut step")
    args = ap.parse_args()

    video = os.path.abspath(args.input)
    if not os.path.exists(video):
        sys.exit(f"File not found: {video}")

    if not OCR_BIN.exists():
        sys.exit(f"ocr_timestamp binary not found at {OCR_BIN}. Run: swiftc -O ocr_timestamp.swift -o ocr_timestamp")

    out_dir = args.out_dir or (Path(video).stem + "_clips")
    out_dir = os.path.abspath(out_dir)
    calib_cache = args.calib_cache or (Path(video).stem + "_calib_cache.json")
    cache = args.cache or (Path(video).stem + "_ocr_cache.json")
    visual_cache = args.visual_cache or (Path(video).stem + "_visual_cache.json")

    if args.crop is not None:
        crop = args.crop
    else:
        crop = calibrate(video, calib_cache)

    config = PipelineConfig(
        video=video,
        interval=args.interval,
        gap=args.gap,
        mode=args.mode,
        crop=crop,
        cache=cache,
        visual_cache=visual_cache,
        enable_visual_fusion=args.enable_visual_fusion,
        enable_scene_snap=args.enable_scene_snap,
        no_visual_anchor=args.no_visual_anchor,
        fuse_window=args.fuse_window,
        min_clip=args.min_clip,
        scene_threshold=args.scene_threshold,
        black_min_duration=args.black_min_duration,
        dry_run=args.dry_run,
    )

    t0 = time.perf_counter()
    result = run(config)

    if args.dry_run or args.skip_cut:
        return

    print(f"cutting out_dir={out_dir}")
    t_cut = time.perf_counter()
    split_video(video, result.splits, out_dir, result.filtered, config.mode, result.boundary_map)
    phase_times = dict(result.phase_times)
    phase_times["cut"] = time.perf_counter() - t_cut

    total = time.perf_counter() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    for phase, secs in phase_times.items():
        print(f"phase name={phase} elapsed={secs:.1f} pct={100 * secs / total:.0f}")
    print(f"total elapsed={total:.1f} peak_rss_mb={peak_mb:.0f} child_cpu={ru.ru_utime + ru.ru_stime:.1f}")
    print("done")


if __name__ == "__main__":  # pragma: no cover
    main()
