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

1. **Scan** (`scan()`): Decodes at `FRAMES_PER_SAMPLE/N` fps, **crop-only** (no preprocessing — it has the higher OCR yield, ~67% vs ~45% per single frame), 3 frames per interval. `ocr_batch()` runs the binary over all frames; majority vote within each interval window. Windows that read nothing get a second **preprocessing fallback** pass (`_VF_PREPROCESS`, which uniquely recovers a few %). Results cached to `<stem>_ocr_cache.json` as raw OCR text (re-parsed at load so parser fixes don't require re-scan).

2. **Filter** (`drop_date_islands()` → `drop_digit_drop_runs()` → `filter_ocr_outliers()`): Runs in that order. `drop_date_islands` removes **date islands** — a single isolated reading whose date differs from both dated neighbours — including misreads that land exactly on a real session change. `drop_digit_drop_runs` removes multi-window digit-drop misreads (e.g. NOV 26 → NOV 6 spanning ≥2 windows) that survive the island filter because they look like a genuine run. `filter_ocr_outliers` removes remaining drift-inconsistent misreads (a reading inconsistent with all neighbors within a `max_run`-step window both directions; real boundaries survive because later same-date frames pass the forward check).

3. **Boundary detection** (`find_all_boundaries()`): Iterates filtered readings. Emits a `Boundary` when camera time jumps forward more than `video_advance + min_gap_s` (60s floor) or backward by >30 min (new tape segment). Type is `large_gap` when jump exceeds `gap_s` or is backward; `gap` for smaller pauses.

4. **Grouping** (`group_clips()`): Filters boundaries to cut points based on `--mode`. In `daily` mode, only confirmed date changes become cuts, **in tape order** — a date that recurs later on the tape (out-of-order re-recording) becomes a separate clip; occurrences are never regrouped. Phantom removal happens upstream in stage 2 (`drop_date_islands`), not here.

5. **Refinement** (`ocr_refinement()` → `LongDeadZonePolicy` / `ShortSpanPolicy`): For each `large_gap` coarse split, dense 1s scan of `[prev_sample, coarse_t]` window using parallel frame extraction + batch OCR. Finds the last frame confirming the old session rather than the first new-session frame. **Splice Dead Zone fallback**: when the dense scan is all-`None` (head-switch noise blanks OCR over the whole window), the boundary is an *Ambiguity Window* — refinement anchors the cut to the **last** visual event within the `None`-span (end of the noise burst), or to the end of the `None`-span when no visual event exists. Never the early `coarse_t`. See `docs/adr/0001-splice-boundary-placement-policy.md`.

6. **Cut** (`cut_clip_with_boundary_encode()`): Re-encodes only small boundary segments (~3–6s) at CRF 18 to achieve frame-accurate splits; stream-copies everything else. Uses ffmpeg concat demuxer to join segments.

## Key domain facts

- **Camera clock runs ~2× real time** on this specific camcorder. `--gap 3600` (1 camera-hour) is the empirically validated threshold (F1=0.920 on 215-boundary golden set); prior default of 300 and field value of 900 both had unacceptable FP rates.
- **OCR success rate ~86%** per-window (crop-only primary + preprocessing fallback + 3-frame majority vote + date-only acceptance) on the 5.9hr test file — 1824/2128 windows. The older all-preprocessed scan got ~33%, and the require-a-time parser (before date-only acceptance) capped per-window yield at ~64%. The outlier filter and `None`-skipping make the pipeline robust to the rest.
- **Timestamp format**: `M/ D/YY` (bottom line) and `H:MM AM/PM` (top line), with spaces instead of leading zeros. Years outside 1985–2005 are rejected as OCR hallucinations. **The overlay can be set to date-only (no time line) for long spans** — `parse_timestamp` accepts a date with no time (or no AM/PM) and falls back to **midnight**, keeping the date. Rejecting date-only reads (the old behavior) made those spans invisible and collapsed multiple real date changes into one clip; see `.scratch/issue-005-date-only-span-contamination.md`.
- **Default crop** `560:130:40:350` covers the full bottom overlay band on 640×480 source. Captures left/center/right overlays. Old right-anchored default `250:110:385:370` clipped off-center overlays (issue-009).
- **Default mode is `daily`**: one clip per calendar date, no date split across clips. Use `--mode session` for intra-day splits.
- **Date islands (replaces phantom collapse)**: a single isolated reading whose date differs from both neighbours is an OCR misread (wrong day/month/year). `drop_date_islands()` removes these before boundary detection, so they never create spurious boundaries — including a misread sitting *exactly* on a real session change, which the former `_collapse_revert_phantoms` mis-handled by merging the two real sessions. A real session is a contiguous run of ≥2 same-date readings and is never dropped, so genuine short / out-of-order sessions survive (e.g. a 9/01 run physically between 3/25 and 4/08 on a re-recorded tape). Validated on Converse 1990.mp4 — 3 merged-session bugs → 0.
- Split output filenames: `<stem>_clipNN_YYYY-MM-DD.mp4` (daily mode) or `<stem>_clipNN_YYYY-MM-DD_HHMM.mp4` (session/scene mode)
- **Detection vs Placement** (do not conflate): *Detection* = does a boundary exist near t (golden-set y/n, F1=0.920). *Placement* = how many seconds the cut lands from the true session change. They are independent — high F1 says nothing about placement accuracy. Placement has its own (human-labeled) ground truth on Splice Dead Zone boundaries; no placement change merges without a measured error. See `CONTEXT.md`.
- **Splice Dead Zone** (≲120s all-`None` at a tape splice) vs **Long Dead Zone** (≳120s, up to 2160s of unreadable footage). The end-of-noise-burst placement policy applies *only* to Splice Dead Zones; Long Dead Zone handling is unsolved/out of scope.
- **Decoder DTS warnings in 3-seg concat output are expected and benign.** Clips with both a lead re-encode and a trail re-encode (3-segment path) emit ~3–23 `non monotonic` DTS warnings from `ffmpeg -v warning -f null -`, all in the first ~0.4s. Root cause: the stream-copied body carries VFR source PTS irregularities; `+igndts` recomputes DTS=PTS across the seam, propagating those into decoder output. Container DTS is strictly increasing (`ffprobe` finds 0 non-monotonic events) — media players are unaffected. Fixing requires re-encoding the entire body, defeating the pipeline's stream-copy design. Decision: accept. See `docs/adr/0003-accept-decoder-dts-warnings-3seg-concat.md`.
- **Visual signals: anchor always-on, drop-filter opt-in.** `detect_visual_boundaries` runs automatically (cached) to supply anchor candidates for splice placement. `fuse_boundaries` (drops OCR boundaries lacking visual corroboration) stays behind `--enable-visual-fusion`, default **off** — VHS pause/resume often has no visual discontinuity, so the filter would delete real boundaries.

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/`. No external PR triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` at root + `docs/adr/`. See `docs/agents/domain.md`.

Dated empirical discoveries go in `docs/findings/NNN-slug.md` (evidence, not goals/decisions) — see `docs/findings/README.md` for how it relates to specs/ADRs/requirements.
