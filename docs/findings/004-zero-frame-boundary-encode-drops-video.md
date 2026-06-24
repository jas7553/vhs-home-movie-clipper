# Finding 004: sub-frame boundary re-encode can produce an audio-only segment that drops video from the whole clip

**Status:** actioned (2026-06-24)
**Trigger:** a daily-mode run (1992 tape, `--interval 3`) produced one clip
(`clip03`, a garbled-boundary date change) with **audio only, no video stream**, and a wrong
container duration (104s for a clip that should be ~166s).
**Scope:** the boundary re-encode + concat cut path (`cut_clip_with_boundary_encode`). Motivates
a per-segment video-presence guard.

---

## What we observed

The clip's leading boundary segment spanned `[1137.61982, 1137.71992]` = **0.1001s** — above the
`MIN_BOUNDARY_SEG = 0.05s` floor, so the lead re-encode ran. `_ffmpeg_encode_seg` formats its
seek/duration args to 3 decimals: `-ss 1137.620 -t 0.100`. On this **VFR** source the only
candidate frame sat at `1137.71992`, right at the rounded window edge, and ffmpeg's input
fast-seek frame-drop discarded it. libx264 received **zero** frames and wrote an **audio-only**
segment.

The ffmpeg **concat demuxer copies its stream layout from the first input file.** With the
audio-only lead first in the list, every following segment's video packets were dropped — the
whole clip became audio-only, and the muxed duration metadata corrupted.

## Evidence

- `ffprobe` on the bad clip: one stream, `aac`, `nb_streams=1`, `duration=104.03`.
- Reproduced deterministically: real `cut_clip_with_boundary_encode(..., exact_start=1137.61982, ...)`
  → audio-only; the rounded value `1137.6` → correct video. The failure is an artifact of the exact
  float × `.3f` rounding × keyframe alignment, which is why it looked intermittent.
- Isolated the lead command `ffmpeg -ss 1137.620 -t 0.100 ... -c:v libx264` → output has **only an
  audio stream, zero video packets**.

## Why it was non-obvious

The code already carried a `MIN_BOUNDARY_SEG` floor *and* a comment naming the exact "zero video
frames" hazard for sub-frame windows. But the floor (0.05s) is below two frame-periods
(2 × 1/29.97 ≈ 0.067s) **and** below the `.3f` rounding error, so a window can clear the floor and
still encode zero frames. Detection (does a boundary exist) and placement (where) were both correct;
the defect was purely in cutting. No test exercised a sub-frame-but-above-floor window at an adverse
VFR alignment.

## Implication

A size floor cannot guarantee a re-encoded boundary segment contains video — only **verifying the
output** can. The fix (committed alongside this finding): `_ffmpeg_encode_seg` probes its output and
returns whether it has video frames; the lead/trail seg joins the concat only if it does, otherwise
it is dropped (the body already starts/ends on the keyframe, identical to the existing sub-frame
fallback). See `cut_clip_with_boundary_encode` and `_seg_has_video_frames`.

## Caveat / open edge

The guard is a backstop, not a root fix of the VFR seek behaviour. If a *future* change ever makes a
**body** stream-copy emit zero video frames, the concat could still go audio-only — the present guard
only covers the two re-encoded boundary segs. A stronger invariant would assert the **final** clip has
a video stream before returning. Not implemented yet; recorded here so the next regression has a
starting point.
