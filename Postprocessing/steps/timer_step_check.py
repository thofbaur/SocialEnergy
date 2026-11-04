#!/usr/bin/env python3
"""Two clocks in one uptime period (Postprocessing Method.md section 19.2).

A beacon's timer can lose time without a reboot: its stored records survive, but everything it
records afterwards is dated on a clock that no longer matches its boot. The readout after such a
loss holds contacts on two clocks. The cause is not known and the change of clock cannot be dated
reliably (beacon 15 p2: the offset grows over about half an hour), so a found case is not repaired:
all records of the period are marked unreliable. Detection, per readout of the uptime model (run
after uptime_model.py):

1. Its contacts are cut into blocks of BLOCK_S of their own timer (at least MIN_BLOCK_CONTACTS
   each); every block is matched against the partners' records like a whole readout
   (uptime_model.contact_boots) and gives a boot with peak and second peak.
2. A step: two consecutive reliable blocks (peak >= MIN_PEAK, peak >= MIN_RATIO x second peak)
   whose boots differ by more than STEP_MIN_S.
3. Accepted only if the earlier clock is the period's (its boot within STEP_MIN_S of the period's
   boot): a step leaves the period's clock. An earlier block off the period's boot and a later one
   on it is a single outlier block (a chance peak), not a step (13 p1, 2026-10-22: one block 44 min
   early with peaks 15 / 10, the next on the period's boot with 331).
4. Accepted only if the later clock obeys H1: its boot is not after the readout's own upper limit
   (line time of Current Timer - Current Timer, + eps). A step that would put the readout before
   its own boot is a spurious match.
5. Accepted only if the later clock is confirmed by at least MIN_CONFIRM reliable blocks of
   FINE_BLOCK_S (one block could be a chance peak).

A period whose two clocks are explained by the accepted clock stops of clock_stop_check.py (run
before; their shifts add up to the step within STOP_EXPLAIN_S) is repaired by the dating
(model_dating.py) and not marked.

Unreliable, for a period with two clocks: all records of that beacon (contacts, self-reports, eco
sessions) in all readouts of the period, with and without Current Timer. They are not re-dated;
the conversion (generate_output.py) keeps them with the status "unreliable time (two clocks in one
period)". The partners' records of the same encounters are on their own clocks and stay.

Outputs (quality/ subfolder of --output-dir):
- timer_step_check.csv           every candidate step: readout, blocks, boots, peaks, verdict
- unreliable_time_records.csv    (log file, line) of every record in an unreliable range
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import time
from pathlib import Path

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import clock_stop_check as csc
from steps import hard_rule_check as hrc
from lib import logcache
from lib import postprocessing as pp
from steps import uptime_model as um

BLOCK_S = 4 * 3600
FINE_BLOCK_S = 3600
MIN_BLOCK_CONTACTS = 50
MIN_READOUT_CONTACTS = 200
STEP_MIN_S = 120
MIN_CONFIRM = 2
CHECK_CSV = "timer_step_check.csv"
TWO_CLOCKS = "two clocks in the period (not explained): all its records unreliable"
EXPLAINED = "two clocks explained by clock stops: repaired by the dating"
STOP_EXPLAIN_S = 300


def _reliable(c) -> bool:
    return um.contact_reliable(c)


def block_boots(readouts, assign, boots, drift_of, rd, b2, tm, targets, block_s, min_contacts):
    """Boots of the timer blocks of each target readout: readout -> [(timer from, to, boot, peak, second)]."""
    order = np.argsort(rd, kind="stable")
    rd_sorted = rd[order]
    rd_blocks = rd.copy()
    ext = list(readouts)
    ext_assign = dict(assign)
    blocks = []
    for i in targets:
        lo, hi = np.searchsorted(rd_sorted, i), np.searchsorted(rd_sorted, i, side="right")
        sel = order[lo:hi]
        t = tm[sel]
        edges = np.arange(t.min(), t.max() + block_s, block_s)
        for x, y in zip(edges, edges[1:]):
            m = (t >= x) & (t < y)
            if m.sum() < min_contacts:
                continue
            idx = len(ext)
            ext.append(readouts[i])
            ext_assign[idx] = assign[i]
            rd_blocks[sel[m]] = idx
            blocks.append((i, idx, x, y))
    cb = um.contact_boots(ext, ext_assign, boots, drift_of, rd_blocks, b2, tm)
    out = collections.defaultdict(list)
    for i, idx, x, y in blocks:
        c = cb.get(idx)
        if c is not None:
            out[i].append((float(x), float(y), c[0], c[1], c[2], c[3]))
    return out


def find_steps(readouts, assign, boots, drift_of, rd, b2, tm, lb):
    counts = np.bincount(rd, minlength=len(readouts))
    targets = [i for i in assign if counts[i] >= MIN_READOUT_CONTACTS and readouts[i]["ct"] is not None
               and readouts[i]["ct"] - 0 > BLOCK_S]
    coarse = block_boots(readouts, assign, boots, drift_of, rd, b2, tm, targets, BLOCK_S, MIN_BLOCK_CONTACTS)
    rows, accepted = [], []
    for i, bl in coarse.items():
        good = sorted(b for b in bl if _reliable(b[2:]))
        r = readouts[i]
        boot = boots[assign[i]]
        d = drift_of(r["ID"])
        z = r["header"] - r["ct"] * (1 + d)
        limit = z - lb[i] + hrc.eps(r["ct"])
        for (x1, y1, b1, p1, s1, _), (x2, y2, b2_, p2, s2, _) in zip(good, good[1:]):
            step = b2_ - b1
            if abs(step) <= STEP_MIN_S:
                continue
            if abs(b1 - boot) > STEP_MIN_S:
                verdict = "rejected: earlier clock not the period's (outlier block)"
            elif b2_ > limit:
                verdict = "rejected: later clock after the readout's upper limit (H1)"
            else:
                verdict = TWO_CLOCKS
            rows.append([r["ID"], assign[i][1], r["file"], r["first"], um._fmt(r["header"]), int(x1), int(y1),
                         um._fmt(b1), p1, s1, int(x2), int(y2), um._fmt(b2_), p2, s2, round(step, 1),
                         um._fmt(boot), "", verdict])
            if verdict == TWO_CLOCKS:
                accepted.append((i, len(rows) - 1, b1))
    # the later clock must be confirmed by finer blocks
    if accepted:
        fine = block_boots(readouts, assign, boots, drift_of, rd, b2, tm, [a[0] for a in accepted],
                           FINE_BLOCK_S, 10)
        for i, row_idx, old in accepted:
            new = um._ts(rows[row_idx][12])
            confirm = sum(1 for b in fine.get(i, []) if _reliable(b[2:]) and abs(b[2] - new) <= STEP_MIN_S)
            if confirm < MIN_CONFIRM:
                rows[row_idx][-1] = f"rejected: later clock confirmed by {confirm} fine block(s) only"
    return rows


def unreliable_records(log_dir: Path, readouts, assign, steps) -> list[list]:
    """(log file, line, ID, kind, timer) of every record of the periods with two clocks; assign:
    readout index -> period, for the readouts with and without Current Timer."""
    by_key = collections.defaultdict(list)
    for i, k in assign.items():
        by_key[k].append(i)
    index = {(r["file"], r["first"]): i for i, r in enumerate(readouts)}
    marked = sorted({j for row in steps if row[-1] == TWO_CLOCKS for j in by_key[assign[index[(row[2], row[3])]]]})
    out = []
    if not marked:
        return out
    lines = logcache.Lines([log_dir / name for name in sorted({readouts[i]["file"] for i in marked})])
    k = lines.readout_of([(readouts[i]["file"], readouts[i]["first"], readouts[i]["last"]) for i in marked])
    for x in np.flatnonzero(k >= 0).tolist():
        i = marked[k[x]]
        if lines.kind[x] == 1:
            kind, timer = "contact", int(lines.timer[x])
        else:
            rest = lines.rest[lines.other[x]]
            if pp.SELF_REPORT_RE.match(rest):
                kind, timer = "self-report", int(pp.SELF_REPORT_RE.match(rest).group("timer"))
            elif pp.ECO_SESSION_RE.match(rest):
                kind, timer = "eco session", int(pp.ECO_SESSION_RE.match(rest).group("leave"))
            else:
                continue
        out.append([lines.paths[lines.file[x]].name, int(lines.line[x]), readouts[i]["ID"], kind, timer])
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Two clocks in one uptime period (timer lost time without reboot).")
    pp.add_common_arguments(parser)
    args = parser.parse_args(argv)
    t0 = time.time()
    tz = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    quality = args.output_dir / pp.OUTPUT_QUALITY_DIR
    quality.mkdir(parents=True, exist_ok=True)
    readouts = hrc.load_readouts(tz)
    drifts, centre = um.read_drifts(tz)
    drift_of = lambda b: drifts.get(b, centre)
    print(f"Timer step check: {len(readouts)} readouts; reading the contacts ...", flush=True)
    rd, b2, tm, lb, _ = um.read_contacts(args.log_dir, readouts, drift_of)
    key_of = {(r["Log file"], int(r["First line"])): (r["ID"], r["Period"])
              for r in csv.DictReader((tz / um.READOUTS_CSV).open(encoding="utf-8"))}
    boots = {(r["ID"], r["Period"]): um._ts(r["Boot"])
             for r in csv.DictReader((tz / um.PERIODS_CSV).open(encoding="utf-8"))}
    assign = {i: key_of[(r["file"], r["first"])] for i, r in enumerate(readouts) if (r["file"], r["first"]) in key_of}
    steps = find_steps(readouts, assign, boots, drift_of, rd, b2, tm, lb)
    stops = csc.read_stops(quality / csc.CLOCK_STOPS_CSV)
    for row in steps:
        shift = sum(x[1] for x in stops.get((row[0], row[1]), ()))
        if row[-1] == TWO_CLOCKS and shift and abs(shift - row[15]) <= STOP_EXPLAIN_S:
            row[-1] = EXPLAINED
    # readouts without Current Timer belong to the period the uptime model assigned them
    key_hl = {(r["Log file"], int(r["First line"])): (r["ID"], r["Period"])
              for r in csv.DictReader((tz / um.HEADERLESS_CSV).open(encoding="utf-8")) if r["Period"]}
    assign_all = dict(assign)
    assign_all.update({i: key_hl[(r["file"], r["first"])] for i, r in enumerate(readouts)
                       if i not in assign and (r["file"], r["first"]) in key_hl})
    with (quality / CHECK_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Period", "Log file", "First line", "Line time", "Block 1 timer from", "Block 1 timer to",
                    "Block 1 boot", "Block 1 peak", "Block 1 second peak", "Block 2 timer from", "Block 2 timer to",
                    "Block 2 boot", "Block 2 peak", "Block 2 second peak", "Step [s]", "Period boot",
                    "Verdict"])
        w.writerows([row[:17] + row[18:] for row in steps])
    records = unreliable_records(args.log_dir, readouts, assign_all, steps)
    with (quality / pp.DEFAULT_UNRELIABLE_TIME_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Log file", "Line", "ID", "Kind", "Timer"])
        w.writerows(records)
    kinds = collections.Counter(r[3] for r in records)
    for row in steps:
        print(f"  {row[0]} p{row[1]} {row[4]}: step {row[15]} s (peaks {row[8]}/{row[13]}), {row[-1]}")
    print(f"  {len(records)} records marked as unreliably dated: "
          + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"  written to {quality / CHECK_CSV} and {quality / pp.DEFAULT_UNRELIABLE_TIME_CSV} ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
