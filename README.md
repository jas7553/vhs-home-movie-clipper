# vhs-home-movie-clipper

[![CI](https://github.com/jas7553/vhs-home-movie-clipper/actions/workflows/ci.yml/badge.svg)](https://github.com/jas7553/vhs-home-movie-clipper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Splits VHS-ripped home video files into one clip per calendar date by OCR-reading
the camcorder's burned-in timestamp (Apple Vision) and cutting with ffmpeg.
macOS only.

## Setup

```bash
brew install ffmpeg
swiftc -O ocr_timestamp.swift -o ocr_timestamp
pip install -r requirements-dev.txt   # tests/lint only; runtime needs scenedetect
```

## Usage

```bash
python3 split_homevideo.py "YourFile.mp4" --dry-run   # preview splits
python3 split_homevideo.py "YourFile.mp4"             # cut into YourFile_clips/
```

Flags: `--gap N` (camera-time jump threshold, default 3600), `--mode {daily,session}`
(default daily), `--interval N` (OCR sample spacing, default 10s), `--crop W:H:X:Y`
(overlay region; auto-calibrated by default), `--out-dir DIR`,
`--no-enable-scene-snap`, `--no-visual-anchor`, `--dry-run`.

Output: `<stem>_clipNN_YYYY-MM-DD.mp4` (daily) or `..._YYYY-MM-DD_HHMM.mp4` (session).
OCR results are cached next to the video (`<stem>_ocr_cache.json`) and
self-invalidate when scan parameters change.

## Documentation

- `CLAUDE.md` — pipeline architecture and key domain facts (start here)
- `CONTEXT.md` — vocabulary (Boundary, Cut, Splice Dead Zone, Detection vs Placement)
- `docs/REQUIREMENTS.md` — goals and constraints
- `docs/adr/` — decisions: splice placement policy (0001), placement acceptance criteria (0004)
- `docs/findings/` — dated evidence behind the scene-snap pass
