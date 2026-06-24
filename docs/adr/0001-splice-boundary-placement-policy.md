# Splice boundary placement: end-of-noise-burst, not frame-recovery

## Status

accepted

## Context

At a tape **Splice**, head-switch noise blanks the timestamp overlay (OCR returns `None` for the whole span) *and* saturates every visual detector — scene-cut and black-frame events fire continuously across the burst. We confirmed this empirically on `Converse 1990.mp4`: the 480s and 2960s boundaries each have a ~15s noise burst inside which no single frame is identifiable as "the" session change.

This produced a long churn of fixes that kept moving the error from one side to the other ("cut late" → patch → "cut early" → patch). Root causes:

1. **Two distinct problems shared one name.** "Wrong boundary" meant both **Detection** (does a boundary exist near t? — measured, F1=0.920 on the 215-label golden set) and **Placement** (given a real boundary, what second do we cut? — *unmeasured*). Tuning detection while the pain was placement guaranteed circular fixes.
2. **The boundary at a splice is not a frame.** It is an **Ambiguity Window** no available signal resolves finer.

## Decision

- **A Splice Dead Zone boundary is an Ambiguity Window.** Frame-accurate placement is unsatisfiable there; we adopt an edge *policy* instead of seeking a frame.
- **Objective: no wrong-date footage.** The (unwatchable) noise burst belongs to the outgoing clip's tail, so the new-day clip starts clean.
- **Anchor rule:** cut at the **last** visual event within the all-`None` span of the dense refine scan. Fallback when the span has no visual event: cut at the **end of the None-span** (just before the first clean new-date frame). Never `coarse_t` for a confirmed splice.
- **Scope:** policy applies only to **Splice Dead Zones** (≲120s of `None`). **Long Dead Zones** (up to 2160s of genuinely unreadable footage) are a separate, out-of-scope concept.
- **Visual anchoring is always-on; the drop-filter is opt-in.** `detect_visual_boundaries` runs automatically (cached) to supply anchor candidates during refinement. `fuse_boundaries` — which *drops* OCR boundaries lacking visual corroboration — stays behind `--enable-visual-fusion`, default **off**, because VHS pause/resume frequently has no visual discontinuity and the filter would delete real boundaries.
- **Placement is measured by clip content audit, not per-boundary ground truth.** The clip audit (vision-based sampling of frame content vs. filename date) is the primary quality signal. Human-labeling of per-boundary true-change seconds is deprecated — the audit directly answers "is the content correct?" which is the real objective. The Detection F1 golden set (`archive/Converse 1990_golden_labels.jsonl`) is retained as a regression guard for boundary detection only — AI-labeled (machine-generated verdicts), so treat it as indicative, not authoritative.

## Considered options

- **`coarse_t` (first clean new-date OCR):** always late by up to one interval — the original bug.
- **Earliest visual event (`anchors[0]`, as shipped in dbca807):** start-of-burst → cuts ~13–15s *early*; verified to overcorrect on both test boundaries. Superseded.
- **Drop the noise burst (double-cut, discard span):** cleanest clips but loses footage and complicates the cut model. Rejected.
- **Midpoint of the window:** arbitrary; leaves wrong-date footage on whichever side the true change sat. Rejected.
- **Chase a finer signal (audio transient, OCR-confidence, perceptual hash):** rejected as a class — all signals saturate inside the burst, so more signals cannot resolve the window. See `docs/SPEC_rejected_signals.md`.

## Consequences

- `dbca807` is corrected fix-forward (not reverted): anchor flipped to last-in-None-span, fusion drop-filter decoupled back to opt-in.
- `REQUIREMENTS.md` frame-accuracy requirement is narrowed to "where OCR-recoverable"; splice boundaries follow the window policy.
- Long Dead Zone handling remains an open problem.
