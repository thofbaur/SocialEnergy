"""Cases DSA2026 does not have but another study can: small log files, IDs above 999, no contact pairs.

Run from Postprocessing/: python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from lib import postprocessing as pp
from steps import contact_sync_analysis as cs


class DuplicateLogFiles(unittest.TestCase):
    def _files(self, contents: dict) -> list[Path]:
        folder = Path(tempfile.mkdtemp())
        for name, data in contents.items():
            (folder / name).write_bytes(data)
        return [folder / name for name in contents]

    def test_small_copy_of_the_beginning(self):
        long = b"".join(b"2026-08-14 10:58:%02d ID: 5 line %d\n" % (i % 60, i) for i in range(5000))
        a, b, c = self._files({"a.log": long[:700], "b.log": long, "c.log": b"other\n" + long[:700]})
        self.assertEqual(pp.find_duplicate_log_files([a, b, c]), [("copy of the beginning", [a, b])])

    def test_small_identical_and_empty(self):
        a, b, e = self._files({"a.log": b"x\n", "b.log": b"x\n", "e.log": b""})
        self.assertEqual(pp.find_duplicate_log_files([a, b, e]), [("identical", [a, b])])


class ContactSyncKeys(unittest.TestCase):
    def test_transitions_with_large_ids(self):
        # two encounters per (unit, partner), hours apart: one transition each
        u = np.array([0, 0, 1, 1], dtype=np.int64)
        b2 = np.array([1234, 1234, 5, 5], dtype=np.int64)
        t = np.array([0.0, 7200.0, 0.0, 7200.0])
        events = cs.transitions(np.zeros(2), np.zeros(2), u, b2, t)
        self.assertEqual(sorted(events), [(0, 1234), (1, 5)])

    def test_solve_without_pairs(self):
        c, agree, n_good = cs.solve(3, [])
        self.assertTrue((c == 0).all() and (agree == 0).all() and (n_good == 0).all())


if __name__ == "__main__":
    unittest.main()
