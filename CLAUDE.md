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

## Architecture

**`split_homevideo.py`** — main pipeline (`ocr_timestamp.swift` is the OCR binary it shells out to), six stages:

1. **Scan** (`scan()`): Decodes at `FRAMES_PER_SAMPLE/N` fps, **crop-only** (no preprocessing — it has the higher OCR yield, ~67% vs ~45% per single frame), 3 frames per interval. `ocr_batch()` runs the binary over all frames; majority vote within each interval window. Windows that read nothing get a second **preprocessing fallback** pass (`_VF_PREPROCESS`, which uniquely recovers a few %). Results cached to `<stem>_ocr_cache.json` as raw OCR text (re-parsed at load so parser fixes don't require re-scan). **Island verification** (`verify_date_islands()`, inside `scan()`): every *date island* in the coarse readings (a lone dated reading whose date differs from both dated neighbours) gets 1s probes over ±interval (crop-only, then the preprocessing fallback on probes with no parseable date — the same two-pass design as the scan; a 30s 4/19 session in a readability hole read on 1 crop-only probe and 6 preprocessed ones); unreadable probes are inserted as `None` entries, probes reading a neighbour date are inserted, probes reading the island date are inserted only when ≥`ISLAND_MIN_PROBE_HITS` (3) of them form one contiguous block with the island. This is what makes a genuine session shorter than 2×interval survive the island filter (5 real sessions of 9–40s recovered on the 1990 tape, 1 on the 1992 tape — see finding 006). Probes are cached in the same file (`island_probes`), so dry-runs and re-tunes stay fast.

2. **Filter** (`drop_date_islands()` → `drop_year_misread_runs()` → `drop_bounce_runs()` → `drop_out_of_order_twin_runs()` → `drop_short_bracketed_runs()` → `drop_digit_drop_runs()` → `drop_month_confusion_runs()` → `drop_day_confusion_runs()` → `filter_ocr_outliers()`): Runs in that order. `drop_date_islands` removes **date islands** — a single isolated reading whose date differs from both dated neighbours — including misreads that land exactly on a real session change. One exception (`_dead_zone_island`): an island with no legible reading within ±`DEAD_ZONE_HOLE_S` (5s) on either side, `None` probe entries on both sides proving the hole was probed, and a date strictly *between* its nearest non-island neighbour dates is kept — that is a session in a readability hole that OCR reads only once (1992 tapes: ~60s of 9/27 between 9/25 and 10/4; a 10s 1/19 between 1/05 and 1/25), and a misread can never take that shape when both neighbours share a date. `drop_bounce_runs` resolves a **bounce** — ≥3 consecutive runs alternating between two dates that differ in exactly one field (4/28 vs 1/28, 3/07 vs 8/07) — by tape chronology: only the date that lies between the dates before and after the bounce is real, and every run of the other is dropped regardless of length (a persistent one-glyph misread can last 35s, past any run cap); when both or neither fit, nothing is decided and the capped filters below apply. `drop_out_of_order_twin_runs` drops a run ≤`TWIN_RUN_MAX_S` (60s) whose date is a one-field twin of one of the two nearest *long* runs (>60s) around it and lies outside their chronological range (12s of `3/ 3` and 30s of `3/ 5` inside a 3/06→3/07 stretch); anchoring on long runs stops two adjacent misreads vouching for each other. `drop_short_bracketed_runs` drops any run ≤`_CONFUSION_RUN_MAX_S` (10s) bracketed by the same date on both sides, whatever its own date — the run-length analogue of the island rule, needed because preprocessed probes can stabilise a misread with no simple digit relation for a few seconds (6s of `8/ 8` read as `3/18`). `drop_digit_drop_runs` removes multi-window digit-drop misreads in either field (day: NOV 26 → NOV 6; month: 12/22 → 2/22, 11/ 3 → 1/ 3) that survive the island filter because they look like a genuine run; the relation is asymmetric so an A B A B alternation resolves to the full-digit date without a run cap (a late-1990 tape produced 18 alternating 12-22/02-22 clips before this). `drop_month_confusion_runs` removes multi-window month-digit confusion misreads where OCR swaps visually similar month digits (1↔5, 8↔9) for ≥2 windows (e.g. `1/6/90` → `5/6/90`); pairs defined in `_MONTH_CONFUSABLES` (1↔5, 8↔9, 3↔8). `drop_day_confusion_runs` is the same shape for the DAY field — OCR swaps visually similar day digits (6↔8, 5↔6) for ≥2 windows (e.g. `5/26/90` → `5/28/90`); pairs defined in `_DAY_CONFUSABLES` (6↔8, 5↔6, 3↔8), digit comparison in `_day_digits_confusable`. Both confusion filters cap droppable runs at `_CONFUSION_RUN_MAX` readings — without the cap, an `A B A′ B` alternation drops a long REAL session bracketed by confusable dates. `filter_ocr_outliers` removes remaining drift-inconsistent misreads (a reading inconsistent with all neighbors within a `max_run`-step window both directions; real boundaries survive because later same-date frames pass the forward check).

3. **Boundary detection** (`find_all_boundaries()`): Iterates filtered readings. Emits a `Boundary` when camera time jumps forward more than `video_advance + min_gap_s` (60s floor) or backward by >30 min (new tape segment). Type is `large_gap` when jump exceeds `gap_s` or is backward; `gap` for smaller pauses.

4. **Grouping** (`group_clips()`): Filters boundaries to cut points based on `--mode`. In `daily` mode, only confirmed date changes become cuts, **in tape order** — a date that recurs later on the tape (out-of-order re-recording) becomes a separate clip; occurrences are never regrouped. Phantom removal happens upstream in stage 2 (`drop_date_islands`), not here.

5. **Refinement** (`ocr_refinement()` → `LongDeadZonePolicy` / `ShortSpanPolicy`, then `snap_to_scene_cut()`): For each `large_gap` coarse split, dense 1s scan of `[prev_sample − REFINE_LOOKBACK_PAD_S, coarse_t + interval]` using parallel frame extraction + batch OCR — the 20s lookback corrects coarse-label drift (the bucket label at `prev_t` can already be new-session content), and a monotonic `floor_t` stops the window from crossing the previous boundary's cut (crossing cuts mislabel whole spans). Finds the last frame confirming the old session rather than the first new-session frame; a confirmed new-session candidate is not cancelled by an off-date misread, and an off-date strict read whose recoverable digits match the expected new session (`_gap_date_class` == new, e.g. `1/23/90` when 9/23 is expected) *is* a new-session candidate rather than an intermediate date. When the last-old and first-new frames are adjacent on the 1s grid, `_bisect_transition` reads the midpoint frames down to `REFINE_BISECT_MIN_S` (0.25s), so a CLEAN cut's residual is ≤0.25s instead of ≤1s. Scene-snap (pass 3, default on) then snaps the cut sub-second onto a detected shot change. When unclassifiable (`None`) frames sit between the last old-date evidence and the first new-date frame, `_place_content_aware` cuts at the last visual event among them (a readability hole at the head of the new session is indistinguishable from a noise burst ahead of it; 1992 tape #1 leaked 17s of 8/25 into an 8/23 clip before this), else at the first new-date frame. **Splice Dead Zone fallback**: when the dense scan is all-`None` (head-switch noise blanks OCR over the whole window), the boundary is an *Ambiguity Window* — refinement anchors the cut to the **last** visual event within the `None`-span (end of the noise burst), or to the end of the `None`-span when no visual event exists. Never the early `coarse_t`. See `docs/adr/0001-splice-boundary-placement-policy.md`.

6. **Cut** (`cut_clip_with_boundary_encode()`): Re-encodes only small boundary segments (~3–6s) at CRF 18 to achieve frame-accurate splits; stream-copies everything else. Uses ffmpeg concat demuxer to join segments.

## Key domain facts

- **Camera clock runs ~2× real time** on this specific camcorder. `--gap 3600` (1 camera-hour) is the empirically validated threshold; prior default of 300 and field value of 900 both had unacceptable FP rates. (Judged by date-purity audit of the resulting clips — there is no labeled benchmark; the former AI-labeled golden set was abandoned as unreliable. See ADR 0001.)
- **OCR success rate ~86%** per-window (1824/2128 on the 5.9hr test tape): crop-only primary + preprocessing fallback + 3-frame majority vote + date-only acceptance. **This is the single source for the current yield figure** — other docs link here, not restate it. The outlier filter and `None`-skipping make the pipeline robust to the rest.
- **Timestamp format**: `M/ D/YY` (bottom line) and `H:MM AM/PM` (top line), with spaces instead of leading zeros. Years outside 1985–2005 are rejected as OCR hallucinations. **The overlay can be set to date-only (no time line) for long spans** — `parse_timestamp` accepts a date with no time (or no AM/PM) and falls back to **midnight**, keeping the date. Rejecting date-only reads (the old behavior) made those spans invisible and collapsed multiple real date changes into one clip.
- **Default crop** `560:130:40:350` covers the full bottom overlay band on 640×480 source. Captures left/center/right overlays. Old right-anchored default `250:110:385:370` clipped off-center overlays.
- **Default mode is `daily`**: one clip per calendar date, no date split across clips. Use `--mode session` for intra-day splits.
- **Date islands (replaces phantom collapse)**: a single isolated reading whose date differs from both neighbours is an OCR misread (wrong day/month/year). `drop_date_islands()` removes these before boundary detection, so they never create spurious boundaries — including a misread sitting *exactly* on a real session change, which the former `_collapse_revert_phantoms` mis-handled by merging the two real sessions. A real session is a contiguous run of ≥2 same-date readings and is never dropped, so genuine short / out-of-order sessions survive (e.g. a 9/01 run physically between 3/25 and 4/08 on a re-recorded tape). Validated on Converse 1990.mp4 — 3 merged-session bugs → 0. The ≥2 rule alone is **interval-dependent** (a real session shorter than 2×interval yields one coarse reading and looked like an island); island verification in stage 1 removes that dependency by probing at 1s around every island, so `--interval 10` no longer silently merges 10–40s sessions into the previous clip's tail.
- **Detection vs Placement** (do not conflate): *Detection* = does a boundary exist near t. *Placement* = how many seconds the cut lands from the true session change. Independent metrics — defined in `CONTEXT.md`, measurement framed in ADR 0001 (placement judged by clip-content audit, not per-boundary human labels).
- **Splice Dead Zone** (≲120s all-`None` at a tape splice) vs **Long Dead Zone** (≳120s, up to 2160s of unreadable footage). The end-of-noise-burst placement policy applies *only* to Splice Dead Zones; Long Dead Zone handling is unsolved/out of scope.
- **Decoder DTS warnings on long-body stream-copied clips are expected and benign** — container DTS stays strictly increasing, no frozen/dropped frames, media players unaffected. Fixing would require re-encoding the whole body, defeating the stream-copy design. Do not chase them.
- **Visual signals anchor, never filter.** `detect_visual_boundaries` runs automatically (cached) to supply anchor candidates for splice placement. A visual-corroboration drop-filter was tried and removed — VHS pause/resume often has no visual discontinuity, so it deleted real boundaries. Separately, **scene-snap (pass 3, PySceneDetect `AdaptiveDetector`) is default ON** (`--no-enable-scene-snap` to disable): sub-second snap of each refined cut — backward ≤0.5s onto a clean shot change, forward to burst-end at a noise splice (findings 003/005).
- **Reconstruction tenet (fundamental):** concatenating the clips in order must effectively recreate the original video — nothing discarded, nothing reordered. Rules out any footage-dropping cut policy. Seams currently overlap ~±0.3s (keyframe snap), within tolerance. See REQUIREMENTS.md.
- **Placement definition of done: ADR 0004 (ratified 2026-07-03).** CLEAN boundary ≤1s (sub-second residuals are NOT bugs; since 2026-09-16 the bisection step lands CLEAN cuts within 0.25s when the bracketing frames are legible); visible shot change ≤0.5s; Splice Dead Zone judged by purity only (static in outgoing tail = correct); Long Dead Zone no requirement. Measure with `.scratch/placement_report.py` (per-boundary ruler; run before/after ANY placement change; pass `--cumsum-tolerance 0.5` so cuts are frame-matched — the duration-cumsum path drifts by up to ~2s over a tape and mislabels exact cuts as 1s LATE) — not `date_purity.py` (edge-blind backstop).

## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/` (gitignored — local only, never committed). No external PR triage surface.

### Triage labels

Canonical label strings: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.

### Domain docs

`CONTEXT.md` (vocabulary) + `docs/REQUIREMENTS.md` (goals/constraints) + `docs/adr/` (decisions) + `docs/findings/` (dated evidence, `NNN-slug.md`; refer to tapes by year, never by filename).
