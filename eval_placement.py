#!/usr/bin/env python3
"""
eval_placement.py — vision-based splice boundary placement evaluator.

For each Splice Dead Zone boundary (all-None OCR span < 120s between two dates):
  1. Binary-searches the original video to find true_change (LLM reads burned-in timestamp)
  2. Accumulates clip durations to find where the pipeline actually cut
  3. Reports per-boundary placement error and overall median

Usage:
    python3 eval_placement.py VIDEO [--ocr-cache PATH] [--clips-dir DIR]
                                    [--crop W:H:X:Y] [--model MODEL]
                                    [--dry-run] [--json-out PATH]
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

try:
    import anthropic  # only needed for the paid binary-search path
except ImportError:
    anthropic = None

DEFAULT_CROP = "250:110:385:370"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
SDZ_MAX_S = 120.0  # None-spans shorter than this = Splice Dead Zone


# ── frame extraction ──────────────────────────────────────────────────────────

def extract_frame_png(video: str, t: float, crop: str) -> bytes | None:
    r = subprocess.run([
        "ffmpeg", "-ss", f"{t:.3f}", "-i", video,
        "-frames:v", "1", "-vf", f"crop={crop}",
        "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        "-loglevel", "error",
    ], capture_output=True)
    return r.stdout if r.returncode == 0 and r.stdout else None


def clip_duration(clip_path: str) -> float:
    r = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", clip_path,
    ], capture_output=True, text=True)
    return float(r.stdout.strip())


# ── vision ────────────────────────────────────────────────────────────────────

def read_date(client: anthropic.Anthropic, png: bytes, model: str) -> str:
    """Return normalized date like '1/4/90', 'NOISE', or 'NONE'."""
    b64 = base64.standard_b64encode(png).decode()
    resp = client.messages.create(
        model=model,
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a cropped region from a VHS home video with a burned-in "
                        "timestamp. Read the DATE from the bottom line (format M/D/YY, "
                        "e.g. '1/4/90'). "
                        "If the image is garbled analog noise: reply NOISE. "
                        "If no text is visible at all: reply NONE. "
                        "Reply with ONLY the date, NOISE, or NONE — nothing else."
                    ),
                },
            ],
        }]
    )
    return resp.content[0].text.strip()


# ── OCR cache parsing ─────────────────────────────────────────────────────────

def parse_date_text(text: str | None) -> datetime | None:
    if not text:
        return None
    m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2})\b', text)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year += 1900 if year >= 85 else 2000
    if not (1985 <= year <= 2005 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def load_sdz_boundaries(cache_path: str) -> list[dict]:
    """
    Walk OCR cache samples; emit a record for each Splice Dead Zone boundary:
    a None-run < SDZ_MAX_S seconds separating two different dates.
    """
    with open(cache_path) as f:
        cache = json.load(f)
    samples = cache["samples"]  # list of [t, text_or_null]

    boundaries = []
    prev_t: float | None = None
    prev_dt: datetime | None = None
    none_run_start: float | None = None

    for t, text in samples:
        dt = parse_date_text(text)

        if dt is None:
            if none_run_start is None and prev_dt is not None:
                none_run_start = t
        else:
            if none_run_start is not None and prev_dt is not None:
                if dt.date() != prev_dt.date():
                    none_span = t - none_run_start
                    if none_span < SDZ_MAX_S:
                        boundaries.append({
                            "coarse_t": t,
                            "prev_t": prev_t,
                            "none_start": none_run_start,
                            "none_span_s": none_span,
                            "old_date": prev_dt.strftime("%-m/%-d/%y"),
                            "new_date": dt.strftime("%-m/%-d/%y"),
                            "old_dt": prev_dt,
                            "new_dt": dt,
                        })
            none_run_start = None
            prev_t = t
            prev_dt = dt

    return boundaries


# ── readings-based true change (free path, no API) ─────────────────────────────

def load_readings_by_coarse(readings_path: str) -> dict[int, dict[int, datetime | None]]:
    """Group a filename->reading map (b<coarse>_t<t>.png) by coarse int.

    Each group is {t: parsed_date | None}; NOISE/NONE/unparseable collapse to None
    (the old/non-new side), matching find_true_change's classification.
    """
    with open(readings_path) as f:
        raw = json.load(f)
    by_coarse: dict[int, dict[int, datetime | None]] = {}
    for name, text in raw.items():
        m = re.match(r'b0*(\d+)_t0*(\d+)\.png', name)
        if not m:
            continue
        coarse, t = int(m.group(1)), int(m.group(2))
        by_coarse.setdefault(coarse, {})[t] = parse_date_text(text)
    return by_coarse


def true_change_from_readings(
    group: dict[int, datetime | None], old_dt: datetime,
) -> float | None:
    """First t whose reading is a NEW-side date (a real date != old session).

    Mirrors find_true_change: NOISE/NONE/unread count as the old side. Returns None
    when no new-side date appears in the readings (transition not captured → skip).
    """
    for t in sorted(group):
        dt = group[t]
        if dt is not None and dt.date() != old_dt.date():
            return float(t)
    return None


# ── binary search for true change ─────────────────────────────────────────────

def find_true_change(
    client: anthropic.Anthropic,
    video: str,
    lo: float,
    hi: float,
    old_date: str,
    new_date: str,
    crop: str,
    model: str,
) -> tuple[float, int]:
    """
    Binary-search [lo, hi] in the original video for the second where date
    changes from old_date to new_date. Returns (true_change_t, vision_calls).
    """
    calls = 0

    def classify(t: float) -> str:
        nonlocal calls
        png = extract_frame_png(video, t, crop)
        if png is None:
            return "NONE"
        calls += 1
        return read_date(client, png, model)

    def is_new(label: str) -> bool:
        # Treat anything that isn't old/noise/none as "new side"
        return label not in ("NOISE", "NONE", old_date)

    lo_t, hi_t = float(lo), float(hi)

    # Binary search down to 2s window
    while hi_t - lo_t > 2.0:
        mid = (lo_t + hi_t) / 2.0
        if is_new(classify(mid)):
            hi_t = mid
        else:
            lo_t = mid

    # Linear sweep of final 2s window at 1s resolution
    result = hi_t
    for t in [lo_t + i for i in range(int(hi_t - lo_t) + 2)]:
        if t > hi_t:
            break
        label = classify(t)
        if is_new(label):
            result = t
            break

    return result, calls


# ── clip inventory ────────────────────────────────────────────────────────────

def build_clip_start_times(clips_dir: str) -> list[tuple[str, float]]:
    """
    Sort clips by clip number, accumulate durations to get each clip's start
    time in original-video coordinates. Returns [(path, start_t), ...].
    """
    clips = sorted(
        Path(clips_dir).glob("*_clip*.mp4"),
        key=lambda p: int(re.search(r'_clip(\d+)', p.name).group(1)),
    )
    result = []
    cursor = 0.0
    for clip in clips:
        result.append((str(clip), cursor))
        cursor += clip_duration(str(clip))
    return result


def find_pipeline_cut_for_boundary(
    clip_starts: list[tuple[str, float]],
    coarse_t: float,
    new_date_str: str,
) -> float | None:
    """
    Find the clip whose start time is closest to coarse_t and whose date label
    matches new_date. Returns that clip's start time as the pipeline cut.
    """
    # Parse new_date_str into a date object for filename matching
    m = re.match(r'(\d+)/(\d+)/(\d+)', new_date_str)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year += 1900 if year >= 85 else 2000
    target_label = f"{year:04d}-{month:02d}-{day:02d}"

    # Find clips near coarse_t whose filename has the new date
    candidates = [
        (path, start)
        for path, start in clip_starts
        if target_label in Path(path).name and abs(start - coarse_t) < 120
    ]
    if not candidates:
        return None
    # Closest to coarse_t
    return min(candidates, key=lambda x: abs(x[1] - coarse_t))[1]


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="Original video file")
    ap.add_argument("--ocr-cache", default=None, help="OCR cache JSON (default: <stem>_ocr_cache.json)")
    ap.add_argument("--clips-dir", default=None, help="Directory of output clips (default: <stem>_clips)")
    ap.add_argument("--crop", default=DEFAULT_CROP)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--readings", default=None, metavar="PATH",
                    help="Derive true_change from a free-path readings.json (b<coarse>_t<t>.png "
                         "-> timestamp/NOISE/NONE) instead of the paid binary-search API. "
                         "Boundaries with no new-side reading are skipped.")
    ap.add_argument("--dry-run", action="store_true", help="Print boundaries from cache only; no vision calls")
    ap.add_argument("--search-window", type=float, default=60.0,
                    help="Seconds before coarse_t to start binary search (default 60)")
    ap.add_argument("--json-out", default=None, help="Write results JSON to this path")
    args = ap.parse_args()

    video = os.path.abspath(args.video)
    stem = Path(video).stem
    cache_path = args.ocr_cache or f"{stem}_ocr_cache.json"
    clips_dir = args.clips_dir or f"{stem}_clips"

    if not os.path.exists(cache_path):
        sys.exit(f"OCR cache not found: {cache_path}  (run pipeline first)")

    print(f"Loading OCR cache: {cache_path}")
    boundaries = load_sdz_boundaries(cache_path)
    print(f"Found {len(boundaries)} Splice Dead Zone boundaries\n")

    if args.dry_run:
        for i, b in enumerate(boundaries, 1):
            print(f"  {i:2d}. coarse={b['coarse_t']:.0f}s  "
                  f"{b['old_date']} → {b['new_date']}  "
                  f"(None-span {b['none_span_s']:.0f}s)")
        return

    # Build clip inventory
    if not os.path.isdir(clips_dir):
        print(f"WARNING: clips dir not found ({clips_dir}) — pipeline cut times unavailable")
        clip_starts = []
    else:
        print(f"Building clip inventory from {clips_dir}...")
        clip_starts = build_clip_start_times(clips_dir)
        total_dur = clip_starts[-1][1] + clip_duration(clip_starts[-1][0])
        print(f"  {len(clip_starts)} clips, total duration {total_dur:.0f}s\n")

    readings_by_coarse = load_readings_by_coarse(args.readings) if args.readings else None
    if readings_by_coarse is None:
        if anthropic is None:
            sys.exit("anthropic package not installed — use --readings for the free path.")
        client = anthropic.Anthropic()
    else:
        client = None
    results = []
    total_calls = 0
    skipped = 0

    for i, b in enumerate(boundaries, 1):
        coarse_t = b["coarse_t"]
        old_date = b["old_date"]
        new_date = b["new_date"]
        lo = max(0.0, coarse_t - args.search_window)
        hi = coarse_t + 5.0  # small buffer past coarse

        print(f"[{i:2d}/{len(boundaries)}] coarse={coarse_t:.0f}s  {old_date} → {new_date}")

        if readings_by_coarse is not None:
            # Match the readings group by nearest coarse int (tolerate rounding drift).
            ckey = min(readings_by_coarse, key=lambda c: abs(c - coarse_t), default=None)
            group = readings_by_coarse.get(ckey) if ckey is not None and abs(ckey - coarse_t) <= 5 else None
            true_change = true_change_from_readings(group, b["old_dt"]) if group else None
            calls = 0
            if true_change is None:
                skipped += 1
                print("         true_change=?  (no new-side reading — skipped)")
                continue
            print(f"         true_change={true_change:.0f}s  (from readings)")
        else:
            print(f"         binary search [{lo:.0f}s, {hi:.0f}s]...")
            true_change, calls = find_true_change(
                client, video, lo, hi, old_date, new_date, args.crop, args.model
            )
            total_calls += calls
            print(f"         true_change={true_change:.0f}s  ({calls} vision calls)")

        pipeline_cut = find_pipeline_cut_for_boundary(clip_starts, coarse_t, new_date)
        if pipeline_cut is not None:
            error = abs(pipeline_cut - true_change)
            coarse_error = abs(coarse_t - true_change)
            print(f"         pipeline_cut={pipeline_cut:.0f}s  error={error:.0f}s  (coarse error={coarse_error:.0f}s)")
        else:
            error = None
            coarse_error = abs(coarse_t - true_change)
            print(f"         pipeline_cut=?  coarse_error={coarse_error:.0f}s  (clip not found in {clips_dir})")

        results.append({
            "boundary_idx": i,
            "coarse_t": coarse_t,
            "old_date": old_date,
            "new_date": new_date,
            "none_span_s": b["none_span_s"],
            "true_change": true_change,
            "pipeline_cut": pipeline_cut,
            "placement_error_s": error,
            "coarse_error_s": coarse_error,
            "vision_calls": calls,
        })
        print()

    # Summary
    errors = [r["placement_error_s"] for r in results if r["placement_error_s"] is not None]
    coarse_errors = [r["coarse_error_s"] for r in results]

    print("=" * 60)
    print(f"Splice Dead Zone boundaries evaluated: {len(results)}"
          + (f"  (skipped {skipped} with no new-side reading)" if skipped else ""))
    if readings_by_coarse is None:
        print(f"Vision calls total: {total_calls}")
    if errors:
        print("\nPlacement error (pipeline vs true_change):")
        print(f"  median : {median(errors):.1f}s")
        print(f"  max    : {max(errors):.1f}s")
        print(f"  mean   : {sum(errors)/len(errors):.1f}s")
    print("\nCoarse error (coarse_t vs true_change):")
    print(f"  median : {median(coarse_errors):.1f}s")
    print(f"  max    : {max(coarse_errors):.1f}s")
    print(f"  mean   : {sum(coarse_errors)/len(coarse_errors):.1f}s")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"boundaries": results, "summary": {
                "count": len(results),
                "pipeline_median_error_s": median(errors) if errors else None,
                "coarse_median_error_s": median(coarse_errors),
                "total_vision_calls": total_calls,
            }}, f, indent=2)
        print(f"\nResults written to {args.json_out}")


if __name__ == "__main__":
    main()
