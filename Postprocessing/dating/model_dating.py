#!/usr/bin/env python3
"""Dating of all records with the uptime model (Postprocessing Method.md section 21).

    real time = boot of the record's uptime period + timer x (1 + drift of the period)
                + the shifts of the period's clock stops before the timer

with the periods, boots and drifts of uptime_model.py, as corrected by h4_enforce.py (drift of a
period without one there: the beacon's from contact_sync_analysis.py; all in the timer_zero/
subfolder of --output-dir) and the clock stops of clock_stop_check.py
(quality/clock_stops.csv: the beacon was silent and its uptime stood still, Postprocessing
Method.md section 19.3); run run_pipeline.py or the scripts in its order first. Every record is
kept and gets a time precision and a status.

Period of a record:
- Contacts: the period of their readout (a reboot erases all stored contacts, BI 3.4.1).
- Self-reports and eco sessions (kept in flash across a reboot, read out later): the period of
  their readout if they fit it (real time not after the readout's line time + eps), otherwise the
  latest earlier period of the beacon they fit (not after the boot that ended it + eps).
- Readouts without Current Timer: the period the uptime model assigned (timer_zero/
  uptime_model_headerless.csv: same period before and after, the only possible one, or the one
  its contacts support); otherwise not datable, with the model's reason.

Time precision [s] (95 %, from the validation in section 19.1): the best over the period's readouts
of (how that readout fixes the clock + the drift term between it and the record) - a delay-free
readout PRECISION_S["delay-free"], a readout with a reliable contact boot PRECISION_S["contacts"],
the other readouts of a period with readouts between references of other beacons in the same log
file PRECISION_S["log order"] (person-worn beacons PRECISION_S["log order, person"]: they move
between groups, test A of the validation 95 % 38 s against 21 s for room tags), otherwise the delay
cost alone PRECISION_S["delay"]; the drift term per day of timer between the readout's Current Timer
and the record's timer (Model.drift_term: DRIFT_TERM_S_PER_DAY, or DRIFT_SD_FACTOR x the drift
uncertainty of the beacon in contact_sync_drift.csv where larger) - plus STOP_PRECISION_S per clock
stop before the record's timer. (Until 2026-11-04: the best class of the period, wherever its
readout lay, plus the drift term from the nearest readout of any class; that was up to 15 s too
optimistic for the location tags.) A period whose boot the H4 enforcement moved
(quality/h4_enforce.csv) has at least the move as the precision of each readout: the move shows how
far its evidence was off. The boot precision of a period (reboots.csv) stays its best class.

Status:
    ok                                  dated, nothing known against it
    fake                                built by the logger from the bytes of other messages (BI 3.2.2)
    unreliable time (two clocks in one period)
                                        the period's records run on two clocks (timer lost time without
                                        reboot, not explained; timer_step_check.py)
    ID undecided (0-9 / 48-57)          readout whose ID1 stayed undecided (id_correction/id_split_analysis.csv)
    invalid: <check>                    ID2 / RSSI out of range; dated before the beacons were flashed
                                        (postprocessing.FLASH_START) or after LATEST_RECORD (no time,
                                        not_dated.csv); recorded after its own log line + eps
                                        (H1 for the single record: its timer is corrupt)
    duplicate                           the same contact (ID2, timer, RSSI) was already received in an
                                        earlier readout of the same uptime period (sent twice, BI 3.1.4)
    not datable: <reason>               no period (no time; written to not_dated.csv)

All records are written whatever their time (the observation window belongs to the evaluation);
with a valid_span (generate_output.py --valid-date-start / --valid-date-end) records outside it are
not written (counted).
"""

from __future__ import annotations

import bisect
import collections
import csv
import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from steps import battery_replacement_check as bc
from steps import clock_stop_check as csc
from steps import hard_rule_check as hrc
from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza
from steps import uptime_model as um

PRECISION_S = {"delay-free": 13.0, "contacts": 16.0, "log order": 28.0, "log order, person": 40.0, "delay": 90.0}


def log_order_precision(kind: str) -> float:
    """Precision of a boot from log order by beacon type (um.beacon_types)."""
    return PRECISION_S["log order, person"] if kind == "person" else PRECISION_S["log order"]
DRIFT_TERM_S_PER_DAY = hrc.TOL_PER_DAY_S   # drift term of the precision per day of timer, at least (eps) ...
DRIFT_SD_FACTOR = 2.0            # ... or this times the beacon's drift uncertainty (95 %) where larger
H4_ENFORCE_CSV = "h4_enforce.csv"         # written by h4_enforce.py (quality/)
STOP_PRECISION_S = 60.0   # per clock stop: its shift is the silence (+- the partners' sighting interval),
                          # cut by H1 at the later readouts (beacon 15: 8 583 s against about 8 540 s
                          # from the delays of its neighbours in the log)
SILENT = "silent (clock stopped)"
NOT_DATED_CSV = "not_dated.csv"   # contacts, self-reports and eco sessions without a time (column Record)
ID_SPLIT_CSV = tza.ID_SPLIT_CSV
COVERAGE_CSV = "coverage.csv"
UNREAD_CSV = "unread_uptime_periods.csv"
STALLS_CSV = "logger_stalls.csv"
STALL_GAP_S = 1800      # a gap in a log file at least this long ...
BURST_S = 60            # ... followed within this by a readout with Current Timer read out before the gap
                        # (PC receive buffer, logged on waking up; BI 3.2.3 / 3.2.4)
REBOOTS_CSV = "reboots.csv"
BATTERY_CSV = "battery.csv"
LOST_COLUMNS = ["Lost from", "Lost to", "Where", "Largest Contact Count", "Partner records", "Readout before",
                "Readout after", "During a logger stall", "Gap from", "Gap to", "Gap hours", "Own records expected",
                "Readout threshold"]     # the evidence of a lost-records gap, in coverage.csv on its pieces
LOST_VALIDATION_CSV = "lost_records_validation.csv"
_VALIDATION_ROWS: list = []
LOST_MARGIN_S = 120     # partner records counted inside a gap only this far from its ends (precision) ...
LOST_MIN_PARTNER = 10   # ... and at least this many: the beacon was among others, its records are missing
LOST_EDGE_S = 60        # lost interval: first to last partner sighting in the gap, widened by this (timing precision)
HOLE_MIN_S = 600        # a hole inside a readout: own contacts this far apart (scans every 7 s) ...
ECO_MARGIN_S = 300      # ... not within this of an eco session of the beacon (one scan burst per 5 min)
BETWEEN, INSIDE, START = "between readouts", "inside a readout", "start of a readout"
BEFORE_FIRST = "before the first boot"
BEFORE_FIRST_PARTNERS = 3  # ... seen by at least this many partners (not one partner's misdated readout)
START_NEWEST_S = 600    # start of a readout: its newest contact at most this before its real time
FULL_COUNT = pp.STUDY.storage_full_count   # Contact Count from which the contact flash is full (BI 3.4.2; study.toml)
DROPPED_FULL, NOT_LOGGED = "dropped (storage full)", "read out, not logged"
NOT_RECORDED = "not recorded (no readout possible)"
RHO_MIN_PARTNERS = 100  # own records per partner record: from a readout's delivered range with at least this many partner records
THRESHOLD_MARGIN_S = 3600  # readout threshold of a gap: smallest Contact Count of the readouts logged this close to it
READOUT_QUANTILE = 99   # %: factor of the expected own records, calibrated on the logged readouts
ERASED = "erased by reboot"
NOT_SEEN = "not seen (off or alone)"
SEEN_GAP_S = 1800       # erased by reboot only where the partners saw the beacon: sightings this close form one stretch

OK, FAKE, UNRELIABLE, UNDECIDED = "ok", "fake", "unreliable time (two clocks in one period)", "ID undecided (0-9 / 48-57)"
INVALID_ID2, INVALID_RSSI = "invalid: ID2 out of range", "invalid: RSSI out of range"
INVALID_TIMER = "invalid: timer after its log line (H1)"
DUPLICATE = "duplicate"
OUTSIDE = (f"invalid: dated before the flashing ({pp.FLASH_START:%Y-%m-%d %H:%M}) "
           f"or after {pp.LATEST_RECORD:%Y-%m-%d %H:%M}")
NO_PERIOD = "not datable: readout has no period in the uptime model"
HEADERLESS = "not datable: readout without Current Timer, "
FITS_NO_PERIOD = "not datable: fits no uptime period of the beacon"
# contact statuses in the vectorised dating, by index
_ST = [OK, DUPLICATE, INVALID_TIMER, INVALID_RSSI, INVALID_ID2, UNDECIDED, UNRELIABLE, FAKE]


