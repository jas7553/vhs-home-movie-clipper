# vhs-home-movie-clipper

[![CI](https://github.com/jas7553/vhs-home-movie-clipper/actions/workflows/ci.yml/badge.svg)](https://github.com/jas7553/vhs-home-movie-clipper/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jas7553/vhs-home-movie-clipper/graph/badge.svg)](https://codecov.io/gh/jas7553/vhs-home-movie-clipper)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](https://developer.apple.com/macos/)
[![ffmpeg](https://img.shields.io/badge/made%20with-ffmpeg-green.svg)](https://ffmpeg.org/)

Repeatable pipeline for splitting VHS-ripped home video files into logical clips
using burned-in camcorder timestamps, Apple Vision OCR, and ffmpeg stream copy.

## Files

| File | Purpose |
|---|---|
| `split_homevideo.py` | Main pipeline script |
| `ocr_timestamp.swift` | Apple Vision OCR binary source |
| `ocr_timestamp` | Compiled binary (run `swiftc -O ocr_timestamp.swift -o ocr_timestamp`) |

## Usage

```bash
# Dry run (preview splits, no cutting):
python3 split_homevideo.py "YourFile.mp4" --dry-run

# Full run:
python3 split_homevideo.py "YourFile.mp4"

# Re-tune gap without re-scanning (uses cache):
python3 split_homevideo.py "YourFile.mp4" --gap 3600

# Key flags:
#   --interval N          Seconds between OCR samples (default: 10)
#   --gap N               Camera-time jump threshold in seconds (default: 3600,
#                         empirically validated on 215-boundary golden set; F1=0.920)
#   --mode {scene,session,daily}  Clip grouping mode (default: daily)
#   --crop W:H:X:Y        ffmpeg crop for timestamp region (default tuned for 640×480)
#   --cache PATH          Override default cache file location
#   --out-dir DIR         Output directory (default: <stem>_clips/)
#   --min-clip N          Merge clips shorter than N seconds (default: 120; ignored in daily mode)
```

## How It Works

1. **OCR scan**: Extract 3 frames per `--interval` window via a single ffmpeg pass
   (no per-frame seeks). Crop the bottom-right region (`250:110:385:370` for 640×480),
   apply deinterlace + 4× upscale + contrast enhancement, batch through `ocr_timestamp`
   (Apple Vision, M4-native). Majority vote within each window. Results cached as raw
   OCR text to `<stem>_ocr_cache.json` — parser fixes re-apply at load time, no rescan needed.

2. **Parse**: Extract date (`M/ D/YY`) and time (`H:MM AM/PM`) from OCR text.
   Timestamps where AM/PM is absent are discarded — optional AM/PM caused 12-hour
   backward phantom jumps. Years outside 1985–2005 are rejected as hallucinations.

3. **Outlier filter**: Remove isolated misreads and consecutive misread runs. A reading
   is kept if it is consistent (within 900s drift) with EITHER its previous OR next
   neighbor (within a `max_run=3` step window). Real boundary readings pass the forward
   check; consecutive misread runs fail all neighbors and are dropped.

4. **Boundary detection**: Emit a `Boundary` for each pair where:
   - `cam_advance > video_advance + 60s` (camera paused/off), or
   - `cam_advance < -1800s` (backward jump, new tape segment)
   Type is `large_gap` when jump exceeds `--gap` threshold (default 3600s camera-time).

5. **Grouping**: Filter boundaries to cut points by `--mode`:
   - `daily` (default) — only confirmed calendar date changes; no date split across clips
   - `session` — `large_gap` boundaries only
   - `scene` — all detected pauses
   After grouping, `_collapse_revert_phantoms` removes phantom clips from OCR misreads
   (misread year/month creates a short clip with opposite-sign cam jumps on both sides).

6. **Refinement**: For each `large_gap` boundary, dense 1s scan of the preceding
   `[prev_sample, coarse_t]` window in parallel. Cuts at the last confirmed old-session
   frame rather than the first new-session frame.

7. **Cut**: Re-encodes only small boundary segments (~3–6s) at CRF 18 for frame accuracy;
   stream-copies everything else. Concatenates via ffmpeg concat demuxer.
   Output: `<stem>_clipNN_YYYY-MM-DD.mp4` (daily) or `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4` (other modes).

8. **Cache**: Self-invalidating — keyed on `interval`, `crop`, preprocessing filter chain,
   and `frames_per_sample`. Any change triggers automatic rescan.

## Findings & Nuances

### Camera clock rate
The camcorder's internal clock advances at ~2× real time relative to video playback.
60s of video ≈ 2 min of camera time. Effect: `gap_s` thresholds are in camera-seconds,
not wall-clock seconds. `--gap 3600` (1 camera-hour) is empirically validated on a
215-boundary golden label set (F1=0.920). Prior values of 300 and 900 had unacceptable
false-positive rates.

### OCR reliability
Success rate on the 5.9hr Converse 1990 tape: 1096/2128 samples (51%). Fails when:
- Timestamp region is in motion (pan/shake)
- Lighting is very dark or very bright
- The `1/` separator splits across OCR lines

The outlier filter and `None`-skipping logic make the pipeline robust to ~50% failure.

### Phantom clip collapse (daily mode)
OCR misreads that survive the outlier filter (e.g., reading "1999" instead of "1990"
for a single frame in a transition zone) create a phantom short clip. In `daily` mode,
these phantom clips are detected and removed by `_collapse_revert_phantoms`: a clip
shorter than `min_clip_s` (120s) whose bounding boundaries have opposite-sign
`cam_jump_s` values is a misread — a forward jump to the wrong date immediately
followed by a backward jump back. Validated on Converse 1990.mp4: removed 8 phantom
clips including `1999-05-19`, `1999-07-21`, `1990-01-22` (misread month), and
`1990-09-01` (misread in an April sequence).

### Timestamp format
`M/ D/YY` on bottom line, `H:MM AM/PM` on top line. Single-digit months/days use a
leading space (`1/ 4/90` not `01/04/90`). AM/PM is required — when absent the parser
returns `None` (optional AM/PM caused phantom 12-hour backward jumps).

### Split accuracy
Coarse boundaries land within ±`interval`s of the actual cut. The refinement step
does a dense 1s scan of `[prev_sample, coarse_sample]` and finds the last confirmed
old-session frame. Savings range from 0s to 2180s per boundary (shown in `--dry-run`).
