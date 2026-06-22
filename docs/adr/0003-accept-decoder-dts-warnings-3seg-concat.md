# Accept decoder-layer DTS warnings in long-body stream-copy output

## Status

accepted

## Context

Clips whose `exact_start` and `exact_end` are both present and both at least `MIN_BOUNDARY_SEG` from their respective keyframes go through a 3-segment concat path: a re-encoded lead + a stream-copied body + a re-encoded trail. These clips, and any other clip with a long stream-copied body, can emit non-monotonic DTS warnings from the H.264 decoder (`ffmpeg -v warning -f null -`).

Issue-017 fixed the originally-reported cases (clip16, clip71, clip09) by adding `+igndts` to the copy and concat steps. Issue-020 investigated the residual and found warnings concentrated in a small number of clips with long stream-copied bodies.

**Root cause:** The stream-copied body inherits VFR source PTS values — some frames are very close together or have near-duplicate PTS from the original analog capture. The concat step uses `+igndts` to recompute DTS from PTS across the seam, but this propagates those irregular VFR PTS values into the DTS sequence. The H.264 decoder emits decoder-layer warnings wherever it encounters equal or near-equal DTS values throughout the body. The warnings are **not** confined to the seam region — on long clips the decoder warnings span the **entire clip**: e.g. clip04 (1990-01-06, ~32 min) shows up to 103 warnings with DTS values ranging from 241 to 57424, spaced ~561 ticks apart across the full clip duration. The container-level DTS remains strictly increasing — `ffprobe -show_entries packet=dts_time` finds zero backward or equal DTS events.

**Options considered:**

- **Re-encode first GOP of body (A):** moves the encode→copy seam inside the body but does not eliminate it; the body_rest stream-copy carries the same VFR PTS irregularity. Likely ineffective, adds code complexity.
- **`-vsync passthrough` on concat (B):** muxer flags, does not reach the decoder. Essentially already tried via `+igndts`. No effect.
- **Accept as benign (C):** container DTS clean, playback unaffected, no lossy encoding increase. Requires documentation.
- **Full re-encode on 3-seg (D):** definitively fixes warnings but re-encodes the entire body (hundreds of seconds), defeating the pipeline's core design goal of stream-copying all non-boundary content.

## Decision

Accept the decoder-layer warnings as benign. Do not change the encoding strategy.

**Why they are benign:**
1. **Container DTS is clean.** `ffprobe -show_entries packet=dts_time` finds 0 non-monotonic events. Media players (QuickTime, VLC, mpv) use container DTS for seeking and AV sync — not decoder DTS. No playback artifact.
2. **Container PTS is clean and frame count is complete.** Verified on clip04 (worst-case, 103 warnings): `ffprobe` finds 0 duplicate PTS values, uniform frame durations (~0.0334s = 29.97fps), and decoded frame count 58040 vs 58051 expected (within 0.02%). No frozen frames, no dropped or duplicated frames.
3. **Not regressions from the source.** The 76-clip source baseline has 0 such warnings. The warnings are an artifact of `+igndts` recomputing DTS from VFR-source PTS values throughout the stream-copied body; they do not appear in the container and do not affect the output file's decodability.
4. **Fixing requires lossy re-encode of the body.** Option D would re-encode hundreds of seconds of content that the pipeline was specifically designed to stream-copy. The quality loss is imperceptible on VHS source at CRF 18, but the design principle (minimize re-encoding) matters more than eliminating a decoder-layer artifact.

## Consequences

- `ffmpeg -v warning -f null -` run on output clips with long stream-copied bodies may show up to ~100 "non monotonic" warnings per clip, spread across the entire clip duration (not confined to the seam). Short clips or clips without a long stream-copy body may show few or no warnings. This is expected and documented; do not investigate as a bug.
- `ffprobe -show_entries packet=dts_time` must continue to show strictly increasing container DTS (currently passing; must not regress).
- `ffprobe` container PTS and frame count must remain clean (0 duplicate PTS, frame count within 0.02% of expected); these are the authoritative correctness checks.
- Option D (full re-encode on 3-seg) remains available if the design priority shifts — e.g., if output clips need to survive aggressive downstream ffmpeg processing without any warnings.