class Model:
    """Periods, boots, drifts and the period of every readout."""

    def __init__(self, output_dir: Path):
        tz = output_dir / pp.OUTPUT_TIMER_ZERO_DIR
        for name in (um.PERIODS_CSV, um.READOUTS_CSV, tza.OVERVIEW_CSV):
            if not (tz / name).exists():
                raise SystemExit(f"{tz / name} is missing: run run_pipeline.py (or the uptime scripts) first")
        self.readouts = hrc.load_readouts(tz)
        drifts, centre = um.read_drifts(tz)
        self.drift = lambda b: drifts.get(b, centre)
        # drift term of the precision per beacon [s per s of timer]
        self.drift_terms = {}
        path = tz / "contact_sync_drift.csv"
        if path.exists():
            for r in csv.DictReader(path.open(encoding="utf-8")):
                if r.get("Drift sd [s/day]"):
                    self.drift_terms[r["ID"]] = max(DRIFT_TERM_S_PER_DAY, DRIFT_SD_FACTOR * float(r["Drift sd [s/day]"])) / pp.DAY_S
        self.period_drift = {}                   # (ID, period) -> drift of the period (uptime model, H4)
        self.boot, self.reliable, self.log_order = {}, {}, {}
        self.kinds = um.beacon_types(um.PERSONS_CSV, um.ROOMS_CSV)
        for r in csv.DictReader((tz / um.PERIODS_CSV).open(encoding="utf-8")):
            key = (r["ID"], r["Period"])
            self.boot[key] = um._ts(r["Boot"])
            if r.get("Drift [s/day]"):
                self.period_drift[key] = float(r["Drift [s/day]"]) / pp.DAY_S
            self.reliable[key] = int(r["Reliable contact boots"] or 0)
            self.log_order[key] = int(r.get("Log-order readouts") or 0)
        period_of, reliable_readout = {}, set()
        for r in csv.DictReader((tz / um.READOUTS_CSV).open(encoding="utf-8")):
            period_of[(r["Log file"], int(r["First line"]))] = (r["ID"], r["Period"])
            if r.get("Reliable") == "yes":                  # a reliable contact boot from this readout
                reliable_readout.add((r["Log file"], int(r["First line"])))
        self.period = [period_of.get((r["file"], r["first"])) for r in self.readouts]
        # readouts without Current Timer assigned by the uptime model (from their contacts)
        self.headerless, self.headerless_reason, self.own_width = {}, {}, {}
        path = tz / um.HEADERLESS_CSV
        if path.exists():
            index = {(r["file"], r["first"]): i for i, r in enumerate(self.readouts)}
            for r in csv.DictReader(path.open(encoding="utf-8")):
                i = index.get((r["Log file"], int(r["First line"])))
                if i is None:
                    continue
                if r["Period"] and (r["ID"], r["Period"]) in self.boot:
                    self.headerless[i] = (r["ID"], r["Period"])
                elif r.get("Own period boot"):
                    # a period of its own (never read out with Current Timer), boot from the partners
                    key = (r["ID"], f"own {r['Log file']}:{r['First line']}")
                    self.boot[key] = um._ts(r["Own period boot"])
                    self.reliable[key] = 0
                    self.own_width[key] = (um._ts(r["Own period boot to"]) - um._ts(r["Own period boot from"])) / 2
                    self.headerless[i] = key
                else:
                    self.headerless_reason[i] = r["Method"].removeprefix("undecided: ")
        self.anchor_periods = set()
        refs = collections.defaultdict(list)                     # key -> [(Current Timer, its class [s])]
        self.by_beacon = collections.defaultdict(list)          # beacon -> readout indices with a period
        for i, (r, k) in enumerate(zip(self.readouts, self.period)):
            if k is None:
                continue
            self.by_beacon[r["ID"]].append(i)
            if r["first_in_file"] or (r["header"] and r["header"] >= tza.ANCHOR_FROM.timestamp()):
                self.anchor_periods.add(k)
                cls = PRECISION_S["delay-free"]
            elif (r["file"], r["first"]) in reliable_readout:
                cls = PRECISION_S["contacts"]
            elif self.log_order.get(k):
                cls = log_order_precision(self.kinds.get(k[0], "other"))
            else:
                cls = PRECISION_S["delay"]
            if r["ct"] is not None:
                refs[k].append((r["ct"], cls))
        self.refs = {k: (np.array([c for c, _ in v], dtype=float), np.array([p for _, p in v], dtype=float))
                     for k, v in ((k, sorted(v)) for k, v in refs.items())}
        self._envelopes: dict = {}
        self.periods_of = collections.defaultdict(list)          # beacon -> keys sorted by boot
        for k in sorted(self.boot, key=lambda k: self.boot[k]):
            self.periods_of[k[0]].append(k)
        self.lost_max, self.lost_min = {}, {}             # set_accounting
        # clock stops: key -> [(timer at the stop, shift, silent from, seen again)], sorted by timer
        self.stops = csc.read_stops(output_dir / pp.OUTPUT_QUALITY_DIR / csc.CLOCK_STOPS_CSV)
        # boots moved by the H4 enforcement: key -> |move| [s]
        self.h4_move = {}
        path = output_dir / pp.OUTPUT_QUALITY_DIR / H4_ENFORCE_CSV
        if path.exists():
            for r in csv.DictReader(path.open(encoding="utf-8")):
                if r["Kind"] == "period changed" and r["Move [s]"]:
                    self.h4_move[(r["ID"], r["Period"])] = abs(float(r["Move [s]"]))

    def set_accounting(self, lines: logcache.Lines) -> None:
        """Contacts a readout with Contact Count lost at least / at most (timer_zero_analysis.contact_accounting,
        section 13.8) on the uptime model's periods: received in later readouts and the reboot before
        the next readout as the model has them (readout_overview.csv has them on the timer-zero
        analysis' periods)."""
        rank = {k: n + 1 for keys in self.periods_of.values() for n, k in enumerate(keys)}   # by boot
        # a readout without Current Timer and without contacts (mostly a single Status line) carries
        # nothing: it is no "next readout" of its period (as in the timer-zero analysis, 13.5)
        of = lines.readout_of([(r["file"], r["first"], r["last"]) for r in self.readouts])
        has_contacts = np.bincount(of[(lines.kind == 1) & (of >= 0)], minlength=len(self.readouts)) > 0
        items = [SimpleNamespace(beacon_id=r["ID"],
                                 period=rank.get(self.period[i] or (self.headerless.get(i) if has_contacts[i] else None), 0),
                                 start_time=r["start"] if r["start"] is not None else -math.inf,
                                 current_timer=r["ct"], contact_count=r["count"], source=SimpleNamespace(name=r["file"]),
                                 first_line=r["first"], last_line=r["last"])
                 for i, r in enumerate(self.readouts)]
        acc = tza.contact_accounting(lines, items)
        for i, item in enumerate(items):
            a = acc[id(item)]
            if a.lost_max is not None:
                self.lost_max[i], self.lost_min[i] = float(a.lost_max), float(a.lost_min or 0)

    def drift_term(self, beacon: str) -> float:
        """Drift uncertainty of the beacon as a time precision per second of timer (95 %)."""
        return self.drift_terms.get(beacon, DRIFT_TERM_S_PER_DAY / pp.DAY_S)

    def drift_of(self, key) -> float:
        return self.period_drift.get(key, self.drift(key[0]))

    def _stops_before(self, key, timer: float) -> list:
        return [s for s in self.stops.get(key, ()) if s[0] < timer]

    def real(self, key, timer: float) -> float:
        return (self.boot[key] + timer * (1 + self.drift_of(key))
                + sum(s[1] for s in self._stops_before(key, timer)))

    def real_array(self, key, timers: np.ndarray) -> np.ndarray:
        """real() for an array of timers."""
        t = np.asarray(timers, dtype=np.float64)
        out = self.boot[key] + t * (1 + self.drift_of(key))
        for tau, shift, *_ in self.stops.get(key, ()):
            out = out + np.where(t > tau, shift, 0.0)
        return out

    def real_keys(self, keys: list, k: np.ndarray, timers: np.ndarray) -> np.ndarray:
        """real() for arrays: period keys[k] and timer, element by element."""
        t = timers.astype(np.float64)
        boot = np.array([self.boot[x] for x in keys])
        scale = np.array([1 + self.drift_of(x) for x in keys])
        shift = np.zeros(len(t))
        for n, rows in _groups(k, [n for n, x in enumerate(keys) if self.stops.get(x)]):
            for tau, s, *_ in self.stops[keys[n]]:
                shift[rows] += np.where(t[rows] > tau, s, 0.0)
        return boot[k] + t * scale[k] + shift

    def readout_real(self, i: int) -> float:
        return self.real(self.period[i], self.readouts[i]["ct"])

    def next_boot(self, key) -> float:
        keys = self.periods_of[key[0]]
        j = keys.index(key)
        return self.boot[keys[j + 1]] if j + 1 < len(keys) else math.inf

    def precision(self, key, timer: float) -> float:
        if key in self.own_width:                  # own period: half the width of the fitted peak
            return round(PRECISION_S["contacts"] + self.own_width[key], 1)
        return round(float(self._envelope(key, np.array([float(timer)]))[0])
                     + STOP_PRECISION_S * len(self._stops_before(key, timer)), 1)

    def _envelope(self, key, t: np.ndarray) -> np.ndarray:
        """Best over the period's readouts r of (class of r, at least the H4 move) + drift term x
        |t - Current Timer of r|: the lower envelope of these cones, from both sides of t. A period
        without a readout with Current Timer: its boot precision."""
        ref = self.refs.get(key)
        if ref is None or not len(ref[0]):
            return np.full(len(t), self.boot_precision(key))
        cts, cls = ref
        s = self.drift_term(key[0])
        if key not in self._envelopes:
            c = np.maximum(cls, self.h4_move.get(key, 0.0))
            left, right = c.copy(), c.copy()               # best cone value at each Current Timer
            for j in range(1, len(c)):                     # ... from the readouts at or before it
                left[j] = min(left[j], left[j - 1] + s * (cts[j] - cts[j - 1]))
            for j in range(len(c) - 2, -1, -1):            # ... from the readouts at or after it
                right[j] = min(right[j], right[j + 1] + s * (cts[j + 1] - cts[j]))
            self._envelopes[key] = (left, right)
        left, right = self._envelopes[key]
        j = np.searchsorted(cts, t, side="right") - 1      # the last readout at or before t
        jl, jr = np.maximum(j, 0), np.minimum(j + 1, len(cts) - 1)
        from_left = np.where(j >= 0, left[jl] + s * (t - cts[jl]), np.inf)
        from_right = np.where(j + 1 < len(cts), right[jr] + s * (cts[jr] - t), np.inf)
        return np.minimum(from_left, from_right)

    def precision_keys(self, keys: list, k: np.ndarray, timers: np.ndarray) -> np.ndarray:
        """precision() for arrays: period keys[k] and timer, element by element."""
        t = timers.astype(np.int64)
        out = np.empty(len(t))
        for n, rows in _groups(k, range(len(keys))):
            key, tt = keys[n], t[rows]
            if key in self.own_width:
                out[rows] = PRECISION_S["contacts"] + self.own_width[key]
                continue
            stops = np.zeros(len(tt), dtype=np.int64)
            for tau, *_ in self.stops.get(key, ()):
                stops += tt > tau
            out[rows] = self._envelope(key, tt.astype(float)) + STOP_PRECISION_S * stops
        values, inverse = np.unique(out, return_inverse=True)
        return np.array([round(v, 1) for v in values.tolist()])[inverse]

    def boot_precision(self, key) -> float:
        """Precision of the boot itself: by how it is fixed, at least the H4 enforcement's move."""
        if key in self.own_width:
            return round(PRECISION_S["contacts"] + self.own_width[key], 1)
        if key in self.anchor_periods:
            base = PRECISION_S["delay-free"]
        elif self.reliable.get(key):
            base = PRECISION_S["contacts"]
        elif self.log_order.get(key):
            base = log_order_precision(self.kinds.get(key[0], "other"))
        else:
            base = PRECISION_S["delay"]
        return max(base, self.h4_move.get(key, 0.0))

    def neighbours(self, i: int):
        """The beacon's readouts with a period right before and after readout i (log order)."""
        items = self.by_beacon.get(self.readouts[i]["ID"], [])
        j = bisect.bisect_left(items, i)
        prev = items[j - 1] if j > 0 else None
        nxt = items[j] if j < len(items) else None
        return prev, nxt

    def stored_period(self, key, timers: tuple, line_time: float):
        """Period of a self-report / eco session (timers) read out in period key, or None."""
        e = hrc.eps(max(timers))
        if all(self.real(key, t) <= line_time + e for t in timers):
            return key
        keys = self.periods_of[key[0]]
        for k in reversed(keys[:keys.index(key)]):
            end = self.next_boot(k)
            if all(self.real(k, t) <= end + e for t in timers):
                return k
        return None


