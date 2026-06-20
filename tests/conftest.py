import subprocess
import unittest.mock as mock

import pytest

_BLOCKED_BINARIES = {"ffmpeg", "ffprobe", "ocr_timestamp"}


def _guarded_run(original):
    """Wrap subprocess.run to fail fast when a test calls an external binary without mocking."""
    def _run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args:
            binary = str(args[0]).split("/")[-1]
            if binary in _BLOCKED_BINARIES:
                raise RuntimeError(
                    f"Unit test called '{binary}' without mocking. "
                    f"Use mock.patch('split_homevideo.extract_frame') or similar, "
                    f"or mark the test @pytest.mark.slow."
                )
        return original(args, *a, **kw)
    return _run


@pytest.fixture(autouse=True)
def block_external_binaries(request):
    """Block ffmpeg/ffprobe/ocr_timestamp in unit tests to catch CI environment mismatches."""
    if "slow" in request.keywords:
        yield
        return
    with mock.patch("subprocess.run", side_effect=_guarded_run(subprocess.run)), \
         mock.patch("subprocess.Popen", side_effect=_guarded_run(subprocess.Popen)):
        yield


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-slow", action="store_true", default=False, help="run slow integration tests")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-slow"):
        skip = pytest.mark.skip(reason="pass --run-slow to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip)
