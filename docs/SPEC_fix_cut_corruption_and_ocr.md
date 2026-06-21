# SPEC: Fix clip corruption (timebase/stream) + improve OCR

**Status:** ready to implement
**Audience:** fresh AI agent, no prior context
**File under change:** `split_homevideo.py` (and optionally `ocr_timestamp.swift`)
**Test asset:** `Converse 1990.mp4` (4.3 GB, present in repo root). OCR cache `Converse 1990_ocr_cache.json` present — reuse it to avoid re-scanning.

---

## Background / how this tool works

`split_homevideo.py` splits one long VHS-capture `.mp4` into per-session clips by reading the
burned-in camcorder timestamp via Apple Vision OCR (`ocr_timestamp` Swift binary). Pipeline stages:
scan → filter OCR outliers → detect splits (camera-time jumps) → refine boundaries at 1s → cut.
See `CLAUDE.md` and the two existing specs in `docs/` for design intent.

Goals the tool MUST honor (in priority order):

1. **No lossy behavior.** Stream-copy the bulk of each clip; only re-encode the tiny boundary
   segments (~3–6s) where a cut falls between keyframes. CRF 18.
2. **Chronological / sequential.** Cuts follow byte/time position in the source (camcorder is
   chronological). Timestamps are noisy and used only for split detection + labels, never for ordering.
3. **Near-perfect clip boundaries.** Frame-accurate split points.

---

## Problem statement (verified, not theoretical)

A full run produced 133 clips. Probed every one with ffprobe:

- **Only clip01 plays correctly (29.97 fps). 128 clips have a corrupted frame rate; 4 are audio-only.**
- Corrupted rates are all over the map: `60000/1001` (59.94, 2× fast), `2997/250` (12 fps, slow-mo),
  `2997/10` (299 fps), `120/1`, etc. Example: clip15 has 2697 video frames (≈90 s of real content)
  stretched to 225 s → slow motion; its audio is the correct 90 s.

### Root cause A — timebase corruption in concat (causes the wrong frame rates)

`cut_clip_with_boundary_encode()` builds a clip from up to 3 segments
(leading re-encode, body stream-copy, trailing re-encode) and joins them with the **ffmpeg concat
demuxer + `-c copy`**:

- Body segment: stream-copied, inherits source video timebase `1/29970`.
- Boundary segments: `libx264` re-encode, get libx264's default timebase.
- The concat demuxer with `-c copy` does **not** reconcile differing timebases. PTS deltas get
  reinterpreted under one arbitrary timescale → garbage frame durations → bogus container frame rate
  → slow/fast playback.
- clip01 is the only correct clip precisely because its trailing keyframe landed exactly on
  `exact_end`, so it had no trail segment, no concat, just a single stream-copy. This confirms the
  concat path is the fault.

### Root cause B — third stream + no `-map` (causes the audio-only clips)

Source has **3 streams**: `h264` (v:0), `aac` (a:0), and **`mjpeg` with `attached_pic=1`** (cover-art
thumbnail). No `-map` is used anywhere, and `-c:v libx264` targets all video streams. ffmpeg
auto-selects streams **per segment**, inconsistently; when segment stream layouts disagree the concat
demuxer drops the real video track → audio-only output (4 clips).

### Root cause C — labels read from unfiltered samples (cosmetic, wrong filenames)

`label_for()` (both the copy in `main()` and in `split_video()`) iterates raw `samples`, so OCR
misreads that the outlier filter would have rejected leak into filenames. Observed bad labels:
clip02=`1990-12-04`, clip04=`05:24`, clip113=`06:00` — implausible given chronological neighbors.
Cut positions are fine; only the date/time in the filename is wrong.

---

## Required changes

### Change 1 — pin streams (fixes audio-only)

In **every** ffmpeg invocation that reads the source and writes a segment or clip
(`_ffmpeg_copy_seg`, `_ffmpeg_encode_seg`, and the fallback single-copy path), map exactly the real
video + audio and drop the attached pic:

```
-map 0:v:0 -map 0:a:0
```

Do **not** map `0:v` (would grab the mjpeg). Apply to the leading/body/trailing seg builders.

### Change 2 — force one timescale everywhere (fixes wrong frame rates)

