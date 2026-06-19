# Vision-refine prototype

**Status:** prototype (opt-in). Default pipeline unchanged.

Reads the camera timestamp in the refinement window with a vision model instead of the
OCR binary + scene-cut anchor. Goal: read degraded / transitioning / head-switch-noise
frames that Apple Vision (`ocr_timestamp`) misses, and place splice cuts at the **end of
the noise burst** (see `docs/adr/0001-splice-boundary-placement-policy.md`).

## Why two flavors

Refinement reads each 1-second frame in `[prev_t+1, coarse_t)` and classifies it as a
timestamp / `NOISE` / `NONE`. Shared walker `_resolve_vision_cut()`:

- a **date** that is a new-session jump → cut at `last_old_t + 1`
- **NOISE** → counts as the outgoing clip's tail (advances `last_old_t`), so an all-NOISE
  Splice Dead Zone lands the cut at the end of the burst, no separate visual anchor
- **NONE / unread** → skipped
- nothing classified at all → fall back to `coarse_t` (Long Dead Zone, out of scope)

Two ways to get the classifications:

| Path | Flag | Cost | Notes |
|---|---|---|---|
| **Free (Claude Code)** | `--vision-export` + `--vision-readings` | $0 | Pro/Max subscription reads frames; no API. **Primary path** — project constraint is no API top-up. |
| Paid (API) | `--vision-refine` | ~$1–4/run | Live Claude Haiku (`claude-haiku-4-5`). Needs `anthropic` pkg + API **credit balance** (the subscription does NOT cover the metered API). |

`--vision-readings` takes precedence over `--vision-refine` if both are passed.

## Free path — run it

```bash
# 1. Export refinement-window PNGs + manifest.json (ffmpeg only, ~3 min on Converse 1990).
#    A pre-filter exports only Splice Dead Zone windows (None-span < 120s); Long Dead
#    Zones are skipped automatically (they fall back to coarse_t). Pass --vision-export-all
#    to override. On Converse 1990 this drops 5 boundaries but ~67% of the frames.
python3 split_homevideo.py "Converse 1990.mp4" --vision-export vision_frames

# 2. Have Claude Code read the PNGs and write vision_frames/readings.json
#    (map: PNG filename -> "M/D/YY H:MM AM/PM" | "NOISE" | "NONE").
#    Partial files are fine — unread frames are skipped.

# 3. Apply the readings (no API) and cut:
python3 split_homevideo.py "Converse 1990.mp4" --vision-readings vision_frames/readings.json
```

`manifest.json` lists each large_gap boundary with `coarse_t`, `prev_t`, `prev_dt`, and its
frame files. Frame name is `b<coarse>_t<t>.png` (zero-padded).

### Pre-filter (which boundaries get frames)

`_boundary_needs_vision()` gates the export on the **width of the None-span** (`coarse_t −
prev_t`), the only available signal — the window is all-None by construction, so the OCR
cache can never confirm its interior:

- **Splice Dead Zone** (span < `SPLICE_DEAD_ZONE_MAX_S` = 120s) → vision recovers the
  transition / end-of-noise-burst → **export**.
- **Long Dead Zone** (≥ 120s) → unsolved, refine falls back to `coarse_t` (ADR 0001) → **skip**.
  These windows also hold the bulk of the frames (one was 2179), so skipping them is where
  the token saving comes from. Same 120s threshold the placement anchor uses.

## Paid path — run it

```bash
export ANTHROPIC_API_KEY=...        # or: ant auth login  (needs API credits, not just Pro/Max)
python3 split_homevideo.py "Converse 1990.mp4" --vision-refine --no-visual-anchor
```

Prints per-boundary token count + $ cost.

## Test it (no cut, no API)

Validate the walker against known frames without re-encoding the 4 GB file:

```python
import json
from datetime import datetime
import split_homevideo as s

readings = json.load(open("vision_frames/readings.json"))
t, method = s.refine_split(
    "x", coarse_t=2960.0, prev_t=2940.0,
    prev_dt=datetime.fromisoformat("1990-01-06T18:58:00"),
    gap_s=3600, crop=s.DEFAULT_CROP, tmpdir="/tmp",
    visual_times=None, vision_readings=readings,
)
print(t, method)   # 2960 boundary -> 2954.0 vision  (end of noise burst)
```

## Findings (2026-06-18, Converse 1990.mp4)

Export = 54 boundaries, 4926 frames. Read by Claude Code:

| Boundary | Window | Vision result | Verdict |
|---|---|---|---|
| coarse 2960s | 19 frames, 20s | 8× `1/6/90` → 5× NOISE burst → 6× `1/7/90`; cut **2954s** | **Win** — clean end-of-noise-burst placement |
| coarse 13710s (clip 42) | 9 frames, 10s | all `6/24/90`; cut **13710s (+0s)** | Coarse already correct; vision confirms, no regression |
| coarse 15870s (clip 45) | 520s window | all `7/8/90` (11:28 AM→7:17 PM), no `7/18` | Long Dead Zone is one readable day; new date is ≥ coarse. Confirms, no regression |
| coarse 3500s (clip 10) | 19 frames, 20s | 7× `1/20/90` then 12× `1/24/90`; cut **3481s (−19s)** | Legit — sustained out-of-order `1/20` segment is a real session change, not a misread |

**Headline:** the three handoff "placement-late" clips (10, 42, 45) are **out-of-window** — the
new-session date never occurs inside `[prev_t, coarse_t]`, so no in-window reader (OCR or
vision) can pull the cut earlier. Coarse_t is already at/near the true change. Vision's real
value is (1) end-of-noise-burst placement on genuine Splice Dead Zones, (2) reading Long Dead
Zones the binary can't, confirming they're single-day and shouldn't move.

## Known limitations / next steps

- **Lone-misread guard (done):** `_resolve_vision_cut` honors a session jump (forward or
  backward) only when the next classified date frame also jumps, so a single out-of-order /
  misread date no longer triggers a cut. A *sustained* out-of-order run (e.g. clip 10's 7
  consecutive `1/20` frames) is still honored — that's a real session change, not a misread.
- **Reading thousands of frames through Claude Code is context-bound.** The `--vision-export`
  pre-filter now drops Long Dead Zone windows automatically (~67% of frames on Converse 1990);
  the remaining Splice Dead Zone frames are still ~1600 — read only the boundaries under audit.
- **Placement-late is not a refinement problem.** Pulling those cuts earlier needs a *wider*
  window or forward reading past `coarse_t` — out of current scope.
- **Full pipeline audit run (2026-06-19):** 52 clips produced, no duplicate dates, chronological order, no misread dates in filenames. The pre-fix "8-FAIL baseline" is superseded — phantom collapse + refine fixes removed all known phantom clips. Pipeline placement error: **17.1s median** (28/40 SDZ boundaries, coarse baseline 22.5s).
