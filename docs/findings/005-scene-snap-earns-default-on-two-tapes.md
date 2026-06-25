# Finding 005: scene-snap earns its default-on slot — measured on two tapes

**Status:** actioned (2026-06-25)
**Trigger:** a "trim to core" review questioned whether the two back-to-back refinement
passes (OCR-refine, then scene-snap) compete over the same seam — i.e. whether one
subsumes the other and the pipeline carries a redundant pass. Scene-snap had just been
flipped default-on, but **no prior run log showed it ever firing** (every logged run
predated the flip), so the question had never been measured.
**Scope:** decides whether scene-snap (pass 3) stays default-on and whether either
refinement pass can be removed. Extends finding [003] (1990-only) to the 1992 tape.

---

## What was measured

Scene-snap was replayed standalone against both real videos, using each boundary's
logged OCR-refined time as the anchor (`prev_t = coarse − win`) — no OCR re-scan needed,
since `snap_to_scene_cut` only needs an anchor, a `prev_t`, and the video. This isolates
exactly what pass 3 does on top of pass 2.

| Tape | boundaries | fired | clean (backward) | burst (forward) |
|---|---|---|---|---|
| 1990 | 81 | 14 (17%) | 13, mean −0.29s, max −0.49s | 1, +0.73s |
| 1992 | 29 | 9 (31%)  | 8, mean −0.21s, max −0.47s | 1, +0.04s (noise) |

Cost ≈ 0.2s/boundary (PySceneDetect over a ~22s window) — negligible against a multi-hour
scan. Every move is sub-second: ≤0.49s on clean cuts, +0.73s on the one real burst.

## What the frame audit said

Three representative fires were verified frame-by-frame (the project's trusted direct
oracle, not an aggregate metric — see finding [003] for why the offset aggregate alone
misleads). All three **improved** placement; none jittered:

- **1990 burst (+0.73s)** — the same splice as finding [003]'s c3506. Frames go head-switch
  noise → clearing → clean hallway (clock 6:29→6:30). Snap lands at burst-end, past the
  noise onto clean footage. Correct per ADR 0001 (noise stays with the outgoing clip).
- **1990 clean (−0.49s)** — true shot change 8/15 highchair → 8/18 bottle-feed. OCR anchor
  sat ~0.8s into the new date; snap pulled the cut back to ~0.3s past the transition,
  cutting the new-date leak into the old clip by ~0.5s.
- **1992 clean (−0.25s)** — true shot change 12/13 baby → 12/20 cookies. OCR anchor sat
  ~0.12s into the new date; snap landed essentially on the transition.

## Implication

**Both passes earn their keep. Keep scene-snap default-on. Remove neither.** The "competing
over the same seam" premise is wrong: the passes are sequential and complementary, not
duplicate.

- **OCR-refine (pass 2)** is date-AWARE: it decides *which* session a frame belongs to and
  *where* across gaps up to the 120s dead-zone, then places an integer-second-granular cut.
  Irreplaceable — scene-snap is date-blind with a ±20s window and cannot tell one date from
  the next.
- **Scene-snap (pass 3)** is a date-BLIND, sub-second frame-snap on pass 2's output. It
  fires on ~21% of boundaries, never more than ~0.7s, and (audited) tightens date-purity at
  the seam plus handles the noise-burst case. It cannot do semantic placement, so it cannot
  replace pass 2 either.

A single scene-based refine (the original mental model) would lose the date-aware placement
entirely.

## Known architectural note (not actioned)

Two independent visual detectors coexist: `detect_visual_boundaries` (ffmpeg scene/black
filter, cached → `visual_times`, feeds OCR-refine's dead-zone anchor) and
`snap_to_scene_cut`'s inline PySceneDetect `AdaptiveDetector`. They serve different roles —
coarse cached anchors vs. precise adaptive per-boundary frame-snap — and unifying them risks
the adaptive precision that finding [003] established as necessary (fixed-threshold ffmpeg
fires on VHS luma noise). Flagged as a known seam; left intact.

## Evidence

Replay + frame-extraction scripts and the straddle frames are local (scratchpad / not
committed). Offsets are from the cached OCR-refined boundaries of the 1990 and 1992 tapes.

[003]: 003-scene-snap-clean-vs-noise-burst.md
