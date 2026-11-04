#!/usr/bin/env python3
"""Check the uptime periods and boots against the hard rules of the system.

Every solution for the boots must obey these rules (Background Information chapters 2, 3); the
soft evidence (delay distribution, contacts, reboot frequency) may only choose between solutions
that do. The check tests a solution: by default (--model current) per uptime period the boot and
drift of the contact sync where applied (timer_zero/contact_sync_periods.csv), otherwise of the
timer-zero analysis; with --model uptime (the pipeline) the final uptime model.
For a readout r of beacon b in period k:

    R_r  = B_k + C_r (1 + d_b)        real time at which the beacon read its uptime (Current Timer C_r)
    T(e) = B_k + t_e (1 + d_b)        real time of a record e with timer t_e
    eps  = TOL_S + TOL_PER_DAY_S * uptime in days    (rounding, precision of the boots, drift uncertainty)

Fake contact records (BI 3.2.2, quality/fake_records.csv) are not checked: their timers are bytes
of other messages.

H1  Nothing is logged before it happens: R_r <= line time of its Current Timer + eps, and
    T(e) <= line time of e + eps for every contact of the readout (BI 2.6, 3.6).
H2  One readout, one clock: Current Timer and contacts of a readout share the boot. Holds by
    construction (contacts belong to their readout); not checked.
H3  Uptime only grows: within a period, Current Timer does not decrease in log order (BI 3.4.1).
H4  Readouts happen in log order, across all beacons: one Base, one connection at a time, the
    order is kept up to the log (BI 2.1, 3.2.3). The real times R of all readouts must not
    decrease in log order. A single readout with a wrong R would make many others look wrong, so
    the violators are the readouts outside the longest non-decreasing sequence of R (compared in
    steps of H4_STEP_S, to absorb the precision of the boots): the smallest set whose removal
    restores the order. Reported when its distance to the sequence exceeds eps (as for H1). Per
    period: all its readouts outside (its boot is shifted) or only some (it probably mixes two
    boots).
H5  A new boot lies after the real time of the previous period's last readout (- eps).
H6  Delay-free readouts (first readout of a log file, readouts after 2026-08-29 19:30) are
    logged at their real time: line time - R_r within [-eps, eps + ANCHOR_MARGIN_S] (BI 3.2.3).
H7  A documented battery replacement has a boot in its window (battery_replacement_check.py);
    a period without readout counts (verdict "change without a readout").
H8  Uptime starts at the boot: covered by H1 for the contacts (their timers lie at most the
    readout duration above Current Timer).
H9  No boot before the beacons were flashed (postprocessing.FLASH_START, 2026-08-12 23:00).

Output (quality/ subfolder of --output-dir):
- hard_rule_check.csv   one row per violation: rule, beacon, period, readout (file, line, line
                        time), value, limit, excess in seconds, phase
                        (--model uptime: hard_rule_check_uptime_model.csv)
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza

OUTPUT_CSV = "hard_rule_check.csv"
TOL_S = 13.0                   # rounding (3 s) + precision of the boots (10 s)
TOL_PER_DAY_S = 1.5
ANCHOR_MARGIN_S = 10.0          # H6: delay of a delay-free readout 0 .. this (+- eps); used by uptime_model.py
H4_STEP_S = 60


def _ts(value: str) -> float:
    return datetime.strptime(value, pp.TIMESTAMP_FORMAT).timestamp()


def _fmt(t: float) -> str:
    return datetime.fromtimestamp(t).strftime(pp.TIMESTAMP_FORMAT)


def phase(t: float, span: pp.ValidTimeSpan) -> str:
    if t < span.start.timestamp():
        return "before the deployment"
    return "deployment" if t <= span.end.timestamp() else "after the deployment"


def load_model(tz_dir: Path) -> dict[tuple[str, str], tuple[float, float, str]]:
    """(ID, period) -> (boot, drift s/s, source)."""
    model = {}
    for row in csv.DictReader((tz_dir / tza.PERIODS_CSV).open(encoding="utf-8")):
        model[(row["ID"], row["Period"])] = (_ts(row["Zero true"]), float(row["Drift [s/day]"]) / pp.DAY_S, "readouts")
    sync = tz_dir / "contact_sync_periods.csv"
    if sync.exists():
        for row in csv.DictReader(sync.open(encoding="utf-8")):
            key = (row["ID"], row["Period"])
            if key in model and row["Zero new"]:
                model[key] = (_ts(row["Zero new"]), float(row["Drift new [s/day]"]) / pp.DAY_S, "contact sync")
    return model


def load_uptime_model(tz_dir: Path, readouts: list[dict]) -> dict[tuple[str, str], tuple[float, float, str]]:
    """The solution of uptime_model.py: periods and boots; sets each readout's period to its model period."""
    model = {}
    for row in csv.DictReader((tz_dir / "uptime_model_periods.csv").open(encoding="utf-8")):
        model[(row["ID"], row["Period"])] = (_ts(row["Boot"]), float(row["Drift [s/day]"]) / pp.DAY_S, "uptime model")
    period = {}
    for row in csv.DictReader((tz_dir / "uptime_model_readouts.csv").open(encoding="utf-8")):
        period[(row["Log file"], int(row["First line"]))] = row["Period"]
    for r in readouts:
        r["period"] = period.get((r["file"], r["first"]), "")
    return model