def _groups(k: np.ndarray, wanted):
    """(n, indices of k == n) for the n in wanted that occur in k."""
    order = np.argsort(k, kind="stable")
    ks = k[order]
    for n in wanted:
        a, b = np.searchsorted(ks, n, "left"), np.searchsorted(ks, n, "right")
        if b > a:
            yield n, order[a:b]


def _line_gaps(name: str, numbers: np.ndarray, times: np.ndarray, gaps: list, file_start: dict) -> None:
    """Gaps of at least STALL_GAP_S between consecutive lines of one log file (times: line times in
    file order), appended to gaps as [file, line, time before, time after, time after the burst]: the
    time of the first line more than BURST_S after the first gap still open (None if none)."""
    if not len(times):
        return
    file_start.setdefault(name, float(times[0]))
    open_gaps, pos = [], 0
    for k in (np.flatnonzero(np.diff(times) >= STALL_GAP_S) + 1).tolist() + [len(times)]:
        if open_gaps:
            hit = np.flatnonzero(times[pos:k + 1] > open_gaps[0][3] + BURST_S)
            if len(hit):
                for g in open_gaps:
                    g[4] = float(times[pos + hit[0]])
                open_gaps = []
        if k < len(times):
            gaps.append([name, int(numbers[k]), float(times[k - 1]), float(times[k]), None])
            open_gaps.append(gaps[-1])
            pos = k + 1


def _undecided(output_dir: Path) -> set[tuple[str, int]]:
    path = output_dir / pp.OUTPUT_ID_CORRECTION_DIR / ID_SPLIT_CSV
    if not path.exists():
        return set()
    return {(r["Log file"], int(r["First line"])) for r in csv.DictReader(path.open(encoding="utf-8"))
            if not r["ID1 analysis"]}


def _fakes(output_dir: Path) -> set[tuple[str, int]]:
    path = output_dir / pp.OUTPUT_TIMER_ZERO_DIR / um.FAKES_CSV
    return {(r["Log file"], int(r["Line"])) for r in csv.DictReader(path.open(encoding="utf-8"))} \
        if path.exists() else set()


def _contact_key(id2: int, timer: int, rssi: int) -> int:
    return ((id2 & 0x7FFF) << 40) | ((timer & 0xFFFFFFFF) << 8) | (rssi & 0xFF)


