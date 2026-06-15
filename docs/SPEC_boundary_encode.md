# Spec: Frame-Accurate Splits with Boundary Re-encode

## Problem

`snap_to_keyframe` snaps split points backward to the nearest keyframe (required for
H.264 stream copy). This causes ~3s of old-session content to appear at the **head**
of the next clip. Snapping forward instead shifts the bleed to the tail of the prior
clip — same problem, different direction. Stream copy cannot produce a pixel-accurate
split at a non-keyframe boundary.

## Solution

Re-encode only the small boundary segments (~3–6s per split) and stream copy everything
else. The vast majority of footage remains lossless; boundary frames undergo one
generation of H.264 re-encode at CRF 18 (visually lossless in practice).

## How It Works (per split boundary)

Given `exact_t` from `refine_split` (no keyframe snapping applied):

1. `kf_before` = last keyframe ≤ `exact_t`  (existing `snap_to_keyframe`)
2. `kf_after`  = first keyframe ≥ `exact_t`  (new `snap_to_keyframe_forward`)

**Clip A** (ending at split):
- A1: `[clip_start → kf_before]` — stream copy
- A2: `[kf_before → exact_t]`   — re-encode CRF 18
- Concat A1 + A2 → Clip A output

**Clip B** (starting at split):
- B1: `[exact_t → kf_after]`    — re-encode CRF 18
- B2: `[kf_after → clip_end]`   — stream copy
- Concat B1 + B2 → Clip B output

Edge cases:
- `exact_t == kf_before`: skip A2 (split lands exactly on a keyframe)
- `exact_t == kf_after`:  skip B1 (same)
- First clip: no `exact_start`, skip B1 logic
- Last clip:  no `exact_end`, skip A2 logic
- `kf_before == kf_after` (exact_t between two distant keyframes): both A2 and B1
  encode from the same input range — no overlap in output

## Changes to `split_homevideo.py`

### Remove
- The `snap_to_keyframe` call loop in `main()` — split points are no longer snapped
  before being passed to the cutting stage.

### Add: `snap_to_keyframe_forward(video, t, look_ahead=30.0) -> float`
Mirrors `snap_to_keyframe` but returns the **first** keyframe `≥ t`.

```python
def snap_to_keyframe_forward(video: str, t: float, look_ahead: float = 30.0) -> float:
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", f"{t:.3f}%+{look_ahead:.0f}",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv",
        video,
    ], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 3 and "K" in parts[-1]:
            try:
                pts = float(parts[1])
                if pts >= t:
                    return pts
            except ValueError:
                pass
    return t  # fallback: no keyframe found, use exact_t
```

### Add: `cut_clip_with_boundary_encode(video, start, end, exact_start, exact_end, out_path, crf=18)`

- `start / end`: full clip extent (seconds)
- `exact_start`: if not None, leading boundary of this clip needs re-encode;
  `kf_after = snap_to_keyframe_forward(video, exact_start)`
- `exact_end`: if not None, trailing boundary needs re-encode;
  `kf_before = snap_to_keyframe(video, exact_end)`
- Writes intermediate segments to a `tempfile.TemporaryDirectory`
- Joins via ffmpeg concat demuxer

### Modify: `split_video()`
Call `cut_clip_with_boundary_encode` instead of raw ffmpeg stream copy.
Pass `exact_start` / `exact_end` per clip derived from the refined split list.

### Modify: `main()`
- Remove keyframe-snap loop
- Track per-split `exact_t` values
- Pass them into `split_video` so each split point is the `exact_end` of clip N
  and the `exact_start` of clip N+1

## ffmpeg Commands

**Stream copy segment:**
```bash
ffmpeg -ss <start> -i input.mp4 -t <duration> \
  -c copy -avoid_negative_ts make_zero -y seg.mp4
```

**Re-encode boundary segment:**
```bash
ffmpeg -ss <start> -i input.mp4 -t <duration> \
  -c:v libx264 -crf 18 -preset fast \
  -c:a copy \
  -avoid_negative_ts make_zero -y seg.mp4
```

**Concat:**
```bash
# concat_list.txt:
# file '/tmp/.../seg_a1.mp4'
# file '/tmp/.../seg_a2.mp4'
ffmpeg -f concat -safe 0 -i concat_list.txt -c copy -y clip_out.mp4
```

Audio (`-c:a copy`) is always stream-copied — audio keyframes don't cause the same
alignment problem as video keyframes.

## What Doesn't Change

- `scan()`, `filter_ocr_outliers()`, `find_splits()`, `refine_split()` — untouched
- Cache format — untouched
- Output filename convention — untouched
- `--dry-run` output — add a line per split showing the boundary re-encode range
  (e.g. `boundary re-encode: 1000.0s–1003.4s | 1003.4s–1007.1s`)
