#!/usr/bin/env python3
"""
Split a home video into logical clips by reading the burned-in timestamp.

Usage:
    python3 split_homevideo.py <input.mp4> [--interval 10] [--gap 300] [--out-dir ./clips]

Arguments:
    --interval  Seconds between sampled frames (default: 10)
    --gap       Time gap (seconds) between consecutive timestamps that
                triggers a new clip, even on the same date (default: 300)
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# --- defaults ---
DEFAULT_INTERVAL = 10          # sample every N seconds
DEFAULT_GAP = 300              # 5-minute gap = new clip
DEFAULT_CROP = "250:110:385:370"  # w:h:x:y for 640x480 bottom-right timestamp

OCR_BIN = Path(__file__).parent / "ocr_timestamp"
VIDEO_TIMESCALE = 29970  # matches source tbn; same on all segs/concat to prevent PTS mis-scaling

# ------------------------------------------------------------------ #
# Timestamp parsing
# ------------------------------------------------------------------ #

DATE_PATTERN = re.compile(
    r"(\d{1,2})[/\s]+(\d{1,2})[/\s]+(\d{2,4})"
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
        "-vf", f"crop={crop}",
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
        "-vf", f"fps=1/{interval},crop={crop}",
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
        if cached.get("interval") == interval and cached.get("crop") == crop:
            print(f"  (loaded from cache: {cache_path})")
            return [
                (float(t), datetime.fromisoformat(dt) if dt else None)
                for t, dt in cached["samples"]
            ]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: single-pass extraction
        print(f"  Extracting frames (single pass, 1 frame/{interval}s)...", flush=True)
        frame_paths = extract_all_frames(video, interval, crop, tmpdir)
        print(f"  Extracted {len(frame_paths)} frames.", flush=True)

        # Phase 2: batch OCR
        print(f"  Running OCR on {len(frame_paths)} frames (batch)...", flush=True)
        ocr_results = ocr_batch(frame_paths)

        # Phase 3: build results keyed by float timestamp
        results: dict[float, datetime | None] = {}
        for path in frame_paths:
            t = float(frame_index(path) * interval)
            text = ocr_results.get(path, "")
            results[t] = parse_timestamp(text) if text else None

    samples = sorted(results.items())

    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "interval": interval,
                "crop": crop,
                "samples": [(t, dt.isoformat() if dt else None) for t, dt in samples],
            }, f)
        print(f"  (scan cached to {cache_path})")

    return samples


def filter_ocr_outliers(
    samples: list[tuple[float, datetime | None]], max_drift_s: float = 900
) -> list[tuple[float, datetime]]:
    """
    Remove isolated OCR misreads from the valid-reading list.

    A reading is kept if it is consistent (within max_drift_s) with EITHER its
    previous OR its next valid neighbor.  A real clip boundary fails the
    "consistent with prev" check but passes "consistent with next" (subsequent
    frames confirm the new date/time).  An isolated OCR error fails both.
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
    kept.append(valid[-1])
    return kept


def find_splits(
    samples: list[tuple[float, datetime | None]], gap_s: int
) -> list[tuple[float, float | None, datetime | None]]:
    """
    Return list of (split_t, prev_t, prev_dt) where a new clip starts.
    First entry is always (0.0, None, None). prev_t/prev_dt are the last
    valid sample before the split — used for boundary refinement.

    Split when camera time advanced MORE than expected (video advance + gap_s),
    or when it went backwards by >30 min. Isolated OCR misreads are filtered
    first; None samples are skipped throughout.
    """
    clean = filter_ocr_outliers(samples)
    splits: list[tuple[float, float | None, datetime | None]] = [(0.0, None, None)]
    prev: tuple[float, datetime] | None = None
    for t, dt in clean:
        if prev is not None:
            prev_t, prev_dt = prev
            video_advance = t - prev_t
            cam_advance = (dt - prev_dt).total_seconds()
            # Forward jump: camera ran much faster than real time (was paused/off)
            jumped_forward = cam_advance > video_advance + gap_s
            # Backward jump: only flag if large (>30 min) — small reversals are OCR noise
            jumped_backward = cam_advance < -1800
            if jumped_forward or jumped_backward:
                splits.append((t, prev_t, prev_dt))
        prev = (t, dt)
    return splits


