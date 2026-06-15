# Spec: Replace per-frame ffmpeg subprocesses with single-pass extraction

## Problem

`scan()` in `split_homevideo.py` spawns one `ffmpeg` subprocess per sample frame —
2,124 calls on a 5.9hr video at `--interval 10`. Each call seeks into a 4GB H.264
file, which requires scanning back to the nearest keyframe (potentially 30–60s of
compressed data), decoding all intermediate frames, and discarding them. This
repeated seek-decode-discard cycle is the dominant cost of the entire pipeline.

Profiled cost per frame: ~150–250ms (seek + decode + BMP write). Total scan time:
~5–10 minutes for a 5.9hr video.

## Fix

Replace the per-frame subprocess loop with a single `ffmpeg` invocation that decodes
the file sequentially, applying a `fps=1/N` filter to emit one frame every N seconds.
No seeking. One process. Linear decode. Estimated speedup: 5–15×.

## Scope

**Only change `scan()`** in `split_homevideo.py`.

- `extract_frame()` is also called by `refine_split()`, but refinement windows are
  ~10 frames each (negligible cost). Leave `extract_frame()` and `refine_split()`
  unchanged.
- `ocr_batch()`, `parse_timestamp()`, `filter_ocr_outliers()`, `find_splits()`,
  `split_video()`, and `refine_split()` are all untouched.
- Cache format is unchanged (same JSON schema).

## Implementation

### New ffmpeg command (replaces the ThreadPoolExecutor loop)

```python
def extract_all_frames(video: str, interval: int, crop: str, tmpdir: str) -> list[str]:
    """
    Single-pass: decode video sequentially at 1/interval fps, crop each frame.
    Returns list of BMP paths in timestamp order (frame 0 = t=0, frame 1 = t=interval, ...).
    """
    out_pattern = os.path.join(tmpdir, "frame_%06d.bmp")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", video,
        "-vf", f"fps=1/{interval},crop={crop}",
        "-start_number", "0",
        "-y", out_pattern,
    ]
    subprocess.run(cmd, check=True)
    # Collect in sorted order — filename sort = chronological order
    return sorted(glob.glob(os.path.join(tmpdir, "frame_*.bmp")))
```

Note: `-hwaccel videotoolbox` is intentionally dropped. Software-filter pipelines
(`fps`, `crop`) require a software pixel format; mixing videotoolbox hardware decode
with software filters needs `hwdownload,format=nv12` inserted before `crop`, adding
complexity for no meaningful gain on 640×480 source. Software H.264 decode of
640×480 at ~0.1fps effective throughput is not a bottleneck.

### Mapping frame files → timestamps

Frame filenames are zero-indexed integers (`frame_000000.bmp` = t=0,
`frame_000001.bmp` = t=interval, etc.). Extract the index from the filename:

```python
def frame_index(path: str) -> int:
    return int(re.search(r"frame_(\d+)\.bmp", os.path.basename(path)).group(1))

# t in seconds:
t = frame_index(path) * interval
```

### Revised `scan()` structure

```python
def scan(video, interval, crop, cache_path=None):
    # --- cache check (unchanged) ---
    if cache_path and os.path.exists(cache_path):
        ...  # same as current

    duration = get_duration(video)
    expected = int(duration) // interval  # approximate; actual count may differ by 1

    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: single-pass extraction
        print(f"  Extracting frames (single pass, 1 frame/{interval}s)...", flush=True)
        frame_paths = extract_all_frames(video, interval, crop, tmpdir)
        print(f"  Extracted {len(frame_paths)} frames.", flush=True)

        # Phase 2: batch OCR (unchanged)
        print(f"  Running OCR on {len(frame_paths)} frames (batch)...", flush=True)
        ocr_results = ocr_batch(frame_paths)

        # Phase 3: build results dict keyed by float timestamp
        results: dict[float, datetime | None] = {}
        for path in frame_paths:
            t = float(frame_index(path) * interval)
            text = ocr_results.get(path, "")
            results[t] = parse_timestamp(text) if text else None

    samples = sorted(results.items())

    # --- cache save (unchanged) ---
    ...

    return samples
```

Also add `import glob` at the top of the file (needed for `glob.glob`).

### Remove `extract_frame()` from `scan()` call path

The `extract_frame()` function itself stays in the file (still used by
`refine_split()`). Just stop calling it from `scan()`.

## Edge Cases

**Frame count mismatch**: `fps=1/interval` may emit `floor(duration/interval)` or
`ceil(duration/interval)` frames depending on rounding. The index-to-timestamp
mapping (`index * interval`) is correct regardless. If the last frame's calculated
`t` slightly exceeds `duration`, it's harmless — `parse_timestamp` just returns a
datetime and the split logic clips at video end anyway.

**Cache invalidation**: Cache is keyed on `interval` and `crop` (current behavior).
Single-pass extraction uses the same interval and crop values, so existing caches
remain valid and reusable.

**`interval` not dividing duration evenly**: Handled by the frame index mapping.
No behavior change vs. current code.

**ffmpeg unavailable or error**: `subprocess.run(cmd, check=True)` raises
`CalledProcessError`. Current code uses `capture_output=True` in `extract_frame()`
and checks returncode. New code uses `check=True` for simplicity — on failure the
whole scan aborts with a clear traceback. Acceptable given single-pass semantics
(partial output is useless).

## What NOT to change

- `extract_frame()` — keep for `refine_split()`
- `ocr_batch()` — interface unchanged (takes list of paths, returns dict)
- All downstream logic (`filter_ocr_outliers`, `find_splits`, `refine_split`,
  `split_video`, `main`)
- Cache JSON schema
- CLI flags and defaults
- Progress output format for OCR phase

## Verification

After implementing:

1. `python3 split_homevideo.py "YourFile.mp4" --dry-run` should produce
   identical split points to the current implementation (same clips, same timestamps).
   Compare against existing cache output if available.
2. Check frame count: `ls <stem>_clips_tmpdir/frame_*.bmp | wc -l` should be ~2124
   for a 5.9hr video at interval=10.
3. Time the scan phase before/after. Target: under 60s for a 5.9hr video
   (vs. current ~5–10min).
