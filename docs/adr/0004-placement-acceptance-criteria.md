# Placement acceptance criteria: a per-boundary-class definition of done

## Status

accepted — ratified by the owner 2026-07-03, numbers as proposed (≤1.0s CLEAN /
≤0.5s VISIBLE SHOT CHANGE / purity-only SDZ / no LDZ requirement). The owner chose
the 1s floor over the sub-second option (open question 2: resolved — sub-second
residuals are not bugs) and accepted the c21041 gap (open question 4: resolved —
0.5–1.0s at a visible shot change grades CLEAN and passes). REQUIREMENTS.md
replacement wording applied same day. Questions 5 (measure_placement re-baseline;
largely superseded by `.scratch/placement_report.py`) and 6 (LDZ obligations)
remain open but do not block the spec.

## Context

The project has a codified placement spec scattered across three documents —
REQUIREMENTS.md L23 (1-second floor, frame-level aspirational), L25/L29 (Splice Dead
Zone is an Ambiguity Window judged by content purity), and ADR 0001 (end-of-noise-burst
policy) — but no single per-class definition of done. The observable consequence:

- Spot-checks are graded against an unwritten "frame-perfect boundaries" standard.
  Under that standard the pipeline *always* fails, because at a splice frame-accuracy
  is unsatisfiable by design (CONTEXT.md: "Frame-accurate Placement is unsatisfiable
  there") and the noise burst is *deliberately* left in the outgoing clip's tail —
  which looks exactly like a misplaced cut to a reviewer who hasn't read ADR 0001.
- Each placement review re-litigated what counts as a failure. One review ruled a 0.3s
  tail residual "within spec"; a later one found 2–4s leaks and had to re-derive
  the pass/fail line from REQUIREMENTS line numbers; another invented its own
  acceptance list. Three documents, three ad-hoc rubrics.
- The existing requirements *contradict each other* at the margin (see "Contradictions
  this ADR must resolve" below), so even a diligent reviewer cannot grade consistently.

This ADR defines boundary **classes** with machine-decidable membership, a per-class
acceptance criterion, the measurement procedure for each, and what a human spot-check
should expect to see when the pipeline is working as specified. It also proposes
replacement wording for the REQUIREMENTS.md "Boundary timing" paragraph (quoted below;
REQUIREMENTS.md is not edited by this ADR).

### Contradictions this ADR must resolve

Found while cross-checking the evidence base; each is resolved by a decision below.

1. **REQUIREMENTS L17 vs L23.** L17: cross-date contamination "is a failure regardless
   of magnitude or duration." L23: "1-second resolution is the acceptable floor." A cut
   within the 1s floor can leave up to ~1s of adjacent-date footage at a clip edge
   (verified ~0.3s of next-date footage in a tail at integer-second
   granularity), so L17 as written is unsatisfiable without the sub-second dense scan
   L23 explicitly defers. Practice ("≤1s" acceptance lines from prior reviews) has
   already resolved this toward L23. This ADR codifies that: purity is judged at the
   1-second measurement floor.
2. **ADR 0001 vs the SDZ seconds-guardrails.** ADR 0001: splice quality "is measured by
   clip-content (date-purity) audit, not a labeled boundary set" — a per-second error
   at an Ambiguity Window is meaningless. Yet prior reviews gate changes on
   `measure_placement.py` SDZ median/max seconds. Resolution: the seconds numbers are a
   *relative regression guardrail* (did a change move splice cuts the wrong way), never
   an *acceptance criterion*. Acceptance at an SDZ is purity-only.
3. **SDZ guardrail numbers disagree.** A 2026-06-21 review measured SDZ median
   1.0s / max 12s on the hand-labeled set; a later review reports "SDZ median 2.0s,
   max 4s — not worse than baseline" *after* fixing `measure_placement.py`'s crop
   (`250:110:385:370` → `560:130:40:350`) and its stale `group_clips` call. The two
   figures come from different script versions and are not comparable. The guardrail
   needs a re-baseline before it can gate anything; neither number is cited as
   authoritative here.
4. **Scene-snap default status.** Finding 003 and the scene-snap memory say the flag is
   default-off / "target logic not safe to default"; finding 005 (later, 2026-06-25)
   flips it default-on after the v3 constants; the code is ground truth:
   `--enable-scene-snap` defaults to **on** (`split_homevideo.py`,
   `argparse.BooleanOptionalAction, default=True`). The VISIBLE SHOT CHANGE class below
   assumes default-on. CLAUDE.md's "Visual signals" bullet does not mention scene-snap
   at all and should be updated separately.
5. **The purity oracle cannot see the defects this spec regulates.**
   `.scratch/date_purity.py` (a) still uses the old right-anchored crop
   `250:110:385:370`, which the crop-widening work showed clips off-center overlays,
   and (b) deliberately samples only the clip interior, skipping the first/last 1.5s
   ("boundary re-encode region"). Both blind spots sit exactly where edge leaks live.
   It remains useful as a gross-contamination backstop; it is **not** the acceptance
   oracle. The per-class procedures below define what is.

### Evidence base and its thinness

Be honest about how little ground truth exists — every number below is validated on at
most two tapes (the 1990 and 1992 tapes) with no labeled benchmark (ADR 0001 abandoned
the AI-labeled golden set):

- The **≤0.5s** post-snap figure rests on **two** frame-audited clean snaps (finding
  005: a −0.49s snap landing ~0.3s past the transition; a −0.25s snap landing
  essentially on it), plus four snaps frame-verified as landing on genuine
  content-change frames without a residual-seconds measurement (finding 003 v2
  spot-check), plus one audited burst. That is the entire audited population.
- Scene-snap fires on only **14/81 (17%)** and **9/29 (31%)** of boundaries on the two
  tapes (finding 005) — most boundaries have *no* visual discontinuity (VHS
  pause/resume), so VISIBLE SHOT CHANGE is a minority class by construction.
- The end-of-burst SDZ policy was established on **two** boundaries (ADR 0001) and
  re-audited on one (finding 005's +0.73s burst).
- The hand-labeled SDZ placement set contains **five** usable boundaries
  (`measure_placement.py` `TRUE` table).
- The 120s Splice-vs-Long Dead Zone threshold: observed bursts are ~2–15s; CONTEXT.md's
  ≲120s ceiling covers 33 of 34 dead-zone date-changes on the test material. Nothing
  between ~15s and 120s has been observed, so the ceiling is a classification
  convention, not a measured property.

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

The seconds-based SDZ statistics from `measure_placement.py` (median/max over the
hand-labeled set) remain in use as a **regression guardrail** for placement changes —
strictly relative, before/after on the same script version and boundary set — and must
be re-baselined after the script's crop fix (contradiction 3) before gating anything.

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
   role `date_purity.py` keeps, after its crop is fixed.

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

### Proposed REQUIREMENTS.md replacement wording

To replace the current "Boundary timing must be accurate to within one second…"
paragraph (L23), verbatim proposal:

> **Boundary placement is judged per boundary class; the acceptance criteria live in
> ADR 0004.** Where OCR is legible within ~2s on both sides of the change (a CLEAN
> boundary), the cut must land within 1 second of the true session change: up to one
> second of adjacent-date footage at a clip edge is within specification (the dense
> refinement scan is integer-second), and more than one second is a placement failure.
> Where a scene cut is visible at the transition, scene-snap tightens the target to
> 0.5 seconds. At a Splice Dead Zone the boundary is an Ambiguity Window: placement is
> judged only by content purity — the incoming clip starts with zero wrong-date
> footage and the unreadable noise burst stays with the outgoing clip's tail — never
> by a per-second error. Long Dead Zones (>120s unreadable) carry no placement
> requirement. Frame-level precision (≤1 frame, ~33ms) remains aspirational and is not
> an acceptance criterion for any class.

If adopted, L17's "regardless of magnitude or duration" clause needs the companion
amendment "…as measured at the 1-second audit floor (ADR 0004)" — otherwise
contradiction 1 stands.

## Deviations from the drafting brief's starting proposal

1. **CLEAN ≤1s: kept, but restated two-sidedly and operationally** (no old-date
   attributable frame ≥1s after the cut; no new-date attributable frame ≥1s before
   it). The one-number form hides which side leaked; a prior review showed head and tail
   leaks have different root causes and must be separately visible in a report.
2. **VISIBLE SHOT CHANGE ≤0.5s: kept, but the class is defined by the detector
   signature (adaptive cut within ±0.5s), not by "a scene cut exists somewhere near
   the transition."** Consequence, stated openly: a true shot change whose adaptive
   cut sits 0.5–1.0s from the placed cut (finding 003's c21041, 0.89s — a verified
   genuine shot change that v3's tight window makes a no-op) grades under CLEAN and
   *passes* at 0.89s. The alternative — defining the class by the transition itself —
   would make c21041 a standing failure that finding 003 already ruled acceptable,
   because widening the accept window reintroduced the 6 mid-session decoy snaps
   (−0.59 to −0.96s, all into old-session content) that the tight window exists to
   block. Owner may override (open question 4).
3. **SDZ purity: strengthened.** The brief said "outgoing tail may carry the
   unwatchable noise burst." Correct, but insufficient: prior reviews
   show the gap is often *garbled-attributable*, not pure noise, and garbled
   *new*-date frames dragged into the tail were a real, fixed bug. The criterion
   therefore distinguishes noise (tail is fine) from date-attributable garble (must
   land on its own date's side), and explicitly checks both emitted clips, not just
   the incoming head.
4. **Added a guardrail/acceptance split for SDZ seconds** (contradiction 2) instead of
   silently dropping `measure_placement.py` — the tool stays, demoted to relative
   regression use, pending re-baseline (contradiction 3).
5. **Added the classifier's extension rule and attributability definitions** so class
   membership is genuinely machine-decidable rather than "≲120s" hand-waving.

## Considered options

- **Single global threshold (1s everywhere):** simplest, but reproduces today's
  failure mode at splices — 1s is unsatisfiable inside an Ambiguity Window, so every
  splice spot-check "fails" and the number is noise. Rejected (this is the status quo
  being replaced).
- **Frame-perfect everywhere:** requires sub-second dense scanning (~10× OCR cost per
  the dense-scan analysis) *and* is still unsatisfiable at splices where all signals
  saturate (ADR 0001, `docs/SPEC_rejected_signals.md`). Rejected; kept as an explicit
  owner option for CLEAN boundaries only (open question 2).
- **Purity-only everywhere (no seconds at all):** attractive uniformity, but a
  seconds-free criterion cannot express "the cut is 4s late" at a CLEAN boundary where
  the true change *is* recoverable to 1s — regressions would hide until they exceed a
  whole purity sample. Rejected for CLEAN/VSC; adopted for SDZ where it is the only
  meaningful measure.
- **Per-class criteria (chosen):** matches what the evidence actually supports per
  class, and gives spot-checks a decidable rubric.

## Open questions for the owner

Only the owner can decide these; the ADR stays "proposed" until they are answered.

> **2026-07-03 owner ruling (partial).** The owner stated a guiding tenet: *concatenating
> the clips in order must effectively recreate the original video* (now in
> REQUIREMENTS.md, Fundamental assumptions). This resolves **question 3**: the noise
> burst stays in the outgoing tail — any discard/double-cut variant is permanently
> rejected (independently supported by the burst memo's frame audit: ~85% of all-`None`
> span seconds are real footage). It also settles the memo's TV-filler question: no-
> overlay content stays. **Question 7** is partially resolved: the per-class report tool
> now exists (`.scratch/placement_report.py`, frame-audit validated 2026-07-02/03) and
> `date_purity.py`'s crop is fixed. Questions 1, 2, 4, 5, 6 remain open.

1. **Ratify the numbers**: ≤1.0s CLEAN, ≤0.5s VISIBLE SHOT CHANGE (on two audited
   boundaries), purity-only SDZ. Any of these can be tightened/loosened, but then the
   evidence gap must be closed with new audits.
2. **Is frame-perfect at CLEAN boundaries worth it?** Getting below the 1s floor
   requires a sub-second dense scan at roughly 10× the OCR cost of refinement windows.
   If ~1s residuals at clip edges genuinely bother you when watching, this is the only
   honest path — say so and it becomes a costed work item; otherwise the 1s floor is
   the standard and sub-second residuals stop being filed as bugs.
3. **Is keeping the noise burst in the outgoing tail acceptable long-term?** ADR 0001
   rejected the double-cut ("discard the burst") option because it loses footage.
   The tradeoff is that every splice-preceding clip ends with seconds of static that
   *looks* wrong. If unwatchable-but-preserved is the wrong call for a family archive,
   reopening the double-cut option changes this spec's SDZ tail rule.
4. **The c21041 gap**: is a 0.5–1.0s leak at a *visible* shot change acceptable
   (current spec: yes, graded CLEAN), or should scene-snap's accept window be widened
   with a better decoy guard so VISIBLE SHOT CHANGE covers it? The decoy evidence
   (6/13 wrong movers before v3) says widening naively is worse.
5. **Re-baseline the SDZ guardrail**: which `measure_placement.py` configuration and
   boundary set is canonical (post-crop-fix)? The 1.0s/12s vs 2.0s/4s discrepancy must
   be resolved before the guardrail can gate merges again.
6. **Should any LDZ obligation exist at all** — e.g., "a clip spanning a Long Dead
   Zone must not assert a date label for the unreadable span," or a report-only
   listing? Currently the spec's only LDZ duty is "don't misclassify it as an SDZ."
7. **Upgrade the purity oracle?** `date_purity.py` needs the current crop and
   edge-region coverage before it can serve even as the backstop for this spec; the
   per-class report tool in "Measurement procedure" does not exist yet and needs a
   build decision.

## Consequences

- Spot-checks gain a decidable rubric: static at an outgoing tail before a splice is
  graded PASS, not filed as a misplaced cut; a 2s leak at a CLEAN boundary is a
  defect without needing a new REQUIREMENTS exegesis per issue.
- Prior reviews' ad-hoc acceptance lines are superseded by the class table once
  this ADR is accepted; future placement reviews cite a class and a criterion.
- A placement report tool (measurement procedure above) becomes the acceptance
  instrument; `date_purity.py` is demoted to backstop and needs its crop fixed;
  `measure_placement.py` is demoted to relative guardrail pending re-baseline.
- REQUIREMENTS.md L23 (and a clause of L17) get the replacement wording above if the
  owner ratifies; until then the current text stands and this ADR is advisory.
- The known doc drift called out in Context (scene-snap default status in older
  finding/memory text; CLAUDE.md's visual-signals bullet) should be cleaned up in a
  separate housekeeping change — not done here to keep this ADR single-purpose.