def refine_split(
    video: str,
    coarse_t: float,
    prev_t: float,
    prev_dt: datetime,
    gap_s: int,
    crop: str,
    tmpdir: str,
) -> float:
    """
    Dense 1s scan of [prev_t+1, coarse_t) to find the transition point.

    Tracks the last frame confirming the OLD session and cuts there + 1s,
    rather than at the first NEW-session frame. This handles OCR returning None
    during the actual switch (camera motion, power-on noise) — those ambiguous
    frames belong to the old clip's tail, not the new clip's head.
    """
    window = list(range(int(prev_t) + 1, int(coarse_t)))
    if not window:
        return coarse_t

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
            return float(last_old_t) + 1.0
        else:
            last_old_t = t
    return coarse_t


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
        "-c:a", "copy",
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

        # Leading boundary: re-encode [exact_start, kf_after]
        body_start = start
        if exact_start is not None:
            kf_after = snap_to_keyframe_forward(video, exact_start)
            if kf_after > exact_start:
                seg = os.path.join(tmpdir, "seg_0_lead.mp4")
                _ffmpeg_encode_seg(video, exact_start, kf_after, seg, crf)
                segs.append(seg)
                body_start = kf_after
            # exact_start == kf_after: already on keyframe, skip B1

        # Trailing boundary: compute kf_before and reserve trail segment
        body_end = end
        trail_seg: str | None = None
        if exact_end is not None:
            kf_before = snap_to_keyframe(video, exact_end)
            if kf_before < exact_end:
                body_end = kf_before
                trail_seg = os.path.join(tmpdir, "seg_2_trail.mp4")
                _ffmpeg_encode_seg(video, kf_before, exact_end, trail_seg, crf)
            # exact_end == kf_before: already on keyframe, skip A2

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


def _label_for(filtered: list[tuple[float, datetime]], start: float) -> str:
    """Return a date/time label string for the clip starting at `start` seconds."""
    for t, dt in filtered:
        if t >= start:
            return dt.strftime("%Y-%m-%d_%H%M")
    return f"{int(start):05d}s"


def split_video(video: str, splits: list[float], out_dir: str, filtered: list[tuple[float, datetime]]):
    os.makedirs(out_dir, exist_ok=True)
    duration = get_duration(video)
    stem = Path(video).stem

    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        label = _label_for(filtered, start)
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
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--crop", default=DEFAULT_CROP,
                    help="ffmpeg crop 'w:h:x:y' for timestamp region")
    ap.add_argument("--cache", default=None,
                    help="JSON file to cache OCR scan results (saves time on re-runs)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    video = os.path.abspath(args.input)
    if not os.path.exists(video):
        sys.exit(f"File not found: {video}")

    out_dir = args.out_dir or (Path(video).stem + "_clips")
    out_dir = os.path.abspath(out_dir)

    cache = args.cache or (Path(video).stem + "_ocr_cache.json")

    if not OCR_BIN.exists():
        sys.exit(f"ocr_timestamp binary not found at {OCR_BIN}. Run: swiftc -O ocr_timestamp.swift -o ocr_timestamp")

    print(f"Scanning: {video}")
    print(f"  interval={args.interval}s  gap={args.gap}s  crop={args.crop}")
    samples = scan(video, args.interval, args.crop, cache_path=cache)

    valid = [(t, dt) for t, dt in samples if dt]
    print(f"\nOCR success: {len(valid)}/{len(samples)} frames")
    if valid:
        print(f"  date range: {valid[0][1].date()} → {valid[-1][1].date()}")

    split_contexts = find_splits(samples, args.gap)
    duration = get_duration(video)

    print(f"\nRefining {len(split_contexts) - 1} split boundary(ies) at 1s resolution...")
    with tempfile.TemporaryDirectory() as tmpdir:
        splits: list[float] = [0.0]
        for coarse_t, prev_t, prev_dt in split_contexts[1:]:
            refined_t = refine_split(video, coarse_t, prev_t, prev_dt, args.gap, args.crop, tmpdir)
            saved = coarse_t - refined_t
            print(f"  coarse={coarse_t:.0f}s → refined={refined_t:.0f}s  (saved {saved:.0f}s)")
            splits.append(refined_t)

    filtered: list[tuple[float, datetime]] = filter_ocr_outliers(samples)

    print(f"\nFound {len(splits)} clip(s):")
    for idx, start in enumerate(splits):
        end = splits[idx + 1] if idx + 1 < len(splits) else duration
        dur = end - start
        label = _label_for(filtered, start)
        print(f"  {idx+1:3d}. {start:7.0f}s → {end:7.0f}s  ({dur/60:5.1f} min)  {label}")

    if args.dry_run:
        if len(splits) > 1:
            print("\nBoundary re-encode ranges (per split):")
            for t in splits[1:]:
                kf_b = snap_to_keyframe(video, t)
                kf_a = snap_to_keyframe_forward(video, t)
                print(f"  boundary re-encode: {kf_b:.1f}s–{t:.1f}s | {t:.1f}s–{kf_a:.1f}s")
        print("\nDry run — not cutting.")
        return

    print(f"\nCutting into {out_dir}/")
    split_video(video, splits, out_dir, filtered)
    print("Done.")


if __name__ == "__main__":
    main()
