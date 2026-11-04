#!/usr/bin/env python3
"""Determine the true timer-zero time (boot time) of every beacon uptime period.

Model (Background Information.md, 2.5, 3.4.3, 3.6), per readout n:

    z_observed(n) = line time - Current Timer
                  = z0  +  DRIFT * uptime(n)  +  L(n)  +  rounding

- z0: true boot time of the uptime period,
- DRIFT: clock drift of the beacons, measured -21 s per day of uptime
  (the beacon clock runs fast, so the timer-zero time moves earlier),
- L(n) >= 0: logging delay of the readout's Current Timer line (only later).

Steps:
1. Readouts: uninterrupted lines of one ID1, from Status/Current Timer to
   Transfer complete or the next ID.
2. Uptime periods per beacon, readouts in time order. A new period starts at
   readout n if
   b)  Current Timer(n) < Current Timer(n-1), or
   a)  the drift-corrected z(n) lies after the true time of the previous
       readout (booted after it) and exceeds the period's edge by more than
       REBOOT_MARGIN_S. The previous readout's true time is its edge-based
       time (edge + Current Timer), not its line time, which can be late; or
   a') booted after the previous readout, but within REBOOT_MARGIN_S of the
       edge, confirmed by the next PERSISTENCE_READOUTS readouts, or
   a'') the same, confirmed by the contact sync: reboot_hints.csv (written by
       contact_sync_analysis.py, independent of the logging delay; in the pipeline
       found in round 1, used in round 2) holds a boot of this beacon that the
       drift-corrected z(n) fits (hint_fits: at most REBOOT_HINT_EARLY_S before it,
       a readout is not logged before it happens; at most REBOOT_HINT_TOL_S after it).
   (These periods are the start of the uptime model, which decides the periods
   itself, section 19.)
3. Edge per period after removing the drift: readouts after ANCHOR_FROM have
   no delay; if a period has such anchors, their mean is z0, otherwise the
   minimum. Delay L(n) = drift-corrected z(n) - z0.
4. Checks: values below the edge, delays below the lower bound given by the
   contacts recorded during the readout, delayed first-in-file readouts,
   persistent steps, Current Timer decreases without a boot after the
   previous readout.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import sys

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import postprocessing as pp

DRIFT_S_PER_S = pp.STUDY.drift_common_s_per_day / pp.DAY_S   # timer-zero shift per second of uptime (study.toml)
REBOOT_MARGIN_S = 3600
PERSISTENCE_READOUTS = 2
ANCHOR_FROM = pp.DELAY_FREE_FROM               # logs after this time have no delay (study.toml)
ROUNDING_S = 2
FIRST_IN_FILE_DELAY_FLAG_S = 60
PERSISTENT_STEP_S = 3600            # delay-free readout this far above the edge: inconsistent
LONG_STALL_S = 3600                 # consecutive readouts this late: long logger stall
LONG_STALL_READOUTS = 3
MAX_CONTACT_EXCESS_S = pp.DELAY_BOUND_EXCESS_S   # contacts further above Current Timer are not used for the bound
DRIFT_FIT_MIN_DAYS = 3                 # own drift only for periods spanning at least this uptime
DRIFT_FIT_MIN_READOUTS = 5             # ... and (without anchor) at least this many readouts
DRIFT_MIN_S_PER_DAY = pp.STUDY.drift_min_s_per_day   # own drift outside this plausible range: common drift is used
DRIFT_MAX_S_PER_DAY = pp.STUDY.drift_max_s_per_day
MAX_PLAUSIBLE_TIMER_S = pp.MAX_PLAUSIBLE_TIMER_S
HEADERLESS_MAX_GAP_S = 6 * 3600        # largest contact timer may lie this far below the uptime

PERIODS_CSV = "timer_zero_periods.csv"
OVERVIEW_CSV = "readout_overview.csv"
REBOOT_HINTS_CSV = "reboot_hints.csv"   # boots confirmed by the contact sync (contact_sync_analysis.py); cleared by
REBOOT_HINT_TOL_S = 300                 # run_pipeline.py at the start of each run, read only with --reboot-hints;
                                        # a readout fits a hint up to this late
REBOOT_HINT_EARLY_S = 13                # ... and up to this early (rounding and boot precision, hard_rule_check.TOL_S)
# Automatic ID split of IDs 0-9 / 48-57 (Background Information.md, 3.2.1 and 5)
ID_SPLIT_CSV = "id_split_analysis.csv"           # automatic ID split, in the id_correction/ output folder
MISLABEL_UNTIL = pp.ID_BUG_UNTIL      # ID1 48-57 written as 0-9 before this; None: no ID bug (study.toml)


@dataclass
class Readout:
    beacon_id: str
    source: Path
    line_number: int
    start_time: datetime
    first_in_file: bool
    header_time: Optional[datetime] = None      # line time of Current Timer
    current_timer: Optional[int] = None
    contacts: int = 0
    self_reports: int = 0
    eco_sessions: int = 0
    contact_count: Optional[int] = None         # stored contacts reported in the header
    voltage: Optional[int] = None
    status: Optional[int] = None
    first_line: int = 0                         # line numbers of the readout in its log file
    last_line: int = 0
    transfer_complete: bool = False
    delay_lower_bound: float = 0.0
    max_contact_timer: Optional[int] = None
    end_time: Optional[datetime] = None         # line time of the readout's last line
    drift: float = DRIFT_S_PER_S                # drift of the readout's uptime period
    # filled by the analysis
    period: Optional[int] = None
    new_period_reason: str = ""
    zero_true: Optional[datetime] = None
    delay: Optional[float] = None
    flags: list[str] = field(default_factory=list)

    @property
    def zero_observed(self) -> datetime:
        return self.header_time - timedelta(seconds=self.current_timer)

    def corrected_zero(self) -> datetime:
        """Observed timer-zero time with the clock drift removed (z0 + delay)."""
        return self.zero_observed - timedelta(seconds=self.drift * self.current_timer)

    def edge_candidate(self) -> datetime:
        """Latest possible true timer-zero time from this readout: the drift-corrected
        timer-zero time minus the minimum delay shown by its contacts."""
        return self.corrected_zero() - timedelta(seconds=self.delay_lower_bound)


def collect_readouts(lines: logcache.Lines) -> list[Readout]:
    """Split the merged log lines into readouts (step 1). Contact lines come in runs of one file and
    ID1, taken at once: of a run only its first line can start a readout."""
    readouts: list[Readout] = []
    seen_sources: set[Path] = set()
    current: Optional[Readout] = None
    header_t = 0                                          # line time of current's Current Timer [s]
    kind, file, id1, number, t, timer, other = (lines.merged(k) for k in
                                                ("kind", "file", "id1", "line", "t", "timer", "other"))
    valid = [pp.is_valid_beacon_id(b) for b in lines.ids]
    new = np.ones(len(kind), dtype=bool)
    new[1:] = (kind[1:] == 0) | (kind[:-1] == 0) | (file[1:] != file[:-1]) | (id1[1:] != id1[:-1])
    starts = np.flatnonzero(new)
    for a, b, contacts, f, c, line_number, ta in zip(
            starts.tolist(), np.r_[starts[1:], len(kind)].tolist(), (kind[starts] == 1).tolist(),
            file[starts].tolist(), id1[starts].tolist(), number[starts].tolist(), t[starts].tolist()):
        beacon_id, source = lines.ids[c], lines.paths[f]
        if not valid[c]:
            current = None
            continue
        if contacts:                                      # contact lines, most of the logs
            is_status, ct_match, rest = False, None, ""
        else:
            rest = lines.rest[other[a]]
            is_status = pp.STATUS_RE.match(rest) is not None
            ct_match = pp.CURRENT_TIMER_RE.match(rest)
        has_content = current is not None and (
            current.current_timer is not None or current.contacts or current.transfer_complete)
        if (current is None or beacon_id != current.beacon_id
                # line numbers are per file: a readout never spans two
                or source is not current.source
                or (is_status and has_content)
                or (ct_match and current.current_timer is not None)
                or current.transfer_complete):
            current = Readout(beacon_id, source, line_number, logcache.line_time(ta), source not in seen_sources)
            seen_sources.add(source)
            readouts.append(current)
            current.first_line = line_number
        current.end_time = logcache.line_time(t[b - 1])
        current.last_line = int(number[b - 1])
        if contacts:
            tm = timer[a:b].astype(np.int64)
            current.contacts += b - a
            plausible = tm[tm <= MAX_PLAUSIBLE_TIMER_S]
            if len(plausible):
                current.max_contact_timer = max(current.max_contact_timer or 0, int(plausible.max()))
            if current.current_timer is not None:
                excess = tm - current.current_timer
                sel = excess <= MAX_CONTACT_EXCESS_S       # larger excesses come from fake records
                if sel.any():
                    logged = (t[a:b][sel] - header_t).astype(np.float64)
                    current.delay_lower_bound = max(current.delay_lower_bound, float((excess[sel] - logged).max()))
            continue
        if ct_match:
            current.header_time = logcache.line_time(ta)
            header_t = ta
            current.current_timer = int(ct_match.group("timer"))
            current.line_number = line_number
            continue
        if pp.SELF_REPORT_RE.match(rest):
            current.self_reports += 1
            continue
        if pp.ECO_SESSION_RE.match(rest):
            current.eco_sessions += 1
            continue
        header = (pp.CONTACT_COUNT_RE.match(rest), pp.VOLTAGE_RE.match(rest), pp.STATUS_RE.match(rest))
        if header[0]:
            current.contact_count = int(header[0].group("count"))
        elif header[1]:
            current.voltage = int(header[1].group("voltage"))
        elif header[2]:
            current.status = int(header[2].group("status"))
        if rest.startswith(pp.TEXT_PREFIX["transfer_complete"]):
            current.transfer_complete = True
    return readouts


def read_reboot_hints(path: Path) -> dict[str, list[datetime]]:
    """Beacon -> boots confirmed by the contact sync (reboot_hints.csv); empty if the file is missing."""
    hints: dict[str, list[datetime]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as csv_file:
            for row in csv.DictReader(csv_file):
                hints.setdefault(row["ID"], []).append(datetime.strptime(row["Boot"], pp.TIMESTAMP_FORMAT))
    return hints


def hint_fits(zero: datetime, boot: datetime) -> bool:
    """A readout with drift-corrected timer-zero time zero fits the boot of a reboot hint: delay
    >= 0 (- REBOOT_HINT_EARLY_S), at most REBOOT_HINT_TOL_S."""
    return -REBOOT_HINT_EARLY_S <= (zero - boot).total_seconds() <= REBOOT_HINT_TOL_S


def split_periods(readouts: list[Readout], hints: Optional[list[datetime]] = None) -> list[list[Readout]]:
    """Uptime periods of one beacon's readouts in time order (step 2); hints: boots of this beacon
    confirmed by the contact sync (rule a'')."""
    # The previous readout's line time can itself be late. Its true time is
    # estimated from the current period's edge so far (running minimum of the
    # drift-corrected timer-zero times):
    #   true time = edge + Current Timer * (1 + DRIFT)
    # A reboot after the previous readout gives a drift-corrected timer-zero
    # time later than that; within one period this only happens if the readout
    # is delayed by more than the previous readout's uptime.
    periods: list[list[Readout]] = []
    edge: Optional[datetime] = None
    for index, readout in enumerate(readouts):
        if index == 0:
            periods.append([readout])
            edge = readout.edge_candidate()
            continue
        prev = readouts[index - 1]
        prev_true_time = edge + timedelta(seconds=prev.current_timer * (1 + DRIFT_S_PER_S))
        corrected = readout.corrected_zero()
        boot_after_prev = corrected > prev_true_time
        required_delay = (corrected - edge).total_seconds()
        reason = ""
        if readout.current_timer < prev.current_timer:
            reason = "b: Current Timer decreased"
            if corrected < prev_true_time - timedelta(seconds=REBOOT_MARGIN_S):
                readout.flags.append("Current Timer decreased without boot after previous readout")
        elif boot_after_prev and required_delay > REBOOT_MARGIN_S:
            reason = "a: booted after previous readout"
        elif boot_after_prev and any(hint_fits(corrected, h) for h in hints or ()):
            reason = "a'': booted after previous readout, confirmed by the contact sync"
        elif boot_after_prev:
            following = readouts[index + 1:index + 1 + PERSISTENCE_READOUTS]
            if len(following) < PERSISTENCE_READOUTS:
                readout.flags.append("possible reboot undecided (too few following readouts)")
            elif all(f.corrected_zero() > prev_true_time for f in following):
                reason = "a': booted after previous readout, confirmed by following readouts"
            else:
                readout.flags.append("late readout looked like a reboot (not confirmed)")
        if reason:
            readout.new_period_reason = reason
            periods.append([readout])
            edge = readout.edge_candidate()
        else:
            periods[-1].append(readout)
            edge = min(edge, readout.edge_candidate())
    return periods


def _hull_drift(points: list[tuple[float, float]]) -> Optional[float]:
    """Slope of the best line below all (uptime [d], timer-zero [s]) points."""
    points = sorted(points)
    hull: list[tuple[float, float]] = []
    for p in points:
        while len(hull) >= 2 and (hull[-1][0] - hull[-2][0]) * (p[1] - hull[-2][1]) - \
                (hull[-1][1] - hull[-2][1]) * (p[0] - hull[-2][0]) <= 0:
            hull.pop()
        hull.append(p)
    best, slope = None, None
    for (x0, y0), (x1, y1) in zip(hull, hull[1:]):
        if x1 == x0:
            continue
        b = (y1 - y0) / (x1 - x0)
        cost = sum(y - (y0 + b * (x - x0)) for x, y in points)
        if best is None or cost < best:
            best, slope = cost, b
    return slope


def period_drift(period: list[Readout]) -> tuple[float, str, Optional[float]]:
    """Drift used for an uptime period: (drift in s per s, source, unclamped own drift in s/day).

    - Period with delay-free anchors and at least DRIFT_FIT_MIN_DAYS of uptime
      before them: steepest line through the anchor that no earlier readout
      falls below.
    - Otherwise, period spanning DRIFT_FIT_MIN_DAYS with DRIFT_FIT_MIN_READOUTS
      readouts: best line below all readouts (lower edge).
    - Otherwise the common drift.
    An own drift outside [DRIFT_MIN_S_PER_DAY, DRIFT_MAX_S_PER_DAY] is replaced by
    the common drift.
    Points are the readouts' latest possible timer-zero times (observed minus
    the contact bound) against their uptime in days.
    """
    base = period[0].zero_observed
    points = [(r.current_timer / pp.DAY_S,
               (r.zero_observed - base).total_seconds() - r.delay_lower_bound) for r in period]
    span = max(x for x, _ in points) - min(x for x, _ in points)
    anchors = [(r.current_timer / pp.DAY_S, (r.zero_observed - base).total_seconds())
               for r in period if r.header_time >= ANCHOR_FROM]
    own, source = None, ""
    if anchors and span >= DRIFT_FIT_MIN_DAYS:
        xa, ya = max(anchors)
        earlier = [(x, y) for x, y in points if x < xa - 0.5]
        if earlier and xa - min(x for x, _ in earlier) >= DRIFT_FIT_MIN_DAYS:
            own = max((y - ya) / (x - xa) for x, y in earlier)
            source = "own, line through anchor"
    if own is None and span >= DRIFT_FIT_MIN_DAYS and len(points) >= DRIFT_FIT_MIN_READOUTS:
        own = _hull_drift(points)
        source = "own, lower edge" if own is not None else ""
    if own is None:
        return DRIFT_S_PER_S, "common", None
    if not DRIFT_MIN_S_PER_DAY <= own <= DRIFT_MAX_S_PER_DAY:
        # Outside the plausible range the fit follows the delays, not the
        # clock (positive values are physically impossible): use the common drift.
        return DRIFT_S_PER_S, f"common ({source} gave {own:.1f} s/day, outside the plausible range)", own
    return own / pp.DAY_S, source, own


@dataclass
class Period:
    beacon_id: str
    number: int
    readouts: list[Readout]
    zero_true: datetime
    edge_source: str
    drift: float                 # s per s of uptime
    drift_source: str
    flags: list[str]


def analyse_beacon(beacon_id: str, readouts: list[Readout], hints: Optional[list[datetime]] = None) -> list[Period]:
    """Steps 2-4 for one beacon (readouts with a Current Timer, in time order)."""
    result: list[Period] = []
    for number, period in enumerate(split_periods(readouts, hints), start=1):
        drift, drift_source, _ = period_drift(period)
        for readout in period:
            readout.drift = drift
        anchors = [r for r in period if r.header_time >= ANCHOR_FROM]
        if anchors:
            base = min(r.corrected_zero() for r in anchors)
            offsets = [(r.corrected_zero() - base).total_seconds() for r in anchors]
            zero_true = base + timedelta(seconds=sum(offsets) / len(offsets))
            edge_source = f"anchors after {ANCHOR_FROM:%Y-%m-%d %H:%M} ({len(anchors)})"
        else:
            candidates = [r.edge_candidate() for r in period]
            zero_true = min(candidates)
            by_bound = min(candidates) < min(r.corrected_zero() for r in period)
            edge_source = "minimum, lowered by the contact bound" if by_bound else "minimum"
        flags: list[str] = []
        late_run = 0
        stall_start: Optional[Readout] = None
        for readout in period:
            readout.period = number
            readout.zero_true = zero_true
            readout.delay = (readout.corrected_zero() - zero_true).total_seconds()
            if readout.delay < -ROUNDING_S:
                readout.flags.append("below the edge")
            if readout.delay < readout.delay_lower_bound - ROUNDING_S:
                readout.flags.append("delay below the lower bound from the contacts")
            delay_free = readout.first_in_file or readout.header_time >= ANCHOR_FROM
            if delay_free and readout.delay > PERSISTENT_STEP_S:
                readout.flags.append("delay-free readout far above the edge")
                flags.append("persistent step: a delay-free readout lies more than "
                             f"{PERSISTENT_STEP_S} s above the edge")
            elif readout.first_in_file and readout.delay > FIRST_IN_FILE_DELAY_FLAG_S:
                readout.flags.append("first readout in file is delayed")
            if readout.delay > LONG_STALL_S:
                late_run += 1
                stall_start = stall_start or readout
            else:
                late_run, stall_start = 0, None
            if late_run == LONG_STALL_READOUTS:
                flags.append(f"long logger stall from {stall_start.header_time:%Y-%m-%d %H:%M}: "
                             f"{LONG_STALL_READOUTS} or more consecutive readouts more than "
                             f"{LONG_STALL_S} s late")
        if any("below the edge" in r.flags for r in period):
            flags.append("readouts below the edge")
        result.append(Period(beacon_id, number, period, zero_true, edge_source,
                             drift, drift_source, flags))
    return result


def analyse(lines: logcache.Lines, hints: Optional[dict[str, list[datetime]]] = None
            ) -> tuple[list[Readout], list[Period]]:
    """hints: boots confirmed by the contact sync per beacon (read_reboot_hints), used by rule a''."""
    readouts = collect_readouts(lines)
    by_beacon: dict[str, list[Readout]] = {}
    for readout in readouts:
        if readout.current_timer is not None:
            by_beacon.setdefault(readout.beacon_id, []).append(readout)
    periods: list[Period] = []
    for beacon_id in sorted(by_beacon, key=pp.id_sort_key):
        beacon_readouts = sorted(by_beacon[beacon_id], key=lambda r: r.header_time)
        periods.extend(analyse_beacon(beacon_id, beacon_readouts, (hints or {}).get(beacon_id)))
    return readouts, periods


def _fmt(value: Optional[datetime]) -> str:
    return value.strftime(pp.TIMESTAMP_FORMAT) if value else ""


def read_id_decisions(path: Path) -> dict[tuple[str, int], str]:
    """(log file, first line) -> state of the automatic ID split of IDs 0-9 / 48-57
    (id_correction/id_split_analysis.csv of run_pipeline.py / id_split_analysis.py)."""
    decisions: dict[tuple[str, int], str] = {}
    if not path.exists():
        return decisions
    with path.open(encoding="utf-8") as csv_file:
        for row in csv.DictReader(csv_file):
            decisions[(row["Log file"], int(row["First line"]))] = (
                f"{row['ID1 analysis']} ({row['Method']})" if row["ID1 analysis"] else "undecided")
    return decisions


def id_correction(readout: Readout, decisions: dict[tuple[str, int], str]) -> str:
    """State of the automatic ID split for the disputed readouts (IDs 0-9 before MISLABEL_UNTIL)."""
    return decisions.get((readout.source.name, readout.first_line), "")


def write_overview_csv(path: Path, readouts: list[Readout], periods: list[Period],
                       issues: list[tuple[Readout, str, str]], decisions: dict[tuple[str, int], str],
                       accounting: dict[int, ContactAccounting]) -> None:
    """One row per readout in log order; zero time also for readouts assigned by find_issues()."""
    sizes = {(p.beacon_id, p.number): len(p.readouts) for p in periods}
    zero_from = {id(r): category for r, category, _ in issues if category.startswith("no own Current Timer")}
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["No", "Log file", "ID", "Start time", "End time", "Current Timer",
                         "Current Timer present", "Transfer complete present",
                         "Expected contacts (Contact Count)", "Received contacts",
                         "Received stored contacts (timer <= Current Timer)", "Recorded during the readout",
                         "Not received", "Received in later readouts", "Lost at least", "Lost at most",
                         "Contact accounting note",
                         "Self-reports", "Delay [s]", "Zero time", "Voltage", "Status", "Eco sessions",
                         "First line", "Last line", "First in file",
                         "Period", "Readouts in period", "Zero time from", "Delay lower bound [s]",
                         "ID split 0-9 / 48-57", "Current Timer line", "Line time of Current Timer",
                         "Zero observed", "New period reason", "Flags"])
        blank = lambda v: "" if v is None else v
        for number, r in enumerate(readouts, start=1):
            a = accounting[id(r)]
            if r.current_timer is not None:
                source = "own Current Timer"
            else:
                source = zero_from.get(id(r), "").removeprefix("no own Current Timer, ")
            writer.writerow([
                number, r.source.name, r.beacon_id, _fmt(r.start_time), _fmt(r.end_time),
                r.current_timer if r.current_timer is not None else "",
                "yes" if r.current_timer is not None else "no",
                "yes" if r.transfer_complete else "no",
                blank(r.contact_count), a.received,
                a.stored_received if r.current_timer is not None else "",
                a.during if r.current_timer is not None else "",
                blank(a.not_received), blank(a.later), blank(a.lost_min), blank(a.lost_max), a.note,
                r.self_reports,
                round(r.delay, 1) if r.delay is not None else "", _fmt(r.zero_true),
                r.voltage if r.voltage is not None else "",
                r.status if r.status is not None else "",
                r.eco_sessions, r.first_line, r.last_line, "yes" if r.first_in_file else "",
                r.period or "", sizes.get((r.beacon_id, r.period), "") if r.period else "", source,
                round(r.delay_lower_bound, 1) if r.current_timer is not None else "",
                id_correction(r, decisions),
                r.line_number if r.current_timer is not None else "", _fmt(r.header_time),
                _fmt(r.zero_observed) if r.current_timer is not None else "", r.new_period_reason,
                "no own Current Timer" if r.current_timer is None else "; ".join(r.flags),
            ])


def write_periods_csv(path: Path, periods: list[Period]) -> None:
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["ID", "Period", "First readout", "Last readout", "Readouts",
                         "Zero true", "Edge from", "Drift [s/day]", "Drift from", "Max delay [s]",
                         "First-in-file readouts", "Flags"])
        for p in periods:
            writer.writerow([
                p.beacon_id, p.number, _fmt(p.readouts[0].header_time), _fmt(p.readouts[-1].header_time),
                len(p.readouts), _fmt(p.zero_true), p.edge_source,
                round(p.drift * pp.DAY_S, 1), p.drift_source,
                round(max(r.delay for r in p.readouts), 1),
                sum(1 for r in p.readouts if r.first_in_file), "; ".join(p.flags),
            ])


def print_summary(readouts: list[Readout], periods: list[Period]) -> None:
    with_header = [r for r in readouts if r.current_timer is not None]
    print(f"Timer-zero analysis: {len(readouts)} readouts, {len(with_header)} with own Current Timer, "
          f"{len(periods)} uptime periods of {len({p.beacon_id for p in periods})} beacons")
    reasons: dict[str, int] = {}
    flags: dict[str, int] = {}
    for r in with_header:
        if r.new_period_reason:
            reasons[r.new_period_reason[:2].strip(":")] = reasons.get(r.new_period_reason[:2].strip(":"), 0) + 1
        for f in r.flags:
            flags[f] = flags.get(f, 0) + 1
    print("  new periods by rule:", ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())))
    for f, n in sorted(flags.items(), key=lambda item: -item[1]):
        print(f"  readouts flagged '{f}': {n}")
    step = sum(1 for p in periods if any(f.startswith("persistent step") for f in p.flags))
    stall = sum(1 for p in periods if any(f.startswith("long logger stall") for f in p.flags))
    print(f"  periods with a persistent step (delay-free readout above the edge): {step}")
    print(f"  periods with a long logger stall: {stall}")
    lowered = sum(1 for p in periods if "contact bound" in p.edge_source)
    print(f"  periods with the edge lowered by the contact bound: {lowered}")
    print(f"  periods with edge from anchors: {sum(1 for p in periods if p.edge_source.startswith('anchors'))}")
    by_day: dict[str, list[float]] = {}
    for r in with_header:
        by_day.setdefault(r.header_time.strftime("%Y-%m-%d"), []).append(r.delay)
    print("  delay per day (median / 90 % / max, s):")
    for day in sorted(by_day):
        values = sorted(by_day[day])
        print(f"    {day}: {len(values):4d} readouts  {values[len(values) // 2]:8.0f} "
              f"{values[int(0.9 * (len(values) - 1))]:8.0f} {values[-1]:9.0f}")
    first = sorted(r.delay for r in with_header if r.first_in_file)
    if first:
        print(f"  first-in-file readouts: {len(first)}, delay median {first[len(first) // 2]:.0f} s, "
              f"max {first[-1]:.0f} s")
    own = sorted(p.drift * pp.DAY_S for p in periods if not p.drift_source.startswith("common"))
    rejected = sum(1 for p in periods if "outside the plausible range" in p.drift_source)
    if own:
        print(f"  periods with own drift: {len(own)} (fits outside the range, common used: {rejected}), "
              f"median {own[len(own) // 2]:.1f}, "
              f"10 % {own[len(own) // 10]:.1f}, 90 % {own[9 * len(own) // 10]:.1f} s/day; "
              f"others use the common {DRIFT_S_PER_S * pp.DAY_S:.0f} s/day")



# Issue categories of find_issues (summary printed; the zero-time source of readout_overview.csv)
ISSUE_NO_HEADER_KNOWN = "no own Current Timer, zero from neighbours"
ISSUE_NO_HEADER_UNKNOWN = "no own Current Timer, no timer-zero time"
ISSUE_NO_HEADER_ASSIGNED = "no own Current Timer, assigned by contact timers"
ISSUE_SINGLE = "single readout in its uptime period"


def _contacts_fit(readout: Readout, candidate: Readout) -> bool:
    """Does the header-less readout's largest contact timer fit the candidate's period?

    Contacts are recorded up to the readout, so the largest contact timer is
    close to the beacon's uptime at the readout. In the candidate's period the
    uptime at the readout's line time is (line time - zero true) / (1 + drift)
    counter seconds. The line time can be late, so the largest timer may lie
    below it by up to HEADERLESS_MAX_GAP_S, but never above it.
    """
    if readout.max_contact_timer is None or candidate.zero_true is None:
        return False
    uptime = (readout.start_time - candidate.zero_true).total_seconds() / (1 + candidate.drift)
    return uptime - HEADERLESS_MAX_GAP_S <= readout.max_contact_timer <= uptime + ROUNDING_S


def find_issues(readouts: list[Readout], periods: list[Period]) -> list[tuple[Readout, str, str]]:
    """Readouts without a usable timer-zero time or with inconsistencies.

    Returns (readout, category, detail) rows.
    """
    issues: list[tuple[Readout, str, str]] = []
    by_beacon: dict[str, list[Readout]] = {}
    for readout in readouts:
        by_beacon.setdefault(readout.beacon_id, []).append(readout)
    for beacon_readouts in by_beacon.values():
        beacon_readouts.sort(key=lambda r: r.start_time)
        for index, readout in enumerate(beacon_readouts):
            if readout.current_timer is not None or not readout.contacts:
                continue
            prev = next((r for r in reversed(beacon_readouts[:index]) if r.current_timer is not None), None)
            nxt = next((r for r in beacon_readouts[index + 1:] if r.current_timer is not None), None)
            if prev and nxt and prev.period == nxt.period:
                readout.period, readout.zero_true = prev.period, prev.zero_true
                issues.append((readout, ISSUE_NO_HEADER_KNOWN, f"{readout.contacts} contacts"))
            else:
                why = "no readout before" if prev is None else "no readout after" if nxt is None else \
                    f"reboot between (period {prev.period} -> {nxt.period})"
                fitting = [c for c in (prev, nxt) if c is not None and _contacts_fit(readout, c)]
                if len(fitting) == 1:
                    readout.period, readout.zero_true = fitting[0].period, fitting[0].zero_true
                    issues.append((readout, ISSUE_NO_HEADER_ASSIGNED,
                                   f"{readout.contacts} contacts; {why}; assigned to period {fitting[0].period}"))
                else:
                    detail = "fits both candidates" if len(fitting) == 2 else "fits no candidate"
                    issues.append((readout, ISSUE_NO_HEADER_UNKNOWN,
                                   f"{readout.contacts} contacts; {why}; largest contact timer {detail}"))
    for period in periods:
        if len(period.readouts) == 1:
            issues.append((period.readouts[0], ISSUE_SINGLE, f"period {period.number}"))
        stall = next((f for f in period.flags if f.startswith("long logger stall")), None)
        if stall:
            for readout in period.readouts:
                if readout.delay > LONG_STALL_S:
                    issues.append((readout, "long logger stall",
                                   f"period {period.number}, {readout.delay / 3600:.1f} h late"))
        for readout in period.readouts:
            for flag in readout.flags:
                detail = f"delay {readout.delay:.0f} s"
                if flag.startswith("delay below"):
                    detail += f", lower bound {readout.delay_lower_bound:.0f} s"
                issues.append((readout, flag, detail))
    issues.sort(key=lambda row: (pp.id_sort_key(row[0].beacon_id), row[0].start_time))
    return issues


@dataclass
class ContactAccounting:
    """Expected and received contacts of one readout (Background Information 2.1, 3.2.3)."""
    received: int = 0                   # contact lines
    stored_received: int = 0            # ... with timer <= Current Timer (recorded before the readout)
    during: int = 0                     # ... with timer > Current Timer (recorded during the readout)
    leftover_timers: list[int] = field(default_factory=list)   # timers <= an earlier Current Timer of the period
    later: Optional[int] = None         # stored contacts received in later readouts of the same period
    not_received: Optional[int] = None  # Contact Count - stored_received
    lost_min: Optional[int] = None
    lost_max: Optional[int] = None
    note: str = ""


def contact_accounting(lines: logcache.Lines, readouts: list[Readout]) -> dict[int, ContactAccounting]:
    """Per readout (keyed by id()): expected contacts (Contact Count), received contact lines, and
    what happened to the stored contacts that were not received.

    Not received contacts are either still on the beacon (an interrupted readout keeps the rest,
    BI 2.1 step 4) and arrive in a later readout of the same uptime period, or they are lost (the
    Base confirmed them, but the logger did not write them, BI 3.2.3). A reboot deletes all stored
    contacts (BI 3.1.1). Per readout with Contact Count:
    - received later: contacts of later readouts of the same period with a timer <= its Current Timer;
    - lost at most: not received - received later;
    - lost at least: not received - (Contact Count of the next readout of the period - its contacts
      recorded after this readout); a reboot before the next readout: all not received are lost.
    Contacts sent twice (BI 3.1.4) are counted as received each time.
    """
    import bisect
    acc = {id(r): ContactAccounting() for r in readouts}
    # earlier Current Timer of the same period, for leftover detection
    by_period: dict[tuple[str, int], list[Readout]] = {}
    for r in readouts:
        if r.period:
            by_period.setdefault((r.beacon_id, r.period), []).append(r)
    prev_ct: dict[int, int] = {}
    for rs in by_period.values():
        rs.sort(key=lambda r: r.start_time)
        top = None
        for r in rs:
            if top is not None:
                prev_ct[id(r)] = top
            if r.current_timer is not None:
                top = r.current_timer if top is None else max(top, r.current_timer)
    # map contact lines to readouts (per file, by line range)
    of = lines.readout_of([(r.source.name, r.first_line, r.last_line) for r in readouts])
    sel = np.flatnonzero((lines.kind == 1) & (of >= 0))
    i, timer = of[sel], lines.timer[sel].astype(np.int64)
    n = len(readouts)
    ct = np.array([-1 if r.current_timer is None else r.current_timer for r in readouts], dtype=np.int64)
    top = np.array([prev_ct.get(id(r), -1) for r in readouts], dtype=np.int64)
    has_ct = (ct >= 0)[i]
    received = np.bincount(i, minlength=n)
    stored = np.bincount(i[has_ct & (timer <= ct[i])], minlength=n)
    during = np.bincount(i[has_ct & (timer > ct[i])], minlength=n)
    left = (top >= 0)[i] & (timer <= top[i])
    o = np.argsort(i[left], kind="stable")
    li, lt = i[left][o], timer[left][o]
    cuts = np.flatnonzero(np.diff(li)) + 1
    for a, b in zip(np.r_[0, cuts].tolist(), np.r_[cuts, len(li)].tolist()):
        if b > a:
            acc[id(readouts[int(li[a])])].leftover_timers = np.sort(lt[a:b]).tolist()
    for k, r in enumerate(readouts):
        a = acc[id(r)]
        a.received, a.stored_received, a.during = int(received[k]), int(stored[k]), int(during[k])
    # per readout with Contact Count
    later_periods: dict[str, set[int]] = {}
    for r in readouts:
        if r.period:
            later_periods.setdefault(r.beacon_id, set()).add(r.period)
    for (beacon, period), rs in by_period.items():
        rebooted = any(p > period for p in later_periods.get(beacon, ()))
        for i, r in enumerate(rs):
            a = acc[id(r)]
            if r.current_timer is None or r.contact_count is None:
                continue
            a.not_received = r.contact_count - a.stored_received
            later = rs[i + 1:]
            a.later = sum(bisect.bisect_right(acc[id(j)].leftover_timers, r.current_timer) for j in later)
            missing = max(a.not_received, 0)
            a.lost_max = max(missing - a.later, 0)
            nxt = later[0] if later else None
            if nxt is None:
                if rebooted:
                    a.lost_min = a.lost_max
                    a.note = "reboot before the next readout: not received contacts are deleted"
                else:
                    a.lost_min = 0
                    a.note = "no later readout"
            elif nxt.current_timer is None or nxt.contact_count is None:
                a.lost_min = 0
                a.note = "next readout without header"
            else:
                kept_at_most = nxt.contact_count - (acc[id(nxt)].stored_received -
                                                     bisect.bisect_right(acc[id(nxt)].leftover_timers, r.current_timer))
                a.lost_min = min(max(missing - max(kept_at_most, 0), 0), a.lost_max)
            if a.not_received < 0:
                a.note = (a.note + "; " if a.note else "") + \
                    "more stored contacts received than Contact Count (contacts of another beacon or sent twice?)"
    return acc


def print_accounting_summary(readouts: list[Readout], accounting: dict[int, ContactAccounting]) -> None:
    rows = [(r, accounting[id(r)]) for r in readouts if accounting[id(r)].not_received is not None]
    expected = sum(r.contact_count for r, _ in rows)
    stored = sum(a.stored_received for _, a in rows)
    lo = sum(a.lost_min for _, a in rows); hi = sum(a.lost_max for _, a in rows)
    lossy = sum(1 for _, a in rows if a.lost_min > 0)
    print(f"  contacts: {len(rows)} readouts with Contact Count expect {expected}, received of these {stored}; "
          f"lost at least {lo}, at most {hi} ({lossy} readouts with a certain loss)")


def print_issue_summary(issues: list[tuple[Readout, str, str]]) -> None:
    counts: dict[str, set[str]] = {}
    rows: dict[str, int] = {}
    for readout, category, _ in issues:
        counts.setdefault(category, set()).add(readout.beacon_id)
        rows[category] = rows.get(category, 0) + 1
    print("  issues (readouts / beacons):")
    for category in sorted(rows, key=lambda c: -rows[c]):
        print(f"    {category}: {rows[category]} / {len(counts[category])}")


def add_reboot_hints_argument(parser) -> None:
    parser.add_argument("--reboot-hints", action="store_true",
                        help=f"Use the reboot hints of the contact sync ({REBOOT_HINTS_CSV}, rule a''; run_pipeline.py "
                             "round 2). Without it none are used, and contact_sync_analysis.py writes the file anew: "
                             "the result does not depend on earlier runs.")


def run(lines: logcache.Lines, out_dir: Path, hints: bool = True) -> tuple[Path, Path]:
    """hints: use the reboot hints of the contact sync (REBOOT_HINTS_CSV)."""
    readouts, periods = analyse(lines, read_reboot_hints(out_dir / REBOOT_HINTS_CSV) if hints else {})
    issues = find_issues(readouts, periods)
    overview_csv = out_dir / OVERVIEW_CSV
    periods_csv = out_dir / PERIODS_CSV
    write_periods_csv(periods_csv, periods)
    decisions = read_id_decisions(out_dir.parent / pp.OUTPUT_ID_CORRECTION_DIR / ID_SPLIT_CSV)
    accounting = contact_accounting(lines, readouts)
    write_overview_csv(overview_csv, readouts, periods, issues, decisions, accounting)
    print_accounting_summary(readouts, accounting)
    print_summary(readouts, periods)
    print_issue_summary(issues)
    return overview_csv, periods_csv


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Timer-zero analysis on its own (also run by run_pipeline.py).")
    pp.add_common_arguments(parser)
    add_reboot_hints_argument(parser)
    args = parser.parse_args(argv)
    out_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = logcache.load(args.log_dir)
    run(lines, out_dir, args.reboot_hints)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