def run(log_dir: Path, model_dir: Path, out: Path, valid_span: pp.ValidTimeSpan | None = None,
        validate_lost: int = 0, keep: dict | None = None) -> None:
    """Dates the records of the logs in log_dir with the uptime model in model_dir (an output
    directory of the uptime scripts: timer_zero/, quality/, id_correction/) and writes them to out.
    With keep (a dict): only the contacts, per contact line, into keep (reports/without_pipeline.py);
    nothing is written."""
    out.mkdir(parents=True, exist_ok=True)
    output_dir = model_dir
    model = Model(output_dir)
    fakes = _fakes(output_dir)
    unreliable = pp.read_unreliable_time_csv(output_dir / pp.OUTPUT_QUALITY_DIR / pp.DEFAULT_UNRELIABLE_TIME_CSV)
    undecided = _undecided(output_dir)
    earlier = collections.defaultdict(list)      # period -> [(timer min, timer max, sorted contact keys)]
    rec_span = {}                                # readout -> real time of its oldest and newest contact
    rec_times = {}                               # readout -> sorted real times of its contacts (distinct timers)
    gaps, file_start = [], {}                    # [file, line, time before, time after, time after the burst]
    lo, hi = (valid_span.start.timestamp(), valid_span.end.timestamp()) if valid_span else (-math.inf, math.inf)
    first_ok, last_ok = pp.FLASH_START.timestamp(), pp.LATEST_RECORD.timestamp()
    statuses: list[str] = []
    code = {}

    def status_code(s: str) -> int:
        if s not in code:
            code[s] = len(statuses)
            statuses.append(s)
        return code[s]

    # contacts in columns (tens of millions)
    not_dated_contacts, self_reports, not_dated_reports, eco, not_dated_eco = [], [], [], [], []
    counts = collections.Counter()
    last_real = {}                                   # period -> latest real time of a readout or record
    for i, k in enumerate(model.period):
        if k is not None:
            last_real[k] = max(last_real.get(k, -math.inf), model.readout_real(i))

    by_file = collections.defaultdict(list)
    for i, r in enumerate(model.readouts):
        by_file[r["file"]].append((r["first"], r["last"], i))
    lines = logcache.Lines([log_dir / name for name in sorted(by_file) if (log_dir / name).exists()])
    names = [p.name for p in lines.paths]
    of = lines.readout_of([(r["file"], r["first"], r["last"]) for r in model.readouts])
    model.set_accounting(lines)
    fake, unreliable_line = lines.mask_of(fakes), lines.mask_of(unreliable)
    contact = lines.kind == 1
    local = logcache.local_timestamps(lines.t)       # line times
    # duplicates: the same contact (ID2, timer, RSSI) in an earlier readout of the same period
    contact_key = ((lines.id2.astype(np.int64) & 0x7FFF) << 40) | ((lines.timer.astype(np.int64) & 0xFFFFFFFF) << 8) \
        | (lines.rssi.astype(np.int64) & 0xFF)
    duplicate = np.zeros(len(lines.line), dtype=bool)
    usable = contact & ~fake
    for f, name in enumerate(names):
        a0, numbers = lines.start[f], lines.line[lines.start[f]:lines.start[f + 1]]
        numbers64 = numbers.astype(np.int64)        # searchsorted with an int then needs no copy of the array
        for first, last, i in sorted(by_file[name]):
            key = model.period[i] or model.headerless.get(i)
            if key is None:
                continue
            a = a0 + int(np.searchsorted(numbers64, first, "left"))
            sel = a + np.flatnonzero(usable[a:a0 + int(np.searchsorted(numbers64, last, "right"))])
            if not len(sel):
                continue
            keys_arr = contact_key[sel]
            timers = (keys_arr >> 8) & 0xFFFFFFFF
            tmin, tmax = int(timers.min()), int(timers.max())
            for emin, emax, ekeys in earlier[key]:
                if emax < tmin or emin > tmax:
                    continue
                duplicate[sel[np.isin(keys_arr, ekeys)]] = True
            earlier[key].append((tmin, tmax, np.unique(keys_arr)))
            rec_span[i] = (model.real(key, tmin), model.real(key, tmax))
            rec_times[i] = model.real_array(key, np.unique(timers))
        _line_gaps(name, numbers, local[a0:lines.start[f + 1]], gaps, file_start)
    del contact_key, usable

    # per readout (index len(readouts): a line outside every readout)
    n_r = len(model.readouts)
    period_key = [model.period[i] or model.headerless.get(i) for i in range(n_r)]
    keys = list(dict.fromkeys(k for k in period_key if k is not None))
    index = {k: n for n, k in enumerate(keys)}
    r_key = np.array([-1 if k is None else index[k] for k in period_key] + [-1], dtype=np.int64)
    r_undecided = np.array([(r["file"], r["first"]) in undecided for r in model.readouts] + [False])
    r_reason = [NO_PERIOD if model.period[i] is None and r["ct"] is not None else
                HEADERLESS + model.headerless_reason.get(i, "no readout of the beacon with Current Timer")
                for i, r in enumerate(model.readouts)] + [NO_PERIOD]

    # every contact: status, real time, precision (vectorised; the same rules as the records below)
    c = np.flatnonzero(contact)
    ri = np.where(of[c] >= 0, of[c], n_r)
    k = r_key[ri]
    tm, id2, rssi = lines.timer[c].astype(np.int64), lines.id2[c], lines.rssi[c]
    dated = k >= 0
    when = np.full(len(c), np.nan)
    when[dated] = model.real_keys(keys, k[dated], tm[dated])
    st = np.full(len(c), _ST.index(OK), dtype=np.int8)
    st[duplicate[c]] = _ST.index(DUPLICATE)
    st[dated & (when > local[c] + hrc.eps_array(tm))] = _ST.index(INVALID_TIMER)
    st[(rssi < pp.DEFAULT_VALID_RSSI_MIN) | (rssi > pp.DEFAULT_VALID_RSSI_MAX)] = _ST.index(INVALID_RSSI)
    st[~pp.valid_beacon_ids(id2)] = _ST.index(INVALID_ID2)
    st[r_undecided[ri]] = _ST.index(UNDECIDED)
    st[unreliable_line[c]] = _ST.index(UNRELIABLE)
    st[fake[c]] = _ST.index(FAKE)
    in_range = dated & (first_ok <= when) & (when <= last_ok)      # corrupt timer: no time, kept as not dated
    if keep is not None:
        r_id1 = np.array([int(r["ID"]) if r["ID"].isdigit() else -1 for r in model.readouts] + [-1], dtype=np.int16)
        keep.update(names=names, file=lines.file[c], line=lines.line[c], id2=id2, timer=tm, rssi=rssi,
                    id1=r_id1[ri], when=np.where(in_range, when, np.nan), status=st, statuses=list(_ST))
        return
    counts["contacts not datable"] += int((~dated).sum())
    counts["contacts " + OUTSIDE] += int((dated & ~in_range).sum())
    counts["contacts outside the window"] += int((in_range & ~((lo <= when) & (when <= hi))).sum())
    counts += collections.Counter()                  # drops the zero counts (only events are counted)
    for j in np.flatnonzero(~in_range).tolist():
        n, s = int(c[j]), _ST[st[j]]
        why = OUTSIDE if dated[j] else r_reason[ri[j]]
        not_dated_contacts.append([model.readouts[ri[j]]["ID"] if ri[j] < n_r else lines.ids[lines.id1[n]],
                                   str(int(id2[j])), int(rssi[j]), int(tm[j]), names[lines.file[n]],
                                   int(lines.line[n]), why if s == OK else f"{s}; {why}"])
    ok = in_range & ((st == _ST.index(OK)) | (st == _ST.index(DUPLICATE)))
    if ok.any():
        best = np.full(len(keys), -math.inf)
        np.maximum.at(best, k[ok], when[ok])
        for n in np.flatnonzero(best > -math.inf).tolist():
            last_real[keys[n]] = max(last_real.get(keys[n], -math.inf), float(best[n]))
    window = in_range & (lo <= when) & (when <= hi)
    j = np.flatnonzero(window)
    sts, first_seen = np.unique(st[j], return_index=True)
    code_of = np.zeros(len(_ST), dtype=np.int8)
    for s in sts[np.argsort(first_seen)].tolist():
        code_of[s] = status_code(_ST[s])
        counts[f"contacts {_ST[s]}"] += int((st[j] == s).sum())
    c_id1 = np.array([int(r["ID"]) if r["ID"].isdigit() else 0 for r in model.readouts] + [0], dtype=np.int16)[ri[j]]
    c_id2, c_rssi, c_time = id2[j].astype(np.int16), rssi[j].astype(np.int16), when[j]
    c_prec = model.precision_keys(keys, k[j], tm[j]).astype(np.float32)
    c_status = code_of[st[j]]
    del c, ri, k, tm, id2, rssi, when, st, dated, in_range, ok, window, j

    # self-reports and eco sessions (their lines in log order)
    for n in np.flatnonzero(~contact).tolist():
        rest = lines.rest[lines.other[n]]
        m_s = pp.SELF_REPORT_RE.match(rest)
        m_e = None if m_s else pp.ECO_SESSION_RE.match(rest)
        if not (m_s or m_e):
            continue
        name, number, line_time = names[lines.file[n]], int(lines.line[n]), float(local[n])
        i = int(of[n]) if of[n] >= 0 else None
        r = model.readouts[i] if i is not None else None
        beacon = r["ID"] if r else lines.ids[lines.id1[n]]
        key = None if i is None else (model.period[i] or model.headerless.get(i))
        reason = r_reason[n_r if i is None else i]
        flag = None
        if fake[n]:
            flag = FAKE
        elif unreliable_line[n]:
            flag = UNRELIABLE
        elif r is not None and (name, r["first"]) in undecided:
            flag = UNDECIDED
        timers = (int(m_s.group("timer")),) if m_s else (int(m_e.group("enter")), int(m_e.group("leave")))
        kind = "self-reports" if m_s else "eco sessions"
        k = model.stored_period(key, timers, line_time) if key is not None else None
        status = flag or OK
        if k is None:
            why = reason if key is None else FITS_NO_PERIOD
            row = [beacon, *timers, name, number, why if status == OK else f"{status}; {why}"]
            (not_dated_reports if m_s else not_dated_eco).append(row)
            counts[f"{kind} not datable"] += 1
            continue
        when = [model.real(k, t) for t in timers]
        if not all(first_ok <= w <= last_ok for w in when):
            row = [beacon, *timers, name, number, OUTSIDE if status == OK else f"{status}; {OUTSIDE}"]
            (not_dated_reports if m_s else not_dated_eco).append(row)
            counts[f"{kind} {OUTSIDE}"] += 1
            continue
        if status == OK:
            last_real[k] = max(last_real.get(k, -math.inf), *when)
        if status == OK and any(w > line_time + hrc.eps(t) for w, t in zip(when, timers)):
            status = INVALID_TIMER
        if not all(lo <= w <= hi for w in when):
            counts[f"{kind} outside the window"] += 1
            continue
        prec = max(model.precision(k, t) for t in timers)
        (self_reports if m_s else eco).append([beacon, *when, prec, status])
        counts[f"{kind} {status}"] += 1

    _write_contacts(out, c_id1, c_id2, c_rssi, c_time, c_prec, c_status, statuses, valid_span)
    _write_events(out / pp.DEFAULT_SELF_REPORTS_CSV, ["ID", "Local Time"], self_reports)
    _write_events(out / pp.DEFAULT_ECO_SESSIONS_CSV, ["ID", "Enter Local Time", "Exit Local Time"], eco)
    # one file for the three kinds: self-reports and eco sessions have ID1 only, eco sessions an exit timer
    _write_rows(out / NOT_DATED_CSV, ["Record", "ID1", "ID2", "RSSI", "Timer", "Exit Timer", "Log file", "Line",
                                      "Status"],
                [["contact", *r[:4], "", *r[4:]] for r in not_dated_contacts]
                + [["self-report", r[0], "", "", r[1], "", *r[2:]] for r in not_dated_reports]
                + [["eco session", r[0], "", "", r[1], r[2], *r[3:]] for r in not_dated_eco])
    quality = model_dir / pp.OUTPUT_QUALITY_DIR      # diagnostics of the dating: not data for the evaluation
    stalls = logger_stalls(model, gaps, file_start)
    _write_rows(quality / STALLS_CSV, ["From", "To", "Hours", "Log file", "Line", "Last line before",
                                   "Buffered readouts"],
                [[_fmt_local(a), _fmt_local(z), round((z - a) / 3600, 2), f, n, _fmt_local(t0), " ".join(ids)]
                 for a, z, f, n, t0, ids in stalls])
    print(f"Logger stalls ({quality / STALLS_CSV}): {len(stalls)}")
    # coverage over the window, or without one over the whole data (first boot to last readout)
    span = [model.boot[k] for k in last_real]
    c_lo = lo if valid_span else min(span)
    c_hi = hi if valid_span else max(last_real.values())
    ok_codes = [code[x] for x in (OK, DUPLICATE) if x in code]
    index = _partner_index(c_id1, c_id2, c_rssi, c_time, c_status, ok_codes)
    seen_of = lambda b: _partner_times(index[0], index[1], b) if str(b).isdigit() else np.zeros(0)
    lost = lost_records(model, rec_span, rec_times, eco, index, c_id1, c_time, c_status, ok_codes, stalls,
                        validate_lost)
    if validate_lost:
        _write_rows(quality / LOST_VALIDATION_CSV, ["ID", "Period", "Log file", "First line", "End of the readout before",
                                                "Removed readout real time", "Hours", "Partner records",
                                                "Detectable", "Found", "Gap start - expected [s]",
                                                "Gap end - removed readout [s]", "Lost interval end - removed readout [s]",
                                                "Lost interval end - partners' last sighting [s]"], _VALIDATION_ROWS)
    by_cause = collections.Counter()
    for x in lost:
        by_cause[(x[8], x[12])] += (x[3] - x[2]) / 3600
    print(f"Own records missing (coverage.csv, columns {LOST_COLUMNS[0]} ..): {len(lost)} gaps; "
          + ", ".join(f"{c} ({w}) {h:.0f} h" for (c, w), h in sorted(by_cause.items())))
    # own records delivered up to: a complete readout with Current Timer up to its real time, any other
    # up to its newest contact (the rest stayed on the beacon: read out later, or erased by a reboot)
    delivered = {}
    for i, r in enumerate(model.readouts):
        k = model.period[i] or model.headerless.get(i)
        if k is None:
            continue
        if model.period[i] is not None and r["complete"]:
            end = model.readout_real(i)
        elif i in rec_span:
            end = rec_span[i][1]
        else:
            continue
        delivered[k] = max(delivered.get(k, -math.inf), end)
    rows = coverage(model, last_real, delivered, eco, model_dir, c_lo, c_hi, lost, seen_of)
    _write_rows(out / COVERAGE_CSV, ["ID", "Beacon type", "From", "To", "Hours", "State", "Period", "Eco",
                                     *LOST_COLUMNS], rows)
    hours = collections.Counter()
    for r in rows:
        hours[r[5]] += r[4]
    print(f"Coverage ({out / COVERAGE_CSV}): {len({r[0] for r in rows})} beacons, {len(rows)} intervals; hours "
          + ", ".join(f"{k} {v:.0f}" for k, v in sorted(hours.items())))
    rows = reboots(model, last_real, delivered, model_dir, seen_of)
    _write_rows(out / REBOOTS_CSV, ["ID", "Beacon type", "Period", "Boot Local Time", "Time precision [s]", "Cause",
                                    "Own records erased from", "Own records erased [h]"], rows)
    causes = collections.Counter(r[5].split(" 20")[0] + (" (never read out)" if "never read out" in r[5] else "")
                                 for r in rows)
    print(f"Reboots ({out / REBOOTS_CSV}): {len(rows)}; " + ", ".join(f"{k} {v}" for k, v in sorted(causes.items())))
    rows = battery(model, undecided)
    _write_rows(out / BATTERY_CSV, ["ID", "Local Time", "Time precision [s]", "Battery (mV)", "Contact Count", "Status"],
                rows)
    print(f"Battery ({out / BATTERY_CSV}): {len(rows)} readings of {len({r[0] for r in rows})} beacons")
    print(f"Dated with the uptime model ({model_dir / pp.OUTPUT_TIMER_ZERO_DIR}); written to {out}:")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")


