# Requirements

Expressed goals and constraints for the VHS home movie clipper pipeline. Derived from design decisions, explicit user statements, and implicit preferences observed during development.

---

## Fundamental assumptions

**Concatenating the clips in order must effectively recreate the original video.** Playing all clips back-to-back in filename order should effectively reconstruct the source tape: every second of source footage lives in exactly one clip — nothing discarded, nothing reordered. Splitting must be lossless as a whole, not just per-clip. Frame-exact reconstruction is the stretch goal; small seam artifacts from the boundary re-encode (~±0.3s duplication per seam, measured 2026-07-03) are acceptable flexibility, deleting content is not. This permanently rules out any cut policy that drops footage (e.g., discarding splice noise bursts — the option ADR 0001 rejected), and settles that no-overlay content (recorded-from-TV segments, static) stays in the archive.

**The source tape is chronological.** Footage is recorded sequentially onto the tape; the pipeline reads it forward and splits it forward. The pipeline must never reorder clips, stitch non-adjacent segments together, or merge a later tape segment with an earlier one because their OCR-derived dates happen to match. If the camera's clock was misconfigured and two separate recording sessions both show January 6, they are two clips — not one. Date labels inform clip naming only; they do not drive clip merging or reordering. The video timeline is the ground truth; OCR timestamps are metadata.

---

## Output correctness

**The same calendar date must not appear in more than one clip.** If footage from January 6 exists, all of it goes in one clip. A clip may span multiple dates (e.g., a short recording on Jan 4 followed by Jan 5 footage with no significant gap can share a clip), but no date may be split across clips.

**Each clip must contain footage only from its labeled date(s).** A clip named `_1990-01-06` must not include any frames dated 1990-01-05 or 1990-01-07. This is a correctness requirement, not a quality tradeoff — cross-date contamination is a failure regardless of magnitude or duration, as measured at the 1-second audit floor (ADR 0004).

**Clip boundaries must correspond to real pauses in recording.** A split should represent the camera being stopped and restarted — not an artifact of OCR noise, tape degradation, or algorithmic error.

**Output filenames must accurately reflect the date of the footage.** If the clip contains Jan 6 footage, the filename should say `1990-01-06`, not `1990-01-16` due to a misread.

**Boundary placement is judged per boundary class; the acceptance criteria live in ADR 0004** (ratified 2026-07-03). Where OCR is legible within ~2s on both sides of the change (a CLEAN boundary), the cut must land within 1 second of the true session change: up to one second of adjacent-date footage at a clip edge is within specification (the dense refinement scan is integer-second), and more than one second is a placement failure. Where a scene cut is visible at the transition, scene-snap tightens the target to 0.5 seconds. At a Splice Dead Zone the boundary is an Ambiguity Window: placement is judged only by content purity — the incoming clip starts with zero wrong-date footage and the unreadable noise burst stays with the outgoing clip's tail — never by a per-second error. Long Dead Zones (>120s unreadable) carry no placement requirement. Frame-level precision (≤1 frame, ~33ms) remains aspirational and is not an acceptance criterion for any class.

**At a Splice Dead Zone the boundary is an Ambiguity Window, not a recoverable frame.** Head-switch noise blanks OCR and saturates every visual detector across a ~15s burst, so frame-accurate placement is unsatisfiable there. The cut follows an end-of-noise-burst policy: the noise burst stays with the outgoing clip's tail (objective: zero wrong-date footage in the incoming clip). The cut anchors to the last visual event within the all-`None` span, falling back to the end of that span when no visual event exists. See `docs/adr/0001-splice-boundary-placement-policy.md`.

**Detection and Placement are separate concerns.** Detection (does a boundary exist?) and Placement (how many seconds the cut lands from the true session change) are independent. Both are judged by **clip-content audit** — frame content vs filename date — not by a labeled boundary set (the former AI-labeled golden set was abandoned as unreliable; see ADR 0001). A correct boundary says nothing about landing on the right second.

**A Splice Dead Zone's incoming clip must contain zero wrong-date footage** — the noise burst stays with the outgoing clip's tail (ADR 0001). Quality is judged by clip-content audit, not a per-boundary second-count: frame-accurate placement is unsatisfiable inside the Ambiguity Window, so the objective is content-correctness of the resulting clips.

