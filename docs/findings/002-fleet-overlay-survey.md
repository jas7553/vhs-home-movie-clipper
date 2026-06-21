# Finding 002: Fleet overlay survey — position + format catalog (2-tape baseline)

**Status:** open (2026-06-21)
**Scope:** 2 tapes digitized so far; ~10 tapes remaining to be downloaded.
**Trigger:** Issue 008 — catalog overlay position + format across all available tapes.

---

## Source tapes

| label | duration | frames |
|---|---|---|
| 1990 tape | 5.9 hr (21 280 s) | 640×480, 29.97 fps |
| 1992 tape | 3.75 hr (13 522 s) | 640×480, 59.94 fps timebase† |

† 59.94 fps timebase is a container artifact; footage is NTSC 29.97. Does not affect OCR or cutting.

## Sampling methodology

22 frames extracted from the 1990 tape and 20 from the 1992 tape at intervals spanning
the full duration of each. Full-frame Apple Vision OCR (no crop) used for all reads.
Bounding boxes estimated by bisection sweep (horizontal and vertical crop strips) and
confirmed by visual inspection of extracted frames.

---

## Format styles observed

### Style A — two-line numeric (time + date)

```
H:MM AM/PM       ← top line
M/D/YY           ← bottom line
```

Examples: `2:03 PM / 1/28/90`, `11:28 AM / 9/25/92`

**Overlay position (640×480 frame):**
- Horizontal: x ≈ 415–630 (right ~215 px of frame)
- Vertical: y ≈ 400–470 (bottom 80 px of frame, two-line height ~70 px)
- Corner: **bottom-right**

**Tapes / time ranges:**
- 1990 tape: entire tape (all 22 samples), fully consistent
- 1992 tape: isolated window at ~120 min (`9/25/92` session only)

**Parser status: handled.** Current `parse_timestamp` parses this style correctly.

---

### Style B — word-month date-only (single line)

```
MON. DD YYYY
```

Examples: `NOV. 25 1992`, `DEC. 13 1992`, `DEC.24 1992`

Separator between abbreviation and day varies: `.` (most common), `:` or `,`
(OCR misread of `.`). No time line present.

**Overlay position (640×480 frame):**
- Horizontal: x ≈ 190–540 (center 350 px of frame)
- Vertical: y ≈ 430–462 (bottom ~50 px, single-line height ~32 px)
- Corner: **bottom-center**

**Tapes / time ranges:**
- 1992 tape: dominant style across ~80% of samples (0–90 min, 150–225 min, and
  re-appearing after the numeric session). Not present on 1990 tape.

**Parser status: NOT handled.** `parse_timestamp` requires a numeric `M/D/YY` date;
word-month returns `None`. Issue 010 covers adding word-month parsing.

---

### Style C — time-with-seconds, no date (single line)

```
H:MM:SS AM/PM
```

Examples: `8:08:56 PM`, `10:50:31 AM` (garbled by OCR as `10:5031JAM`)

No date line present.

**Overlay position (640×480 frame):**
- Horizontal: x ≈ 185–570 (center ~385 px, slightly wider than Style B)
- Vertical: y ≈ 425–460 (bottom ~55 px, single-line height ~35 px)
- Corner: **bottom-center** (same region as Style B)

**Tapes / time ranges:**
- 1992 tape: brief windows at ~15 min and ~100 min. Not present on 1990 tape.

**Parser status: NOT handled (two deficiencies).**
1. `H:MM:SS` format not parsed (colon count mismatch, issue 011).
2. No date present — even after parsing, daily-mode cannot assign to a clip (issue 012).

---

## Position summary

| style | horizontal extent | vertical extent | corner |
|---|---|---|---|
| A — two-line numeric | x 415–630 (215 px wide) | y 400–470 (70 px tall) | bottom-right |
| B — word-month date-only | x 190–540 (350 px wide) | y 430–462 (32 px tall) | bottom-center |
| C — time-with-seconds | x 185–570 (385 px wide) | y 425–460 (35 px tall) | bottom-center |

**All three styles sit in the bottom 80 px of a 640×480 frame.**  
Styles B and C are ~175 px wider and ~175 px further left than Style A.
The default crop `250:110:385:370` (x=385+) captures Style A fully but clips
Styles B and C on the left, explaining the near-zero OCR yield on the 1992 tape
in existing scans.

A wide bottom-band crop — e.g., `560:130:40:350` (x=40+, covers 560 px) — captures
all three styles. Issue 009 (widen default crop) is already closed with this fix.

---

## Unhandled formats flagged for parser work

| priority | style | issue |
|---|---|---|
| high | word-month date-only (`NOV. DD YYYY`) | 010 |
| medium | time-with-seconds (`H:MM:SS AM/PM`) | 011 |
| open gap | time-only, no date (Style C) — daily-mode cannot place | 012 |

---

## Notes on intra-tape style switching (1992)

The 1992 tape mixes all three styles within a single cassette:

| approx time | style | example read |
|---|---|---|
| 0–10 min | B (word-month) | `NOV. 25 1992` |
| ~15 min | C (time-with-seconds) | `8:08:56 PM` |
| ~20 min | no read | — |
| 25–90 min | B (word-month) | `NOV. 26–DEC. 2 1992` |
| ~100 min | C (time-with-seconds) | `10:50:31 AM` |
| ~120 min | A (two-line numeric) | `11:28 AM / 9/25/92` |
| 150–225 min | B (word-month) | `DEC. 30 1992`, `NOV. 27`, `DEC. 24` |

Style switching likely reflects different recording sessions on the same cassette,
possibly from a different camera or a camera reset between sessions.

---

## Limitations

Only 2 of ~12 tapes have been digitized. The remaining tapes may introduce additional
styles or positions not seen here. This document should be updated as more tapes are
processed. The `docs/findings/README.md` notes that findings are empirical observations
from available evidence, not projections.
