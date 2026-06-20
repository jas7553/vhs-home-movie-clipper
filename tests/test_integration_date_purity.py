"""
Integration test: every frame sampled from each output clip must match the date
encoded in that clip's filename.  Catches cross-date contamination that unit
tests cannot see.
"""
import os
import re
import subprocess
import tempfile

import pytest

from split_homevideo import DEFAULT_CROP, OCR_BIN, extract_frame, ocr_batch, parse_timestamp

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _find_small_video() -> str:
    """Locate small video in worktree root or, for git worktrees, in the main repo."""
    local = os.path.normpath(os.path.join(_REPO_ROOT, "Converse 1990-small.mp4"))
    if os.path.exists(local):
        return local
    # git worktree: common-dir points into main repo's .git
    r = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True, text=True,
        cwd=os.path.dirname(__file__),
    )
    if r.returncode == 0:
        candidate = os.path.normpath(os.path.join(r.stdout.strip(), "..", "Converse 1990-small.mp4"))
        if os.path.exists(candidate):
            return candidate
    return local  # doesn't exist; skipif will catch it


SMALL_VIDEO = _find_small_video()
SAMPLE_INTERVAL = 5  # seconds between sampled frames


def _has_ocr_binary() -> bool:
    return OCR_BIN.exists()


def _sample_offsets(duration: float, interval: int) -> list[float]:
    offsets = []
    t = 0.0
    while t < duration:
        offsets.append(t)
        t += interval
    return offsets


def _get_duration(video: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists(SMALL_VIDEO), reason="Converse 1990-small.mp4 not present")
@pytest.mark.skipif(not _has_ocr_binary(), reason="ocr_timestamp binary not compiled")
def test_no_cross_date_contamination(tmp_path):
    out_dir = tmp_path / "clips"
    out_dir.mkdir()

    subprocess.run(
        ["python3", os.path.join(_REPO_ROOT, "split_homevideo.py"),
         SMALL_VIDEO, "--out-dir", str(out_dir)],
        check=True, capture_output=True,
    )

    clips = sorted(out_dir.glob("*.mp4"))
    assert clips, "pipeline produced no clips"

    date_pat = re.compile(r"_(\d{4}-\d{2}-\d{2})")
    violations: list[str] = []

    for clip in clips:
        m = date_pat.search(clip.name)
        if not m:
            continue  # fallback clip with no date label
        expected_date = m.group(1)

        duration = _get_duration(str(clip))
        offsets = _sample_offsets(duration, SAMPLE_INTERVAL)

        with tempfile.TemporaryDirectory() as frame_dir:
            paths = [
                p for t in offsets
                if (p := extract_frame(str(clip), t, DEFAULT_CROP, frame_dir)) is not None
            ]
            if not paths:
                continue

            ocr_results = ocr_batch(paths)
            for path, text in ocr_results.items():
                dt = parse_timestamp(text) if text else None
                if dt is None:
                    continue
                actual_date = dt.strftime("%Y-%m-%d")
                if actual_date != expected_date:
                    offset_str = os.path.basename(path).removeprefix("frame_").removesuffix(".bmp")
                    offset = float(offset_str)
                    violations.append(
                        f"{clip.name}: frame at +{offset:.1f}s reads {actual_date}, expected {expected_date}"
                    )

    assert not violations, "Cross-date contamination:\n" + "\n".join(violations)