def reboots(model: Model, last_real, delivered, model_dir: Path, seen_of=None) -> list[list]:
    """Every boot of a beacon, in time order: the boots of the uptime periods with a dated record
    (the same as in coverage) and the boots of the periods never read out (quality/
    unread_uptime_periods.csv, no precision: only bounded by the partners' sightings). Cause:
    first boot; battery replacement <documented time> if a documented replacement of the beacon
    (battery_replacements.csv) has its window (battery_replacement_check.window, up to LATE_S
    late) around the boot - the nearest boot per replacement; otherwise reset. Own records erased:
    from the end of the previous period's delivered records (all of a period never read out) to
    the boot - the beacon's stored records of that time are lost (BI 3.4.1) -, with seen_of only
    where the partners saw the beacon (as erased by reboot in coverage): from the first such stretch,
    the hours of all of them."""
    unread = collections.defaultdict(list)
    path = model_dir / pp.OUTPUT_QUALITY_DIR / UNREAD_CSV
    if path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            unread[r["ID"]].append((um._ts(r["From"]), r["Cause"]))
    replaced = collections.defaultdict(list)             # beacon -> [(time, window from, window to, text)]
    for r in bc.read_replacements():
        t = datetime.strptime(r["Battery replaced"], "%Y-%m-%d %H:%M")
        a, z = bc.window(t, r["Accuracy"])
        if not any(cause.endswith(r["Battery replaced"]) for _, cause in unread.get(r["ID"], [])):
            replaced[r["ID"]].append((t.timestamp(), a.timestamp(), z.timestamp() + bc.LATE_S, r["Battery replaced"]))
    rows = []
    for b, all_keys in sorted(model.periods_of.items(), key=lambda x: pp.id_sort_key(x[0])):
        keys = [k for k in all_keys if k in last_real]
        if not b.isdigit() or not keys:
            continue
        events = [(model.boot[k], k) for k in keys] + [(t, cause) for t, cause in unread.get(b, [])]
        events.sort(key=lambda e: e[0])
        cause_of = {}
        for t, a, z, text in replaced.get(b, []):
            near = [(abs(boot - t), boot) for boot, k in events if isinstance(k, tuple) and a <= boot <= z]
            if near:
                cause_of[min(near)[1]] = f"battery replacement {text}"
        prev_end = None
        for n, (boot, k) in enumerate(events):
            if isinstance(k, tuple):
                cause = "first boot" if n == 0 else cause_of.get(boot, "reset")
                period, prec, end = k[1], model.boot_precision(k), max(boot, delivered.get(k, boot))
            else:
                cause, period, prec, end = f"{k} (period never read out)", "", "", boot
            erased = ["", ""]
            if prev_end is not None and n:
                if seen_of is None or not isinstance(k, tuple):
                    erased = [_fmt_local(prev_end), round(max(0.0, boot - prev_end) / 3600, 2)]
                else:
                    seen = [(a, z) for a, z, s in _seen_split(seen_of(b), prev_end, boot) if s]
                    erased = [_fmt_local(seen[0][0]) if seen else "",
                              round(sum(z - a for a, z in seen) / 3600, 2)]
            rows.append([b, model.kinds.get(b, "other"), period, _fmt_local(boot), prec, cause, *erased])
            prev_end = end
    return rows


def battery(model: Model, undecided) -> list[list]:
    """Battery voltage and Contact Count (contacts stored at the readout's start, BI 2.4) of every
    readout that reported a voltage (with Current Timer), at the readout's real time; Status ok or
    ID undecided (0-9 / 48-57)."""
    rows = []
    for i, r in enumerate(model.readouts):
        k = model.period[i]
        if r.get("voltage") is None or k is None:
            continue
        status = UNDECIDED if (r["file"], r["first"]) in undecided else OK
        rows.append([r["ID"], model.readout_real(i), model.precision(k, r["ct"]), r["voltage"],
                     "" if r["count"] is None else r["count"], status])
    rows.sort(key=lambda x: (pp.id_sort_key(x[0]), x[1]))
    return [[b, _fmt_local(t), *rest] for b, t, *rest in rows]


def logger_stalls(model: Model, gaps, file_start) -> list[tuple]:
    """Times the logger did not capture although the Base kept reading out (BI 3.2.4): a gap of at
    least STALL_GAP_S in a log file, followed within BURST_S by a readout with Current Timer that was
    read out before the gap (its real time at most BURST_S + eps after the last line before it). Those
    lines waited in the PC's receive buffer and were stamped on waking up; what the Base read out after
    them is lost. From: the newest real time of the buffered readouts. To: the first line logged after
    the burst (the next log file's first line if the file ends with it).
    Returns (from, to, log file, line, last line time before the gap, buffered readout IDs)."""
    names = sorted(file_start)
    stalls = []
    for name, number, t0, t1, after in gaps:
        buffered = [i for i, r in enumerate(model.readouts)
                    if r["file"] == name and model.period[i] is not None and r["header"] is not None
                    and t1 <= r["header"] <= t1 + BURST_S
                    and model.readout_real(i) <= t0 + BURST_S + hrc.eps(r["ct"])]
        if not buffered:
            continue                                       # a time without readouts (no heartbeat)
        if after is None:                                  # the file ends with the burst
            later = names[names.index(name) + 1:]
            after = file_start[later[0]] if later else t1
        start = max(model.readout_real(i) for i in buffered)
        stalls.append((start, max(start, after), name, number, t0,
                       sorted({model.readouts[i]["ID"] for i in buffered}, key=pp.id_sort_key)))
    return stalls


def _partner_index(c_id1, c_id2, c_rssi, c_time, c_status, ok_codes):
    """The partners' records of every beacon that it would have recorded as well (pp.symmetric_threshold,
    BI 2.1; status ok or duplicate): (seen beacon, real time, recording beacon), sorted by beacon and time."""
    id1_all = np.frombuffer(c_id1, dtype=np.int16).astype(np.int64)
    id2_all = np.frombuffer(c_id2, dtype=np.int16).astype(np.int64)
    ok = np.isin(np.frombuffer(c_status, dtype=np.int8), ok_codes) & (
        np.frombuffer(c_rssi, dtype=np.int16) >= pp.symmetric_threshold(id1_all, id2_all))
    id2 = id2_all[ok]                # int64: searchsorted with an int then needs no copy of the array
    t = np.frombuffer(c_time, dtype=np.float64)[ok]
    id1 = id1_all[ok]
    order = np.lexsort((t, id2))
    return id2[order], t[order], id1[order]


