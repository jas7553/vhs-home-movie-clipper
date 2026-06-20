# ADR 0002 — Remove LLM Vision Prototype

**Status:** Accepted  
**Date:** 2026-06-20

## Context

`split_homevideo.py` contained a prototype refinement path that used Claude Haiku
vision to read timestamps from frames that the Apple Vision OCR binary missed —
particularly across Splice Dead Zones (≤120s all-`None` spans at tape splices).

Three CLI flags gated the prototype:

- `--vision-refine` — call the Anthropic API per-frame (paid, ~$1–4/run)
- `--vision-export` / `--vision-export-all` — dump PNGs + manifest.json for manual
  review, no API call
- `--vision-readings PATH` — apply pre-labeled readings (no API call)

The prototype was validated on 2026-06-19: median placement error on Splice Dead Zone
boundaries was 17.1s (28/40 boundaries, meeting REQUIREMENTS line 25). That measurement
was captured against the `ocr_refinement` strategy, not the vision paths — the vision
paths were an exploratory oracle, never part of the production run.

## Decision

Delete all vision prototype code:

- `VISION_MODEL`, `VISION_MAX_WORKERS`, `VISION_PROMPT` constants
- `import base64`, `TYPE_CHECKING` block, lazy `import anthropic`
- Functions: `_extract_frame_png`, `_vision_frame_name`, `vision_read_frame`,
  `_resolve_vision_cut`, `_refine_split_vision`, `_readings_for_window`,
  `_boundary_needs_vision`, `_export_vision_frames`, `vision_api_refinement`,
  `vision_readings_refinement`
- CLI flags and their `main()` wiring
- Test files `test_boundary_needs_vision.py`, `test_vision_resolve.py`

`main()` unconditionally uses `ocr_refinement(...)`.

## Rationale

- The production path (`ocr_refinement`) already meets the placement target.
- The vision paths introduced an Anthropic SDK dependency that is never exercised on
  the default code path, added ~300 lines of dead code, and left misleading `PROTOTYPE`
  flags in the CLI help.
- Keeping dead prototype code raises maintenance burden and makes future changes
  (e.g. strategy selection, refinement config) harder to reason about.
- If LLM vision refinement is revisited, it can be reintroduced as a separate tool
  (or a clean `RefinementStrategy` plugin) with a proper evaluation harness.

## Consequences

- `split_homevideo.py` loses the `--vision-*` flags. Existing invocations using those
  flags will error at argument parsing.
- The `anthropic` package is no longer a dependency for this script.
- All 181 non-vision tests continue to pass.
