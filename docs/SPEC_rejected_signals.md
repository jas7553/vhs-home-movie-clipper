# Rejected boundary-detection signals

Log of candidate signals evaluated for detecting session boundaries (camera
paused/stopped) beyond the existing OCR-timestamp-jump + scdet visual-fusion
approach. Recorded so these aren't re-investigated from scratch.

## ffmpeg `silencedetect`

Audio dropout/pop at REC start/stop. **Rejected.** Fires ~459 times in 15
minutes on ordinary home-movie audio (natural speech pauses) — not
discriminative, near-zero precision relative to real boundaries.

## ffmpeg `freezedetect`

Frozen-frame run, theory being camera pauses on a static frame before
stopping. **Rejected.** No threshold tested (n=-30dB/d=0.5s through
n=-15dB/d=1.0) cleanly separates real pause artifacts from ordinary
low-motion shots or VHS noise; loosened thresholds catch slow pans lasting
50-212s, not stop/start events.

## `astats` RMS/loudness discontinuity

Deprioritized without a dedicated test — derived from the same audio
characteristics as `silencedetect`, which was already non-discriminative on
this material.

## OCR-dropout-rate as boundary signal

Theory: a span of failed OCR reads (noise obscuring the burned-in overlay)
correlates with a real tape splice. Deprioritized, not run empirically —
lower priority than the scene-score result below.

## ffmpeg full-frame `scene_score` (histogram delta, reusing scan() frames)

Tested on `test_15min.mp4` against the golden boundary table
(`docs/SPEC_two_layer_clip_detection.md`), with a small prototype script
(`prototype_scene_score.py`, deleted after evaluation — see git history if
needed) computing `lavfi.scene_score` per frame via
`-vf "select='gte(scene,0)',metadata=print"` and sweeping a threshold.

| threshold | candidates | golden hits | misses | false positives |
|---|---|---|---|---|
| 0.05 | 36 | 7/7 | none | 30 |
| 0.10 | 33 | 7/7 | none | 27 |
| 0.15 | 31 | 7/7 | none | 25 |
| 0.20 | 24 | 5/7 | 140, 560 | 20 |
| 0.30 | 17 | 4/7 | 24, 140, 560 | 14 |

**Rejected as a standalone/independent boundary proposer.** No fixed
threshold gives full recall without 4x+ false positives. Known b&w
artifact/noise regions score *higher* (0.3-0.77) than real cuts
(0.17-0.62) — the signal conflates "real session cut" with "tape
noise/static," not separable by threshold alone.

Cost was cheap (~3s for 15min of footage, ~290x realtime decode), so this
isn't rejected on performance grounds — purely on precision/recall.

Possible residual value as a second corroborating signal (alongside the
existing scdet-based `detect_visual_boundaries`/`fuse_boundaries`), but
unvalidated whether it catches anything scdet already doesn't — would need
a direct diff of the two signals' hit sets before pursuing further. Not
prioritized.

## Not yet evaluated

- Reusing scan() frames for perceptual-hash diff (distinct from scene_score —
  untested separately).
- Apple Vision `VNGenerateImageFeaturePrintRequest` similarity embedding.
- Per-character OCR confidence as a soft signal in `filter_ocr_outliers`
  (currently binary keep/drop).
