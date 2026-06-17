# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
# Compile the OCR binary (required before first run):
swiftc -O ocr_timestamp.swift -o ocr_timestamp

# Dependencies: ffmpeg + ffprobe (via Homebrew)
brew install ffmpeg
```

## Running

```bash
# Dry run — preview splits, no cutting:
python3 split_homevideo.py "YourFile.mp4" --dry-run

# Full run:
python3 split_homevideo.py "YourFile.mp4"

# Re-tune gap without re-scanning (cache hit):
python3 split_homevideo.py "YourFile.mp4" --gap 3600
```

Key flags: `--interval N` (OCR sample rate, default 10s), `--gap N` (camera-time jump threshold, default 3600s), `--mode {scene,session,daily}` (default daily), `--crop W:H:X:Y` (timestamp region), `--cache PATH`, `--out-dir DIR`.

## Architecture

Two-file project:

**`ocr_timestamp.swift`** — compiled to `ocr_timestamp` binary. Takes image paths as args, runs Apple Vision `VNRecognizeTextRequest` on each, prints `<path>\t<text>` to stdout. Called in batch by the Python script.

**`split_homevideo.py`** — main pipeline, six stages:

1. **Scan** (`scan()`): Single-pass ffmpeg (`fps=FRAMES_PER_SAMPLE/N,crop=...`) extracts 3 frames per interval into a temp dir. `ocr_batch()` runs the binary once over all frames. Majority vote within each interval window. Results cached to `<stem>_ocr_cache.json` as raw OCR text (re-parsed at load time so parser fixes don't require re-scan).

2. **Filter** (`filter_ocr_outliers()`): Removes isolated misreads and consecutive misread runs. A reading is dropped if it's inconsistent with all neighbors within a `max_run`-step window in both directions. Real boundary readings are protected because subsequent same-date frames always pass the forward check.

3. **Boundary detection** (`find_all_boundaries()`): Iterates filtered readings. Emits a `Boundary` when camera time jumps forward more than `video_advance + min_gap_s` (60s floor) or backward by >30 min (new tape segment). Type is `large_gap` when jump exceeds `gap_s` or is backward; `gap` for smaller pauses.

4. **Grouping** (`group_clips()` + `_collapse_revert_phantoms()`): Filters boundaries to cut points based on `--mode`. In `daily` mode, only confirmed date changes become cuts. After grouping, `_collapse_revert_phantoms` removes phantom clips bracketed by opposite-sign `cam_jump_s` values — OCR misreads (wrong year, wrong month) that create a short spurious clip before immediately reverting.

5. **Refinement** (`refine_split()`): For each `large_gap` coarse split, dense 1s scan of `[prev_sample, coarse_t]` window using parallel frame extraction + batch OCR. Finds the last frame confirming the old session rather than the first new-session frame.

6. **Cut** (`cut_clip_with_boundary_encode()`): Re-encodes only small boundary segments (~3–6s) at CRF 18 to achieve frame-accurate splits; stream-copies everything else. Uses ffmpeg concat demuxer to join segments.

## Key domain facts

- **Camera clock runs ~2× real time** on this specific camcorder. `--gap 3600` (1 camera-hour) is the empirically validated threshold (F1=0.920 on 215-boundary golden set); prior default of 300 and field value of 900 both had unacceptable FP rates.
- **OCR success rate ~44%** on a 5.9hr test file. The outlier filter and `None`-skipping make the pipeline robust to this.
- **Timestamp format**: `M/ D/YY` (bottom line) and `H:MM AM/PM` (top line), with spaces instead of leading zeros. Years outside 1985–2005 are rejected as OCR hallucinations.
- **Default crop** `250:110:385:370` is tuned for 640×480 source with bottom-right timestamp overlay.
- **Default mode is `daily`**: one clip per calendar date, no date split across clips. Use `--mode session` for intra-day splits.
- **Phantom collapse**: in `daily` mode, misread years/months create an A→B→A or A→B→C jump pattern where B is short and the surrounding jumps have opposite signs. `_collapse_revert_phantoms` removes these. Validated on Converse 1990.mp4 — removed 8 phantoms (including 1999-05-19, 1999-07-21, 1990-01-22 misreads).
- Split output filenames: `<stem>_clipNN_YYYY-MM-DD.mp4` (daily mode) or `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4` (session/scene mode)