def lost_records(model: Model, rec_span, rec_times, eco, index, c_id1, c_time, c_status, ok_codes,
                 stalls, validate: int = 0) -> list[tuple]:
    """Own records missing although the beacon had contacts. Without a reboot the firmware deletes
    contacts only when the Base confirmed them or when the flash is full (oldest block discarded,
    BI 3.4.2). Within a period, the readouts in log order (with Current
    Timer and complete: up to its real time; incomplete or without Current Timer (assigned by the
    uptime model): up to its newest contact - the rest stays on the beacon)
    must continue each other: the next readout's oldest contact reaches back to the end of the one
    before (to the boot for the first). A gap between them was read out and not logged - or a time
    without contacts. Kept only if the partners recorded the beacon at least LOST_MIN_PARTNER times
    inside the gap (LOST_MARGIN_S from its ends), counting only records at an RSSI the beacon itself
    records too (pp.symmetric_threshold): then it had contacts, and its own records are missing.
    Cause: DROPPED_FULL if a readout of the period from the one before the gap to the one after it
    (any, also without contacts) shows Contact Count >= FULL_COUNT - the flash was full and discarded
    its oldest blocks; otherwise NOT_LOGGED - read out by the Base and not logged (BI 3.2.3 buffer
    overflow during a pause, 3.2.4 logger not capturing) - unless no readout was possible in the gap
    (_readout_test: the own records the beacon would have made there stay below the readout threshold
    of the time): NOT_RECORDED, the beacon did not record although its partners saw it.

    Holes inside a readout: in the merged timeline of all own contacts of the period, two
    consecutive contacts at least HOLE_MIN_S apart that both lie within one readout's delivered
    range (its oldest to its newest contact), not within ECO_MARGIN_S of an eco session of the
    beacon, with the same partner rule, and - for a readout with Current Timer - missing contacts by
    its Contact Count (lost at most > 0; sparse partner records around an outdoor location tag pass
    the partner rule although nothing is missing): lines lost inside the stream of a readout
    (NOT_LOGGED).

    Start of a readout: a gap before a readout with Current Timer that misses contacts by its Contact
    Count (lost at least > 0) although its delivered run reaches its newest contact (within
    START_NEWEST_S of its real time) is kept without the partner rule: the count proves the loss and
    the two delivered runs of the same clock bound it; the whole gap is lost (START).

    Before the first boot: the partners recorded the beacon at least LOST_MIN_PARTNER times more than
    LOST_MARGIN_S before its first boot, by at least BEFORE_FIRST_PARTNERS partners - it ran in an earlier uptime period that was never read out,
    and the reboot erased its records (ERASED, BEFORE_FIRST): from the partners' first sighting
    (- LOST_EDGE_S) to the first boot.

    Returns (beacon, period, from, to, partner records, readout before, readout after, during a
    logger stall, cause, largest Contact Count, gap from, gap to, where: BETWEEN / INSIDE / START /
    BEFORE_FIRST, own records expected, readout threshold (_readout_test; "" where not run))."""
    id2, t, id1 = index                     # partner records (_partner_index)
    id1_all = np.frombuffer(c_id1, dtype=np.int16).astype(np.int64)
    # the beacons' own records (any RSSI) and the Contact Count of every readout at its real time: is a
    # readout in a gap possible at all (_readout_test)?
    own_ok = np.isin(np.frombuffer(c_status, dtype=np.int8), ok_codes)
    own_id, own_t = id1_all[own_ok], np.frombuffer(c_time, dtype=np.float64)[own_ok]
    order = np.lexsort((own_t, own_id))
    own_id, own_t = own_id[order], own_t[order]
    cc = sorted((model.readout_real(i), r["count"]) for i, r in enumerate(model.readouts)
                if model.period[i] is not None and r.get("count") is not None)
    cc_t, cc_v = np.array([x[0] for x in cc]), np.array([x[1] for x in cc], dtype=np.int64)
    seq = collections.defaultdict(list)                     # period -> [(readout, start, end)] in log order
    counts = collections.defaultdict(list)                  # period -> [(readout, Contact Count)] in log order
    for i, r in enumerate(model.readouts):
        k = model.period[i] or model.headerless.get(i)
        if k is None:
            continue
        if r.get("count"):
            counts[k].append((i, r["count"]))
        if model.period[i] is not None and r["complete"]:
            seq[k].append((i, rec_span[i][0] if i in rec_span else None, model.readout_real(i)))
        elif i in rec_span:                   # incomplete: the rest stays on the beacon for the next one
            seq[k].append((i, rec_span[i][0], rec_span[i][1]))
    own_of = lambda b: own_t[np.searchsorted(own_id, int(b), "left"):np.searchsorted(own_id, int(b), "right")]
    factor = _readout_factor(model, seq, lambda b: _partner_times(id2, t, b), own_of)
    test = SimpleNamespace(own_of=own_of, cc_t=cc_t, cc_v=cc_v, factor=factor)
    out = []
    for k, items in seq.items():
        if k[0].isdigit():
            out += _period_gaps(model, k, items, _partner_times(id2, t, k[0]), counts[k], stalls, test)
    for b, keys in model.periods_of.items():
        if b.isdigit() and keys and seq.get(keys[0]):
            lo_i, hi_i = np.searchsorted(id2, int(b), "left"), np.searchsorted(id2, int(b), "right")
            out += _before_first(model, keys[0], seq[keys[0]][0][0], t[lo_i:hi_i], id1[lo_i:hi_i], stalls)
    eco_of = collections.defaultdict(list)
    for r in eco:
        eco_of[str(r[0])].append((r[1] - ECO_MARGIN_S, r[2] + ECO_MARGIN_S))
    by_period = collections.defaultdict(list)
    for i in rec_times:
        k = model.period[i] or model.headerless.get(i)
        if k is not None and k[0].isdigit():
            by_period[k].append(i)
    for k, readouts in by_period.items():
        out += _inside_holes(model, k, readouts, rec_times, _partner_times(id2, t, k[0]), eco_of[k[0]], stalls)
    out.sort(key=lambda x: (pp.id_sort_key(x[0]), x[2]))
    if validate:
        _validate_lost(model, seq, counts, id2, t, validate)
    return out


def _partner_times(id2, t, b):
    lo_i, hi_i = np.searchsorted(id2, int(b), "left"), np.searchsorted(id2, int(b), "right")
    return t[lo_i:hi_i]


def _seen_split(tb, a: float, z: float) -> list[tuple[float, float, bool]]:
    """a..z cut into the stretches where the partners saw the beacon (sightings at most SEEN_GAP_S apart,
    widened by LOST_EDGE_S) and the rest: [(from, to, seen)], covering a..z in order."""
    s = tb[np.searchsorted(tb, a, "left"):np.searchsorted(tb, z, "right")]
    pieces, cur = [], a
    if len(s):
        cut = np.flatnonzero(np.diff(s) > SEEN_GAP_S)
        for first, last in zip(np.r_[0, cut + 1].tolist(), np.r_[cut, len(s) - 1].tolist()):
            sa, sz = max(cur, float(s[first]) - LOST_EDGE_S), min(z, float(s[last]) + LOST_EDGE_S)
            if sa > cur:
                pieces.append((cur, sa, False))
            if sz > sa:
                pieces.append((sa, sz, True))
            cur = max(cur, sz)
    if cur < z:
        pieces.append((cur, z, False))
    return pieces


def _partners_in(tb, a, z) -> int:
    """Partner records of the beacon in a..z, LOST_MARGIN_S away from both ends."""
    a, z = a + LOST_MARGIN_S, z - LOST_MARGIN_S
    return int(np.searchsorted(tb, z) - np.searchsorted(tb, a)) if z > a else 0


def _before_first(model: Model, k, i, tb, partners, stalls) -> list[tuple]:
    """Partner records of the beacon before its first boot k (see lost_records); i: its first readout,
    partners: the recording beacon of each record in tb."""
    boot = model.boot[k]
    early = tb < boot - LOST_MARGIN_S
    seen = tb[early]
    if len(seen) < LOST_MIN_PARTNER or len(np.unique(partners[early])) < BEFORE_FIRST_PARTNERS:
        return []
    a = float(seen[0]) - LOST_EDGE_S
    r = model.readouts[i]
    during = [f"{_fmt_local(s0)}..{_fmt_local(s1)}" for s0, s1, *_ in stalls if s0 < boot and s1 > a]
    return [(k[0], k, a, boot, len(seen), "", f"{r['file']}:{r['first']}", " ".join(during), ERASED, 0, a, boot,
             BEFORE_FIRST, "", "")]


def _inside_holes(model: Model, k, readouts, rec_times, tb, eco_b, stalls) -> list[tuple]:
    """Holes inside the readouts of one period (see lost_records)."""
    merged = np.unique(np.concatenate([rec_times[i] for i in readouts]))
    if len(merged) < 2:
        return []
    spans = [(float(rec_times[i][0]), float(rec_times[i][-1]), i) for i in readouts if len(rec_times[i])]
    out = []
    for j in np.flatnonzero(np.diff(merged) >= HOLE_MIN_S):
        a, z = float(merged[j]), float(merged[j + 1])
        inside = [i for lo, hi, i in spans if lo <= a and z <= hi]
        if not inside or any(e0 < z and e1 > a for e0, e1 in eco_b):
            continue
        if all(model.lost_max.get(i, 1.0) <= 0 for i in inside):
            continue                    # its Contact Count says nothing is missing
        n = _partners_in(tb, a, z)
        if n < LOST_MIN_PARTNER:
            continue
        r = model.readouts[inside[0]]
        here = f"{r['file']}:{r['first']}"
        seen = tb[np.searchsorted(tb, a):np.searchsorted(tb, z, "right")]
        la, lz = max(a, float(seen[0]) - LOST_EDGE_S), min(z, float(seen[-1]) + LOST_EDGE_S)
        during = [f"{_fmt_local(s0)}..{_fmt_local(s1)}" for s0, s1, *_ in stalls if s0 < z and s1 > a]
        out.append((k[0], k, la, lz, n, here, here, " ".join(during), NOT_LOGGED, r.get("count") or 0, a, z, INSIDE,
                    "", ""))
    return out


def _rho(item, tb, own) -> float | None:
    """Own records per partner record of the beacon in the delivered range of a readout (item:
    (readout, oldest contact, end)); None with fewer than RHO_MIN_PARTNERS partner records there."""
    if item is None or item[1] is None or item[2] is None or item[2] <= item[1]:
        return None
    a, z = item[1], item[2]
    n = int(np.searchsorted(tb, z, "right") - np.searchsorted(tb, a, "left"))
    if n < RHO_MIN_PARTNERS:
        return None
    return int(np.searchsorted(own, z, "right") - np.searchsorted(own, a, "left")) / n


def _expected(tb, own, a, z, before, after) -> float | None:
    """Own records the beacon would have made in a..z: the partners' records of it there times its own
    records per partner record in the readouts before and after (the larger: in favour of a loss)."""
    rho = [x for x in (_rho(before, tb, own), _rho(after, tb, own)) if x is not None]
    if not rho:
        return None
    return int(np.searchsorted(tb, z, "right") - np.searchsorted(tb, a, "left")) * max(rho)


