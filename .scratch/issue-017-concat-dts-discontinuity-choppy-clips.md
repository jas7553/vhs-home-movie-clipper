# Issue 017: Choppy playback from re-encode→concat DTS discontinuity at clip boundaries

**Status:** closed — partially fixed by bf5745c. Residual 3-seg concat case tracked in issue-020.
**Labels:** wontfix
**Context:** cut stage — `cut_clip_with_boundary_encode()` / `_ffmpeg_*_seg` / concat demuxer
in `split_homevideo.py` (around the `-f concat -c copy -fflags +genpts` invocation).
Architecture stage 6 ("Cut"). Found during a regression sweep of the `Converse 1990`
output clips (interval=3 run).

---

## Theory

The cut stage builds each clip by concatenating up to three segments — a re-encoded
leading boundary segment (`[exact_start, kf_after]`, libx264 CRF 18, **default GOP /
B-frames**), a stream-copied body that starts on a keyframe, and an optional
re-encoded trailing segment — via the concat demuxer with `-c copy -fflags +genpts`.

The re-encoded segment and the stream-copied body have **different GOP structure and
frame-duration patterns**. When concatenated under `-c copy` with regenerated PTS, the
junction produces **non-monotonic / backward DTS** and **variable frame durations**.
Players (and any downstream re-mux) hitch at the seam — the "choppy at the start (and
sometimes end) of clips" the user reported. Clips spanning tape-splice noise amplify
it because the source region there already has irregular timestamps that re-muxing
surfaces.

## Evidence

Feedback loop: `ffmpeg -v warning -i <file> -f null -` and counting
`"non monotonic"` muxer warnings. **Source is the baseline.**

- Full source `Converse 1990.mp4`: **1** non-monotonic-DTS warning in the entire file.
- Output clips inject the rest. Per-clip warning counts (worst):
  `clip16=100, clip71=27, clip10=20, clip67=11, clip84=9, clip36=8`,
  plus ~10 more clips at 1–6. ~22/89 clips affected.
- clip16 packet dump at the lead-seg→body junction (`ffprobe -select_streams v:0
  -show_entries packet=dts_time,pts_time,duration_time,flags`):
  ```
  dts 0.454388   duration 0.033367   (lead-seg tail)
  dts 0.421021   duration 0.109109   (body start: DTS goes BACKWARD; duration jumps)
  ```
  `avg_frame_rate=1677930390/56006833` (non-integer → VFR-looking output).
- clip09 (`packet=pts_time` in file order): PTS non-monotonic for the first ~9 frames
  (0.05→0.18→0.11→0.08→0.15→0.31→…) until the next keyframe at 0.355s — the
  re-encoded lead segment's B-frame reorder colliding with concat `+genpts`.
- Clips with **no** lead re-encode (e.g. clip13: sub-frame `exact_start`, body starts
  directly on a keyframe) are clean (0 warnings, monotonic PTS from frame 0).

## Findings

Choppiness is **introduced by the cut/concat pipeline**, not inherited from the
source. It correlates exactly with the presence of a re-encoded boundary segment
joined to a stream-copy body. Root contributors: (1) B-frames in the re-encoded
segment, (2) GOP/duration mismatch across the concat seam, (3) `+genpts` deriving
DTS that go backward across the seam.

## Verification method

1. Baseline: `ffmpeg -v warning -i "Converse 1990.mp4" -f null - 2>&1 | grep -c "non monotonic"`
   → ~1 for the whole 5.9hr source.