def load_readouts(tz_dir: Path) -> list[dict]:
    """All readouts in log order (readout_overview.csv) with line range, Current Timer, the line time
    of its Current Timer, battery voltage and period."""
    out = []
    for o in csv.DictReader((tz_dir / tza.OVERVIEW_CSV).open(encoding="utf-8")):
        first, last = int(o["First line"]), int(o["Last line"])
        ct = int(o["Current Timer"]) if o["Current Timer present"] == "yes" else None
        t = _ts(o["Line time of Current Timer"]) if ct is not None else None
        # complete: Transfer complete and all stored contacts received; contacts recorded during the
        # readout (timer > Current Timer) do not count, they are not part of Contact Count (BI 2.4)
        expected = o["Expected contacts (Contact Count)"]
        received = o.get("Received stored contacts (timer <= Current Timer)") or o["Received contacts"]
        complete = o["Transfer complete present"] == "yes" and (
            not expected or not received or int(received) >= int(expected))
        out.append(dict(ID=o["ID"], file=o["Log file"], first=first, last=last, ct=ct, header=t,
                        start=_ts(o["Start time"]) if o["Start time"] else None,
                        count=int(expected) if expected else None,
                        voltage=int(o["Voltage"]) if o["Voltage"].isdigit() else None,
                        period=o["Period"], first_in_file=o["First in file"] == "yes", complete=complete))
    return out


def eps(uptime_s: float) -> float:
    """The tolerance of the hard rules at an uptime (seconds); the only place of the formula."""
    return TOL_S + TOL_PER_DAY_S * max(uptime_s, 0.0) / pp.DAY_S


def eps_array(uptime_s: np.ndarray) -> np.ndarray:
    """eps of each uptime of an array (NaN stays NaN)."""
    return TOL_S + TOL_PER_DAY_S * np.maximum(uptime_s, 0.0) / pp.DAY_S


def read_fakes(path: Path) -> set[tuple[str, int]]:
    """(log file, line) of the fake contact records found by uptime_model.py on the same logs in the
    same run (timer_zero/uptime_model_fake_records.csv), or, where that list is missing, those
    listed by data_quality_reports.py (path; written after the pipeline)."""
    model_list = path.parent.parent / pp.OUTPUT_TIMER_ZERO_DIR / "uptime_model_fake_records.csv"
    if model_list.exists():
        path = model_list
    elif not path.exists():
        return set()
    return {(r["Log file"], int(r["Line"])) for r in csv.DictReader(path.open(encoding="utf-8"))}


def stop_shift(stops, key, timer: float) -> float:
    """Shift of the accepted clock stops of a period before a timer (clock_stop_check.py)."""
    return sum(s[1] for s in stops.get(key, ()) if s[0] < timer)


