#!/usr/bin/env python3
"""H4 as a hard rule: readouts happen in log order (Postprocessing Method.md section 19.4).

The uptime model solves each beacon on its own and can only treat H4 (which couples the boots of
different beacons) as a cost. This step enforces it on the final model with the smallest change of
the periods' boots and drifts: a boot moves at most MOVE_FACTOR times its precision (the class of
model_dating.PRECISION_S: a delay-free readout, reliable contact boots, log-order predictions, the
delay alone), a drift at most DRIFT_MAX_S_PER_DAY; both are weighted by the precision (a drift by the time shift it
causes at the period's last readout). Under the change every readout lies at most eps (minus
ROUND_MARGIN_S for the boots written to whole seconds) after every readout logged after it within
H4_WINDOW positions - exactly the condition of hard_rule_check.py: then no readout is more than eps
outside the log-ordered sequence of its neighbours. The other hard rules stay: H1 (no readout after
its upper limit), H5 (a boot not before the previous period's last readout), H6 (delay-free
readouts), H9 (no boot before the flashing); the clock stops of clock_stop_check.py shift the
readouts after them. A linear programme (scipy HiGHS); an H4 condition the data cannot meet within
these bounds gets a slack at SLACK_COST per second and is reported as a contradiction.

Run after clock_stop_check.py, before timer_step_check.py and hard_rule_check.py. Rewrites the boots
and drifts in timer_zero/uptime_model_periods.csv (and the boot, real time and delay of the readouts
in uptime_model_readouts.csv); model_dating.py dates with the period's drift from there. The
model's own values stay in the columns "Boot before H4" / "Drift before H4 [s/day]", from which a
second run starts again: running it twice gives the same result.

Outputs: quality/h4_enforce.csv - per changed period: beacon, period, boot and drift before and after,
move, precision class; per remaining contradiction: the two readouts and the excess.
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import time
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import clock_stop_check as csc
from steps import hard_rule_check as hrc
from dating import model_dating as mdt
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza
from steps import uptime_model as um

H4_WINDOW = 50                 # pairs up to this many readouts apart in log order (as hard_rule_check.py)
MOVE_FACTOR = 2.0
DRIFT_MAX_S_PER_DAY = 2 * hrc.TOL_PER_DAY_S      # twice the drift uncertainty in eps
PRECISION_S = mdt.PRECISION_S
SLACK_COST = 10.0              # per second of violated H4 (a move costs 1 / precision per second)
ROUND_MARGIN_S = 1.0           # the boots are written to whole seconds: each pair keeps this much in hand
ANCHOR_MARGIN_S = um.ANCHOR_MARGIN_S
OUT_CSV = mdt.H4_ENFORCE_CSV
BOOT_BEFORE, DRIFT_BEFORE = "Boot before H4", "Drift before H4 [s/day]"     # columns added to uptime_model_periods.csv


def _write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


class Rows:
    """Sparse constraint rows A x <= b."""

    def __init__(self):
        self.r, self.c, self.v, self.b = [], [], [], []

    def add(self, cols_vals, rhs: float) -> None:
        row = len(self.b)
        for c, v in cols_vals:
            self.r.append(row); self.c.append(c); self.v.append(v)
        self.b.append(rhs)

    def matrix(self, n_vars: int):
        return coo_matrix((self.v, (self.r, self.c)), shape=(len(self.b), n_vars)).tocsr(), np.array(self.b)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Enforce H4 (readouts in log order) on the uptime model's boots.")
    pp.add_common_arguments(parser)
    args = parser.parse_args(argv)
    t0 = time.time()
    tz = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    quality = args.output_dir / pp.OUTPUT_QUALITY_DIR
    periods = list(csv.DictReader((tz / um.PERIODS_CSV).open(encoding="utf-8")))
    readouts = list(csv.DictReader((tz / um.READOUTS_CSV).open(encoding="utf-8")))
    # the model's own boot and drift, kept next to the enforced ones: a second run starts from them
    # again (idempotent; the moves in quality/h4_enforce.csv stay those against the model)
    for p in periods:
        if BOOT_BEFORE in p:
            p["Boot"], p["Drift [s/day]"] = p[BOOT_BEFORE], p[DRIFT_BEFORE]
        else:
            p[BOOT_BEFORE], p[DRIFT_BEFORE] = p["Boot"], p["Drift [s/day]"]
    boot = {(p["ID"], p["Period"]): um._ts(p["Boot"]) for p in periods}
    drift = {(p["ID"], p["Period"]): float(p["Drift [s/day]"]) / pp.DAY_S for p in periods}
    reliable = {(p["ID"], p["Period"]): int(p["Reliable contact boots"] or 0) for p in periods}
    log_order = {(p["ID"], p["Period"]): int(p.get("Log-order readouts") or 0) for p in periods}
    stops = csc.read_stops(quality / csc.CLOCK_STOPS_CSV)
    pos, first_in_file = {}, set()
    for n, o in enumerate(csv.DictReader((tz / tza.OVERVIEW_CSV).open(encoding="utf-8"))):
        key = (o["Log file"], int(o["First line"]))
        pos[key] = n
        if o["First in file"] == "yes":
            first_in_file.add(key)
    rows = []
    for r in readouts:
        k = (r["ID"], r["Period"])
        key = (r["Log file"], int(r["First line"]))
        if k not in boot or key not in pos or not r["Current Timer"]:
            continue
        ct = float(r["Current Timer"])
        header = um._ts(r["Line time of Current Timer"])
        a = ct * (1 + drift[k]) + sum(s[1] for s in stops.get(k, ()) if s[0] < ct)
        rows.append(dict(k=k, pos=pos[key], ct=ct, header=header, a=a, lb=float(r["Delay lower bound [s]"] or 0),
                         eps=hrc.eps(ct), anchor=key in first_in_file or header >= tza.ANCHOR_FROM.timestamp(), src=r))
    rows.sort(key=lambda x: x["pos"])
    keys = sorted({x["k"] for x in rows})
    col = {k: n for n, k in enumerate(keys)}
    n = len(keys)
    anchor_period = {x["k"] for x in rows if x["anchor"]}
    kinds = um.beacon_types(um.PERSONS_CSV, um.ROOMS_CSV)
    prec = np.array([PRECISION_S["delay-free"] if k in anchor_period else PRECISION_S["contacts"] if reliable[k]
                     else mdt.log_order_precision(kinds.get(k[0], "other")) if log_order[k] else PRECISION_S["delay"]
                     for k in keys])
    ct_max = np.zeros(n)
    for x in rows:
        ct_max[col[x["k"]]] = max(ct_max[col[x["k"]]], x["ct"])
    R = np.array([boot[x["k"]] + x["a"] for x in rows])
    # variables: x (boot moves, n), g (drift changes in s/s, n), t >= |x| (n), q >= |g| ct_max (n), slacks (m)
    X, G, T, Q = 0, n, 2 * n, 3 * n
    A = Rows()
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, min(len(rows), i + 1 + H4_WINDOW)):
            if rows[i]["k"] == rows[j]["k"]:
                continue
            pairs.append((i, j))
    m = len(pairs)
    S = 4 * n
    viol_before = 0
    for p, (i, j) in enumerate(pairs):              # R_i' - R_j' - s_p <= eps - margin
        ci, cj = col[rows[i]["k"]], col[rows[j]["k"]]
        rhs = R[j] - R[i] + min(rows[i]["eps"], rows[j]["eps"]) - ROUND_MARGIN_S
        viol_before += rhs + ROUND_MARGIN_S < 0
        A.add([(X + ci, 1), (G + ci, rows[i]["ct"]), (X + cj, -1), (G + cj, -rows[j]["ct"]), (S + p, -1)], rhs)
    for x, Rx in zip(rows, R):                      # H1 and H6 per readout (relaxed where the model breaks them)
        c = col[x["k"]]
        h1 = x["header"] - Rx - x["lb"] + x["eps"]
        A.add([(X + c, 1), (G + c, x["ct"])], max(h1, 0.0))
        if x["anchor"]:
            h6 = x["header"] - Rx - x["eps"] - ANCHOR_MARGIN_S
            A.add([(X + c, -1), (G + c, -x["ct"])], max(-h6, 0.0))
    last = {}
    for x, Rx in zip(rows, R):
        if Rx >= last.get(x["k"], (-np.inf, 0))[0]:
            last[x["k"]] = (Rx, x["ct"])
    by_beacon = collections.defaultdict(list)
    for k in keys:
        by_beacon[k[0]].append(k)
    for ks in by_beacon.values():                   # H5: last readout of the earlier period <= boot of the later + TOL
        ks.sort(key=lambda k: boot[k])
        for k1, k2 in zip(ks, ks[1:]):
            r1, c1 = last[k1]
            A.add([(X + col[k1], 1), (G + col[k1], c1), (X + col[k2], -1)], max(boot[k2] - r1 + hrc.TOL_S, 0.0))
    for k in range(n):                              # |x| <= t, |g| ct_max <= q
        A.add([(X + k, 1), (T + k, -1)], 0.0)
        A.add([(X + k, -1), (T + k, -1)], 0.0)
        A.add([(G + k, ct_max[k]), (Q + k, -1)], 0.0)
        A.add([(G + k, -ct_max[k]), (Q + k, -1)], 0.0)
    nv = 4 * n + m
    cost = np.concatenate([np.zeros(2 * n), 1.0 / prec, 1.0 / prec, np.full(m, SLACK_COST)])
    h9 = np.array([pp.FLASH_START.timestamp() - boot[k] for k in keys])
    gmax = DRIFT_MAX_S_PER_DAY / pp.DAY_S
    bounds = ([(max(-MOVE_FACTOR * prec[k], min(h9[k], 0.0)), MOVE_FACTOR * prec[k]) for k in range(n)]
              + [(-gmax, gmax)] * n + [(0, None)] * (2 * n + m))
    mat, b = A.matrix(nv)
    res = linprog(cost, A_ub=mat, b_ub=b, bounds=bounds, method="highs")
    if res.status != 0:
        print(f"H4 enforcement: no solution ({res.message}); model unchanged")
        return 0
    move, dg, slack = res.x[X:X + n], res.x[G:G + n], res.x[S:]
    new_boot = {k: boot[k] + move[col[k]] for k in keys}
    new_drift = {k: drift[k] + dg[col[k]] for k in keys}
    for p in periods:
        k = (p["ID"], p["Period"])
        if k in new_boot:
            p["Boot"] = um._fmt(new_boot[k])
            p["Drift [s/day]"] = round(new_drift[k] * pp.DAY_S, 3)
    _write_csv(tz / um.PERIODS_CSV, list(periods[0].keys()), periods)
    for x in rows:
        r, k = x["src"], x["k"]
        real = um._ts(um._fmt(new_boot[k])) + x["a"] + x["ct"] * (round(new_drift[k] * pp.DAY_S, 3) / pp.DAY_S - drift[k])
        r["Boot"] = um._fmt(new_boot[k])
        r["Real time"] = um._fmt(real)
        r["Delay [s]"] = round(x["header"] - real, 1)
    _write_csv(tz / um.READOUTS_CSV, list(readouts[0].keys()), readouts)
    report = []
    for k in keys:
        c = col[k]
        if abs(move[c]) >= 0.5 or abs(dg[c]) * ct_max[c] >= 0.5:
            report.append({"Kind": "period changed", "ID": k[0], "Period": k[1], "Boot before": um._fmt(boot[k]),
                           "Boot after": um._fmt(new_boot[k]), "Move [s]": round(move[c], 1),
                           "Drift before [s/day]": round(drift[k] * pp.DAY_S, 3), "Drift after [s/day]": round(new_drift[k] * pp.DAY_S, 3),
                           "Shift at last readout [s]": round(dg[c] * ct_max[c], 1), "Precision [s]": prec[c],
                           "Readout": "", "Later readout": "", "Excess [s]": ""})
    for p in np.flatnonzero(slack > 0.5):
        i, j = pairs[p]
        a, b2 = rows[i]["src"], rows[j]["src"]
        report.append({"Kind": "contradiction", "ID": a["ID"], "Period": a["Period"], "Boot before": "", "Boot after": "",
                       "Move [s]": "", "Drift before [s/day]": "", "Drift after [s/day]": "", "Shift at last readout [s]": "",
                       "Precision [s]": "", "Readout": f"{a['Log file']}:{a['First line']}",
                       "Later readout": f"{b2['ID']} p{b2['Period']} {b2['Log file']}:{b2['First line']}",
                       "Excess [s]": round(slack[p], 1)})
    _write_csv(quality / OUT_CSV, ["Kind", "ID", "Period", "Boot before", "Boot after", "Move [s]", "Drift before [s/day]",
                                   "Drift after [s/day]", "Shift at last readout [s]", "Precision [s]", "Readout",
                                   "Later readout", "Excess [s]"], report)
    moved = np.abs(move) >= 0.5
    drifted = np.abs(dg) * ct_max >= 0.5
    print(f"H4 enforcement: {len(rows)} readouts, {n} periods, {m} pairs within {H4_WINDOW}; pairs out of order "
          f"before {viol_before}, after {int((slack > 0.5).sum())} (contradictions); {int(moved.sum())} boots moved "
          f"(|move| median {np.median(np.abs(move[moved])) if moved.any() else 0:.1f} s, max {np.abs(move).max():.1f} s), "
          f"{int(drifted.sum())} drifts changed (max {np.abs(dg).max() * pp.DAY_S:.2f} s/day) ({time.time() - t0:.0f} s); "
          f"{quality / OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