2. Per clip after the fix:
   ```
   for c in "Converse 1990_clips/"*.mp4; do
     n=$(ffmpeg -v warning -i "$c" -f null - 2>&1 | grep -c "non monotonic")
     echo "$n  $(basename "$c")"
   done
   ```
   Target: **every clip ≈ 0** (a clip should not exceed its own source span's count).
3. Spot-check the seam is monotonic:
   `ffprobe -v error -select_streams v:0 -show_entries packet=dts_time -of csv=p=0 <clip> | head -40`
   — DTS must be strictly non-decreasing.
4. Eyeball playback of a previously-bad clip start (clip16, clip71, clip09).

## Implementation suggestions

Pick the cheapest that makes the seam monotonic without re-encoding whole clips:

- **Re-encode boundary segments with a concat-safe GOP**: add `-bf 0` (no B-frames)
  and/or `-g 1` / closed-GOP to `_ffmpeg_encode_seg` so the seg has no reorder and a
  clean DTS=PTS ordering at its tail.
- **Fix concat muxing flags**: alongside `+genpts`, add
  `-avoid_negative_ts make_zero -muxpreload 0 -muxdelay 0`, and verify the segments
  share an identical timebase (`VIDEO_TIMESCALE` is already pinned — confirm it is
  actually applied to the re-encoded segs, not just the final concat).
- If neither yields monotonic DTS, consider a single re-encode of the **whole boundary
  region** as one segment (lead+first GOP of body) instead of concatenating a tiny seg
  onto a copy, eliminating the seam.

Keep the existing sub-frame guard (`MIN_BOUNDARY_SEG`) — do not reintroduce
zero-frame segments.

## Acceptance criteria

- [ ] After a full run, no output clip exceeds ~0 non-monotonic-DTS warnings
      (`ffmpeg -v warning … | grep -c "non monotonic"`), matching the source baseline.
- [ ] DTS is strictly non-decreasing across the lead/trail concat seam (ffprobe check).
- [ ] Re-encoded boundary frames remain frame-accurate (cut placement unchanged) and
      the sub-frame `MIN_BOUNDARY_SEG` guard still holds.
- [ ] Clips clip16, clip71, clip09 play smoothly from the first frame (no start hitch).
- [ ] No regression in clip count / boundaries vs the current run.

## Blocked by

None — can start immediately. Independent of issues 018 and 019.

---

## Update — first fix attempt verified INEFFECTIVE (re-opened)

Commit 3127b7d added `-bf 0` to `_ffmpeg_encode_seg` (kills B-frame reorder at the
re-encoded boundary segment) plus `-avoid_negative_ts make_zero -muxpreload 0
-muxdelay 0` on the concat step. This addresses only the **lead-seg→body concat
seam**. It does **not** fix the dominant choppiness.

### Empirical result (regenerated all 89→85 clips, warm cache, then re-ran the loop)

Per-clip non-monotonic-DTS warnings (`ffmpeg -v warning -i CLIP -f null - | grep -c
"non monotonic"`):

- **Total ≈ 187 across 15 clips — essentially unchanged** from the pre-fix ~200/22.
- **clip16 = 100 (identical to before the fix).** clip67=27, clip10=20, clip63=11,
  clip80=9, clip71=6.

### Why the fix missed

The big offenders are **pervasive** (clip16's warnings recur ~every 561 muxer ticks
across the *whole* clip, not at a single seam) — they come from **stream-copying VFR /
irregular-timestamp source regions** in `_ffmpeg_copy_seg`, and/or from `-fflags
+genpts` regenerating duplicate/backward DTS over that VFR body. The `-bf 0` change
only touches the tiny re-encoded boundary segment; the worst clips (clip16) may not
even be concats (single stream-copy → `len(segs)==1` → `shutil.move`, no concat step
at all), so neither the `-bf 0` nor the new concat flags run for them.

### Revised direction for the next attempt

- Confirm first whether each bad clip is a concat or a single copy
  (`len(segs)`). clip16-class clips are likely **pure stream-copies** — the fix must
  live in the copy path, not the concat path.
- The source has only ~1 non-monotonic warning across the entire 5.9hr file, yet a
  short copied span shows 100 — so the act of `-ss/-t -c copy` extraction (edit
  lists / start-time offset / `+genpts`) is *introducing* the duplicate DTS. Probe a
  bad copied span directly and compare its DTS to the same source frames.
- Candidate fixes to evaluate: drop `+genpts` (or replace with explicit
  `-fflags +igndts`/timestamp reset), add `-avoid_negative_ts make_zero` to
  `_ffmpeg_copy_seg` itself, or normalize timestamps on the copied body
  (`-vsync passthrough`/`setts`) so DTS stays monotonic without re-encoding.

### Verification (unchanged, MUST run on regenerated clips, not unit tests)

Regenerate to a scratch `--out-dir` (warm cache ≈ 2 min) and require **every** clip
≈ 0 non-monotonic warnings, matching the source baseline. The previous attempt was
not verified against regenerated output — that is how the no-op shipped.
