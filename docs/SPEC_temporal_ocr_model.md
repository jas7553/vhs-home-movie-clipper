# Temporal linear model for OCR gap-filling and outlier rejection

## Status

proposed — not yet implemented or benchmarked

> **Baseline correction (2026-06-21):** the OCR-rate figures below (`~44%`) predate
> two changes that already raised yield: crop-only-primary scanning and date-only
> timestamp acceptance. Current per-window yield on Converse 1990 is **~86%**
> (1824/2128). This weakens — but does not eliminate — the gap-filling motivation;
> the residual-outlier-rejection idea remains useful. Also note `drop_date_islands`
> has since **replaced** `_collapse_revert_phantoms` (deleted); references to the
> latter below are historical — integrate any residual filter with
> `drop_date_islands` instead.

## Problem

OCR succeeds on ~44% of sampled frames (pre-2026-06-21 figure; see baseline
correction above). The remaining frames return `None`. The current pipeline handles
this via:

1. **Majority vote** (3 frames per 10s interval): local smoothing within a window.
2. **`filter_ocr_outliers`**: drops readings inconsistent with neighbors within a
   `max_run`-step sliding window. Uses pairwise `drift` = `|cam_advance − vid_advance|`.

Both exploit temporal coherence, but only *locally* (within a few steps). Neither fits a
global model of how camera time relates to video time within a continuous segment.

The consequence: a long run of `None` readings in the middle of a continuous segment is
simply skipped. A plausible-but-wrong date that happens to be consistent with its immediate
neighbors (e.g., a single-frame hallucination of `1999-05-19` in a `1990-xx-xx` segment)
can survive the local window check and trigger a phantom boundary.

## Theory

Within a single continuous recording segment (no camera stop/start between two detected
boundaries), camera time increments at a fixed ratio to real (video) time:

```
camera_time(t) = slope × video_time(t) + intercept
```