---

## Robustness

**The pipeline must be robust to low OCR success rates.** On VHS source material, OCR reads may succeed on fewer than half of sampled frames. The pipeline must produce correct output even when large spans have no readable timestamps.

**The pipeline must tolerate consecutive OCR misreads without creating phantom clip boundaries.** A run of 2–3 frames reading a wrong date should be filtered out, not treated as a real date change.

**A genuine recording session must not be lost because it is shorter than the sampling interval.** The coarse scan samples every `--interval` seconds, so a 10–40s session may produce a single reading that is structurally identical to a single-frame misread. Detection must resolve that ambiguity by looking closer (dense probes around the reading), not by assuming the shorter explanation — a dropped short session is cross-date contamination of the neighbouring clip (see finding 006).

**Gap thresholds must be empirically tuned, not guessed.** The `--gap` default (currently 3600s camera-time) was validated by date-purity audit of the resulting clips (ADR 0001). Any change to the default requires the same evidence.

**Re-tuning must not require re-scanning.** Changing `--gap` or `--mode` should hit the OCR cache and return results in seconds. Only changes to the preprocessing filter chain or OCR engine require a new scan.

---

## Performance

**The pipeline should fully utilize available hardware.** The MacBook Air M4 has 10 CPU cores and a Neural Engine. Frame extraction, OCR, and boundary refinement should parallelize across cores. OCR in particular should not process frames sequentially when batch processing is possible.

**A full scan of a 6-hour tape at 10s intervals should complete in minutes, not hours.** (Current: ~10 min for scan + OCR; refinement + encode adds time proportional to boundary count.)

**OCR preprocessing should reuse a single ffmpeg decode pass.** One pass extracts all frames; no seek-per-frame for the bulk scan stage.

---

## Clip structure

**`--mode daily`** is the primary intended mode. Each clip corresponds to one or more calendar dates; no date spans two clips.

**`--mode session`** is a secondary mode for cases where intra-day splits are wanted (e.g., morning and afternoon as separate clips).

**Very short clips (< ~2 min) should be merged with a neighbor rather than produced as standalone files**, except in `daily` mode where every confirmed date change is a real boundary regardless of clip length.

**The labeling system (OCR-derived date/time in filename) must degrade gracefully.** If OCR cannot determine the date for a clip, the filename falls back to clip number only — it does not emit a wrong date.

---

## Signal quality

**The primary boundary signal is OCR timestamps.** Other signals (scene score, silence, freeze) have been evaluated and rejected as standalone proposers — they all saturate inside a splice noise burst (ADR 0001).

**Visual signals anchor placement; they never propose or veto boundaries.** `detect_visual_boundaries` supplies anchor candidates for splice placement only. A corroboration drop-filter was removed because VHS pause/resume often has no visual discontinuity.

**VHS static/noise in the frame before a boundary is a reliable real-splice indicator** and should be usable as a positive signal if a second-signal path is added.

---

## Source material constraints

**Camera clock runs approximately 2× real time** on the specific camcorder used. All camera-time thresholds (e.g., `--gap 3600`) are in camera-seconds, not wall-clock seconds.

**Timestamp format is `M/ D/YY` (date) and `H:MM AM/PM` (time)**, with a space before single-digit months and days. OCR implementations must account for the space-before-digit ambiguity this creates (e.g., `/ 6` misread as `16`).

**Source video is 640×480 VHS digitized footage.** The default crop (`560:130:40:350`) covers the full bottom overlay band on this format; per-tape auto-calibration scales it to other frame sizes. Other tapes may require `--crop` adjustment.

---

## Operability

**A dry-run mode must be fast.** `--dry-run` should show the proposed clip list without performing refinement or encoding, completing in under 10 seconds on a cached scan.

**The pipeline must be runnable as a single command with sensible defaults.** No required flags beyond the input filename for the common case.

**The cache must be self-invalidating.** Changing the OCR engine, preprocessing filter chain, or sample interval automatically triggers a new scan on the next run. Manual cache deletion must not be required.
