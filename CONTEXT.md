# VHS Home Movie Clipper

Splits a VHS digitization into per-day clips by OCR-reading the camera's burned-in timestamp. This glossary fixes the language used to reason about where clips are cut and how cut quality is judged.

## Language

**Boundary**:
A point in the video timeline where one recording session ends and the next begins (camera stopped then restarted). The primary signal is a forward jump in OCR'd camera time.
_Avoid_: cut, split (those are the *action*, not the event)

**Cut**:
The action of placing a clip division at a chosen timeline position. A **Cut** is the pipeline's attempt to land on a **Boundary**.
_Avoid_: split, break

**Dead Zone**:
A span where OCR returns `None` on every frame. Two kinds, distinguished by length (see below). Bare "Dead Zone" is ambiguous — always qualify.
_Avoid_: gap, blind spot

**Splice Dead Zone**:
A *short* **Dead Zone** (≲120s of `None`) caused by head-switch noise at a tape **Splice**. The span is a single noise burst; the end-of-burst **Placement** policy applies. 33 of 34 dead-zone date-changes on the test material are this kind.
_Avoid_: dead zone (unqualified)

**Long Dead Zone**:
A *long* **Dead Zone** (≳120s, up to 2160s on the test material) of genuinely unreadable footage — not a single noise burst. Cause unconfirmed (long timestamp-free shot, blank/damaged tape). The end-of-burst policy does **not** apply; handling is undecided and out of scope for the Placement work.
_Avoid_: dead zone (unqualified)

**Splice**:
A physical tape section join (head-switching transition) that produces analog noise. The noise creates a **Dead Zone** in OCR and saturates every visual detector simultaneously.
_Avoid_: seam, join

**Ambiguity Window**:
The `[prev_t, coarse_t]` interval around a splice **Boundary** within which the true session change is unlocatable to finer than the window. No available signal resolves it further; the **Cut** must adopt an edge **Policy** instead of seeking a frame.
_Avoid_: error window, fuzz

**Detection**:
Judging whether a **Boundary** exists near a candidate timestamp (true/false-positive/negative). No trustworthy labeled benchmark exists — the former AI-labeled golden set was abandoned as unreliable; judged by spot-checking and clip date-purity (see ADR 0001).
_Avoid_: accuracy (overloaded)

**Placement**:
Judging, given a real **Boundary**, how many seconds the **Cut** lands from the true session change. Measured by **clip-content audit** (frame content vs filename date), per ADR 0001 — distinct from **Detection**.
_Avoid_: accuracy, precision (overloaded)

## Relationships

- A **Splice** causes a **Dead Zone**, which produces an **Ambiguity Window** around a **Boundary**
- A **Cut** targets a **Boundary**; its quality splits into **Detection** (right boundary exists) and **Placement** (right second)
- **Detection** and **Placement** are independent — getting the right **Boundary** says nothing about landing on the right second

## Flagged ambiguities

- "wrong boundary" was used to mean both a **Detection** failure (cut where no boundary is) and a **Placement** failure (cut at the right boundary but wrong second). Resolved: these are distinct, separately measured concepts.
- At a **Splice**, the **Boundary** is an **Ambiguity Window**, not a recoverable frame. Frame-accurate **Placement** is unsatisfiable there.