def load_stops(quality_dir: Path) -> dict:
    from steps import clock_stop_check as csc          # here: clock_stop_check imports this module
    return csc.read_stops(quality_dir / csc.CLOCK_STOPS_CSV)


def contact_excess(log_dir: Path, readouts: list[dict], model, fakes: set[tuple[str, int]], stops=None
                   ) -> dict[int, tuple[float, int, float]]:
    """Per readout index: largest T(e) - line time - eps over its contacts, with the line number and
    line time (the first contact with the largest; readouts in the order of their first contact)."""
    stops = stops or {}
    index = [i for i, r in enumerate(readouts) if r["period"] and (r["ID"], r["period"]) in model]
    ranges = [(readouts[i]["file"], readouts[i]["first"], readouts[i]["last"]) for i in index]
    names = [n for n in dict.fromkeys(x[0] for x in ranges) if (log_dir / n).exists()]
    if not names:
        return {}
    lines = logcache.Lines([log_dir / n for n in names])
    k = lines.readout_of(ranges)
    rows = np.flatnonzero((lines.kind == 1) & (k >= 0) & ~lines.mask_of(fakes)
                          & (lines.timer <= tza.MAX_PLAUSIBLE_TIMER_S) & pp.valid_beacon_ids(lines.id2)
                          & (lines.rssi >= pp.DEFAULT_VALID_RSSI_MIN) & (lines.rssi <= pp.DEFAULT_VALID_RSSI_MAX))
    if not len(rows):        # the rest are dropped by the validity checks of the conversion
        return {}
    which = np.array(index, dtype=np.int64)[k[rows]]               # readout of each contact
    timer = lines.timer[rows].astype(np.int64)
    keys = [(readouts[i]["ID"], readouts[i]["period"]) for i in index]
    boot = np.array([model[key][0] for key in keys])[k[rows]]
    drift = np.array([model[key][1] for key in keys])[k[rows]]
    shift = np.zeros(len(rows))
    for j, key in enumerate(keys):
        if stops.get(key):
            sel = k[rows] == j
            shift[sel] = [stop_shift(stops, key, x) for x in timer[sel].tolist()]
    t = logcache.local_timestamps(lines.t[rows])
    excess = boot + timer * (1 + drift) + shift - t - eps_array(timer)
    order = np.argsort(which, kind="stable")
    starts = np.flatnonzero(np.r_[True, which[order][1:] != which[order][:-1]])
    group = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(order)]))
    best = np.maximum.reduceat(excess[order], starts)
    _, first = np.unique(group[excess[order] == best[group]], return_index=True)
    at = order[np.flatnonzero(excess[order] == best[group])[first]]  # first contact with the largest excess
    worst: dict[int, tuple[float, int, float]] = {}
    for g in np.argsort(order[starts], kind="stable").tolist():  # readouts in the order of their first contact
        x = int(at[g])
        worst[int(which[x])] = (float(excess[x]), int(lines.line[rows[x]]), float(t[x]))
    return worst


