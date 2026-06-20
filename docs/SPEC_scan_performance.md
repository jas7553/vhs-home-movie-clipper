# Scan performance: history of optimizations and rejected approaches

## Current baseline (as of 2026-06-20)

`scan()` on `Converse 1990-small.mp4` (2700s / 45 min, 810 frames at 3fps/10s):

- **Wall time: ~42s**, 204% CPU
- Virtually all time is in scan — cache-hit runs complete in 0.09s
- OCR batch dominates; extraction is already fast (see Phase 1 below)

The pipeline is `extract_all_frames()` (single-pass ffmpeg) → `ocr_batch()` (serial
`ocr_timestamp` subprocess) → majority-vote bucketing.

---

## Phase 1: per-frame subprocess loop → single-pass ffmpeg (implemented, large win)

**See `SPEC_single_pass_scan.md` for full detail.**

Original `scan()` called `extract_frame()` once per sample — one `ffmpeg` subprocess
per frame, each doing a seek into the file. On a 5.9hr video: 2,124 subprocess
calls × 150–250ms each = 5–10 minutes total.

Replaced with a single `ffmpeg` invocation using `fps=3/10` filter. Result: linear
streaming decode, no redundant seeks, one process startup.

---

## Phase 2: parallel chunked ffmpeg extraction (tried 2026-06-20, rejected)

**Hypothesis:** Split video into N time ranges, run N ffmpeg processes in parallel
via `ThreadPoolExecutor`. Fast-seek (`-ss` before `-i`) to each chunk start.
Expected speedup: 4–6× (M4, 10 cores, measured at 252% CPU headroom).

**Implementation:** `do_chunk(i)` seeks to `i * chunk_s`, decodes to
`(i+1) * chunk_s + interval`, uses `-start_number` for global frame index
assignment. Dedup by frame index at boundaries.

**Measured result on `Converse 1990-small.mp4`:**

| Approach | Wall | CPU% | Frames |
|---|---|---|---|
| Sequential (single-pass) | 42.2s | 204% | 810 |
| Parallel 8 chunks | 44.5s | 259% | 810 |
| Parallel 8 chunks (2nd run) | 45.3s | 253% | 810 |

**Rejected.** Parallel is 2–3s *slower* than sequential. Two causes:

1. **Sequential streaming is already the fast path.** The `fps=3/10` filter means
   ffmpeg skips most frames internally — effective decode throughput is very low.
   Streaming linearly from start to finish with zero seeks is already near-optimal.

2. **OCR is the bottleneck, not extraction.** `ocr_batch()` runs all 810 paths
   through `ocr_timestamp` in one serial subprocess. Both approaches hit the same
   serial OCR wall. Parallelizing extraction doesn't move the needle.

3. **Process startup × 8 + I/O contention costs more than parallel decode saves.**

The implementation was reverted after measurement. The parallel `extract_all_frames`
with chunked design is in git history if needed.

---

## Phase 3: parallel OCR batches (measured bottleneck — not yet implemented)

**Profiled 2026-06-20 on `Converse 1990-small.mp4` (810 frames):**

| Phase | Time | % of scan |
|---|---|---|
| `extract_all_frames` (parallel 8-chunk) | 10.3s | 22% |
| `ocr_batch` (serial) | 36.5s | **78%** |

OCR dominates. This is the correct lever.

**Hypothesis:** Split 810 paths across N `ocr_timestamp` subprocesses via
`ThreadPoolExecutor`. Each subprocess is independent (reads BMPs, writes to stdout).
No shared state. Expected speedup: ~N× up to CPU saturation.

On M4 (10 cores), predicted scan total with N=8: ~10s extraction + ~5s OCR ≈ 15s
(vs current 46s) — roughly 3× end-to-end improvement.

**Risk:** Apple Vision framework may serialize internally across processes sharing the
same GPU/ANE. If so, N>1 gives no benefit. Measure before assuming linear scaling.

**Implementation plan:**

1. Replace `ocr_batch()` in-place (same signature, same caller sites at lines 269
   and 904). No caller changes needed.

```python
def ocr_batch(paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    n_workers = min(os.cpu_count() or 4, 8)
    chunk_size = max(1, math.ceil(len(paths) / n_workers))
    chunks = [paths[i:i+chunk_size] for i in range(0, len(paths), chunk_size)]

    def run_chunk(chunk: list[str]) -> dict[str, str]:
        result = subprocess.run([str(OCR_BIN)] + chunk, capture_output=True, text=True)
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "\t" in line:
                p, _, text = line.partition("\t")
                out[p] = text
        return out

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        merged: dict[str, str] = {}
        for chunk_result in pool.map(run_chunk, chunks):
            merged.update(chunk_result)
    return merged
```

2. Add `import math` at the top (not currently imported).

3. `ocr_batch` is called in two places:
   - `scan()` line 269: 810 paths (bulk scan — primary target)
   - `refine_split()` line 904: small batch (~10–30 paths per boundary, already fast)
   Both calls benefit automatically; no special-casing needed.

**Verification:** Before/after `time python3 split_homevideo.py "Converse
1990-small.mp4" --dry-run` (delete cache first). Output clips must be identical.
Also measure OCR alone at N=1,2,4,8 to characterize actual scaling curve before
committing to n_workers default.
