# SPEC: Two-Layer Clip Detection

## Status: IMPLEMENTED (historical spec) — defaults since changed

> The two-layer design (`find_all_boundaries` + `group_clips` + `--mode`) shipped.
> Two CLI defaults stated below are **out of date**: the current default mode is
> **`daily`** (not `session`) and the default `--gap` is **3600** (not 300). See
> CLAUDE.md / README.md for current behavior. The AM/PM-required note in
> "Pre-implementation fixes" was also later reversed (see the inline note there).

---

## Background & Motivation

The single `find_splits()` / `--gap` design collapses boundary detection and clip-grouping into one step. This prevents detecting small pauses within a session, and prevents grouping footage by calendar date instead of pause size.

Goal is to add `--mode {scene,session,daily}` so the same OCR scan output can produce different clip structures without re-scanning. Modes exist to let us validate empirically which output is most useful (e.g. whether `scene` produces 600 unusable micro-clips on a 6-hour tape, or something reasonable).

### Pre-implementation fixes (already landed)

Two bugs found during OCR analysis of `test_15min.mp4` and fixed before spec was finalized:

**1. `parse_timestamp` month misread** — `DATE_PATTERN` used `[/\s]+` as separator, allowing `11 5/90` to parse as November instead of rejecting it (slash misread as `1`, then space as separator). Fix: changed to `\s*/\s*` — requires a literal `/`, allows optional surrounding whitespace. `11 5/90` now returns None.

**2. `parse_timestamp` AM/PM required** — `(AM|PM)?` was optional. When OCR dropped the PM indicator, `7:14` parsed as 7 AM, creating phantom 12-hour backward jumps that clustered and survived `filter_ocr_outliers`. Fix: `if not ampm: return None`.

> **Superseded (2026-06-21):** `if not ampm: return None` was later reversed. A
> time without AM/PM (or no time at all) now falls back to **midnight** and keeps
> the date, because the camcorder overlay can be set to date-only for long spans
> and rejecting those reads collapsed multiple real dates into one clip. The
> 12-hour-jump hazard is still avoided — the ambiguous *time* is dropped, not used
> as 7 AM. See `.scratch/issue-005-date-only-span-contamination.md`.

**3. `filter_ocr_outliers` consecutive boundaries** — original rule "keep if consistent with prev OR next" drops real readings caught between two consecutive boundaries (e.g. 11:44 AM between an overnight jump before it and a 96-min jump after it — fails both checks and was wrongly discarded). Fix: when a reading fails both checks, additionally test whether prev and next are consistent with *each other*. If they are → isolated outlier, drop. If they aren't → reading is between two real boundaries, keep.

These fixes reduced false splits from 14 → 7 on `test_15min.mp4` at `--gap 900`.

---

## Architecture

Replace `find_splits()` with two stages:

1. **`find_all_boundaries(samples, min_gap_s=60)`** — emits every candidate boundary at a low floor
2. **`group_clips(boundaries, mode, gap_s)`** — decides which boundaries become cuts

Add `--mode {scene,session,daily}` CLI flag. `refine_split`, `cut_clip_with_boundary_encode`, `split_video` unchanged.

---

## New type: `Boundary`

```python
@dataclass
class Boundary:
    video_t:    float               # video-file position of boundary
    type:       str                 # 'gap' | 'large_gap'
    cam_before: datetime | None     # last valid timestamp before boundary
    cam_after:  datetime | None     # first valid timestamp after boundary
    cam_jump_s: float               # cam seconds: cam_after - cam_before (negative = backward)
    prev_t:     float | None        # video_t of last valid sample before (for refine_split)
    prev_dt:    datetime | None     # datetime of last valid sample before (for refine_split)
```

`prev_t`/`prev_dt` are populated for all boundary types even though only `large_gap` uses them today — cheap to store, available if artifact refinement is added later.

Type semantics:
- `gap` — forward camera jump > `min_gap_s` (60s) but ≤ `gap_s` above threshold
- `large_gap` — forward camera jump > `gap_s` above threshold, or backward > 30 min

Artifact detection is **deferred** from this iteration.

---

## Stage 1: `find_all_boundaries(samples, min_gap_s=60, gap_s=300)`

**Input:** raw `samples` from `scan()`.

Run `filter_ocr_outliers(samples)` → clean valid readings. Iterate consecutive pairs:

```
cam_advance   = (dt_b - dt_a).total_seconds()
video_advance = t_b - t_a
jumped_forward  = cam_advance > video_advance + min_gap_s
jumped_backward = cam_advance < -1800
```

If either: emit `Boundary`.
- `type = 'large_gap'` if `cam_advance > video_advance + gap_s` or backward jump
- `type = 'gap'` otherwise

