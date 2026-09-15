# Placement acceptance criteria: a per-boundary-class definition of done

## Status

accepted — ratified by the owner 2026-07-03. Sub-second residuals at CLEAN
boundaries are not bugs; 0.5–1.0s at a visible shot change grades CLEAN and passes.

## Context

Before this ADR, "frame-perfect boundaries" was the unwritten grading standard, under
which the pipeline always failed: at a splice, frame accuracy is unsatisfiable by
design (ADR 0001) and the noise burst is deliberately left in the outgoing clip's
tail, which looks exactly like a misplaced cut to a reviewer who hasn't read the
policy. Each review re-derived its own rubric. This ADR fixes one decidable rubric
per boundary class.

## Decision

### Boundary classes

Classes are decided per placed cut `t_c`, from two machine-derivable inputs:

- **Dense scan `D`**: 1s-interval OCR of the *source* over `[t_c − 10s, t_c + 10s]`
  (`extract_frame` + `ocr_batch`, crop `560:130:40:350`, strict `parse_timestamp`).
  If the non-legible run through `t_c` reaches the window edge, extend symmetrically
  until the run is bracketed by legible reads or 120s per side is reached.
  Each sample is one of: **legible** (strict parse succeeds), **garble-attributable**
  (parse fails but `_gap_date_class` assigns old/new from raw text), or
  **noise** (no date evidence).
- **Scene cuts `S`**: PySceneDetect `AdaptiveDetector` output over
  `[t_c − 20s, t_c + 2s]` (the finding-003 v2 window; fixed-threshold detectors fire
  on VHS luma noise and must not be used here).

Let `R` = the maximal contiguous run of non-legible samples in `D` containing or
immediately adjacent to `t_c` (empty when legible reads exist within 1s on both
sides). Classify top-down, first match wins:

| Class | Machine-decidable membership |
|---|---|
| **LONG DEAD ZONE** | `len(R) > 120s`, or `R` cannot be bracketed by a legible read on either side within the 120s/side extension. |
| **SPLICE DEAD ZONE** | `2s < len(R) ≤ 120s`, bracketed by a legible old-date read before `R` and a legible new-date read after `R`. Typically carries the burst signature in `S`: two adaptive cuts ≤ 6s apart spanning the run (`SCENE_SNAP_BURST_MAX_S`, finding 003). |
| **VISIBLE SHOT CHANGE** | Meets CLEAN, **and** exactly one adaptive cut in `S` within `[t_c − 0.5s, t_c + 0.5s]` with no second adaptive cut within 6s of it (single-cut signature; a pair ≤ 6s apart is a burst → SDZ). |
| **CLEAN** | A legible read exists in `[t_c − 2s, t_c]` **and** in `[t_c, t_c + 2s]` (old and new date respectively at a true boundary). |

Notes:

- Garble-attributable samples count as *non-legible* for classification but remain
  *date-attributable* for the purity judgments below. This is deliberate: a 5s run of
  garbled new-date frames classifies the boundary as SDZ, but those frames still
  belong to the incoming clip (a prior garbled-new fix and re-scope).
- The class is decided around the *placed* cut. A cut so misplaced that the true
  change is outside the (extended) window is a gross failure caught by the whole-clip
  purity backstop, not by this classifier.
- Deliberate anti-gerrymander: a genuine shot change whose adaptive cut sits 0.5–1.0s
  from the placed cut (the finding-003 c21041 case, 0.89s) classifies as plain CLEAN,
  not VISIBLE SHOT CHANGE — see "Deviations" for why we accept this.

### Per-class acceptance criteria

| Class | Acceptance criterion | Basis |
|---|---|---|
| **CLEAN** | No date-attributable frame of the *previous* session's date at `t ≥ t_c + 1.0s`, and no date-attributable frame of the *next* session's date at `t ≤ t_c − 1.0s`. Equivalently: cut within ≤ 1.0s of the true change; up to 1.0s of adjacent-date footage at a clip edge is **in spec**. | REQUIREMENTS L23 (1s floor); integer-second dense scan, ~0.3s residual achievable, sub-second requires ~10× OCR per the dense-scan analysis; prior reviews graded 2–4s leaks as real defects and ≤1s as pass. |
| **VISIBLE SHOT CHANGE** | CLEAN criterion, tightened to **≤ 0.5s**: the cut lands within 0.5s of the shot-change frame. | Finding 005's two audited clean snaps (−0.49s → ~0.3s residual; −0.25s → on the transition); v3 `SCENE_SNAP_ACCEPT_S = 0.5` bounds the snap move. **Thin evidence — two audited boundaries**; ratify or demote to ≤1.0s. |
| **SPLICE DEAD ZONE** | Judged by **content purity only**; a per-second error number is not an acceptance measure here (ADR 0001). Three conditions: (a) the incoming clip contains **zero** date-attributable wrong-date frames — legible *or* garble-attributable — at its head; (b) the outgoing clip's tail contains no date-attributable *new*-date frames (legible or garble-attributable); (c) the outgoing tail **may and normally will** carry the non-attributable noise burst — up to `len(R)` seconds of static/garble at the tail is correct behavior, not a defect. | ADR 0001 (objective: no wrong-date footage; burst belongs to the tail); REQUIREMENTS L29; garbled-new belongs to the incoming clip; legible next-date frames inside the burst belong to the new clip (later re-scope). |
| **LONG DEAD ZONE** | **No acceptance criterion — explicitly out of scope** (ADR 0001, CONTEXT.md). Only obligations: the classifier must not treat a >120s run as an SDZ (no end-of-burst placement), and the run's occurrence should be logged for the owner. A spot-check finding a bad cut inside an LDZ is *recorded, not graded*. | ADR 0001 scope clause; CONTEXT.md Long Dead Zone entry. |