def _longest_ordered(values: list[int]) -> set[int]:
    """Positions of a longest non-decreasing subsequence (patience sorting, O(n log n))."""
    tails, tails_pos, prev = [], [], [-1] * len(values)
    for j, v in enumerate(values):
        k = bisect.bisect_right(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_pos.append(j)
        else:
            tails[k], tails_pos[k] = v, j
        prev[j] = tails_pos[k - 1] if k else -1
    keep, j = set(), tails_pos[-1] if tails_pos else -1
    while j != -1:
        keep.add(j)
        j = prev[j]
    return keep


def check(readouts, model, battery_csv: Path, log_dir: Path, span: pp.ValidTimeSpan) -> list[list]:
    out = []

    def add(rule, r, value, limit, excess, note=""):
        t = r["header"] if r.get("header") else 0.0
        out.append([rule, r["ID"], r.get("period", ""), r.get("file", ""), r.get("line", ""), _fmt(t) if t else "",
                    value, limit, round(excess, 1), phase(t, span) if t else "", note])

    stops = load_stops(battery_csv.parent) if battery_csv.parent.exists() else {}
    real = {}
    for i, r in enumerate(readouts):
        if r["ct"] is not None and r["period"] and (r["ID"], r["period"]) in model:
            boot, drift, _ = model[(r["ID"], r["period"])]
            real[i] = boot + r["ct"] * (1 + drift) + stop_shift(stops, (r["ID"], r["period"]), r["ct"])
    # H1 header and H6
    for i, R in real.items():
        r = readouts[i]
        e = eps(r["ct"])
        if R - r["header"] > e:
            add("H1 header logged before it happened", dict(r, line=r["first"]), _fmt(R), _fmt(r["header"]), R - r["header"] - e)
        anchor = r["first_in_file"] or r["header"] >= tza.ANCHOR_FROM.timestamp()
        if anchor and r["header"] - R > e + ANCHOR_MARGIN_S:
            add("H6 delay-free readout late", dict(r, line=r["first"]), f"delay {r['header'] - R:.0f} s",
                f"{e + ANCHOR_MARGIN_S:.0f} s", r["header"] - R - e - ANCHOR_MARGIN_S)
    # H1 contacts
    fakes = read_fakes(battery_csv.parent / pp.DEFAULT_FAKE_RECORDS_CSV)
    for i, (excess, line, t) in contact_excess(log_dir, readouts, model, fakes, stops).items():
        if excess > 0:
            r = readouts[i]
            add("H1 contact logged before it happened", dict(r, line=line), "", _fmt(t), excess,
                "contact of a header-less readout" if r["ct"] is None else "")
    # H3 per period in log order
    last_ct: dict[tuple[str, str], tuple[int, dict]] = {}
    for r in readouts:
        if r["ct"] is None or not r["period"]:
            continue
        key = (r["ID"], r["period"])
        if key in last_ct and r["ct"] < last_ct[key][0]:
            add("H3 Current Timer decreased within a period", dict(r, line=r["first"]), r["ct"], last_ct[key][0],
                last_ct[key][0] - r["ct"])
        last_ct[key] = (max(r["ct"], last_ct.get(key, (0, None))[0]), r)
    # H4 in log order, across beacons: readouts outside the longest non-decreasing sequence of R
    order = sorted(real)                                    # readout indices in log order
    keep = _longest_ordered([int(real[i] // H4_STEP_S) for i in order])
    outside = [order[j] for j in range(len(order)) if j not in keep]
    total = collections.Counter((readouts[i]["ID"], readouts[i]["period"]) for i in order)
    out_per = collections.Counter((readouts[i]["ID"], readouts[i]["period"]) for i in outside)
    for j, i in enumerate(order):
        if j in keep:
            continue
        r = readouts[i]
        key = (r["ID"], r["period"])
        before = max((real[order[x]] for x in range(max(0, j - 50), j) if x in keep), default=None)
        after = min((real[order[x]] for x in range(j + 1, min(len(order), j + 51)) if x in keep), default=None)
        excess = max(real[i] - after if after is not None else 0.0, before - real[i] if before is not None else 0.0)
        if excess <= eps(r["ct"] or 0.0):           # within the precision of the boot, as for H1
            continue
        kind = ("all readouts of the period outside: its boot is shifted" if out_per[key] == total[key] else
                f"{out_per[key]} of {total[key]} readouts of the period outside: it probably mixes two boots")
        add("H4 readout out of log order", dict(r, line=r["first"]), _fmt(real[i]),
            f"{_fmt(before) if before else '-'} .. {_fmt(after) if after else '-'}", excess, kind)
    # H5 boot after the previous period's last readout
    last_real: dict[tuple[str, str], float] = {}
    periods_of: dict[str, list[str]] = collections.defaultdict(list)
    for i, R in real.items():
        r = readouts[i]
        key = (r["ID"], r["period"])
        last_real[key] = max(last_real.get(key, R), R)
        if r["period"] not in periods_of[r["ID"]]:
            periods_of[r["ID"]].append(r["period"])
    for beacon, plist in periods_of.items():
        plist.sort(key=int)
        for a, b in zip(plist, plist[1:]):
            boot_b = model[(beacon, b)][0]
            if boot_b < last_real[(beacon, a)] - TOL_S:
                add("H5 boot before the previous period's last readout",
                    dict(ID=beacon, period=b, header=boot_b), _fmt(boot_b), _fmt(last_real[(beacon, a)]),
                    last_real[(beacon, a)] - boot_b, f"previous period {a}")
    # H9 no boot before the beacons were flashed
    for (beacon, period), (boot, _, _) in model.items():
        if boot < pp.FLASH_START.timestamp() - TOL_S:
            add("H9 boot before the beacons were flashed", dict(ID=beacon, period=period, header=boot),
                _fmt(boot), _fmt(pp.FLASH_START.timestamp()), pp.FLASH_START.timestamp() - boot)
    # H7 battery replacements: a boot in the window, or a period boundary across it (a period
    # started by the replacement and never read out); beacons documented as defective are skipped
    if battery_csv.exists():
        boots_of = collections.defaultdict(list)
        for (beacon, _), (boot, _, _) in model.items():
            boots_of[beacon].append(boot)
        timeline = collections.defaultdict(list)
        for i, R in real.items():
            timeline[readouts[i]["ID"]].append((R, readouts[i]["period"]))
        for v in timeline.values():
            v.sort()
        for row in csv.DictReader(battery_csv.open(encoding="utf-8")):
            if row["Verdict"] == "beacon silent":
                continue
            w0, w1 = _ts(row["Window from"]), _ts(row["Window to"])
            if any(w0 <= b <= w1 for b in boots_of[row["ID"]]):
                continue
            before = [p for t, p in timeline[row["ID"]] if t < w0]
            after = [p for t, p in timeline[row["ID"]] if t > w1]
            if before and after and before[-1] != after[0]:
                continue
            t = datetime.strptime(row["Battery replaced"], "%Y-%m-%d %H:%M").timestamp()
            add("H7 no boot in the window of a battery replacement", dict(ID=row["ID"], header=t),
                "", f"{row['Window from']} .. {row['Window to']}", 0.0, row["Verdict"])
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check the uptime periods and boots against the hard rules.")
    pp.add_common_arguments(parser)
    parser.add_argument("--model", choices=("current", "uptime"), default="current",
                        help="Solution to check: the timer-zero analysis with the contact sync (current) or "
                             "uptime_model.py (uptime). Default: current")
    args = parser.parse_args(argv)
    span = pp.ValidTimeSpan(args.valid_date_start, args.valid_date_end)
    tz_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    out_dir = args.output_dir / pp.OUTPUT_QUALITY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    readouts = load_readouts(tz_dir)
    model = load_uptime_model(tz_dir, readouts) if args.model == "uptime" else load_model(tz_dir)
    print(f"Hard rule check: {len(readouts)} readouts, {len(model)} uptime periods; reading the contacts ...", flush=True)
    rows = check(readouts, model, out_dir / "battery_replacement_check.csv", args.log_dir, span)
    name = OUTPUT_CSV if args.model == "current" else OUTPUT_CSV.replace(".csv", "_uptime_model.csv")
    with (out_dir / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rule", "ID", "Period", "Log file", "Line", "Line time", "Value", "Limit", "Excess [s]", "Phase", "Note"])
        w.writerows(rows)
    counts = collections.Counter((r[0], r[9]) for r in rows)
    for rule in sorted({r[0] for r in rows}):
        print(f"  {rule}: " + ", ".join(f"{ph or 'no time'} {counts[(rule, ph)]}" for ph in
                                        ("before the deployment", "deployment", "after the deployment", "")
                                        if counts[(rule, ph)]))
    print(f"  written to {out_dir / name} ({len(rows)} violations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
