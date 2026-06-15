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
python3 split_homevideo.py "YourFile.mp4" --gap 900
```

Key flags: `--interval N` (OCR sample rate, default 10s), `--gap N` (camera-time jump threshold, default 300s), `--crop W:H:X:Y` (timestamp region), `--cache PATH`, `--out-dir DIR`.

## Architecture

Two-file project:

**`ocr_timestamp.swift`** — compiled to `ocr_timestamp` binary. Takes image paths as args, runs Apple Vision `VNRecognizeTextRequest` on each, prints `<path>\t<text>` to stdout. Called in batch by the Python script.

**`split_homevideo.py`** — main pipeline, five stages:

1. **Scan** (`scan()`): Single-pass ffmpeg (`fps=1/N,crop=...`) extracts one frame every N seconds into a temp dir. `ocr_batch()` runs the binary once over all frames. Results cached to `<stem>_ocr_cache.json`.

2. **Filter** (`filter_ocr_outliers()`): Removes isolated misreads. A reading is kept if it's consistent (within 900s drift) with its prior OR next valid neighbor. This preserves real clip boundaries while discarding single-frame OCR noise.

3. **Split detection** (`find_splits()`): Iterates filtered readings. Triggers a split when camera time jumps forward more than `video_advance + gap_s` (camera was paused/off) or backward by >30 min (new tape segment).

4. **Refinement** (`refine_split()`): For each coarse split, dense 1s scan of `[prev_sample, coarse_t]` window using parallel frame extraction + batch OCR. Finds the last frame confirming the old session rather than the first new-session frame.

5. **Cut** (`cut_clip_with_boundary_encode()`): Re-encodes only small boundary segments (~3–6s) at CRF 18 to achieve frame-accurate splits; stream-copies everything else. Uses ffmpeg concat demuxer to join segments.

## Key domain facts

- **Camera clock runs ~2× real time** on this specific camcorder. `--gap 900` (15 camera-minutes) was the effective threshold used in practice, despite the default being 300.
- **OCR success rate ~44%** on a 5.9hr test file. The outlier filter and `None`-skipping make the pipeline robust to this.
- **Timestamp format**: `M/ D/YY` (bottom line) and `H:MM AM/PM` (top line), with spaces instead of leading zeros. Years outside 1985–2005 are rejected as OCR hallucinations.
- **Default crop** `250:110:385:370` is tuned for 640×480 source with bottom-right timestamp overlay.
- Split output filenames: `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4`
