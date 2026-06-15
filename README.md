# vhs-home-movie-clipper

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
python3 split_homevideo.py "YourFile.mp4" --gap 1800

# Key flags:
#   --interval N   Seconds between OCR samples (default: 10)
#   --gap N        Camera-time jump threshold for new clip in seconds (default: 300,
#                  but 900 was used in practice — see Findings below)
#   --crop W:H:X:Y ffmpeg crop for timestamp region (default tuned for 640×480)
#   --cache PATH   Override default cache file location
#   --out-dir DIR  Output directory (default: <stem>_clips/)
```

## How It Works

1. **OCR scan**: Sample one frame every `--interval` seconds. Crop the bottom-right
   region where the burned-in camcorder timestamp lives (`250:110:385:370` for
   640×480). Run each crop through `ocr_timestamp` (Apple Vision, M4-native).

2. **Parse**: Extract date (`M/ D/YY`) and time (`H:MM AM/PM`) from OCR text.
   Multi-line OCR noise is handled by flattening newlines before regex matching.
   Timestamps where time fails to parse are discarded (returning `None`) — defaulting
   to `00:00` caused large false jumps.

3. **Outlier filter**: Before split detection, remove isolated OCR misreads. A
   reading is kept if it is consistent (within 900s drift) with EITHER its previous
   OR its next valid neighbor. This preserves real clip boundaries (consistent with
   next neighbor) while removing single-frame noise (inconsistent with both).

4. **Split detection**: For each pair of consecutive valid readings `(t1, dt1)` and
   `(t2, dt2)`:
   - `video_advance = t2 - t1` (seconds of video between samples)
   - `cam_advance = (dt2 - dt1).total_seconds()`
   - Split if `cam_advance > video_advance + gap_s` (camera was off/paused)
   - Split if `cam_advance < -1800` (large backward jump, new segment)
   - `None` samples are skipped without resetting state

5. **Cut**: `ffmpeg -ss <start> -i input -t <duration> -c copy` — stream copy,
   no re-encode, lossless. Output files named `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4`.

6. **Cache**: Scan results saved to `<stem>_ocr_cache.json`. Re-loaded automatically
   on subsequent runs if `--interval` and `--crop` match.

## Findings & Nuances

### Camera clock rate
The camcorder's internal clock appears to advance at ~2× real time relative to the
video file's playback duration. This is visible in continuous sections: 60s of video
≈ 2 min of camera time. Cause unknown (likely a clock calibration issue on this
specific camera). Effect: the `gap_s` threshold must be interpreted as camera-seconds,
not wall-clock seconds. `--gap 900` (15 camera-minutes) was the effective value used.

### OCR reliability
Apple Vision reads the timestamp correctly most of the time but fails when:
- The timestamp region is in motion (camera pan/shake)
- Lighting is very dark or very bright
- The `1/` month-day separator gets split across lines

OCR success rate on a 5.9hr test file: 940/2128 samples (44%). The outlier
filter and None-skipping logic make the pipeline robust to this level of failure.

### Year filter
Timestamps are filtered to years 1985–2005. OCR occasionally hallucinates years
like 2049 or 1980; this filter catches them.

### Timestamp format
`M/ D/YY` on bottom line, `H:MM AM/PM` on top line. The date uses padded spaces
rather than leading zeros (e.g., `1/ 4/90` not `01/04/90`). The regex uses `[/\s]+`
to match both separators.

### Split accuracy (known limitation)
Splits are placed at the **sample time** where the timestamp jump was detected,
not the exact frame where the recording session changed. With `--interval 10`, the
split point can be up to 10 seconds late. Compounded with ffmpeg keyframe-snapping
(`-ss` before `-i`), this means ~0–10s of "next session" content can appear at the
tail of a clip.

**Example**: The last ~2s of `clip03_1990-01-04_1723.mp4` belongs to the session
captured in `clip04_1990-01-04_1750.mp4`. The actual camera switch happened between
sample t=220s and t=230s; the split was placed at t=230s.

**Fix (implemented)**: After coarse detection, a 1s-resolution refinement scan runs
over each split boundary's [prev_sample, detected_sample] window. Savings range from
0s to 2180s per boundary (visible in `--dry-run` output).
