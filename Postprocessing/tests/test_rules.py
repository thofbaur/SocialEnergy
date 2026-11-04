"""The ID bug and the tolerance of the hard rules.

Run from Postprocessing/: python -m unittest discover -s tests
"""

import csv
import math
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from lib import postprocessing as pp
from steps import base_mislabel_check as bmc
from steps import hard_rule_check as hrc


class IdBug(unittest.TestCase):
    def test_before_the_fix_only(self):
        if pp.ID_BUG_UNTIL is None:
            self.skipTest("the study's logger has no ID bug")
        until = pp.ID_BUG_UNTIL
        self.assertTrue(pp.id_bug_applies(until - timedelta(seconds=1)))
        self.assertFalse(pp.id_bug_applies(until))                      # strictly before
        self.assertFalse(pp.id_bug_applies(until + timedelta(days=1)))
        self.assertTrue(pp.id_bug_applies(until.timestamp() - 1))       # also as a timestamp
        self.assertFalse(pp.id_bug_applies(until.timestamp()))

    def test_switched_off(self):
        with mock.patch.object(pp, "ID_BUG_UNTIL", None):
            self.assertFalse(pp.id_bug_applies(pp.FLASH_START))

    def test_only_logged_0_to_9(self):
        self.assertEqual(sorted(pp.ID_BUG_LOW), list(range(10)))
        self.assertEqual(sorted(d + pp.ID_BUG_OFFSET for d in pp.ID_BUG_LOW), list(range(48, 58)))


class Eps(unittest.TestCase):
    def test_scalar_and_array_agree(self):
        uptimes = [0.0, 1.0, 3600.0, pp.DAY_S, 7.5 * pp.DAY_S, 2 ** 24 - 1]
        self.assertEqual(hrc.eps_array(np.array(uptimes)).tolist(), [hrc.eps(u) for u in uptimes])
        self.assertEqual(hrc.eps_array(np.array(uptimes, dtype=np.int64)).tolist(), [hrc.eps(u) for u in uptimes])

    def test_values(self):
        self.assertEqual(hrc.eps(0), hrc.TOL_S)
        self.assertEqual(hrc.eps(-5), hrc.TOL_S)                            # no negative uptime
        self.assertAlmostEqual(hrc.eps(2 * pp.DAY_S), hrc.TOL_S + 2 * hrc.TOL_PER_DAY_S)
        self.assertTrue(math.isnan(hrc.eps_array(np.array([np.nan]))[0]))  # no Current Timer: no eps



class NoModuleState(unittest.TestCase):
    """Settings passed along instead of kept in module globals: two uses in one process do not mix."""

    def test_undecided(self):
        if pp.ID_BUG_UNTIL is None:
            self.skipTest("the study's logger has no ID bug")
        start = pp.ID_BUG_UNTIL - timedelta(hours=1)
        inside = bmc.Readout("5", "a.log", 10, start, last=20)
        other = bmc.Readout("5", "a.log", 30, start, last=40)
        path = Path(tempfile.mkdtemp()) / "id_split_analysis.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, ["Log file", "First line", "Last line", "ID1 analysis"])
            w.writeheader()
            w.writerow({"Log file": "a.log", "First line": 12, "Last line": 15, "ID1 analysis": ""})
            w.writerow({"Log file": "a.log", "First line": 30, "Last line": 40, "ID1 analysis": "53"})
        corrected = bmc.Undecided.load(path)
        self.assertEqual((corrected.ambiguous(inside, "5"), corrected.ambiguous(other, "5")), (True, False))
        self.assertTrue(bmc.Undecided.load(None).ambiguous(other, "5"))      # uncorrected logs: always
        self.assertFalse(corrected.ambiguous(inside, "15"))                    # not an ID 0-9
        late = bmc.Readout("5", "a.log", 10, pp.ID_BUG_UNTIL, last=20)
        self.assertFalse(bmc.UNCORRECTED.ambiguous(late, "5"))                 # after the cutoff

    def test_location_tags_by_file(self):
        folder = Path(tempfile.mkdtemp())
        a, b = folder / "a.csv", folder / "b.csv"
        a.write_text("ID;Room\n3;kitchen\n7;hall\n", encoding="utf-8")
        b.write_text("ID,Room\n9,office\n", encoding="utf-8")
        self.assertEqual(pp.location_tags(a), frozenset({3, 7}))
        self.assertEqual(pp.location_tags(b), frozenset({9}))
        self.assertEqual(pp.location_tags(folder / "missing.csv"), frozenset())

    def test_solve_without_pool(self):
        from steps import uptime_model as um
        with mock.patch.object(um, "solve_beacon", side_effect=lambda *job: list(job)):
            self.assertEqual(um.solve_beacons([(1, 2), (3, 4)]), [[1, 2], [3, 4]])     # no pool: in this process



class Precision(unittest.TestCase):
    """The time precision of a record: the best over the period's readouts of (its class + drift term
    x distance), not the best class of the period plus the distance to the nearest readout."""

    def model(self, cts, classes, h4_move=0.0):
        from dating import model_dating as mdt
        m = mdt.Model.__new__(mdt.Model)
        key = ("1", "1")
        m.refs = {key: (np.array(cts, dtype=float), np.array(classes, dtype=float))}
        m._envelopes, m.own_width, m.stops = {}, {}, {}
        m.h4_move = {key: h4_move} if h4_move else {}
        m.drift_terms = {"1": 1.5 / pp.DAY_S}
        return m, key

    def brute(self, cts, classes, t, floor=0.0):
        return min(max(c, floor) + 1.5 / pp.DAY_S * abs(t - ct) for ct, c in zip(cts, classes))

    def test_far_from_the_delay_free_readout(self):
        day = pp.DAY_S
        cts, classes = [0, 10 * day], [13.0, 28.0]           # delay-free at the start, log order 10 days on
        m, key = self.model(cts, classes)
        self.assertAlmostEqual(m.precision(key, 0), 13.0)
        self.assertAlmostEqual(m.precision(key, 10 * day), 28.0)   # before: 13 (best class) + 0
        self.assertAlmostEqual(m.precision(key, 5 * day), 13.0 + 7.5)

    def test_envelope_equals_brute_force(self):
        rng = np.random.default_rng(1)
        for _ in range(20):
            n = int(rng.integers(1, 12))
            cts = np.sort(rng.uniform(0, 20 * pp.DAY_S, n)).tolist()
            classes = rng.choice([13.0, 16.0, 28.0, 40.0, 90.0], n).tolist()
            move = float(rng.choice([0.0, 20.0]))
            m, key = self.model(cts, classes, move)
            t = rng.uniform(-pp.DAY_S, 21 * pp.DAY_S, 50)
            got = m._envelope(key, t)
            for ti, g in zip(t, got):
                self.assertAlmostEqual(g, self.brute(cts, classes, ti, move), places=6)


if __name__ == "__main__":
    unittest.main()
