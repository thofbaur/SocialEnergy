#!/usr/bin/env python3
"""Clock stops: a beacon falls silent and its uptime stands still (Postprocessing Method.md
section 19.3).

A beacon whose 32 kHz clock stops while it sleeps does nothing until the clock runs again: no
advertising, no scanning, no uptime. Then it goes on where it stopped, without a reboot (beacon
15, 2026-08-14). Its records after the stop are dated too early by the length of the stop on the
period's boot. Detection, per worn beacon (run after uptime_model.py, before timer_step_check.py):

1. Silence: no partner record of the beacon for at least GAP_MIN_S, after at least two partners
   recorded it at least twice in the GROUP_WINDOW_S before (its group). Unexpected: at the rate
   it was recorded in the GROUP_WINDOW_S before, at least EXPECTED_MIN records would have fallen
   into the silence (in eco mode, with a scan burst every 5 min, a silence of 15 min is normal).
   Active group: the group members recorded each other during the silence at least ACTIVE_MIN of
   their rate before (they kept scanning).
2. Rejected as a reset: a boot of the beacon in the uptime model within the silence (+- RESET_TOL_S).
3. Its own records in the same period after the silence (at least MIN_CHECK in the CHECK_S of
   its timer after the stop) are needed: without them a silence cannot be told from a beacon that
   was shielded or out of range, and nothing is to be shifted (listed, not accepted). Among the
   silences that can be checked, the clock ran on in almost all (shielding by the body is common).
4. Shift test: the beacon's records from the stop on are shifted against the partners' reverse
   records (a reverse record within MATCH_S). The stop must explain them: the best shift within
   STOP_TOL_S of the silence matches at least MIN_GAIN more of them than no shift and lies
   MIN_PROMINENCE above the median over all shifts from -SHIFT_MARGIN_S / 4 to the silence +
   SHIFT_MARGIN_S. The shift is that best one.
5. H1 with the shift: every later readout of the period (Current Timer after the stop) must still
   not lie after its own log line - the shifts of the period up to it at most its delay + eps.
   Readouts that keep their boot refute a stop (rejected if the shift would have to be cut by more
   than STOP_TOL_S); a smaller excess is cut from the shift.

The stop lies at the beacon's timer of its last sighting before the silence (the timers of its own
records run on without a gap). Stops of one period add up: a record with timer t is dated
    boot + t (1 + drift) + sum of the shifts of the period's stops before t
(model_dating.py).

Outputs (quality/ subfolder of --output-dir):
- clock_stops.csv    every candidate: beacon, period, silence, group, own records checked, matched
                     shares, smallest delay of the later readouts, timer at the stop, shift, verdict
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import hard_rule_check as hrc
from lib import postprocessing as pp
from steps import uptime_model as um

CLOCK_STOPS_CSV = "clock_stops.csv"
GAP_MIN_S = 15 * 60
GROUP_WINDOW_S = 600
GROUP_EDGE_S = 180              # the group's activity is measured inside the silence this far from its ends
EXPECTED_MIN = 100
ACTIVE_MIN = 0.5
RESET_TOL_S = 60
MIN_CHECK = 30
CHECK_S = 3600                  # own records checked: this much of the beacon's timer after the stop
MATCH_S = 30
SHIFT_STEP_S = 10
SHIFT_MARGIN_S = 1200
STOP_TOL_S = 180
MIN_GAIN, MIN_PROMINENCE = 0.10, 0.10
STOP = "stop"
HEADER = ["ID", "Period", "Silent from", "Seen again", "Silence [min]", "Group", "Records expected",
          "Group active", "Group seeing it again", "Own records checked", "Matched at shift 0",
          "Matched at shift", "Matched median", "Smallest later delay [s]", "Timer at the stop",
          "Shift [s]", "Verdict"]


def _fmt(t: float) -> str:
    return datetime.fromtimestamp(round(t)).strftime(pp.TIMESTAMP_FORMAT)


def read_stops(path: Path) -> dict[tuple[str, str], list[tuple[float, float, float, float]]]:
    """Accepted stops per (ID, period): [(timer at the stop, shift [s], silent from, seen again)],
    sorted by timer (empty if the file is missing)."""
    out = collections.defaultdict(list)
    if path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if r["Verdict"] == STOP:
                out[(r["ID"], r["Period"])].append((float(r["Timer at the stop"]), float(r["Shift [s]"]),
                                                     um._ts(r["Silent from"]), um._ts(r["Seen again"])))
    for v in out.values():
        v.sort()
    return dict(out)


class Records:
    """The symmetric contacts of all readouts with a period, dated with the uptime model: per
    beacon the times its partners recorded it (and by whom), per (recorder, seen) pair the times."""

    def __init__(self, readouts, assign, boots, drift_of, rd, b2, tm):
        n = len(readouts)
        boot_r = np.full(n, np.nan)
        drift_r = np.zeros(n)
        for i, k in assign.items():
            if k in boots:
                boot_r[i] = boots[k]
                drift_r[i] = drift_of(readouts[i]["ID"])
        id1_r = np.array([int(r["ID"]) if r["ID"].isdigit() else -1 for r in readouts], dtype=np.int64)
        real = boot_r[rd] + tm * (1 + drift_r[rd])
        keep = np.isfinite(real) & (id1_r[rd] >= 0) & (id1_r[rd] != b2)
        self.rd, self.tm, self.real = rd[keep], tm[keep], real[keep]
        self.id1, self.id2 = id1_r[self.rd], b2[keep]
        self.base = int(max(self.id1.max(initial=0), self.id2.max(initial=0))) + 1     # key = id1 * base + id2
        self.pair = self._split(self.id1 * self.base + self.id2, self.real)
        order = np.lexsort((self.real, self.id2))
        s_id2, s_real, s_id1 = self.id2[order], self.real[order], self.id1[order]
        cuts = np.flatnonzero(np.diff(s_id2)) + 1
        self.seen = {int(s_id2[a]): (s_real[a:e], s_id1[a:e])
                     for a, e in zip(np.r_[0, cuts], np.r_[cuts, len(s_id2)])}

    @staticmethod
    def _split(code: np.ndarray, t: np.ndarray) -> dict[int, np.ndarray]:
        order = np.lexsort((t, code))
        sc, st = code[order], t[order]
        cuts = np.flatnonzero(np.diff(sc)) + 1
        return {int(sc[a]): st[a:e] for a, e in zip(np.r_[0, cuts], np.r_[cuts, len(sc)])}

    def pair_times(self, recorder: int, seen: int) -> np.ndarray:
        if not (0 <= recorder < self.base and 0 <= seen < self.base):
            return np.empty(0)
        return self.pair.get(recorder * self.base + seen, np.empty(0))

    def count(self, recorder: int, seen: int, a: float, z: float) -> int:
        t = self.pair_times(recorder, seen)
        return int(np.searchsorted(t, z) - np.searchsorted(t, a))


def match_shares(rec: Records, b: int, own_real: np.ndarray, own_id2: np.ndarray, shifts: np.ndarray) -> np.ndarray:
    """Share of the beacon's records (dated own_real) with a reverse record within MATCH_S, per shift."""
    hits = np.zeros(len(shifts))
    for p in np.unique(own_id2):
        rev = rec.pair_times(int(p), b)
        if not len(rev):
            continue
        x = own_real[own_id2 == p][None, :] + shifts[:, None]
        hits += (np.searchsorted(rev, x + MATCH_S, side="right") > np.searchsorted(rev, x - MATCH_S)).sum(axis=1)
    return hits / max(len(own_real), 1)


