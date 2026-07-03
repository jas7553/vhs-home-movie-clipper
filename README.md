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
#                         empirically validated by date-purity audit of the output)
#   --mode {scene,session,daily}  Clip grouping mode (default: daily)
#   --crop W:H:X:Y        ffmpeg crop for timestamp region (default tuned for 640×480)
#   --out-dir DIR         Output directory (default: <stem>_clips/)
#   --enable-scene-snap / --no-enable-scene-snap   Sub-second shot-change snap (default: on)
#   --enable-visual-fusion  Drop OCR boundaries lacking visual corroboration (default: off)
#   --no-visual-anchor    Disable cached visual anchors for splice placement
#   --dry-run             Preview splits without cutting
```

## How It Works

1. **OCR scan**: Extract 3 frames per `--interval` window via a single ffmpeg pass
   (no per-frame seeks). Crop the bottom overlay band (`560:130:40:350` for 640×480)
   and batch through `ocr_timestamp` (Apple Vision, M4-native). The scan is **crop-only
   first** — that has the higher OCR yield (~67% vs ~45% per frame); the deinterlace +
   4× upscale + contrast `_VF_PREPROCESS` chain runs only as a **fallback** on windows
   crop-only could not read (it uniquely recovers a few %). Majority vote within each
   window. Results cached as raw OCR text to `<stem>_ocr_cache.json` — parser fixes
   re-apply at load time, no rescan needed.

2. **Parse**: Extract date (`M/ D/YY`) and time (`H:MM AM/PM`) from OCR text. A reading
   with no time, or a time with no AM/PM, falls back to **midnight** and keeps the date —
   the camcorder overlay can be set to date-only for long spans, and daily-mode cuts on
   the date. Years outside 1985–2005 are rejected as hallucinations.

3. **Filter chain**: Remove OCR misreads before boundary detection — date islands
   (single isolated off-date reading), year-misread runs, digit-drop runs
   (`NOV 26` → `NOV 6`), month-digit confusion runs (`1↔5`, `8↔9`), day-digit
   confusion runs (`6↔8`, `5↔6`; droppable runs capped at `_CONFUSION_RUN_MAX`
   readings so long real sessions are never dropped), then drift-inconsistent
   outliers. See `CLAUDE.md` stage 2 for the exact order and rules.

4. **Boundary detection**: Emit a `Boundary` for each pair where:
   - `cam_advance > video_advance + 60s` (camera paused/off), or
   - `cam_advance < -1800s` (backward jump, new tape segment)
   Type is `large_gap` when jump exceeds `--gap` threshold (default 3600s camera-time).

   Phantom date-change boundaries from isolated OCR misreads are prevented **upstream**
   by `drop_date_islands` (a single reading whose date differs from both neighbours is
   dropped before boundary detection), so grouping never sees them.

5. **Grouping**: Filter boundaries to cut points by `--mode`:
   - `daily` (default) — only confirmed calendar date changes; no date split across clips
   - `session` — `large_gap` boundaries only
   - `scene` — all detected pauses

6. **Refinement**: For each `large_gap` boundary, dense 1s scan of
   `[prev_sample − 20s, coarse_t + interval]` in parallel (the lookback pad corrects
   coarse-label drift; a monotonic floor stops the window crossing the previous
   boundary's cut). Cuts at the last confirmed old-session frame rather than the first
   new-session frame. A scene-snap pass (PySceneDetect, default on) then snaps each cut
   sub-second onto a detected shot change — backward for clean cuts, forward to
   burst-end at noise splices.

7. **Cut**: Re-encodes only small boundary segments (~3–6s) at CRF 18 for frame accuracy;
   stream-copies everything else. Concatenates via ffmpeg concat demuxer.
   Output: `<stem>_clipNN_YYYY-MM-DD.mp4` (daily) or `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4` (other modes).

8. **Cache**: Self-invalidating — keyed on `interval`, `crop`, preprocessing filter chain,
   and `frames_per_sample`. Any change triggers automatic rescan.

## Findings & Nuances

### Camera clock rate
The camcorder's internal clock advances at ~2× real time relative to video playback.
60s of video ≈ 2 min of camera time. Effect: `gap_s` thresholds are in camera-seconds,
not wall-clock seconds. `--gap 3600` (1 camera-hour) is empirically validated by
date-purity audit of the resulting clips — there is no labeled benchmark; the former
AI-labeled golden set was abandoned as unreliable (see
`docs/adr/0001-splice-boundary-placement-policy.md`). Prior values of 300 and 900 had
unacceptable false-positive rates.

### OCR reliability
Per-window success rate on the 5.9hr Converse 1990 tape: 1824/2128 windows (~86%) —
crop-only primary + preprocessing fallback + 3-frame majority vote + date-only
acceptance. Fails when:
- Timestamp region is in motion (pan/shake)
- Lighting is very dark or very bright
- Head-switch noise at a tape splice blanks the overlay entirely (Splice Dead Zone)

The outlier filter and `None`-skipping logic make the pipeline robust to the rest.

### Date-island removal (daily mode)
An isolated OCR misread (e.g. reading "1999" instead of "1990", or a wrong day, for a
single frame in a transition zone) would otherwise create a phantom date-change
boundary. `drop_date_islands` removes these **before** boundary detection: a single
reading whose date differs from **both** neighbours is a misread and is dropped. A real
session is a contiguous run of ≥2 same-date readings and is never dropped, so genuine
short / out-of-order sessions survive (e.g. a `9/01` run physically between `3/25` and
`4/08` on a re-recorded tape). This replaced the earlier cut-level
`_collapse_revert_phantoms` heuristic, which mis-merged two real sessions when a misread
landed exactly on a real session change.

### Timestamp format
`M/ D/YY` on bottom line, `H:MM AM/PM` on top line. Single-digit months/days use a
leading space (`1/ 4/90` not `01/04/90`). A reading with no time — or a time with no
AM/PM — falls back to **midnight** and keeps the date (the overlay can be set to
date-only for long spans). Earlier the parser required AM/PM and returned `None`
otherwise; that discarded real date-only spans and collapsed multiple dates into one
clip, so the guard was replaced by the midnight fallback (the 12-hour-jump hazard it
guarded against is avoided by dropping the ambiguous time, not the date).

### Split accuracy
Coarse boundaries land within ±`interval`s of the actual cut; refinement (dense 1s
scan + scene-snap) brings them to the per-class acceptance spec in
`docs/adr/0004-placement-acceptance-criteria.md`: ≤1s where OCR is legible on both
sides, ≤0.5s at visible shot changes, content-purity-only inside splice noise bursts
(frame-accuracy is unsatisfiable there — the burst stays with the outgoing clip's
tail by design). Placement is measured per boundary by a local ruler tool, not
eyeballed; see the ADR for what a correct spot-check looks like.