### Measurement procedure per class

A placement report tool grades every cut in a run's clip list as follows. Steps 1–2
are the shared classifier inputs defined above.

1. Dense scan `D` over `[t_c − 10s, t_c + 10s]` of the **source** (extend per the rule
   above). Record per-sample: legible date / garble class / noise.
2. Adaptive scene-cut list `S` over `[t_c − 20s, t_c + 2s]`.
3. Classify (table above).
4. **CLEAN** — from `D`: fail if any old-date-attributable sample at
   `t ≥ t_c + 1.0` or any new-date-attributable sample at `t ≤ t_c − 1.0`.
   Fully automatic; 1s sampling is exactly the criterion's resolution.
5. **VISIBLE SHOT CHANGE** — the ≤0.5s bound is below 1s sampling resolution, so:
   extract straddle frames at `t_c ± 0.25s` and `t_c ± 0.5s` (`extract_frame`), OCR
   them, and *visually* audit the four frames (the frame-level audit is the project's
   trusted oracle — finding 003 showed the offset aggregate alone misleads, and the
   cheap OCR-readability auto-classifier was tried and rejected there). Pass if the
   shot change falls within the ±0.5s bracket.
6. **SPLICE DEAD ZONE** — three checks:
   (a) dense 1s OCR of the **emitted incoming clip's** first `max(6, len(R))` seconds:
   every attributable sample must match the clip's own date;
   (b) dense 1s OCR of the **emitted outgoing clip's** last `max(6, len(R) + 2)`
   seconds: no new-date-attributable sample (noise and old-date are both fine);
   (c) confirm the burst sits in the tail, not the head — the incoming clip's first
   ~2s should not be dominated by non-attributable noise (that would mean the cut
   landed at burst-start, the exact failure mode of finding 003's v1 snap).
   Note the emitted-clip scans must OCR the first/last seconds — the region
   `date_purity.py` skips — and must use the current default crop.
7. **LONG DEAD ZONE** — record `len(R)`, the bracketing dates if any, and the cut
   method used; no pass/fail.
8. Whole-run backstop: an interior purity sweep across every clip (12+ samples/clip,
   current crop) to catch gross misplacement the ±10s window cannot see. This is the
   role of `date_purity.py`.

### How to spot-check (what "working as specified" looks like)

For a human scrubbing clip edges in a player. Grade against the class, not against
frame-perfection:

- **At a CLEAN boundary**: the outgoing clip may show up to ~1 second of the next
  day's footage at its very end, or the incoming clip up to ~1 second of the previous
  day at its start. **Within one second this is in spec** — the dense scan is
  integer-second. More than ~1s of wrong-date content at an edge is a
  defect: file it citing this ADR and the measured leak length.
- **At a VISIBLE SHOT CHANGE**: the clip edge should sit essentially on the shot
  change — half a second of slop at most. If you can see a clearly leaked shot
  (~1s+), that is a defect *if* a scene cut is visible at the transition; if the
  transition has no visual discontinuity (most VHS pause/resumes), grade as CLEAN.
- **At a SPLICE DEAD ZONE**: the *outgoing* clip ends with several seconds (observed
  ~2–15s) of head-switch static/garbled frames. **This is correct behavior, not a
  misplaced cut** — the burst is unwatchable and unattributable, and ADR 0001
  deliberately assigns it to the tail so the new day starts clean. What *is* a
  defect: (a) any readable wrong-date footage at the incoming clip's head, (b) the
  static burst appearing at the *head* of the incoming clip, (c) readable or
  date-evidenced next-day footage buried in the outgoing tail's garble.
- **At a LONG DEAD ZONE** (minutes of unreadable footage): the pipeline makes no
  placement promise. Note what you see; do not file it as a placement bug.
- Unsure which class a boundary is? A short static burst at the seam ⇒ SDZ rules; a
  crisp visible shot change ⇒ VISIBLE SHOT CHANGE rules; otherwise CLEAN rules. The
  report tool's classifier output is authoritative when it disagrees with eyeballing.


## Consequences

- `.scratch/placement_report.py` (local, gitignored) implements the measurement
  procedure above and is the ruler for every placement change — run before/after.
- `.scratch/date_purity.py` is the whole-run interior purity backstop (step 8); it is
  edge-blind and must not be used to judge placement.