def _readout_factor(model: Model, seq, partners_of, own_of) -> float:
    """Calibration of _readout_test on the readouts that were logged: a complete readout with Current
    Timer right after a complete one of its period, without a gap between them, holds as Contact Count
    the contacts recorded since the one before. The READOUT_QUANTILE % quantile of its ratio to the
    expected own records (_expected, from the readouts before and after it, as for a gap) is the
    factor by which the expectation can fall short."""
    ratios = []
    for k, items in seq.items():
        if not k[0].isdigit():
            continue
        tb, own = partners_of(k[0]), own_of(k[0])
        for x in range(1, len(items) - 1):
            (p, _, p_stop), (i, start, stop) = items[x - 1], items[x]
            r, rp = model.readouts[i], model.readouts[p]
            if (model.period[i] is None or model.period[p] is None or not r["complete"] or not rp["complete"]
                    or not r.get("count") or start is None or p_stop is None
                    or start > p_stop + hrc.eps(start - model.boot[k])):
                continue
            if _partners_in(tb, p_stop, stop) < LOST_MIN_PARTNER:
                continue
            e = _expected(tb, own, p_stop, stop, items[x - 1], items[x + 1])
            if e:
                ratios.append(r["count"] / e)
    factor = float(np.percentile(ratios, READOUT_QUANTILE)) if ratios else math.inf
    print(f"Lost records, readout test: {len(ratios)} logged readouts, Contact Count / expected own records "
          f"median {np.median(ratios) if ratios else math.nan:.2f}, {READOUT_QUANTILE} % {factor:.2f} (the factor)")
    return factor


def _readout_test(tb, own, test, end, start, before, after):
    """A gap read out and not logged needs a readout in it: the Base reads a beacon only from its
    readout threshold on (BI 2.1), so the lost readout held at least the smallest Contact Count of the
    readouts logged within THRESHOLD_MARGIN_S of the gap. If even test.factor times the own records
    the beacon would have made in the gap (_expected) stays below it, no readout was possible: the
    beacon did not record (e.g. contact tracking off) although its partners saw it.
    Returns (NOT_RECORDED or None, expected own records, threshold); "" where not testable."""
    lo = np.searchsorted(test.cc_t, end - THRESHOLD_MARGIN_S, "left")
    hi = np.searchsorted(test.cc_t, start + THRESHOLD_MARGIN_S, "right")
    if hi <= lo:
        return None, "", ""
    threshold = int(test.cc_v[lo:hi].min())
    e = _expected(tb, own, end, start, before, after)
    if e is None:
        return None, "", threshold
    return (NOT_RECORDED if e * test.factor < threshold else None), round(e), threshold


def _period_gaps(model: Model, k, items, tb, counts_k, stalls, test=None) -> list[tuple]:
    """The gaps of one period (see lost_records); items: [(readout, oldest contact, end)] in log order;
    test: the readout test of the gaps read out, not logged (_readout_test; None: not run)."""
    out = []
    b = k[0]
    own = test.own_of(b) if test else None
    end, prev, prev_i, prev_item = model.boot[k], "boot", -1, None
    for i, start, stop in items:
        r = model.readouts[i]
        here = f"{r['file']}:{r['first']}"
        if start is not None and start > end + hrc.eps(start - model.boot[k]):
            n = _partners_in(tb, end, start)
            head = (model.period[i] is not None and model.lost_min.get(i, 0) > 0 and stop is not None
                    and model.readout_real(i) - stop <= START_NEWEST_S)
            if n >= LOST_MIN_PARTNER or head:
                during = [f"{_fmt_local(s0)}..{_fmt_local(s1)}" for s0, s1, *_ in stalls if s0 < start and s1 > end]
                cc = max((c for x, c in counts_k if prev_i <= x <= i), default=0)
                if n >= LOST_MIN_PARTNER:
                    # lost: where the beacon had contacts - from the partners' first to their last sighting
                    # of it in the gap; before and after it had none, so nothing of its own is missing there
                    seen = tb[np.searchsorted(tb, end):np.searchsorted(tb, start, "right")]
                    a = max(end, float(seen[0]) - LOST_EDGE_S)
                    z = min(start, float(seen[-1]) + LOST_EDGE_S)
                    where = BETWEEN
                else:                           # the Contact Count proves the loss: the whole gap
                    a, z, where = end, start, START
                cause, expected, threshold = DROPPED_FULL if cc >= FULL_COUNT else NOT_LOGGED, "", ""
                if test and cause == NOT_LOGGED and where == BETWEEN:
                    verdict, expected, threshold = _readout_test(tb, own, test, end, start, prev_item,
                                                                 (i, start, stop))
                    cause = verdict or cause
                out.append((b, k, a, z, n, prev, here, " ".join(during), cause, cc, end, start, where,
                            expected, threshold))
        if stop is not None and stop > end:
            end = stop
        prev, prev_i, prev_item = here, i, (i, start, stop)
    return out


def _validate_lost(model: Model, seq, counts, id2, t, n_cases: int, seed: int = 1) -> None:
    """Synthetic test of the lost-records check (Postprocessing Method.md 21): a complete readout with
    Current Timer, between two others of its period, is treated as not logged. Expected: a gap from
    the end of the readout before it to the oldest contact of the one after it, which covers the
    removed readout's records. Only cases without a gap there before; detectable if the partners
    recorded the beacon at least LOST_MIN_PARTNER times in the removed stretch (end of the readout
    before .. removed readout's real time). Written to LOST_VALIDATION_CSV in the output directory."""
    rng = random.Random(seed)
    cases = []
    for k, items in seq.items():
        if not k[0].isdigit():
            continue
        for x in range(1, len(items) - 1):
            i, start, stop = items[x]
            if model.period[i] is None or not model.readouts[i]["complete"] or start is None:
                continue
            cases.append((k, x))
    rows = []
    for k, x in rng.sample(cases, min(n_cases, len(cases))):
        items = seq[k]
        tb = _partner_times(id2, t, k[0])
        base = {(g[10], g[11]) for g in _period_gaps(model, k, items, tb, [], ())}
        end_before = model.boot[k]
        for _, _, stop in items[:x]:
            if stop is not None and stop > end_before:
                end_before = stop
        i, start, stop = items[x]
        nxt_start = items[x + 1][1]
        if start > end_before + hrc.eps(start - model.boot[k]) or nxt_start is None:
            continue                                             # a gap there already / nothing after
        gaps = [g for g in _period_gaps(model, k, items[:x] + items[x + 1:], tb, [], ())
                if (g[10], g[11]) not in base]
        partners = _partners_in(tb, end_before, stop)
        before_stop = tb[np.searchsorted(tb, end_before):np.searchsorted(tb, stop, "right")]
        last_seen = float(before_stop[-1]) if len(before_stop) else None   # partners' last sighting before the readout
        hit = [g for g in gaps if g[10] <= stop and g[11] >= stop - 60]   # a new gap over the removed stretch
        r = model.readouts[i]
        rows.append([k[0], k[1], r["file"], r["first"], _fmt_local(end_before), _fmt_local(stop),
                     round((stop - end_before) / 3600, 2), partners,
                     "yes" if partners >= LOST_MIN_PARTNER else "no", "yes" if hit else "no",
                     round(hit[0][10] - end_before, 1) if hit else "", round(hit[0][11] - stop, 1) if hit else "",
                     round(hit[0][3] - stop, 1) if hit else "",
                     round(hit[0][3] - last_seen, 1) if hit and last_seen is not None else ""])
    det = [r for r in rows if r[8] == "yes"]
    found = [r for r in det if r[9] == "yes"]
    false_hits = [r for r in rows if r[8] == "no" and r[9] == "yes"]
    to_err = np.array(sorted(r[11] for r in found)) if found else np.array([0.0])
    lost_err = np.array(sorted(r[12] for r in found)) if found else np.array([0.0])
    seen_err = np.array(sorted(r[13] for r in found if r[13] != "")) if found else np.array([0.0])
    q = lambda a, f: a[int(f * (len(a) - 1))] if len(a) else 0.0
    print(f"Lost-records validation: {len(rows)} readouts treated as not logged; detectable (>= "
          f"{LOST_MIN_PARTNER} partner records) {len(det)}, found {len(found)} ({len(found) / max(len(det), 1):.0%}); "
          f"found although not detectable {len(false_hits)}; after the removed readout's real time: gap end "
          f"median {np.median(to_err):.0f} s, 90 % {q(to_err, 0.9):.0f} s; lost interval end median "
          f"{np.median(lost_err):.0f} s, 10 / 90 % {q(lost_err, 0.1):.0f} / {q(lost_err, 0.9):.0f} s; lost interval "
          f"end - partners' last sighting before the readout: 10 / 50 / 90 % {q(seen_err, 0.1):.0f} / "
          f"{q(seen_err, 0.5):.0f} / {q(seen_err, 0.9):.0f} s")
    _VALIDATION_ROWS[:] = rows


