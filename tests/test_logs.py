"""RotatingWriter's timestamping (task 000306).

The rotation itself is exercised where it was built; what is new here
is the stamp, which exists because six service restarts were logged
with no indication of when.
"""

from __future__ import annotations

import re

from mechbench_runner.logs import RotatingWriter

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S")


class TestStamping:
    def test_every_line_gets_a_time(self, tmp_path):
        w = RotatingWriter(tmp_path / "x.log", max_bytes=1 << 20)
        w.write("one\ntwo\n")
        w.write("three\n")
        lines = (tmp_path / "x.log").read_text().splitlines()
        assert len(lines) == 3
        assert all(STAMP.match(ln) for ln in lines)

    def test_a_partial_line_is_stamped_once(self, tmp_path):
        # print() often writes the text and the newline separately; the
        # continuation must not get a second stamp mid-line.
        w = RotatingWriter(tmp_path / "x.log", max_bytes=1 << 20)
        w.write("progress: ")
        w.write("done")
        w.write("\n")
        (line,) = (tmp_path / "x.log").read_text().splitlines()
        assert STAMP.match(line)
        assert line.endswith("progress: done")
        assert line.count(line[:10]) == 1  # the date appears exactly once