For this specific camcorder, `slope ≈ 2.0` empirically (CLAUDE.md: "Camera clock runs ~2×
real time"). This ratio may vary slightly per tape or per session, so it should be fit from
data rather than hardcoded.

Given enough good reads within a segment, OLS regression recovers `(slope, intercept)` and
enables two operations not currently available:

### 1. Residual-based outlier rejection

A reading `(video_t, camera_dt)` is suspicious if:

```
residual = |camera_dt.timestamp() − (slope × video_t + intercept)|  (seconds)
```

exceeds some threshold (e.g. 300s = 5 camera-minutes). This catches the "plausible
neighbour, wrong year/month" case that `filter_ocr_outliers` misses when the misread is
isolated and surrounded by `None`.

This is strictly stronger than the current pairwise drift check: it uses the *whole
segment's trend* rather than just adjacent reads.

### 2. Gap-filling: predict camera time where OCR returned None

For intervals where OCR failed entirely, the model predicts the expected camera time:

```
predicted_dt = datetime.fromtimestamp(slope × video_t + intercept)
```

This converts `None` gaps into estimated readings with a known uncertainty band.

Use cases:
- Improve boundary detection confidence inside long `None` runs (currently skipped entirely)
- Supply predicted timestamps to the refinement window so `_resolve_*` has more signal

## Expected gains

The 44% OCR success rate is constrained by two distinct failure modes:

| Failure mode | Fraction of failures | Model helps? |
|---|---|---|
| Head-switch noise / Splice Dead Zone | ~30% | No — physics limit; no readable frame exists |
| Blurry/degraded overlay on otherwise intact footage | ~70% | Yes — model predicts over the gap |

Rough estimate: effective timestamp coverage could rise from 44% to 60–65% within
continuous segments. This does not improve Splice Dead Zone placement (the Ambiguity
Window policy from ADR 0001 still applies there).

The more reliable gain is **outlier rejection quality**: the current `filter_ocr_outliers`
correctly handles isolated misreads but is fooled by misreads surrounded by `None` on both
sides (no neighbor to contradict them). The segment-level residual test catches these.

## Failure modes and limits

**Tape pauses.** If the camera was paused mid-tape, real time elapsed while the clock did
not advance. The slope shifts at the resume point. A single model fit over the whole
segment would be wrong on one side. Mitigation: treat a pause as an implicit segment
boundary (camera clock stalls → `cam_advance` near zero while `vid_advance` is large →
detectable as a slope discontinuity).

**Timestamp resolution is 1 minute.** Adjacent frames (10s apart in video) often read
identical camera times. Slope estimation needs reads spaced far enough apart to measure the
rise. With 1-minute resolution and ~2× rate, you need reads ≳60s of video time apart to
observe a clock increment. Over a 30-minute segment this is fine; over a 2-minute segment
with sparse reads it may not be.

**Slope is ~2× but not exactly 2×.** The ratio is empirically observed, not documented.
It may vary slightly across tapes. Always fit from data; do not hardcode.

**Bootstrap problem.** The model requires a segment to be defined before it can be fit, but
segments are defined by boundaries, which are found from OCR reads. The model assists
outlier rejection *within* a segment, not boundary *detection* between segments. Boundary
detection remains the first pass; the model is a second pass within each detected segment.

**Long Dead Zones.** A Long Dead Zone (≳120s of `None`) may span a boundary (see
CONTEXT.md). Interpolating across one would produce predictions that mix two sessions.
Mitigation: do not extrapolate beyond the `None`-span width limit used for Splice Dead
Zones (120s); fall back to `None` / skip as today.

## Implementation sketch

Pure stdlib — no new dependencies.

```python
from datetime import datetime

def fit_segment_model(
    readings: list[tuple[float, datetime]],
    min_readings: int = 5,
) -> tuple[float, float] | None:
    """OLS fit: camera unix time = slope * video_sec + intercept.
    Returns None if insufficient readings to fit reliably."""
    if len(readings) < min_readings:
        return None
    xs = [v for v, _ in readings]
    ys = [dt.timestamp() for _, dt in readings]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / ss_xx
    intercept = mean_y - slope * mean_x
    return slope, intercept


def model_residual_s(model: tuple[float, float], video_t: float, dt: datetime) -> float:
    slope, intercept = model
    return abs(dt.timestamp() - (slope * video_t + intercept))


def predict_camera_time(model: tuple[float, float], video_t: float) -> datetime:
    slope, intercept = model
    return datetime.fromtimestamp(slope * video_t + intercept)
```

### Integration points

**A. Tighten `filter_ocr_outliers`** (low-risk, additive)

After the existing neighbor-window pass, fit the model on the surviving reads. Re-examine
any read with `residual > RESIDUAL_THRESHOLD_S` (suggested: 300s). This is a second pass,
so it can only drop reads the current filter kept — no false negatives introduced.

**B. Gap-fill for boundary detection** (medium risk)

After `filter_ocr_outliers`, for each `None` interval, if the gap is ≤120s and the model
is available, inject a predicted reading tagged as `(video_t, predicted_dt, source='model')`.
`find_all_boundaries` would treat these as lower-confidence reads (e.g., skip them as
cut points but use them for context).

**C. Gap-fill for refinement** (higher risk, higher reward)

In `_extract_and_ocr_window` (the dense 1s scan), apply the segment model to predict camera
times for frames where OCR returns `None`. This directly improves the `_resolve_*` cut
logic inside Splice Dead Zones — more readings to anchor the last-old-session frame.

Start with A only. Validate on the golden set. Then B. Then C.

## Open questions for investigation

1. **What is the actual slope distribution across tapes?** Run the regression on a long
   scan with many good reads (e.g., Converse 1990.mp4 outside dead zones) and measure
   `slope` per segment. How stable is it? Does it vary across different physical tapes?

2. **What residual threshold catches phantom misreads without dropping real boundaries?**
   A real boundary looks like a large residual (camera time jumps). The threshold must be
   above the expected drift from the ~2× rate variation but below a session-change jump
   (min ~3600s camera seconds = 1800s video). Start at 300s and tune on the golden set.

3. **Does gap-filling change the F1 on the 215-boundary golden set?** Run before/after.
   The risk is that predicted readings in a real `None` span might create phantom
   boundaries — measure this.

4. **How should the model interact with `drop_date_islands`?** (This question originally
   named `_collapse_revert_phantoms`, since deleted.) Date-island removal currently drops
   a single reading whose date differs from both neighbours, before boundary detection. A
   strong residual test upstream might catch misreads it misses (e.g. a 2-frame misread
   run that forms a false "real session"). Check for overlap / redundancy.

## Relationship to existing work

- `filter_ocr_outliers` (`split_homevideo.py`): the model is a global complement to its
  local neighbor check. They are not redundant — both should run.
- `drop_date_islands` (`split_homevideo.py`; replaced the deleted `_collapse_revert_phantoms`):
  runs before boundary detection. A residual filter might catch misread *runs* (≥2 frames)
  that survive island removal. Investigate overlap.
- Vision prototype (removed per ADR 0002): this proposal replaces the "better timestamp
  reading" motivation without LLM dependency — purely algorithmic.
- Splice Dead Zone policy (ADR 0001): the gap-fill idea (option C above) is the one place
  where this work could improve Placement inside Ambiguity Windows, but only if the model
  can predict which side of the boundary each frame belongs to. Treat as speculative until
  benchmarked.
