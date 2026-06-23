# Finding 003: shot-change snap fixes clean cuts but mis-handles noise-burst splices

**Status:** implemented and spot-checked (2026-06-24)
**Trigger:** spike of a PySceneDetect-based "ultra-refinement" pass — snap each OCR-refined
cut onto a precise shot-change frame to remove the 0.5–4s of a neighbour's session that leaks
across most clip boundaries.
**Scope:** decides whether a scene-snap pass (pass 3, after scan + OCR refinement) can ship,
and what its anchor rule must be. Built behind `--enable-scene-snap` (default off); the target
logic is **not** safe to default yet.

---

## Idea under test

OCR places a cut at the last sample where it could still read the old date — interval/second
granular, so a few seconds of the old session leak into the next clip. A real shot change
(camera off→on at a session boundary) is the true cut frame. Decode a window before the
OCR-refined time `t` with PySceneDetect's `AdaptiveDetector`, snap `t` **backward** onto the
nearest detected shot cut within a tight `[t−3s, t]` accept window, else leave `t` unchanged.

`AdaptiveDetector` (rolling-average) is used over `ContentDetector` / the existing ffmpeg
`select=gt(scene,thr)` pass because the fixed-threshold detectors fire on VHS luma noise — the
whole-file ffmpeg pass returns ~384 cuts, mostly spurious. Adaptive with ~20s of context
returns 0–2 clean cuts per window.

## What the metric said

On the 1990 tape (`--interval 3`, cached scan), snap moved **14 of 81** boundaries, **all
backward**, 0.20–1.67s, median 0.62s; 67 unchanged. By construction it is monotonic — it only
moves a cut backward onto a detected adaptive cut, never forward, never without a signal. The
aggregate looked clean and regression-free.

## What frame-level verification said

The metric was misleading. Two of the 14 movers, inspected frame-by-frame, are **opposite
cases**:

### Clean cut — snap is correct

Boundary near 21036s (OCR cut 21036.0, snapped to 21035.11, −0.89s):

| t | content |
|---|---|
| 21034.6 | OLD session — outdoor ladder, overlay `10/7/90` |
| 21035.11 (snap) | NEW session — indoor baby, `10/13/90 4:20 PM` |
| 21036.0 (OCR cut) | 0.89s of the ladder scene leaked into the new clip |

The adaptive cut sits exactly on the content change. Snap removes the leaked old-session
footage — the reported defect, fixed.

### Noise-burst splice — snap is wrong

Boundary near 3504s (OCR cut 3504.0, snapped to 3502.33, −1.67s):

| t | content |
|---|---|
| 3501.6 | OLD session — kitchen, `1/24/90 7:44 AM` (clean) |
| 3502.3–~3504.5 | VHS head-switch **noise burst** (scrambled frames) |
| 3505.0 | NEW session — hallway, `1/27/90 6:30 PM` (clean) |

At a splice the burst has two luma edges: clean→noise at its start (~3502.3) and noise→clean at
its end (~3504.5). `AdaptiveDetector`'s strongest cut is the **start** of the burst. Snapping
backward to it moves the cut to 3502.33, pushing the **entire noise burst into the new clip's
head**. OCR's 3504.0 was closer to right. This is the opposite of the project's established
placement policy (ADR 0001): noise stays with the outgoing clip, the new clip starts clean.

## Why the metric hid it

"All backward, monotonic, 0.2–1.7s" is exactly what *both* cases produce — a correct snap onto
a content change and an incorrect snap onto a burst-start are indistinguishable in the offset
aggregate. Only per-frame inspection separates them. (Consistent with the standing rule to
distrust placement metrics without a visual/oracle ground truth.)

## Cheap auto-classifier (OCR readability) does not separate them

Attempted discriminator: OCR three frames just after each snap — a clean cut should read the new
date, a burst-start should be unreadable. It mislabelled the verified-**clean** 21035 boundary
as a burst (0/3 reads) because the new-session frame has motion blur the OCR missed. Unreliable;
do not gate on it.

## Reliable discriminator (not yet implemented)

The signal that *does* separate the cases is the `AdaptiveDetector` output itself:

- **Noise-burst splice** → two cuts close together (clean→noise, then noise→clean). The
  burst-end cut typically sits **just past** the OCR `t`, so a backward-only window misses it.
- **Clean cut** → a single cut.

## v2 measurement (2026-06-24)

Implemented in `snap_to_scene_cut()` with new constants `SCENE_SNAP_AFTER_S=2.0` and
`SCENE_SNAP_BURST_MAX_S=6.0`. Detection window widened to `[t-20, t+2]`. Measured on 1990 tape
(`--interval 3`, cached scan, `--skip-cut --enable-scene-snap`):

- **coarse=3506** (the noise-burst splice): `snap=+0.73s` — forward move to burst-end (~3505.73).
  v1 would have snapped backward to ~3502.33 (burst-start, −2.67s from refined t=3505).
- **coarse=21041** (the clean cut): `snap=-0.89s` — unchanged backward behavior.
- All other 12 movers: negative or zero (backward clean snaps or cut-exactly-at-t).
- 1 positive offset total across 81 boundaries — exactly the known burst.

Full spot-check of all 13 non-zero-offset snaps (2026-06-24):

- **c3506** (+0.73s burst): burst-end at 3505.73 fully clean (5/22/90), burst excluded. ✓
- **c2984** (−0.05s), **c8363** (−0.21s), **c13679** (−0.24s), **c17342** (−0.28s): all correctly
  landed on genuine content-change frames — before=old session, snap=new session. ✓
- **c21041** (−0.89s): verified clean cut (finding 003 showcase). Now a no-op under v3 (0.89s >
  ACCEPT_S=0.5s tight window). Acceptable: loses 0.89s improvement at one boundary.
- **6 wrong cases** (c3119, c5663, c11783, c13442, c15863, c17915): all −0.59 to −0.96s, all
  snapped into old-session content (mid-session AdaptiveDetector decoys). Eliminated by v3 split
  windows (tight ACCEPT_S=0.5s for clean cuts, wide BURST_ACCEPT_S=3.0s for burst detection only).

v3 constants: `SCENE_SNAP_ACCEPT_S=0.5`, `SCENE_SNAP_BURST_ACCEPT_S=3.0`. Flag still default-off;
spot-check passed on 1990 tape — no verified regressions remain.

## Implication / candidate design (implemented)

A safe snap pass must widen detection to ~`[t−20, t+2]` and branch on the signature:

1. Two cuts bracketing a short span (burst) → anchor to the **later** cut (end of burst) — new
   clip starts clean, consistent with ADR 0001. May land slightly after `t` (a forward move),
   which is correct for splices.
2. A single cut ≤ `t` (clean) → snap backward to it (current behaviour, correct).
3. Neither in the tight window → no-op (VHS pause/resume often has no visual discontinuity;
   ~67/81 boundaries here — unfixable by any visual method).

The flag, the per-boundary `snap_to_scene_cut()` helper, the pipeline wiring, and logging
(`method=snap saved=N.Ns`) exist and work; only the anchor rule (currently backward-to-nearest)
is unsafe. Lossless design is unaffected — snap only changes the target `t` fed to the existing
boundary-segment re-encode, which already produces frame-accurate cuts.

## Evidence

Spike + measurement + classifier scripts and extracted straddle frames are local
(scratchpad / not committed). Offsets and the two verified boundaries above are from the cached
1990-tape scan at `--interval 3`.
