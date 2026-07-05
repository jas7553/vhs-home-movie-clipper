"""
_sidecar_path resolves next to the video, not CWD.
"""
import os

from split_homevideo import _sidecar_path


def test_sidecar_next_to_video_regardless_of_cwd(tmp_path, monkeypatch):
    video = str(tmp_path / "Converse 1990.mp4")
    monkeypatch.chdir("/tmp")
    cache = _sidecar_path(video, "_ocr_cache.json")
    assert cache == str(tmp_path / "Converse 1990_ocr_cache.json")
    assert os.path.dirname(cache) == str(tmp_path)


def test_sidecar_relative_video_path_resolves_to_its_own_dir():
    cache = _sidecar_path("sub/dir/Movie.mp4", "_visual_cache.json")
    assert cache == os.path.join("sub", "dir", "Movie_visual_cache.json")
