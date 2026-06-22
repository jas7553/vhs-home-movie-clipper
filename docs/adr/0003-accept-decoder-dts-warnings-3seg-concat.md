# Accept decoder-layer DTS warnings in 3-segment concat output

## Status

accepted

## Context

Clips whose `exact_start` and `exact_end` are both present and both at least `MIN_BOUNDARY_SEG` from their respective keyframes go through a 3-segment concat path: a re-encoded lead + a stream-copied body + a re-encoded trail. These clips emit non-monotonic DTS warnings from the H.264 decoder (`ffmpeg -v warning -f null -`), concentrated in the first ~0.4s of the clip.

Issue-017 fixed the originally-reported cases (clip16, clip71, clip09) by adding `+igndts` to the copy and concat steps. Issue-020 investigated the residual: ~110 warnings across 15 clips, all on the 3-seg path.

**Root cause:** The stream-copied body inherits VFR source PTS values — some frames are very close together or have near-duplicate PTS from the original analog capture. The concat step uses `+igndts` to recompute DTS from PTS across the seam, but this propagates those irregular VFR PTS values into the DTS sequence. The H.264 decoder sees equal or near-equal DTS values in the first few frames of the body and emits decoder-layer warnings. The container-level DTS remains strictly increasing — `ffprobe -show_entries packet=dts_time` finds zero backward or equal DTS events.

**Options considered:**

- **Re-encode first GOP of body (A):** moves the encode→copy seam inside the body but does not eliminate it; the body_rest stream-copy carries the same VFR PTS irregularity. Likely ineffective, adds code complexity.
- **`-vsync passthrough` on concat (B):** muxer flags, does not reach the decoder. Essentially already tried via `+igndts`. No effect.
- **Accept as benign (C):** container DTS clean, playback unaffected, no lossy encoding increase. Requires documentation.
- **Full re-encode on 3-seg (D):** definitively fixes warnings but re-encodes the entire body (hundreds of seconds), defeating the pipeline's core design goal of stream-copying all non-boundary content.

## Decision

Accept the decoder-layer warnings as benign. Do not change the encoding strategy.

**Why they are benign:**
1. **Container DTS is clean.** `ffprobe` finds 0 non-monotonic events. Media players (QuickTime, VLC, mpv) use container DTS for seeking and AV sync — not decoder DTS. No playback artifact.
2. **Not regressions from the source.** The 76-clip source baseline has 0 such warnings. The warnings are introduced at the concat seam, but they only affect the decoder's internal state during the first few frames of the body, not the output file's decodability.
3. **Fixing requires lossy re-encode of the body.** Option D would re-encode hundreds of seconds of content that the pipeline was specifically designed to stream-copy. The quality loss is imperceptible on VHS source at CRF 18, but the design principle (minimize re-encoding) matters more than eliminating a decoder-layer artifact.

## Consequences

- `ffmpeg -v warning -f null -` run on output clips from the 3-seg path will show ~3–23 "non monotonic" warnings per clip, clustered in the first 0.4s. This is expected and documented; do not investigate as a bug.
- `ffprobe -show_entries packet=dts_time` must continue to show strictly increasing container DTS (currently passing; must not regress).
- Option D (full re-encode on 3-seg) remains available if the design priority shifts — e.g., if output clips need to survive aggressive downstream ffmpeg processing without any warnings.
