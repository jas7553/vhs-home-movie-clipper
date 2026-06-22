# Issue 020: Non-monotonic DTS warnings in 3-seg concat (lead + body + trail)

**Status:** needs-triage
**Labels:** ready-for-agent
**Context:** cut stage — `cut_clip_with_boundary_encode()` / concat demuxer in
`split_homevideo.py`. Residual from issue-017 (partially fixed). Affects clips
that produce all three segments: a re-encoded lead, a stream-copied body, and a
re-encoded trail (i.e., clips whose `exact_start` and `exact_end` are both present
and at least MIN_BOUNDARY_SEG from their respective keyframes).

---

## Background: what issue-017 fixed (and didn't)

Issue-017 addressed non-monotonic DTS warnings (`ffmpeg -v warning -f null -`)
in output clips. Two commits were made:

- **3127b7d** — first fix: added `-bf 0` to `_ffmpeg_encode_seg`, added
  `-avoid_negative_ts make_zero -muxpreload 0 -muxdelay 0` to the concat step.
  Verified **ineffective** — total warnings essentially unchanged (~187/196).

- **bf5745c** — second fix: added `-fflags +igndts` to `_ffmpeg_copy_seg` and
  changed `+genpts` → `+igndts` in the concat step. Reduced warnings from 196
  (17 clips) to **110 (15 clips)**. The three specific clips named in issue-017
  (clip16/1990-02-10, clip71/1990-08-03, clip09/clip10/1990-01-28) are now all
  **0 warnings**. Issue-017 was closed as "partially fixed."

## Remaining problem

15 clips still emit ~3–23 non-monotonic DTS warnings each (total ≈110 across
76 clips, vs 0 for the source baseline). All affected clips go through the
**3-segment concat path** (lead re-encode + body stream-copy + trail re-encode).

### Warning pattern

All warnings in affected clips cluster in the **first 0.4 seconds** of the clip
(the lead+body seam area), not distributed across the clip. The warnings are
duplicate-DTS events (`DTS X >= X`), not backward jumps, with a periodic spacing
of ~561 ticks at 29970Hz (≈18.7ms between events). E.g. for clip19:
```
167 >= 167
728 >= 728
1288 >= 1288
...  (23 total, all before 0.417s)
```

### Key findings from verification (commit bf5745c)

1. **Container DTS is clean.** `ffprobe -show_entries packet=dts_time` shows
   strictly increasing DTS in both video and audio streams for ALL 76 clips.
   `ffprobe` finds 0 backward/equal DTS events.

2. **Warnings come from the decoder layer.** `ffmpeg -v warning -f null -`
   decodes the stream; the null muxer sees non-monotonic DTS from the *decoder*
   output, not the container. This is distinct from the container-level DTS that
   media players use for seeking and AV sync.

3. **`+igndts` is a no-op for this case.** Simulation with exact clip19
   boundaries (exact_start=5967.7, kf_after=5968.029, kf_before=6385.946,
   exact_end=6386.0) confirmed: both `+genpts` (old) and `+igndts` (new) produce
   identical 23 warnings. The fix does not help the 3-seg path.

4. **Pure stream-copy and 2-seg concat paths are clean.** 61/76 clips are 0
   warnings. The 15 dirty clips all share the 3-seg structure.

5. **Example clip19 structure:**
   ```
   lead  [5967.7,   5968.029] = 0.329s  → libx264 CRF18, -bf 0
   body  [5968.029, 6385.946] = 417.9s  → stream copy, +igndts
   trail [6385.946, 6386.0  ] = 0.054s  → libx264 CRF18, -bf 0
   concat: -fflags +igndts -avoid_negative_ts make_zero → 23 warnings
   ```

### Root cause hypothesis

When concatenating a re-encoded segment (clean, known-timebase DTS) with a
stream-copied body (VFR source DTS, avg_frame_rate ≈ 29.954fps ≠ uniform),
then with another re-encoded segment, the H.264 decoder's internal DTS
accounting gets confused at the seam. The mixed re-encode/stream-copy GOP
structure causes the decoder to emit frames with equal DTS in its output queue,
even though the container packets are ordered correctly.

The 561-tick period (18.7ms) is approximately 1.59× a 33.3ms frame — no obvious
relation to audio AAC frame size (44100/1024 ≈ 23.2ms) or video frame duration.

### What was tried and failed

- `-bf 0` on encode segs (3127b7d) — no effect on body/trail seam
- `-avoid_negative_ts make_zero` on copy and concat — no effect
- `-fflags +igndts` on copy and concat — no effect on 3-seg path
- `-muxpreload 0 -muxdelay 0` on concat — no effect

## Candidate fixes (not yet tried)

**Option A — Re-encode first GOP of body after lead seam:**
Instead of stream-copying the body from `kf_after`, re-encode
`[kf_after, kf_after + one_gop]` (e.g., 0.5–1.0s) as libx264 with `-bf 0`,
then stream-copy the rest. Eliminates the encode→copy seam at the cost of
one extra tiny re-encode per clip.

**Option B — Use `-vsync passthrough -fps_mode passthrough` on concat:**
Force the concat demuxer to pass frame timestamps unchanged, bypassing
any timestamp re-ordering the muxer does. May not change decoder behavior.

**Option C — Accept decoder warnings as benign:**
Since container DTS is clean (ffprobe check passes) and the original reported
symptoms (clip16, clip71, clip09) are fixed, declare the remaining decoder-layer
warnings acceptable. Media players use container DTS for seeking/sync; decoder
DTS warnings may not cause visible playback artifacts.

**Option D — Re-encode entire clip when 3-seg path is triggered:**
When `len(segs) == 3`, use a single libx264 pass (CRF 18) over the entire
`[start, end]` span instead of concat. Eliminates the seam entirely at the cost
of full re-encode for multi-boundary clips. Clips with both lead and trail
boundaries are typically short (boundary-dense regions).

## Acceptance criteria

- [ ] After a full run, clips that currently show 23 warnings show ≤1 (matching
      the source rate of ~1/960s of copied content).
- [ ] Container DTS remains strictly non-decreasing (ffprobe check, currently
      passing — must not regress).
- [ ] No regression in clip count / boundary placement.
- [ ] If Option C is chosen: document explicitly why decoder warnings are benign
      and add a note to CLAUDE.md.

## Verification method

Same as issue-017 (regenerate to scratch `--out-dir`, warn-count loop):
```bash
for c in "$OUT_DIR"/*.mp4; do
  n=$(ffmpeg -v warning -i "$c" -f null - 2>&1 | grep -c "non monotonic" || true)
  [[ $n -gt 0 ]] && echo "$n  $(basename "$c")"
done
```
Target: every clip ≤ 1 warning (or document why >1 is acceptable).

## Blocked by

None — can start immediately.
