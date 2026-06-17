"""
Rapid keyboard-driven boundary review tool.

Generates candidate boundary timestamps from the OCR pipeline
(find_all_boundaries) and from a full-frame scene_score sweep, merges/dedupes
them, then walks you through each candidate one at a time: extracts a
before/after frame pair into a single composite image, pops it in Quick Look,
and records your y/n/m verdict to a resumable JSONL labels file.

This is the labeling tool referenced in docs/SPEC_rejected_signals.md's
"highest-leverage next step" — building a real, large, diverse golden
boundary set instead of relying on the 7-boundary test_15min.mp4 fixture.

Run:
    python3 review_boundaries.py "Converse 1990.mp4"

Output: <stem>_golden_labels.jsonl — one JSON object per labeled candidate:
    {"t": 1234.5, "sources": ["ocr_gap"], "verdict": "y"}

Resumable: already-labeled timestamps (within 5s) are skipped on rerun.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.dirname(__file__))
from split_homevideo import (  # noqa: E402
    DEFAULT_CROP,
    DEFAULT_GAP,
    DEFAULT_INTERVAL,
    find_all_boundaries,
    scan,
)

SCENE_SCORE_THRESHOLD = 0.1
MERGE_WINDOW_S = 5.0
LABEL_MATCH_WINDOW_S = 5.0


def compute_scene_score_candidates(
    video: str, cache_json: str, threshold: float = SCENE_SCORE_THRESHOLD
) -> list[float]:
    if os.path.exists(cache_json):
        with open(cache_json) as f:
            scores = [tuple(x) for x in json.load(f)]
    else:
        print("computing full-frame scene scores (one ffmpeg decode pass)...")
        cmd = [
            "ffmpeg", "-i", video,
            "-vf", "select='gte(scene,0)',metadata=print",
            "-f", "null", "-",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True).stderr
        pts_re = re.compile(r"pts_time:(\S+)")
        score_re = re.compile(r"lavfi\.scene_score=(\S+)")
        times: list[float] = []
        vals: list[float] = []
        pending_t = None
        for line in out.splitlines():
            m = pts_re.search(line)
            if m:
                pending_t = float(m.group(1))
                continue
            m = score_re.search(line)
            if m and pending_t is not None:
                times.append(pending_t)
                vals.append(float(m.group(1)))
                pending_t = None
        scores = list(zip(times, vals, strict=False))
        with open(cache_json, "w") as f:
            json.dump(scores, f)

    out: list[float] = []
    last_t: float | None = None
    for t, s in scores:
        if s < threshold:
            continue
        if last_t is None or t - last_t > 2.0:
            out.append(t)
        last_t = t
    return out


def merge_candidates(ocr_times: list[float], visual_times: list[float]) -> list[tuple[float, list[str]]]:
    tagged = [(t, "ocr_gap") for t in ocr_times] + [(t, "scene_score") for t in visual_times]
    tagged.sort(key=lambda x: x[0])
    merged: list[tuple[float, list[str]]] = []
    for t, src in tagged:
        if merged and t - merged[-1][0] <= MERGE_WINDOW_S:
            if src not in merged[-1][1]:
                merged[-1][1].append(src)
        else:
            merged.append((t, [src]))
    return merged


def extract_pair_image(video: str, t: float, out_path: str) -> None:
    before = max(0.0, t - 2.0)
    after = t + 2.0
    f1 = "/tmp/_review_before.jpg"
    f2 = "/tmp/_review_after.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(before), "-i", video, "-frames:v", "1", f1],
                   capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-ss", str(after), "-i", video, "-frames:v", "1", f2],
                   capture_output=True)
    subprocess.run(["montage", f1, f2, "-tile", "2x1", "-geometry", "+2+2", out_path],
                   capture_output=True)
    if not os.path.exists(out_path):
        # ImageMagick `montage` not installed — fall back to showing the "after" frame alone.
        os.replace(f2, out_path)


def load_labels(labels_path: str) -> list[dict]:
    if not os.path.exists(labels_path):
        return []
    with open(labels_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def already_labeled(t: float, labels: list[dict]) -> bool:
    return any(abs(t - rec["t"]) <= LABEL_MATCH_WINDOW_S for rec in labels)


def read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def fmt_t(t: float) -> str:
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP)
    ap.add_argument("--crop", default=DEFAULT_CROP)
    ap.add_argument("--scene-threshold", type=float, default=SCENE_SCORE_THRESHOLD)
    ap.add_argument("--ocr-only", action="store_true",
                     help="skip scene_score candidates, label only ocr_gap-sourced ones")
    args = ap.parse_args()

    stem = os.path.splitext(args.video)[0]
    ocr_cache = f"{stem}_ocr_cache.json"
    scene_cache = f"{stem}_scene_score_cache.json"
    labels_path = f"{stem}_golden_labels.jsonl"

    samples = scan(args.video, args.interval, args.crop, cache_path=ocr_cache)
    boundaries = find_all_boundaries(samples, gap_s=args.gap)
    ocr_times = [b.video_t for b in boundaries]

    visual_times = [] if args.ocr_only else compute_scene_score_candidates(
        args.video, scene_cache, threshold=args.scene_threshold)

    candidates = merge_candidates(ocr_times, visual_times)
    labels = load_labels(labels_path)
    todo = [(t, src) for t, src in candidates if not already_labeled(t, labels)]

    print(f"{len(candidates)} candidates total, {len(todo)} unlabeled, {len(labels)} already labeled")
    if not todo:
        print("nothing to review.")
        return

    img_path = "/tmp/_review_candidate.jpg"
    for i, (t, sources) in enumerate(todo):
        extract_pair_image(args.video, t, img_path)
        proc = subprocess.Popen(["qlmanage", "-p", img_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.35)
        print(f"\n[{i+1}/{len(todo)}] t={fmt_t(t)} ({t:.1f}s)  sources={sources}")
        print("[y] real boundary  [n] not a boundary  [m] unsure  [q] quit+save")
        k = read_key()
        proc.terminate()
        if k == "q":
            break
        verdict = {"y": "y", "n": "n", "m": "m"}.get(k, "m")
        with open(labels_path, "a") as f:
            f.write(json.dumps({"t": t, "sources": sources, "verdict": verdict}) + "\n")
        print(f"  -> recorded: {verdict}")

    print(f"\nlabels saved to {labels_path}")


if __name__ == "__main__":
    main()