def find_stops(readouts, assign, boots, drift_of, rec: Records, tags: set[int]) -> list[list]:
    periods_of = collections.defaultdict(list)                     # beacon -> [(boot, key)]
    for k, t in boots.items():
        if k[0].isdigit():
            periods_of[int(k[0])].append((t, k))
    for v in periods_of.values():
        v.sort()
    later = collections.defaultdict(list)                          # key -> [(Current Timer, delay, eps)]
    for i, r in enumerate(readouts):
        k = assign.get(i)
        if k in boots and r["ct"] is not None and r["header"] is not None:
            delay = r["header"] - (boots[k] + r["ct"] * (1 + drift_of(r["ID"])))
            later[k].append((r["ct"], delay, hrc.eps(r["ct"])))
    # (beacon, period) -> indices of its contacts, via an integer code per readout
    key_list = sorted(set(assign.values()))
    code_of = {k: c for c, k in enumerate(key_list)}
    code_r = np.full(len(readouts), -1, dtype=np.int64)
    for i, k in assign.items():
        code_r[i] = code_of[k]
    code = code_r[rec.rd]
    order = np.argsort(code, kind="stable")
    sorted_code = code[order]
    cuts = np.flatnonzero(np.diff(sorted_code)) + 1
    own_of = {key_list[int(sorted_code[a])]: order[a:e]
              for a, e in zip(np.r_[0, cuts], np.r_[cuts, len(order)]) if sorted_code[a] >= 0}
    rows = []
    for b, (times, who) in sorted(rec.seen.items()):
        if b in tags or b not in periods_of:
            continue
        drift = drift_of(str(b))
        cum: dict = collections.defaultdict(float)                  # shifts of the stops accepted so far, per period
        for g in np.flatnonzero(np.diff(times) >= GAP_MIN_S):
            g0, g1 = float(times[g]), float(times[g + 1])
            if g0 < pp.FLASH_START.timestamp() or g0 > pp.LATEST_RECORD.timestamp():
                continue
            lo = np.searchsorted(times, g0 - GROUP_WINDOW_S, side="right")
            counts = collections.Counter(who[lo:g + 1].tolist())
            group = [p for p, n in counts.items() if n >= 2]
            silence = g1 - g0
            expected = (g - lo + 1) / GROUP_WINDOW_S * silence
            if len(group) < 2 or expected < EXPECTED_MIN:
                continue
            pairs = [(x, y) for x in group for y in group if x != y]
            rate_before = sum(rec.count(x, y, g0 - GROUP_WINDOW_S, g0) for x, y in pairs) / GROUP_WINDOW_S
            rate_during = sum(rec.count(x, y, g0 + GROUP_EDGE_S, g1 - GROUP_EDGE_S) for x, y in pairs) / (
                silence - 2 * GROUP_EDGE_S)
            active = rate_during / rate_before if rate_before > 0 else 0.0
            if active < ACTIVE_MIN:
                continue
            hi = np.searchsorted(times, g1 + GROUP_WINDOW_S)
            back = len(set(who[g + 1:hi].tolist()) & set(group))
            row = [b, "", _fmt(g0), _fmt(g1), round(silence / 60, 1), len(group), round(expected), round(active, 2),
                   back, 0, "", "", "", "", "", "", ""]
            if any(g0 - RESET_TOL_S <= t <= g1 + RESET_TOL_S for t, _ in periods_of[b]):
                row[-1] = "no stop: reset (boot in the silence)"
                rows.append(row)
                continue
            before = [k for t, k in periods_of[b] if t <= g0]
            if not before:
                continue
            k = before[-1]
            row[1] = k[1]
            tau = (g0 - boots[k] - cum[k]) / (1 + drift)
            row[14] = round(tau)
            idx = own_of.get(k, np.empty(0, dtype=int))
            sel = idx[(rec.tm[idx] > tau) & (rec.tm[idx] <= tau + CHECK_S)] if len(idx) else idx
            row[9] = int(len(sel))
            if len(sel) < MIN_CHECK:
                row[-1] = "not checkable: no own records after the silence"
                rows.append(row)
                continue
            shifts = np.arange(-SHIFT_MARGIN_S / 4, silence + SHIFT_MARGIN_S, SHIFT_STEP_S, dtype=float)
            share = match_shares(rec, b, rec.real[sel] + cum[k], rec.id2[sel], shifts)
            near = np.abs(shifts - silence) <= STOP_TOL_S
            jb = int(np.flatnonzero(near)[np.argmax(share[near])])
            s0, sb, bg = share[int(np.argmin(np.abs(shifts)))], share[jb], float(np.median(share))
            row[10:13] = [round(s0, 2), round(sb, 2), round(bg, 2)]
            if sb - s0 < MIN_GAIN or sb - bg < MIN_PROMINENCE:
                row[-1] = "no stop: clock ran on" if sb - s0 < MIN_GAIN else "no stop: the silence does not explain its records"
                rows.append(row)
                continue
            shift = float(shifts[jb])
            # H1 with the shift: the later readouts of the period must not lie after their log line
            bound = min((d + e for ct, d, e in later.get(k, []) if ct > tau), default=np.inf)
            row[13] = round(bound) if np.isfinite(bound) else ""
            excess = cum[k] + shift - bound
            if excess > STOP_TOL_S:
                row[-1] = "no stop: later readouts keep the boot (H1)"
                rows.append(row)
                continue
            if excess > 0:
                shift -= excess
            row[15], row[-1] = round(shift), STOP
            cum[k] += shift
            rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Clock stops: beacon silent, uptime standing still.")
    pp.add_common_arguments(parser)
    args = parser.parse_args(argv)
    t0 = time.time()
    tz = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    quality = args.output_dir / pp.OUTPUT_QUALITY_DIR
    quality.mkdir(parents=True, exist_ok=True)
    readouts = hrc.load_readouts(tz)
    drifts, centre = um.read_drifts(tz)
    drift_of = lambda b: drifts.get(b, centre)
    print(f"Clock stop check: {len(readouts)} readouts; reading the contacts ...", flush=True)
    rd, b2, tm, _, _ = um.read_contacts(args.log_dir, readouts, drift_of)
    index = {(r["file"], r["first"]): i for i, r in enumerate(readouts)}
    assign = {}
    for name in (um.READOUTS_CSV, um.HEADERLESS_CSV):
        for r in csv.DictReader((tz / name).open(encoding="utf-8")):
            i = index.get((r["Log file"], int(r["First line"])))
            if i is not None and r["Period"]:
                assign[i] = (r["ID"], r["Period"])
    # the uptime model's own boots: the check runs before h4_enforce.py, which rewrites Boot and keeps the
    # model's in "Boot before H4" (h4_enforce.BOOT_BEFORE) - so a second run gives the same result
    boots = {(r["ID"], r["Period"]): um._ts(r.get("Boot before H4") or r["Boot"])
             for r in csv.DictReader((tz / um.PERIODS_CSV).open(encoding="utf-8"))}
    tags = {int(x) for x in pp.location_tags() if str(x).isdigit()}
    rec = Records(readouts, assign, boots, drift_of, rd, b2, tm)
    rows = find_stops(readouts, assign, boots, drift_of, rec, tags)
    with (quality / CLOCK_STOPS_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    verdicts = collections.Counter(r[-1] for r in rows)
    print(f"  {len(rows)} unexpected silences of worn beacons in an active group: "
          + "; ".join(f"{k} {v}" for k, v in sorted(verdicts.items())))
    for r in rows:
        if r[-1] == STOP:
            print(f"  {r[0]} p{r[1]} {r[2]} - {r[3][11:]}: stop, shift {r[15]} s from timer {r[14]}")
    print(f"  written to {quality / CLOCK_STOPS_CSV} ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