`min_gap_s = 60` is internal, not user-tunable.

**Output:** `list[Boundary]` sorted by `video_t`.

---

## Stage 2: `group_clips(boundaries, mode, gap_s)`

Returns `list[float]` (video_t values for cuts, with `0.0` prepended). Feeds into existing `refine_split` + `split_video` unchanged.

| mode | which boundaries become cuts |
|---|---|
| `scene` | all (`gap` + `large_gap`) |
| `session` | `large_gap` only |
| `daily` | only where `cam_before.date() != cam_after.date()`, or either is None, or backward jump |

**`daily` semantics:** strict calendar date. All footage from the same date → one clip regardless of gap size within that day. A morning session and evening session on 1/5/90 = one clip.

**`daily` when cam_before or cam_after is None:** cut. Unknown date can't be confirmed same-day; better to over-cut than silently merge footage from different dates.

**Output:** `list[float]`

---

## `refine_split` — only called for `large_gap` boundaries

`gap`-type cuts land at ±`interval`s (coarse) precision. Acceptable: these are brief pauses within a session; second-level precision not needed.

`large_gap` boundaries use existing `refine_split` signature unchanged.

---

## CLI

```
--mode {scene,session,daily}    default: session  (backward compatible)
--gap N                         large_gap threshold; default 300
```

`min_gap_s = 60` detection floor is fixed internal constant.

---

## Cache: store raw OCR text

**Decision:** cache stores raw OCR text strings (what the binary actually output), not parsed datetimes. `parse_timestamp` runs at cache load time, not scan time.

**Why:** discovered during this session that `parse_timestamp` bugs caused the cache to serve wrong parsed datetimes even after the parser was fixed. Raw text is the ground truth; parsing is free. Cache auto-stays-fresh across parser changes.

**Cache format change:** `"samples"` list changes from `[t, isoformat_or_null]` to `[t, raw_ocr_string_or_null]`.

Cache key still includes `interval`, `crop`, `vf_preprocess`, `frames_per_sample`. No parser version needed once raw text is stored.

---

## Labeling

**`scene` / `session` modes:** unchanged — `_label_for()` returns `YYYY-MM-DD_HHMM` from first valid timestamp at or after clip start.

**`daily` mode:** date-only label (`1990-01-05`). `YYYY-MM-DD_HHMM` is misleading when the clip spans the whole day (the time is just when OCR first fired, not when the clip starts).

---

## Integration Test Plan

**Fixture:** run `scan()` once on `test_15min.mp4`, commit raw-text cache as `tests/fixtures/test_15min_ocr_cache.json`.

**Golden reference** (from manual review of `test_15min.mp4`, see `test_15min-scene-timestamps.txt`):

Expected 8 clips / 7 boundaries in `session` mode at `--gap 900`:

| clip | start (s) | end (s) | label |
|------|-----------|---------|-------|
| 1 | 0 | ~24 | 1990-01-01 |
| 2 | ~24 | ~140 | 1990-01-04 |
| 3 | ~140 | ~377 | 1990-01-04 |
| 4 | ~377 | ~480 | 1990-01-04 |
| 5 | ~480 | ~481 | 1990-01-05 (brief pause, ~1s) |
| 6 | ~481 | ~560 | 1990-01-05 |
| 7 | ~560 | ~790 | 1990-01-05 |
| 8 | ~790 | 900 | 1990-01-05 |

All 7 boundaries are required (no optionals). The 2:20 boundary (clip 2→3 at ~140s) is reliably detected — confirmed by OCR hitting both sides of it.

**Tests:**
1. `test_session_mode` — load fixture → `find_all_boundaries` → `group_clips(session, gap_s=900)` → assert 7 boundary video_t values within ±15s of golden
2. `test_daily_mode` — same fixture → `group_clips(daily, gap_s=900)` → assert only 2 boundaries (1/1→1/4 at ~24s, 1/4→1/5 at ~480s)
3. `test_scene_mode` — same fixture → `group_clips(scene, gap_s=900)` → assert all `gap`-type boundaries present in addition to `large_gap` ones; count > session count

---

## Deferred

- **Artifact detection** (`type='artifact'`): B&W fuzz between scenes. Deferred pending mode validation. Short artifacts (≤4s) are invisible at 10s interval regardless; would require dense re-scan or ffmpeg visual signal.
- **Excising artifact fuzz into its own clip**: requires two refinement passes (start and end of fuzz). Not in scope.
- **`--min-gap` user flag**: exposes the 60s detection floor. Not needed until scene mode is validated on the full 6hr tape.