Make all segments and the final concat output share a single video timescale so the concat demuxer
cannot mis-scale PTS. Source video timebase is `1/29970`. Add to every segment-writing command **and**
the final concat command:

```
-video_track_timescale 90000
```

(90000 is a safe common MPEG timescale and an exact multiple-friendly base; 29970 also acceptable as
long as it is identical on all segments and the concat output. Pick one constant, define it once.)

Also add `-fflags +genpts` (or `-avoid_negative_ts make_zero`, already present on segs) to the final
concat command to regenerate clean PTS.

If, after Changes 1+2, copy-based concat still yields a wrong `r_frame_rate` on any clip, fall back to
the **concat filter with a single re-encode** of the joined boundary region only (still keeping the
large body as a stream copy is not possible with the concat filter — so prefer keeping the demuxer
path working; only switch the whole clip to re-encode as a last resort, and note the goal-1 tradeoff).

### Change 3 — label from filtered readings (fixes wrong filenames)

Build the label lookup from `filter_ocr_outliers(samples)` output, not raw `samples`. Factor the
single `label_for` used by both `main()` and `split_video()` so there is one implementation fed the
filtered list. Keep the `YYYY-MM-DD_HHMM` format and the `NNNNNs` fallback.

---

## Verification (must run before declaring done)

Source `.mp4` is present; OCR cache exists, so a re-run is cheap (no re-scan). Re-cut, then probe.

1. Re-run: `python3 split_homevideo.py "Converse 1990.mp4"` (uses cached scan).
2. Probe every clip's video frame rate and stream layout. All clips must report
   `r_frame_rate=2997/100` (29.97) and contain both a video and an audio stream:

```bash
cd "Converse 1990_clips"
bad=0
for f in *.mp4; do
  fr=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$f")
  [ "$fr" = "2997/100" ] || { echo "BAD fps=$fr: $f"; bad=$((bad+1)); }
done
echo "bad=$bad (must be 0)"
```

3. Spot-check the two originally-broken clips play at real speed with synced audio:
   - `Converse 1990_clip10_*` (was audio-only) — must now have video.
   - `Converse 1990_clip15_*` (was 12 fps slow-mo) — video duration must ≈ audio duration.
4. Confirm clip count unchanged (~133) and no clip is 0 bytes.
5. Sanity-check a couple of boundaries are still frame-accurate (Goal 3 not regressed): open a clip,
   confirm the timestamp overlay at the very start matches the filename's session, not the previous one.

**Acceptance:** `bad=0`, zero audio-only clips, clip15-type duration mismatch gone.

---

## Out of scope for this task (do NOT bust the OCR cache here)

The cut fix above reuses the cached scan. The OCR-quality work below requires a **full re-scan**
(deletes/invalidates `Converse 1990_ocr_cache.json`) and ~minutes of compute — do it as a **separate
follow-up**, not mixed into the cut fix.

### Follow-up SPEC — raise OCR rate (currently 960/2128 = 44%) ✓ IMPLEMENTED

Cause: VHS overlay is interlaced + low-contrast + spatially wobbly; `fps=1/N` samples a combed frame
and Vision chokes on comb artifacts + small glyphs. The current `-vf` is just `crop=...` with no
preprocessing. Add a preprocessing chain in `extract_all_frames()` and `extract_frame()` (keep both in
sync). Impact order:

1. **Deinterlace** before crop: prepend `yadif`. Biggest single win.
2. **Upscale the crop ~3–4×**: `scale=iw*3:ih*3:flags=lanczos`. Vision wants larger text.
3. **Contrast**: `eq=contrast=1.5` (or a curves threshold) to harden white-on-dark digits.

Proposed chain: `yadif,crop=<W:H:X:Y>,scale=iw*3:ih*3:flags=lanczos,eq=contrast=1.5`

Swift side (`ocr_timestamp.swift`) is already near-optimal (`.accurate`, `usesLanguageCorrection=false`).
After upscaling, optionally set `request.minimumTextHeight` to skip tiny noise.

Optional robustness: sample 3 frames per interval and take a consensus timestamp (kills wobble misreads).

