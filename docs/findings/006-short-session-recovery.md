# Finding 006: short sessions were silently merged at the default interval

**Status:** actioned (2026-09-15)
**Trigger:** the per-boundary ruler on the 1990 tape kept reporting "intermediate-date
content in gap" and two MISMATCH boundaries whose *old* label was never observed in the
dense scan (`1990-02-10 -> 1990-02-16` with eleven `02-11` frames just before the cut;
`06-28 -> 07-07` with `07-05` frames). Whole-clip purity was clean because those
sessions are shorter than the purity sweep's sample spacing.
**Scope:** detection, not placement. Decides how the island filter treats a lone
dated reading.

---

## What was wrong

`drop_date_islands` defines a real session as >= 2 consecutive same-date readings.
At `--interval 10` a session shorter than ~20s yields exactly one coarse reading, so
it was dropped as a misread and its footage merged into the preceding clip's tail —
10–40s of the wrong date in a clip, a correctness failure. `--interval 3` did not
show it, which is how it was first noticed: the rule was interval-dependent.

## What was measured

1s crop-only probes over ±interval around every island on fresh scans of both
reference tapes (default profile: interval 10, gap 3600, daily).

| Tape | islands | supported (>= 3 contiguous probe hits) | sessions recovered |
|---|---|---|---|
| 1990 | 36 | 6 | 5: 01-13 (9s), 01-24 (15s), 02-11 (11s), 05-22 (25s), 07-05 (39s) |
| 1992 | 16 | 2 | 0 by density; 1 (09-27, ~60s) by the dead-zone rule below |

Every recovered session is a solid block of 4–9 consecutive same-date probe reads
with legible neighbour-date reads on both sides. The two islands that a looser rule
(>= 1 probe hit) had also "confirmed" — `03-27` inside an `08-27` session and `10-18`
inside `10-13` — were sporadic 3↔8 digit misreads recurring on one or two scattered
probes, interleaved with correct reads; the contiguity requirement rejects both.
That pair is also why 3↔8 joined the month and day confusable sets.

The 1992 `09-27` session (~60s between 9/25 and 10/4, overlay faint white on a bright
scene) reads on exactly one coarse window and on one preprocessed probe; every other
frame is unreadable even with the preprocessing chain. Its shape is distinctive: a
clean parse, both adjacent samples `None`, date strictly between two *different*
neighbour dates in tape order. `_dead_zone_island` keeps that shape. A misread cannot
take it when the flanking footage shares a date, and a misread of legible flanking
footage is normally adjacent to a legible read of it.

Cost: ~700 probe frames on the 6h tape (one parallel extraction + one OCR batch,
~30s), cached in the scan cache; subsequent runs pay nothing.

### The dead-zone exception, refined

Two digitisations of the same 1992 footage (tape #1 and tape #3) disagreed on a
real 10s `1/19` session ("7:46 AM 1/19/92" on a thermometer shot, verified by
eye): one sampled it with `None` windows on both sides, the other had a legible
`1/25` window 10s later. Both had every 1s probe around it unreadable. The rule
now asks for a probed readability hole — no legible reading within ±5s and `None`
probe entries on both sides — rather than `None` adjacent coarse windows, and
judges chronology against the nearest *non-island* neighbours (a `3/05` misread
next to a `3/03` misread of the same `3/06` session had vouched for itself). A
date inferred for a time-only read is never probed and so can never claim it
(the 1990 tape's "1:05 AM" read before the 9/01 session had been filled as
3/26 and briefly became a clip).

## Bounces: persistent one-glyph misreads

Tape #1 (1992, 6h, OCR yield 40%) read `4/28` as `1/28` for 35s, `4/23` as
`1/23` for 20s and `3/ 7` as `8/ 7` for 16s, each as an X Y X Y alternation. No
existing filter can settle a bounce: the ≥2 rule passes both sides, the capped
confusion filters cannot drop a 35s run without risking a real session, and a
majority vote is inflated by whichever side the dense probes happened to read.
Tape chronology settles it: `4/28` lies between `4/27` and `4/29`, `1/28` does not.
`drop_bounce_runs` drops every run of the date that does not fit between the
dates flanking the bounce, when exactly one of the two fits; it requires a
single-field twin relation (so a genuine `3/25, 9/01, 3/25` re-recording is never
touched) and ≥3 runs (so a plain boundary between twin dates is not a bounce).
Tape #1: 81 → 74 clips, the three bounce regions collapsed, nothing else changed
on any tape.

## Preprocessed probes, and the misreads they stabilise

Crop-only probes could not confirm a 30s `4/19` session on tape #1 (one read in a
30s unreadable stretch ending at the 4/21 boundary); the preprocessing chain read
six contiguous frames of it. Probes therefore run the scan's two-pass design
(crop-only, then preprocessing on frames with no parseable date). That also
recovered tape #1's `1/29` session, which tape #3 had already found. The cost: the
chain also stabilises misreads for a few seconds — 12s of `3/ 3` and 30s of `3/ 5`
inside a 3/06→3/07 stretch (garbled time line, so no clock evidence), 6s of `8/ 8`
as `3/18`. Two run-level rules absorb them: a run ≤60s that is a one-field twin of
one of the nearest *long* neighbouring runs and lies outside their chronological
range is dropped (`drop_out_of_order_twin_runs`, anchored on long runs so adjacent
misreads cannot vouch for each other), and any run ≤10s bracketed by the same date
on both sides is dropped regardless of its digits (`drop_short_bracketed_runs`).
Tape #1 ends at 78 clips; the shared span with tape #3 now agrees session for
session.

## Sub-second placement at CLEAN boundaries

With the last-old and first-new frames adjacent on the 1s grid, the cut sat up to
1s late (Part 2 tape b14: true change at 2692.75, cut at 2693.0). `_bisect_transition`
now reads the midpoint frames down to 0.25s; an unreadable or off-date midpoint
stops it and the 1s bracket stands. Two extra single-frame reads per CLEAN
boundary. A related fix: `_place_content_aware` never places a cut past the first
confirmed new frame (its `last_old + 1` fallbacks assumed the 1s grid).

Measuring this needs the ruler's frame-matched anchoring (`--cumsum-tolerance 0.5`):
the duration-cumsum path accumulates ~+1.8s over the Part 2 tape and reported
three exact cuts as 1s LATE.

## Off-date misreads of the new session at a splice

1990 tape b72 (`9/ 8` → `9/23`): the first strictly parseable frame after the
splice read `1/23/90`. It matched neither session's date, so the refiner treated it
as an intermediate date and advanced the old-session marker 5s into the new
session; the cut landed 7s late. A strict read whose recoverable digits match the
expected new session and not the old one (`_gap_date_class` == new) is now a
new-session candidate.

## Also fixed on the way

A late-1990 tape's prior output had 43 clips, 18 of them alternating `12-22` /
`02-22` and three `11-xx` / `01-xx` pairs: numeric overlays drop the leading `1` of a
two-digit month for whole windows at a time. `drop_digit_drop_runs` now applies its
day rule to the month field as well; the relation is asymmetric (a dropped digit,
never an added one), so an A B A B alternation resolves to the full-digit date without
a run-length cap. That tape now produces 19 chronological clips.

## What is still out of reach

A session whose overlay shows no date line at all (a different camcorder mode: time
with seconds, no date — ~70s on the 1992 tape near 8540s) cannot be labelled by OCR.
It stays merged with its predecessor; nothing in the pipeline can name its date.
