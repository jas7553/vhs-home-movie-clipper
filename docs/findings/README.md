# Findings

Dated empirical discoveries with evidence. The missing slot between the other doc types:

| Doc type | Holds |
|---|---|
| `docs/findings/` | **evidence** — what we measured/observed, and what it implies |
| `docs/SPEC_*.md` | design **proposals** toward a fix |
| `docs/adr/` | **decisions** made |
| `docs/REQUIREMENTS.md` | **goals / constraints** |
| `CONTEXT.md` | domain **vocabulary** |

A finding records something we learned about the footage or the pipeline that
was non-obvious and is backed by data. It does not commit to a fix — it
*motivates* one. When a finding drives a design, that design goes in a SPEC; when
a choice is made, it goes in an ADR; both should link back to the finding.

Naming: `NNN-slug.md`, zero-padded, in discovery order.

Each finding carries a **Status** (`open` / `superseded` / `actioned`) and a date.
Leave out source filenames — they are a privacy risk (refer to tapes by year).
