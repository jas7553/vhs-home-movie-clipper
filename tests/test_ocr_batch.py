"""
ocr_batch() wire-format: each line from the binary is "<path>\t<text>".
Lines without a tab are silently skipped (e.g. binary noise or error lines).
"""
import unittest.mock as mock

from split_homevideo import ocr_batch


class TestOcrBatchParsing:
    def test_tab_separated_lines_parsed(self):
        stdout = "/tmp/frame_001.bmp\tsome text\n/tmp/frame_002.bmp\t5:01 PM\n"
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout=stdout)
            result = ocr_batch(["/tmp/frame_001.bmp", "/tmp/frame_002.bmp"])
        assert result == {"/tmp/frame_001.bmp": "some text", "/tmp/frame_002.bmp": "5:01 PM"}

    def test_lines_without_tab_ignored(self):
        stdout = "no tab here\n/tmp/frame.bmp\tgood line\n"
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout=stdout)
            result = ocr_batch(["/tmp/frame.bmp"])
        assert result == {"/tmp/frame.bmp": "good line"}

    def test_empty_paths_skips_subprocess(self):
        with mock.patch("subprocess.run") as m:
            result = ocr_batch([])
        m.assert_not_called()
        assert result == {}

    def test_empty_stdout_returns_empty(self):
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout="")
            result = ocr_batch(["/tmp/frame.bmp"])
        assert result == {}

    def test_first_tab_only_splits(self):
        # text field may itself contain tabs — only the first \t is the delimiter
        stdout = "/tmp/frame.bmp\ttext\twith\ttabs\n"
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout=stdout)
            result = ocr_batch(["/tmp/frame.bmp"])
        assert result == {"/tmp/frame.bmp": "text\twith\ttabs"}
