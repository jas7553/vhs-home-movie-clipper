# Plan: Fix Wrong-Date Footage at OCR Dead Zone Boundaries

## Context

Pipeline: `split_homevideo.py` + `ocr_timestamp` binary. Splits a VHS digitization into per-day clips using camera timestamp OCR. Default mode is `daily` — one clip per calendar date. Full architecture in CLAUDE.md.

This plan was authored from a live debugging session. The user ran the pipeline on `Converse 1990.mp4` and found two cut placement errors.

---

## Observed Symptoms

**Clip 02 (`1990-01-04`, 24s → 480s)**  
At 6′55″ into the clip (video position ~439s), footage timestamped `1/5/90` is visible. That footage should belong to clip 03 (`1990-01-05`). The correct cut point is ~439s, but the pipeline placed it at 480s — 41 seconds late.

**Clip 04 (`1990-01-06`, 994s → 2960s)**  
Last 7 seconds (2953s–2960s) contain footage timestamped `1/7/90`. Should start clip 05 (`1990-01-07`). Pipeline placed cut at 2960s — 7 seconds late.

---

## Root Cause: OCR Dead Zones at Tape Boundaries

VHS head-switching produces analog noise at tape section joins. During these transitions the camera's timestamp overlay is unreadable — OCR returns `None` for every frame in the zone.

Confirmed from cache (`Converse 1990_ocr_cache.json`):

```
# 480s boundary
  410s: '6:56 PM 1/ 4/90'   ← last valid 1/4 read  (prev_t)
  420s: None
  430s: None                  ← 60s dead zone
  440s: None                    all coarse frames fail
  450s: None                    (3 frames/10s × 6 windows
  460s: None                     = 18 frames, all None)
  470s: None
  480s: '11:44 AM 1/ 5/90'  ← first valid 1/5 read (coarse_t)

# 2960s boundary
  2940s: '6:58 PM 1/ 6/90'  ← last valid 1/6 read  (prev_t)
  2950s: None                ← 20s dead zone
  2960s: '8:09 AM 1/ 7/90'  ← first valid 1/7 read (coarse_t)
```

### Why refinement doesn't help

`refine_split()` does a dense 1-frame/s OCR scan over `[prev_t+1, coarse_t)` looking for the first new-date frame, intending to move the cut earlier. But the same physical noise that kills the coarse scan (3-frame majority vote) also kills the dense scan (single frames). Zero valid reads → function returns `coarse_t` unchanged.

Both boundaries reported `saved 0s` in the run output, confirming this.

### Why coarse_t is always late

`coarse_t` is the first interval where OCR *succeeds* showing the new date. Because OCR fails during the transition, this first success necessarily comes *after* the actual date change. The true boundary sits somewhere in `[prev_t, coarse_t]` but is invisible to OCR.

---

## Existing Infrastructure That Could Solve This

`detect_visual_boundaries()` already exists in `split_homevideo.py`. It runs an ffmpeg decode pass using `showinfo` (scene cuts) and `blackdetect` (black frames). VHS head-switching noise typically produces a visible noise bar or transient black frame — exactly the event that creates OCR dead zones. This is the signal that can anchor the cut within the blind spot.

`fuse_boundaries()` already exists to cross-reference OCR boundaries with visual signals.

Both are gated behind `--enable-visual-fusion` (off by default). The flag was not used in the failing run.

---

## Proposed Path Forward

### Option A — Enable visual fusion by default (recommended)

Change `--enable-visual-fusion` default from `False` to `True`. Add a `--no-visual-fusion` escape hatch.

**Tradeoff**: Adds one extra ffmpeg decode pass (~same wall time as scanning, since it's I/O bound and OCR dominates). Caches results to `<stem>_visual_cache.json` so re-runs with different `--gap` are free.

**Expected fix**: For both problem boundaries, a black frame or scene cut near 439s / 2953s would corroborate the OCR boundary and allow `fuse_boundaries` to anchor the cut to the visual event rather than the first successful OCR read.

**Verification**: Re-run with `--enable-visual-fusion` on `Converse 1990.mp4` (cache hit for OCR, new pass for visual). Check that clip 02 no longer contains 1/5 footage and clip 04 no longer ends with 1/7 footage.

### Option B — Use visual signal inside refine_split

Instead of only using visual boundaries as a filter (dropping unconfirmed OCR boundaries), use them as a positive anchor: if `refine_split` returns `coarse_t` (meaning the dense scan found nothing), check whether a black frame or scene cut falls within `[prev_t, coarse_t]` and use that as the refined cut point.

**Tradeoff**: More surgical than Option A. Does not require visual fusion to be always-on. But requires threading visual boundary data into `refine_split`, which currently has no knowledge of them.

### Option C — Midpoint heuristic (weak fallback)

When dense refine finds nothing, cut at `(prev_t + coarse_t) / 2` instead of `coarse_t`. No additional ffmpeg pass. But arbitrary — sometimes better, sometimes worse. Not recommended unless visual fusion is too slow on very long files.

### Option D — Do nothing

Both errors are a direct consequence of unreadable OCR in the transition zone. The pipeline behaves correctly given available data. Accept that VHS head-switching creates ≤60s ambiguity windows at tape joins.

---

## Recommended Implementation (Option A)

1. In `split_homevideo.py`, change the `--enable-visual-fusion` default:
   ```python
   ap.add_argument("--enable-visual-fusion", action="store_true", default=True, ...)
   ap.add_argument("--no-visual-fusion", dest="enable_visual_fusion", action="store_false")
   ```
2. Verify visual cache is populated and reused across re-runs (already implemented).
3. Run on `Converse 1990.mp4` and confirm clip 02 / clip 04 boundaries improve.
4. Run against the 215-boundary golden set to check F1 doesn't regress (benchmark harness unknown — ask user if one exists).

---

## Key Files

| File | Role |
|------|------|
| `split_homevideo.py` | Main pipeline — all logic here |
| `ocr_timestamp.swift` | Apple Vision OCR binary (compiled to `ocr_timestamp`) |
| `Converse 1990_ocr_cache.json` | Cached coarse OCR results (do not delete) |
| `CLAUDE.md` | Architecture overview and domain facts |
| `docs/REQUIREMENTS.md` | Pipeline goals and constraints |

## Domain Facts Relevant to This Work

- Camera clock runs ~2× real time. `gap_s=3600` is the validated threshold.
- OCR success rate ~86% per-window (was ~44% when this plan was written, before
  crop-only-primary scanning and date-only acceptance); still lower in transition zones.
- Timestamp format: `M/ D/YY` bottom line, `H:MM AM/PM` top line (the time line may be
  absent — date-only reads are accepted and fall back to midnight).
- Default crop `250:110:385:370` for 640×480 source.
- Visual cache key: scene_threshold + black_min_duration (see `detect_visual_boundaries`).