def coverage(model: Model, last_real, delivered, eco, model_dir: Path, lo: float, hi: float, lost=(),
             seen_of=None) -> list[list]:
    """Per beacon, non-overlapping intervals covering lo..hi - the window if one is given, else the
    whole data from the first boot to the last readout (Postprocessing Method.md 21):

    recorded            boot of an uptime period to the end of its delivered records (delivered: a
                        complete readout up to its real time, an incomplete one or one without
                        Current Timer up to its newest contact): the beacon's own records of this
                        time exist
    erased by reboot    end of the delivered records of a period to the next boot: recorded, but
                        erased by the reboot (also the rest of an incomplete last readout; and an
                        earlier period before the first boot, seen by the partners, lost_records)
    never read out      an uptime period started by a battery replacement and never read out
                        (quality/unread_uptime_periods.csv)
    not read out        after the end of the beacon's delivered records to the end (hi)
    before first boot   start (lo) to the beacon's first boot (or to the partners' first sighting of it)
    no data             beacon in list_persons.csv / list_rooms.csv without any uptime period
    dropped (storage full)
                        within a period: a gap between consecutive readouts in which the partners
                        recorded the beacon, with full contact flash (lost_records): oldest discarded
    read out, not logged
                        the same without full storage: read out by the Base and not logged
    not recorded (no readout possible)
                        the same, but no readout was possible in it (lost_records, _readout_test):
                        the beacon did not record although its partners saw it
    silent (clock stopped)
                        a clock stop (clock_stop_check.py): the beacon neither recorded nor was
                        seen, from its last sighting before the stop to its first after it
    not seen (off or alone)
                        within erased by reboot: where the partners did not see the beacon (_seen_split,
                        with seen_of: the partners' records of a beacon). Off, or without company: it had
                        no contacts there (BI 2.1), so the reboot erased nothing of that time

    Each interval is cut where an eco session of the beacon (reduced scanning) starts or ends; the
    column Eco marks the pieces. Logger outages as such are not marked: the logger writes only during
    readouts and has no heartbeat; what they cost shows as lost records.

    The pieces of a lost-records gap (lost_records; all but silent ones) carry its evidence, LOST_COLUMNS:
    the lost interval, where it lies, the partner records in it, the readouts around it and so on - per
    gap, repeated on each of its pieces (cut by eco sessions, clock stops, seen stretches)."""
    lost_of = collections.defaultdict(list)
    evidence = collections.defaultdict(list)
    for b, k, a, z, n, ra, rb, st, cause, cc, ga, gz, where, ex, thr in lost:
        lost_of[k].append((a, z, cause))
        evidence[b].append((a, z, [_fmt_local(a), _fmt_local(z), where, cc if cc else "", n, ra, rb, st,
                                   _fmt_local(ga), _fmt_local(gz), round((gz - ga) / 3600, 2), ex, thr]))
    none = [""] * len(LOST_COLUMNS)
    kinds = um.beacon_types(um.PERSONS_CSV, um.ROOMS_CSV)
    unread = collections.defaultdict(list)
    path = model_dir / pp.OUTPUT_QUALITY_DIR / UNREAD_CSV
    if path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            unread[r["ID"]].append((um._ts(r["From"]), um._ts(r["To"])))
    eco_of = collections.defaultdict(list)
    for r in eco:
        eco_of[r[0]].append((r[1], r[2]))
    beacons = set(model.periods_of) | {b for b in kinds if b.isdigit()}
    rows = []
    for b in sorted(beacons, key=pp.id_sort_key):
        base = []
        keys = [k for k in model.periods_of.get(b, []) if k in last_real]
        if not keys:
            base.append((lo, hi, "no data", ""))
        else:
            first = model.boot[keys[0]]
            # partners saw it before its first boot: an earlier period, never read out, erased (lost_records)
            pre = min((a for a, z, cause in lost_of.get(keys[0], []) if z <= first), default=first)
            if pre > lo:
                base.append((lo, pre, "before first boot", ""))
            if pre < first:
                base.append((pre, first, ERASED, ""))
            for n, k in enumerate(keys):
                nxt = model.boot[keys[n + 1]] if n + 1 < len(keys) else hi
                # the delivered records can reach a few seconds past the next boot (within the precision
                # of the boots): the next period starts there
                start, end = model.boot[k], min(max(model.boot[k], delivered.get(k, model.boot[k])), max(nxt, model.boot[k]))
                cur = start
                for a, z, cause in sorted(lost_of.get(k, [])):
                    if cur <= a < end:
                        base.append((cur, a, "recorded", k[1]))
                        base.append((a, min(z, end), cause, k[1]))
                        cur = min(z, end)
                base.append((cur, end, "recorded", k[1]))
                state = "erased by reboot" if n + 1 < len(keys) else "not read out"
                cursor = end
                for u0, u1 in sorted(unread.get(b, [])):
                    if cursor <= u0 < nxt:
                        base.append((cursor, u0, state, ""))
                        base.append((u0, min(u1, nxt), "never read out", ""))
                        cursor = min(u1, nxt)
                if cursor < nxt:
                    base.append((cursor, nxt, state, ""))
        # erased by reboot only where the partners saw the beacon: elsewhere it had no contacts to erase
        if seen_of is not None:
            tb, cut = seen_of(b), []
            for start, end, state, period in base:
                if state != ERASED or end <= start:
                    cut.append((start, end, state, period))
                    continue
                cut += [(a, z, ERASED if seen else NOT_SEEN, period) for a, z, seen in _seen_split(tb, start, end)]
            base = cut
        # clock stops: the beacon recorded nothing and was not seen (clock_stop_check.py)
        silent = sorted((s[2], s[3], k[1]) for k, v in model.stops.items() if k[0] == b for s in v)
        if silent:
            cut = []
            for start, end, state, period in base:
                cur = start
                for a, z, p in silent:
                    if cur < z and a < end:
                        if cur < a:
                            cut.append((cur, a, state, period))
                        cut.append((max(a, cur), min(z, end), SILENT, p))
                        cur = min(z, end)
                if cur < end:
                    cut.append((cur, end, state, period))
            base = cut
        marks = eco_of.get(b, [])
        for start, end, state, period in base:
            start, end = max(start, lo), min(end, hi)
            if end <= start:
                continue
            cuts = sorted({start, end} | {x for a, z in marks for x in (a, z) if start < x < end})
            for a, z in zip(cuts, cuts[1:]):
                mid = (a + z) / 2
                in_eco = any(m0 <= mid <= m1 for m0, m1 in marks)
                ev = none if state == SILENT else next((e for g0, g1, e in evidence.get(b, ()) if g0 <= mid <= g1),
                                                       none)
                rows.append([b, kinds.get(b, "other"), _fmt_local(a), _fmt_local(z), round((z - a) / 3600, 3),
                             state, period, "yes" if in_eco else "", *ev])
    return rows


def _fmt_local(t: float) -> str:
    return datetime.fromtimestamp(round(t)).strftime(pp.TIMESTAMP_FORMAT)


def _local_seconds(t: np.ndarray) -> np.ndarray:
    """Local times rounded to the second, as seconds since 1970-01-01 local; the local UTC offset must
    be the same over the whole range (the deployment lies in summer time)."""
    if not len(t):
        return np.empty(0, dtype=np.int64)
    offsets = {time.localtime(float(x)).tm_gmtoff for x in (t.min(), t.max())}
    if len(offsets) != 1:
        raise SystemExit("local UTC offset changes within the records: vectorised dating not possible")
    return np.round(t).astype(np.int64) + offsets.pop()


CONTACTS_HEADER = ["ID1", "ID2", "RSSI", "Contact Local Time", "Time precision [s]", "Status"]


def _write_day(job) -> None:
    """One daily contacts file (in a process of its own); the time text is made once per second."""
    path, a1, a2, ar, local, ap, ast, statuses = job
    u, inv = np.unique(local, return_inverse=True)
    text = np.char.replace(np.datetime_as_string(u.astype("datetime64[s]"), unit="s"), "T", " ").tolist()
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CONTACTS_HEADER)
        w.writerows(zip(a1.tolist(), a2.tolist(), ar.tolist(), [text[k] for k in inv.tolist()], ap.tolist(),
                        [statuses[x] for x in ast.tolist()]))


def _write_contacts(out: Path, id1, id2, rssi, when, prec, status, statuses, valid_span) -> None:
    t = np.frombuffer(when, dtype=np.float64)
    order = np.argsort(t, kind="stable")
    local = _local_seconds(t[order])
    days = local // pp.DAY_S + 719163                     # days since 0001-01-01 (date.toordinal)
    a1, a2, ar = (np.frombuffer(x, dtype=np.int16)[order] for x in (id1, id2, rssi))
    ap = np.round(np.frombuffer(prec, dtype=np.float32)[order].astype(np.float64), 1)
    ast = np.frombuffer(status, dtype=np.int8)[order]
    jobs, written = [], set()
    start = 0
    while start < len(order):
        day = days[start]
        end = int(np.searchsorted(days, day, side="right"))
        path = pp.contacts_csv_path_for_day(out / pp.DEFAULT_CONTACTS_CSV, datetime.fromordinal(int(day)).date())
        written.add(path)
        jobs.append((path, a1[start:end], a2[start:end], ar[start:end], local[start:end], ap[start:end],
                     ast[start:end], statuses))
        start = end
    with ProcessPoolExecutor(max_workers=min(6, os.cpu_count() or 1)) as pool:   # the days in parallel
        list(pool.map(_write_day, jobs))
    for path in out.glob("contacts_*.csv"):
        stamp = path.stem.rsplit("_", 1)[-1]
        if stamp.isdigit() and len(stamp) == 8 and path not in written:
            path.unlink()
            print(f"Removed {path.name} (no contacts of that day in the window)")
    print(f"  {len(order)} contacts in {len(written)} daily file(s)")


def _write_events(path: Path, time_header: list[str], rows) -> None:
    rows = sorted(rows, key=lambda r: r[1])
    seen, unique = set(), []
    for r in rows:                                   # records sent twice (BI 3.1.4): same beacon and time
        key = (r[0], *(round(x) for x in r[1:-2]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([*time_header, "Time precision [s]", "Status"])
        for r in unique:
            w.writerow([r[0], *(datetime.fromtimestamp(x).strftime(pp.TIMESTAMP_FORMAT) for x in r[1:-2]), r[-2], r[-1]])


def _write_rows(path: Path, header: list[str], rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
