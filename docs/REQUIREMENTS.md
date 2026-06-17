# Requirements

Expressed goals and constraints for the VHS home movie clipper pipeline. Derived from design decisions, explicit user statements, and implicit preferences observed during development.

---

## Fundamental assumptions

**The source tape is chronological.** Footage is recorded sequentially onto the tape; the pipeline reads it forward and splits it forward. The pipeline must never reorder clips, stitch non-adjacent segments together, or merge a later tape segment with an earlier one because their OCR-derived dates happen to match. If the camera's clock was misconfigured and two separate recording sessions both show January 6, they are two clips — not one. Date labels inform clip naming only; they do not drive clip merging or reordering. The video timeline is the ground truth; OCR timestamps are metadata.

---

## Output correctness

**The same calendar date must not appear in more than one clip.** If footage from January 6 exists, all of it goes in one clip. A clip may span multiple dates (e.g., a short recording on Jan 4 followed by Jan 5 footage with no significant gap can share a clip), but no date may be split across clips.

**Clip boundaries must correspond to real pauses in recording.** A split should represent the camera being stopped and restarted — not an artifact of OCR noise, tape degradation, or algorithmic error.

**Output filenames must accurately reflect the date of the footage.** If the clip contains Jan 6 footage, the filename should say `1990-01-06`, not `1990-01-16` due to a misread.

**Boundary timing must be accurate to the actual stop/start frame *where OCR is recoverable*.** For boundaries with readable timestamps on both sides, refinement resolves to 1-second resolution and uses the last frame of the outgoing session as the cut point, not the first frame of the incoming one.

**At a Splice Dead Zone the boundary is an Ambiguity Window, not a recoverable frame.** Head-switch noise blanks OCR and saturates every visual detector across a ~15s burst, so frame-accurate placement is unsatisfiable there. The cut follows an end-of-noise-burst policy: the noise burst stays with the outgoing clip's tail (objective: zero wrong-date footage in the incoming clip). The cut anchors to the last visual event within the all-`None` span, falling back to the end of that span when no visual event exists. See `docs/adr/0001-splice-boundary-placement-policy.md`.

**Detection and Placement are separate, separately-measured concerns.** Detection (does a boundary exist?) is scored by the golden set's y/n verdicts (F1=0.920). Placement (how many seconds the cut lands from the true session change) requires its own metric — a human-labeled true-change second on Splice Dead Zone boundaries. No boundary-placement change may be merged without a measured placement error; a high Detection F1 says nothing about Placement.

---

## Robustness

**The pipeline must be robust to low OCR success rates.** On VHS source material, OCR reads may succeed on fewer than half of sampled frames. The pipeline must produce correct output even when large spans have no readable timestamps.

**The pipeline must tolerate consecutive OCR misreads without creating phantom clip boundaries.** A run of 2–3 frames reading a wrong date should be filtered out, not treated as a real date change.

**Gap thresholds must be empirically tuned, not guessed.** The `--gap` default (currently 3600s camera-time) was derived from a 215-boundary golden label set with measured F1=0.920. Any change to the default requires evidence from labeled data.

**Re-tuning must not require re-scanning.** Changing `--gap`, `--mode`, or `--min-clip` should hit the OCR cache and return results in seconds. Only changes to the preprocessing filter chain or OCR engine require a new scan.

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

**The primary boundary signal is OCR timestamps.** Other signals (scene score, silence, freeze) have been evaluated and rejected as standalone proposers; see `docs/SPEC_rejected_signals.md`.

**Visual corroboration is opt-in, not default.** The existing `detect_visual_boundaries` / `fuse_boundaries` path may be used to corroborate OCR-detected boundaries but must not propose new ones independently.

**VHS static/noise in the frame before a boundary is a reliable real-splice indicator** and should be usable as a positive signal if a second-signal path is added.

---

## Source material constraints

**Camera clock runs approximately 2× real time** on the specific camcorder used. All camera-time thresholds (e.g., `--gap 3600`) are in camera-seconds, not wall-clock seconds.

**Timestamp format is `M/ D/YY` (date) and `H:MM AM/PM` (time)**, with a space before single-digit months and days. OCR implementations must account for the space-before-digit ambiguity this creates (e.g., `/ 6` misread as `16`).

**Source video is 640×480 VHS digitized footage.** The default crop (`250:110:385:370`) targets the bottom-right timestamp overlay on this format. Other tapes may require `--crop` adjustment.

---

## Operability

**A dry-run mode must be fast.** `--dry-run` should show the proposed clip list without performing refinement or encoding, completing in under 10 seconds on a cached scan.

**The pipeline must be runnable as a single command with sensible defaults.** No required flags beyond the input filename for the common case.

**The cache must be self-invalidating.** Changing the OCR engine, preprocessing filter chain, or sample interval automatically triggers a new scan on the next run. Manual cache deletion must not be required.
