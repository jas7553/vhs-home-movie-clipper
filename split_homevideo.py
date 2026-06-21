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
                (default tuned for 640x480 with bottom-right overlay)
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
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Protocol

# --- defaults ---
DEFAULT_INTERVAL = 10          # sample every N seconds
DEFAULT_GAP = 3600             # 1-hour camera-time gap = new clip (empirically tuned; see golden_labels analysis)
DEFAULT_CROP = "250:110:385:370"  # w:h:x:y for 640x480 bottom-right timestamp
DEFAULT_MODE = "daily"
DEFAULT_SCENE_THRESHOLD = 0.4
DEFAULT_BLACK_MIN_DURATION = 0.1
DEFAULT_FUSE_WINDOW = 5.0      # seconds within which a visual signal corroborates an OCR boundary
DEFAULT_MIN_CLIP_S = 120.0     # clips shorter than this are merged into prior clip; validated on golden set
ARTIFACT_MIN_S = 3.0           # hard floor applied in all modes; catches refinement-collision slivers
SPLICE_DEAD_ZONE_MAX_S = 120.0 # None-span up to this = Splice Dead Zone (vision/anchor applies);
                               # wider = Long Dead Zone, falls back to coarse_t (ADR 0001, out of scope)

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


def _vote_bucket(paths: list[str], ocr: dict[str, str]) -> tuple[datetime, str] | None:
    """Majority-vote the parseable readings in one window. Returns (winner_dt, raw_text) or None."""
    parsed = [(p, text, parse_timestamp(text)) for p in paths for text in (ocr.get(p, ""),)]
    valid = [(p, text, dt) for p, text, dt in parsed if dt is not None]
    if not valid:
        return None
    winner_dt = Counter(dt for _, _, dt in valid).most_common(1)[0][0]
    winner_text = next(text for _, text, dt in valid if dt == winner_dt)
    return winner_dt, winner_text


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
        # Phase 1: crop-only extraction (PRIMARY — higher OCR yield than preprocessing; see _VF_PREPROCESS).
        crop_dir = os.path.join(tmpdir, "crop")
        os.makedirs(crop_dir)
        print(f"  Extracting frames (crop-only, {FRAMES_PER_SAMPLE} frames/{interval}s)...", flush=True)
        crop_paths = extract_all_frames(video, interval, crop, crop_dir, preprocess=False)
        print(f"  Extracted {len(crop_paths)} frames. Running OCR (batch)...", flush=True)
        crop_ocr = ocr_batch(crop_paths)
        crop_buckets = _bucket_frames(crop_paths)

        # Per-window majority vote; remember which windows crop-only could not read.
        readings: dict[int, tuple[datetime, str]] = {}
        unsolved: list[int] = []
        for bk, paths in crop_buckets.items():
            won = _vote_bucket(paths, crop_ocr)
            if won is not None:
                readings[bk] = won
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
                    won = _vote_bucket(pp_buckets.get(bk, []), pp_ocr)
                    if won is not None:
                        readings[bk] = won
            else:
                print(f"  (skipped fallback: frame-count mismatch "
                      f"{len(pp_paths)} != {len(crop_paths)})", flush=True)

        # Assemble results keyed by t_last_frame of each window — the actual video time of the
        # last extracted frame in the window (a true upper bound on when any event was observed),
        # not the window-start label. raw_text is cached so parser fixes propagate without re-scan.
        results: dict[float, tuple[datetime | None, str | None]] = {}
        for bk, paths in crop_buckets.items():
            t_last = float(max(frame_index(p) for p in paths)) * interval / FRAMES_PER_SAMPLE
            results[t_last] = readings.get(bk) or (None, None)

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
    samples: list[tuple[float, datetime]],
    min_gap_s: int = _MIN_GAP_S,
    gap_s: int = DEFAULT_GAP,
) -> list["Boundary"]:
    """
    Stage 1: emit every candidate boundary at a low detection floor (min_gap_s).

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


def drop_date_islands(
    samples: list[tuple[float, datetime]],
) -> list[tuple[float, datetime]]:
    """Drop 'date islands' — a reading whose date differs from BOTH neighbours.

    A real recording session is a contiguous run of same-date readings. A single
    isolated reading of a date that neither the previous nor the next reading
    shares is an OCR misread (wrong day/month/year). Removing these here, before
    boundary detection, stops them from creating spurious boundaries — crucially
    including a misread that lands exactly on a real session change, which would
    otherwise look like a phantom and cause two real sessions to be merged.

    Replaces the old cut-level phantom collapse: unlike that heuristic (which
    keyed on opposite-sign camera jumps + short duration), this never merges a
    genuine short session — e.g. an out-of-order date run on a re-recorded tape —
    into its neighbour. A real session has >= 2 consecutive readings and so is
    never an island.
    """
    if len(samples) < 3:
        return samples
    kept = [samples[0]]
    for i in range(1, len(samples) - 1):
        d = samples[i][1].date()
        if d != samples[i - 1][1].date() and d != samples[i + 1][1].date():
            continue
        kept.append(samples[i])
    kept.append(samples[-1])
    return kept


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


def group_clips(boundaries: list["Boundary"], mode: str, gap_s: int) -> list[float]:
    """
    Stage 2: decide which boundaries become cut points based on mode.

    Returns list[float] of video_t values with 0.0 prepended.
      scene   — all boundaries (gap + large_gap)
      session — large_gap only
      daily   — only confirmed date changes or backward jumps; unknown-date
                boundaries are skipped (avoids splitting same-day footage)

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
) -> tuple[bool, float, float | None]:
    """Walk frame timestamps in order. Return (any_ocr, last_old_t, first_new_t).
    first_new_t is None if no new-session frame was found."""
    any_ocr = False
    last_old_t: float = prev_t
    for t in times:
        reading = readings.get(t)
        dt = reading.dt if reading is not None else None
        if dt is None:
            continue
        any_ocr = True
        cam_advance = (dt - prev_dt).total_seconds()
        video_advance = float(t) - prev_t
        if cam_advance > video_advance + gap_s or cam_advance < -1800:
            return any_ocr, last_old_t, float(t)
        last_old_t = float(t)
    return any_ocr, last_old_t, None


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
) -> float:
    """Decide the cut across the unreadable gap between the last confirmed old-session
    frame and the first confirmed new-session frame.

    The gap is one of three things and the correct cut differs (REQUIREMENTS L23/L25):
      - garbled NEW-date footage  -> cut at its start, so none leaks into the old clip;
      - garbled OLD-date footage  -> keep it with the old clip;
      - a head-switch noise burst -> keep it with the old clip (ADR-0001 end-of-burst).
    Classify each gap frame by its recoverable date digits. Old content extends the
    confirmed-old run through the last 'old'-classified frame; the cut lands on the
    first 'new'-classified frame after that. When no new-date content is visible in
    the gap (pure noise, or all-old garble), fall back to the conservative
    end-of-gap placement, which keeps the ambiguous span with the outgoing clip."""
    fallback = max(last_old_t + 1.0, first_new_t - 1.0)
    if new_dt is None or new_dt.date() == old_dt.date():
        return fallback
    gap = [t for t in window if last_old_t < t < first_new_t]
    classes = {
        t: _gap_date_class(readings[t].raw, old_dt, new_dt)
        for t in gap if t in readings
    }
    last_old_garble = max(
        (t for t in gap if classes.get(t) == "old"), default=int(last_old_t),
    )
    new_frames = [t for t in gap if t > last_old_garble and classes.get(t) == "new"]
    return float(min(new_frames)) if new_frames else fallback


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

        window = list(range(int(prev_t) + 1, int(coarse_t) + self._interval))
        step = max(2, len(window) // 50)
        coarse_times = window[::step]
        readings: dict[int, Reading] = dict(ocr_fn(coarse_times))
        any_ocr_c, last_old_c, first_new_c = _scan_for_transition(
            coarse_times, readings, prev_dt, prev_t, self._gap_s,
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
            window, readings, prev_dt, prev_t, self._gap_s,
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

        window = list(range(int(prev_t) + 1, int(coarse_t) + self._interval))
        readings = ocr_fn(window)
        any_ocr, last_old_t, first_new_t = _scan_for_transition(
            window, readings, prev_dt, prev_t, self._gap_s,
        )

        if first_new_t is not None:
            new_dt = readings[int(first_new_t)].dt
            cut = _place_content_aware(window, readings, last_old_t, first_new_t, prev_dt, new_dt)
            return RefinementResult(cut, "ocr", "")

        # Old-session confirmed but new-session OCR garbled (frames after last_old_t
        # returned None — extracted but parse_timestamp rejected them, e.g. missing day field).
        # Guard: gap after last confirmed old > 10s (1 coarse interval) to avoid triggering
        # on normal end-of-window sparseness.
        if (any_ocr
                and (coarse_t - last_old_t) > 10
                and any((r := readings.get(t)) is None or r.dt is None
                        for t in window if t > int(last_old_t))):
            return RefinementResult(last_old_t + 1.0, "ocr", f"garbled-new after {last_old_t:.0f}s")

        # Splice Dead Zone: anchor to LAST visual event within [prev_t, coarse_t).
        if not any_ocr and self._visual_times:
            anchors = [vt for vt in self._visual_times if prev_t <= vt < coarse_t]
            if anchors:
                return RefinementResult(max(anchors), "visual", "")

        detail = f"SDZ {span:.0f}s no-anchor" if not any_ocr else "all-old-in-window"
        return RefinementResult(coarse_t, "coarse", detail)


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


def _label_for(
    filtered: list[tuple[float, datetime]],
    start: float,
    mode: str = "session",
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
    mode: str = "session", boundary_map: dict[float, Boundary] | None = None
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
    ap.add_argument("--crop", default=DEFAULT_CROP,
                    help="ffmpeg crop 'w:h:x:y' for timestamp region")
    ap.add_argument("--cache", default=None,
                    help="JSON file to cache OCR scan results (saves time on re-runs)")
    ap.add_argument("--visual-cache", default=None,
                    help="JSON file to cache scene-cut/black-frame detection (saves time on re-runs)")
    ap.add_argument("--enable-visual-fusion", action="store_true", default=False,
                    help="Drop OCR boundaries lacking visual corroboration (scene cut or black frame). "
                         "Off by default — VHS pause/resume often has no visual discontinuity so "
                         "this filter would delete real boundaries.")
    ap.add_argument("--no-visual-anchor", action="store_true", default=False,
                    help="Skip visual detection entirely (disables splice dead-zone anchoring; faster, "
                         "but cuts at splice boundaries may be misplaced)")
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

    t0 = time.perf_counter()
    phase_times: dict[str, float] = {}

    print(f"scan file={video} interval={args.interval} gap={args.gap} mode={args.mode} crop={args.crop}")
    samples = scan(video, args.interval, args.crop, cache_path=cache)
    phase_times["scan"] = time.perf_counter() - t0

    valid = [(t, dt) for t, dt in samples if dt]
    date_range = f" date_range={valid[0][1].date()}:{valid[-1][1].date()}" if valid else ""
    print(f"ocr ocr_success={len(valid)} ocr_total={len(samples)}{date_range}")

    filtered: list[tuple[float, datetime]] = filter_ocr_outliers(samples)
    filtered = drop_date_islands(filtered)
    boundaries = find_all_boundaries(filtered, gap_s=args.gap)

    visual_times: list[float] = []
    if not args.dry_run and not args.no_visual_anchor:
        t_vis = time.perf_counter()
        scene_cuts, black_frames = detect_visual_boundaries(
            video, args.scene_threshold, args.black_min_duration, cache_path=visual_cache
        )
        visual_times = sorted(scene_cuts + black_frames)
        phase_times["visual"] = time.perf_counter() - t_vis
        print(f"visual scene_cuts={len(scene_cuts)} black_frames={len(black_frames)}")
        if args.enable_visual_fusion:
            before = len(boundaries)
            boundaries = fuse_boundaries(boundaries, scene_cuts, black_frames, args.fuse_window)
            print(f"visual_fusion confirmed={len(boundaries)} total={before} window={args.fuse_window:.0f}")

    cut_ts = group_clips(boundaries, args.mode, args.gap)
    duration = get_duration(video)

    # Build lookup: video_t → Boundary for refinement decisions
    boundary_map = {b.video_t: b for b in boundaries}

    # Daily mode keeps all date changes regardless of duration, but still collapses
    # sub-second slivers that result from two refined cuts landing 1s apart.
    effective_min_clip = ARTIFACT_MIN_S if args.mode == "daily" else args.min_clip

    if args.dry_run:
        splits: list[float] = [0.0] + list(cut_ts[1:])
        before_merge = len(splits)
        splits = merge_short_clips(splits, effective_min_clip)
        if len(splits) < before_merge:
            print(f"merge_short merged={before_merge - len(splits)} min_clip={effective_min_clip:.0f}")
        print(f"clips count={len(splits)} note=coarse_unrefined interval_error=+-{args.interval}s")
        prev_label: str | None = None
        prev_idx: int | None = None
        for idx, start in enumerate(splits):
            end = splits[idx + 1] if idx + 1 < len(splits) else duration
            dur = end - start
            label = _label_for(filtered, start, args.mode, boundary_map)
            b = boundary_map.get(start)
            btype = f" btype={b.type}" if b else ""
            print(f"clip {idx+1:02d} start={start:.0f} end={end:.0f}"
                  f" dur_s={dur:.0f} dur_min={dur/60:.1f} date={label}{btype}")
            if args.mode == "daily" and prev_label == label and prev_idx is not None:
                prev_dur = start - splits[prev_idx]
                print(f"warn same_date_adjacent clips={prev_idx+1},{idx+1} date={label}"
                      f" durations={prev_dur:.0f}s,{dur:.0f}s")
            prev_label = label
            prev_idx = idx
        print("dry_run=true")
        return

    large_gap_count = sum(1 for vt in cut_ts[1:] if boundary_map.get(vt) and boundary_map[vt].type == "large_gap")
    print(f"refine count={large_gap_count}")
    t_refine = time.perf_counter()
    refined_boundary_map: dict[float, Boundary] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        strategy = ocr_refinement(args.gap, args.crop, tmpdir, args.interval, visual_times)
        splits = [0.0]
        for vt in cut_ts[1:]:
            b = boundary_map.get(vt)
            if b and b.type == "large_gap" and b.prev_t is not None and b.prev_dt is not None:
                t_b = time.perf_counter()
                result = strategy(video, b)
                elapsed_b = time.perf_counter() - t_b
                reason = f" reason={result.detail}" if result.detail else ""
                print(f"boundary coarse={vt:.0f} refined={result.t:.0f} saved={vt - result.t:.0f}"
                      f" win={int(vt - b.prev_t)} cam_jump={b.cam_jump_s:+.0f}"
                      f" date={b.prev_dt.strftime('%Y-%m-%d')} elapsed={elapsed_b:.1f} method={result.method}{reason}")
                splits.append(result.t)
                refined_boundary_map[result.t] = b
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
        label = _label_for(filtered, start, args.mode, refined_boundary_map)
        print(f"clip {idx+1:02d} start={start:.0f} end={end:.1f} dur_s={dur:.0f} dur_min={dur/60:.1f} date={label}")
        if args.mode == "daily" and last_label == label and last_idx is not None:
            prev_dur = start - splits[last_idx]
            print(f"warn same_date_adjacent clips={last_idx+1},{idx+1} date={label}"
                  f" durations={prev_dur:.0f}s,{dur:.0f}s")
        last_label = label
        last_idx = idx

    print(f"cutting out_dir={out_dir}")
    t_cut = time.perf_counter()
    split_video(video, splits, out_dir, filtered, args.mode, refined_boundary_map)
    phase_times["cut"] = time.perf_counter() - t_cut

    total = time.perf_counter() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    for phase, secs in phase_times.items():
        print(f"phase name={phase} elapsed={secs:.1f} pct={100 * secs / total:.0f}")
    print(f"total elapsed={total:.1f} peak_rss_mb={peak_mb:.0f} child_cpu={ru.ru_utime + ru.ru_stime:.1f}")
    print("done")


if __name__ == "__main__":
    main()