Note the cache key in `scan()` is `(interval, crop)`. If the filter chain changes but the crop string
stays the same, the stale cache will be wrongly reused — either add the filter chain to the cache key
or delete the cache file when changing preprocessing. **Handle this**, or verification will silently
test stale data.

Verify OCR follow-up by the printed `OCR success: X/Y frames` line rising well above 44% (target 70%+),
and by the date range / split count staying sane.

### OCR improvement postmortem

> **Superseded (architecture later reversed).** This postmortem credits the
> preprocessing filter chain as the primary-path win. That was overturned: once
> crop-only scanning was measured *with* 3-frame majority voting, preprocessing was
> found to **lower** yield (crop-only ~67% vs preprocessed ~45% per frame) — it blows
> out the timestamp on brighter/lower-contrast frames. The chain is now **fallback-only**
> (`_VF_PREPROCESS`), applied only to windows crop-only could not read (commit
> `7c523eb`). Current per-window yield is ~86% (after date-only acceptance too). See
> CLAUDE.md and the `_VF_PREPROCESS` comment in `split_homevideo.py`.

- **Result: 1393/2128 = 65.5%** (was 960/2128 = 44%). +21.5pp, 49% relative improvement.
- All three filter-chain improvements were implemented (`yadif` + `scale=iw*3` + `eq=contrast=1.5`).
- The "optional" 3-frames-per-interval consensus voting was also implemented; frames are grouped by
  interval window and majority-voted via `Counter.most_common(1)`.
- Cache key now includes `vf_preprocess` and `frames_per_sample` — stale caches auto-invalidate.
- **Did not reach the 70% target.** The remaining ~4.5pp gap is likely from frames where the
  timestamp overlay is partially off-screen, heavily ghosted, or fully absent. Further gains would
  require `minimumTextHeight` tuning on the Swift side or sampling more than 3 frames per window.
- **Side effect: 160 clips detected vs 133 before.** Better OCR reveals split points that were
  previously invisible due to missing readings around real camera pauses.

---

## Postmortem (added after implementation — corrects the diagnosis above)

What actually happened when the changes landed and were verified:

- **Change 2 (timebase pin) was the real fix.** It eliminated the frame-rate
  corruption / slow- and fast-motion across all clips (was 128/133 broken → 0).
- **Change 1 (`-map 0:v:0 -map 0:a:0`) did NOT fix the audio-only clips.** Root cause B
  above — "attached-pic mjpeg confuses stream selection" — was **wrong**. The `-map` is
  good hygiene (correctly drops the cover-art stream) but was irrelevant to the bug. The
  same 4 clips stayed audio-only after it.
- **Real cause of audio-only clips: zero-frame boundary segments.** When `exact_start`
  lands less than one frame (~0.033s at 29.97fps) before the next keyframe, the leading
  re-encode covers a sub-frame window and libx264 emits **zero video frames** — producing
  a segment with only an audio track. That degenerate segment is item #1 in the concat
  list, so the concat demuxer adopts its (audio-only) layout for the whole clip, and the
  stacked audio across segments roughly doubled the clip's duration.
- **Fix: `MIN_BOUNDARY_SEG` guard.** Skip any boundary re-encode shorter than ~1 frame and
  snap the body's stream-copy to the keyframe instead. Cost: up to <1 frame dropped at a
  boundary (acceptable under Goal 3). After this, `audio_only=0`, max A/V skew 0.53s.
- **Operational gotcha found: re-runs orphan renamed clips.** Because labels come from OCR,
  a re-run can rename clips, so new files don't overwrite old ones — the output dir
  accumulated 166 files across two runs. Fix: `split_video()` now deletes this stem's prior
  `*_clip*.mp4` before cutting.
- **Verification is mandatory, not optional.** The first implementation pass edited the code
  and stopped without re-running; the broken clips sat on disk untouched. Always execute the
  re-cut + probe loop below and report the numbers.

## Notes for the implementer

- Do not change split-detection or refinement logic; they are sound. Bug is in the cut + labeling.
- Keep Goal 1 (lossless) intact: body stays a stream copy; only boundary segs re-encode.
- `.gitignore` excludes the big `.mp4` and the `_clips/` output; commit only code/doc changes, and
  only when the user asks.
- Caveman comms mode is active in this session's chat; **code, commits, and this doc stay normal prose.**
