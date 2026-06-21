# Finding 001: overlay position + format vary *within* a single tape

**Status:** open (2026-06-21)
**Trigger:** first run against a second tape (1992) produced badly wrong clips —
one 111-min blob, a sweep of the last 86 min into one clip, and junk 3–4s clips.
**Scope:** changes the fleet plan for the remaining ~12 tapes.

---

## Symptom

Run against the 1992 tape (`--interval 3`, default crop/parser):

```
ocr ocr_success=512 ocr_total=4508   # 11% yield
clips count=9
clip 01 dur_min=111.5   # giant blob, no boundaries
clip 02 date=1992-08-12 dur_s=4   # junk island
clip 08 date=1992-01-31 dur_s=3   # junk island
clip 09 dur_min=86.3    # second blob
```

The 1990 tape gets ~86% per-window yield with the same defaults. The 1992 tape
got **11%** (512/4508).

## Root cause

OCR yield is not random — it is confined to one time band. Reads per 10-min block:

```
  0-100 min   reads = 0        (becomes the 111-min clip 01)
100-150 min   reads ≈ 110/200  (the only readable session)
150-225 min   reads = 0        (becomes the 86-min clip 09)
```

Where OCR *does* read, the text is clean and parses correctly. So the parser and
the numeric format are **not** the problem. The problem is that the overlay outside
that band is invisible to the current crop + parser — and it is invisible for **two
independent reasons**, both varying *within the one tape*:

### 1. Overlay position varies → tight crop clips it off-frame

The default crop `250:110:385:370` is right-anchored, tuned to the 1990 numeric
overlay. Other sessions on the 1992 tape place the overlay further left/center, so
the crop captures only a fragment:

| time | overlay (full frame) | what the crop captured |
|---|---|---|
| 30 min | `NOV. 26 1992` | `26  1992` (`NOV.` clipped off the left) |
| 120 min | `11:28 AM` / `9/25/92` | full — reads fine |
| 200 min | `8:00:26 PM` | `00:26 PM` (`8:0` clipped off the left) |

A wide bottom-band crop `420:120:150:350` fed to the OCR binary recovered all three:
`NOV- 26 1992`, `11:28 Ar 9/25/92`, `8:00:26 PM`. **Position is fixable by widening
the crop** — but one fixed crop cannot be both right-anchored and capture a
left-shifted overlay, so the crop must be wide rather than re-tuned per tape.

### 2. Overlay format varies → parser rejects two of the three styles

`parse_timestamp` hard-requires a numeric `M/D/YY` date (returns `None` without one).
Of the three styles seen on this one tape:

| style | example | handled today? |
|---|---|---|
| two-line numeric | `11:28 AM` + `9/25/92` | yes |
| word-month, date-only | `NOV. 26 1992` | **no** — needs month-name parsing |
| time-with-seconds, **no date** | `8:00:26 PM` | **no** — and has no date at all |

Word-month would flow through the existing date-only→midnight path once parsed.
The time-only style is worse: it carries **no date**, so daily-mode has nothing to
place even with a perfect read. That is an open gap, not a quick parser add.

### Cascade into junk clips

In the thin readable band, OCR is too sparse for `drop_date_islands` to fire — that
filter needs *dated* neighbours on both sides, but here neighbours are mostly `None`.
So misread islands (`8/12`, `1/31`) survive as 3–4s junk clips. Sparse OCR breaks
the island filter as a side effect.

## Not the cause (ruled out)

- **Playback speed.** The 1992 container reports `r_frame_rate` = 59.94 fps vs the
  1990 tape's 29.97 (`avg_frame_rate` ≈ 28.9, 390570 frames / 13522 s) — a timebase
  mismatch baked into the source, which is why the footage looks sped up. It does
  not affect OCR or cutting and "fixing the original" is out of scope. Ignore.
- **Parser / numeric format.** Clean where captured; see above.

## Implications

For the remaining tapes, the working defaults that succeeded on 1990 — **one tight
crop + one numeric format** — do not generalise, and the failure is *intra-tape*
(a single tape mixes styles across sessions), so a per-tape `--crop` override alone
will not solve it. Candidate directions, roughly in priority:

1. **Widen the default crop to the full bottom band** (~`560:130:40:350`). Cheap, no
   new logic, absorbs position drift across sessions. Slightly more noise area;
   Vision handled it in the spot test.
2. **Multi-format parser**: word-month dates (`NOV. 26 1992`) and time styles
   (with seconds; time-only). Word-month reuses the existing midnight fallback.
   **Time-only carries no date — a genuine gap for daily-mode**, flag separately.
3. **Per-file calibration pass** (durable answer): sample ~20 frames across each
   tape, auto-detect overlay bbox + format before scanning, set crop + parser mode
   per file. Turns 12 manual tunings into one automated step.

None of these is decided here. (1) is the obvious first measurement.

## Evidence

Raw OCR cache + extracted full/crop/wide frames at 30/120/200 min, under
`.scratch/frames1992/` (local, not committed). Yield bins and format table above
are from that cache.
