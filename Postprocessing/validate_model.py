#!/usr/bin/env python3
"""Synthetic validation of the uptime model (Postprocessing Method.md section 19.1).

The model is fitted once (uptime_model.fit); each test then changes the inputs of one beacon in a
known way and solves that beacon again with everything else (contact boots of the other readouts,
H4 bounds, cut points of the solution) unchanged. Solving the unchanged beacon reproduces the
fitted solution; the tests compare against that.

A  Injected reboot: inside a period of at least two readouts, a reboot at a random time between
   the real times of two consecutive readouts j-1 and j. The readouts j.. of the period get the
   Current Timer of the new boot (line times unchanged). Three variants: their contact boots move
   with the reboot ("contacts"), they have none ("no contacts"), or they have none and no log-order
   prediction either ("no contacts, no log order": a boot from the delay alone, H4 bounds kept).
   Found if the solution cuts
   exactly at j; also the error of the new boot and changed cut points elsewhere.
B  Late readout: the line time of one readout (not delay-free) is moved later by 10 min, 1 h or
   8 h; its contacts' line times move with it, so its delay lower bound stays. Variants: its H4
   bounds kept or dropped. Correct if no cut point changes; also the change of the boot.
C  Delay-free readouts as ground truth: each delay-free readout in turn loses its H6 status; its
   observed timer-zero time is the true boot (within the timer precision). Error = boot of its
   period without H6 - its timer-zero time.
D  Readouts without Current Timer (--headerless N, needs a full fit): per round one readout with
   Current Timer and contacts per beacon, at a period boundary (the nearest readouts with Current
   Timer before and after it lie in different periods), loses its Current Timer; its contact limit
   is computed from its own log lines as for a real one. uptime_model.assign_headerless then
   assigns it; compared with its period in the fit: right, wrong or undecided. "own period": it is
   the only readout of its period (the truth is none of the two candidates). The boots stay those of
   the fit, which used the readout's own evidence (leaks in, strongest for short periods).

The fitted state is cached in timer_zero/uptime_model_validation_state.pkl, with the digest of the
pipeline's model and of uptime_model.py (fingerprint); fitted again when they changed or with --refit.
Output: timer_zero/uptime_model_validation.csv (one row per test case) and a summary.
"""

from __future__ import annotations

import argparse
import collections
import copy
import csv
import hashlib
import math
import pickle
import random
import sys
import time
from pathlib import Path

import numpy as np

from steps import hard_rule_check as hrc
from lib import logcache
from lib import postprocessing as pp
from steps import uptime_model as um

STATE_PKL = "uptime_model_validation_state.pkl"
OUT_CSV = "uptime_model_validation.csv"
WINDOW = (pp.DEFAULT_VALID_SPAN.start.timestamp(), pp.DEFAULT_VALID_SPAN.end.timestamp())
LATE_S = (600, 3600, 8 * 3600)
MARGIN_S = 30                      # injected reboot at least this far from both readouts


def fingerprint(args) -> str:
    """What the cached fit stands for: the pipeline's model (periods and readouts of the uptime model)
    and the code of the uptime model. Another run of the pipeline or a change of the code: fitted again."""
    tz_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    digest = hashlib.sha256()
    for path in (tz_dir / um.PERIODS_CSV, tz_dir / um.READOUTS_CSV, Path(um.__file__)):
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


def load_state(args):
    path = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR / STATE_PKL
    key = fingerprint(args)
    if path.exists() and not args.refit:
        with path.open("rb") as f:
            state = pickle.load(f)
        if state.get("fingerprint") == key:
            return state
        print(f"{path.name}: made from another pipeline output or uptime model; fitting again")
    st = um.fit(args)
    state = dict(beacons=st.beacons, inputs=st.inputs, boots=st.boots, segments=st.segments, fingerprint=key)
    with path.open("wb") as f:
        pickle.dump(state, f)
    return state


def cuts_of(segs) -> set[int]:
    return {a for a, _, _, _ in segs if a > 0}


def period_at(segs, j):
    for a, e, boot, flag in segs:
        if a <= j < e:
            return a, e, boot
    raise ValueError(j)


def changed_beacon(bc: um.Beacon, **arrays) -> um.Beacon:
    new = copy.copy(bc)
    for name, value in arrays.items():
        setattr(new, name, value)
    new.z = new.header - new.ct * (1 + new.drift)
    new.eps = hrc.eps_array(new.ct)
    return new


def in_window(t: float) -> bool:
    return WINDOW[0] <= t <= WINDOW[1]


def test_reboot(bc, local, h4, cuts, base, rng, n_per_beacon):
    rows = []
    d = bc.drift
    places = [(a, e, boot) for a, e, boot, _ in base if e - a >= 2]
    choices = [(a, e, boot, j) for a, e, boot in places for j in range(a + 1, e) if in_window(bc.header[j])]
    for a, e, boot, j in rng.sample(choices, min(n_per_beacon, len(choices))):
        R = boot + bc.ct * (1 + d)
        lo, hi = R[j - 1] + MARGIN_S, R[j] - MARGIN_S
        if hi <= lo:
            continue
        true_boot = rng.uniform(lo, hi)
        shift = true_boot - boot
        ct = bc.ct.copy()
        ct[j:e] = np.round(ct[j:e] - shift / (1 + d))
        falls = ct[j] < ct[j - 1]
        # bounds from readouts without Current Timer move with the reboot (their contacts would too)
        cap = bc.cap.copy()
        cap[j:e] += shift
        mod = changed_beacon(bc, ct=ct, cap=cap)
        # no contacts, no log order: the injected readouts keep their H4 bounds but lose the log-order
        # prediction - a boot from the delay alone (precision class "delay" of the dating)
        for variant in ("contacts", "no contacts", "no contacts, no log order"):
            loc = dict(local)
            for k in range(j, e):
                if k in loc:
                    if variant == "contacts":
                        loc[k] = (loc[k][0] + shift, loc[k][1])
                    else:
                        del loc[k]
            hh = h4
            if variant == "no contacts, no log order":
                hh = {k: (v[:3] + (math.nan, math.nan) if j <= k < e else v) for k, v in h4.items()}
            segs = um.solve_beacon(mod, loc, hh, cuts)
            new_cuts = cuts_of(segs)
            _, _, got = period_at(segs, j)
            rows.append(dict(Test="A reboot", Variant=variant, ID=bc.id, Readout=j,
                             Time=um._fmt(bc.header[j]), Parameter="CT falls" if falls else "CT does not fall",
                             Found="yes" if j in new_cuts else "no",
                             **{"Error [s]": round(got - true_boot, 1),
                                "Other cut points changed": len((new_cuts - {j}) ^ cuts_of(base))}))
    return rows


def test_late(bc, local, h4, cuts, base, rng, n_per_beacon):
    rows = []
    choices = [j for a, e, _, _ in base if e - a >= 2 for j in range(a, e)
               if in_window(bc.header[j]) and not bc.anchor[j]]
    for j in rng.sample(choices, min(n_per_beacon, len(choices))):
        _, _, boot = period_at(base, j)
        for late in LATE_S:
            header = bc.header.copy()
            header[j] += late
            mod = changed_beacon(bc, header=header)
            for variant in ("H4 kept", "H4 dropped"):
                hh = h4 if variant == "H4 kept" else {k: v for k, v in h4.items() if k != j}
                segs = um.solve_beacon(mod, local, hh, cuts)
                _, _, got = period_at(segs, j)
                changed = len(cuts_of(segs) ^ cuts_of(base))
                rows.append(dict(Test="B late readout", Variant=variant, ID=bc.id, Readout=j,
                                 Time=um._fmt(bc.header[j]), Parameter=f"{late // 60} min",
                                 Found="yes" if changed == 0 else "no",
                                 **{"Error [s]": round(got - boot, 1), "Other cut points changed": changed}))
    return rows


def test_anchor(bc, local, h4, cuts, base):
    rows = []
    for j in np.flatnonzero(bc.anchor):
        anchor = bc.anchor.copy()
        anchor[j] = False
        segs = um.solve_beacon(changed_beacon(bc, anchor=anchor), local, h4, cuts)
        _, _, got = period_at(segs, j)
        _, _, with_h6 = period_at(base, j)
        changed = len(cuts_of(segs) ^ cuts_of(base))
        rows.append(dict(Test="C delay-free", Variant="in window" if in_window(bc.header[j]) else "outside window",
                         ID=bc.id, Readout=int(j), Time=um._fmt(bc.header[j]),
                         Parameter=f"with H6 {with_h6 - bc.z[j]:+.0f} s",
                         Found="yes" if abs(got - bc.z[j]) <= bc.eps[j] + um.ANCHOR_MARGIN_S else "no",
                         **{"Error [s]": round(got - bc.z[j], 1), "Other cut points changed": changed}))
    return rows


def headerless_limits(log_dir, readouts, idx, drift_of) -> dict:
    """hl_limit (latest boot its contacts allow, largest timer) of the readouts idx, from their log
    lines, as uptime_model.read_contacts computes it for readouts without Current Timer."""
    out = {}
    timers = collections.defaultdict(list)
    by_file = collections.defaultdict(list)
    for i in idx:
        by_file[readouts[i]["file"]].append(i)
    for name, items in by_file.items():
        data = logcache.read(log_dir / name)                  # the cache: no script reads the log text
        line, contact = data["line"].astype(np.int64), data["kind"] == 1
        local = logcache.local_timestamps(data["t"])
        for i in items:
            r = readouts[i]
            d = drift_of(r["ID"])
            for x in np.flatnonzero(contact & (line >= r["first"]) & (line <= r["last"])).tolist():
                t = int(data["timer"][x])
                if not pp.is_valid_beacon_id(str(int(data["id2"][x]))) or not pp.is_valid_rssi(int(data["rssi"][x])) \
                        or t > um.tza.MAX_PLAUSIBLE_TIMER_S:
                    continue
                lim = float(local[x]) - t * (1 + d)
                old = out.get(i)
                out[i] = (min(lim, old[0]), max(t, old[1])) if old else (lim, t)
                timers[i].append(t)
    for i, ts in timers.items():               # low timer, as uptime_model.read_contacts
        out[i] = out[i][:2] + (float(np.percentile(ts, um.HL_LOW_QUANTILE)),)
    return out


def test_headerless(st, args, rng, rounds: int) -> list[dict]:
    readouts, assign, boots = st.readouts, st.assign, st.boots
    rd, b2, tm = st.contacts
    with_contacts = set(np.unique(rd).tolist())
    size = collections.Counter(k for k in assign.values() if k in boots)
    by_b = collections.defaultdict(list)
    for i in sorted(assign):
        if assign[i] in boots and readouts[i]["ct"] is not None:
            by_b[readouts[i]["ID"]].append(i)
    cand = collections.defaultdict(list)
    for b, items in by_b.items():
        for x in range(1, len(items) - 1):
            i, p, n = items[x], items[x - 1], items[x + 1]
            if i in with_contacts and assign[p] != assign[n] and in_window(readouts[i]["header"]):
                cand[b].append(i)
    rows, details = [], []
    for rnd in range(rounds):
        hidden = [rng.choice(v) for v in cand.values()]
        ro = [dict(r) for r in readouts]
        for i in hidden:
            ro[i]["ct"], ro[i]["header"] = None, None
        a2 = {i: k for i, k in assign.items() if i not in set(hidden)}
        hl = dict(st.hl_limit)
        hl.update(headerless_limits(args.log_dir, readouts, hidden, st.drift_of))
        result, _ = um.assign_headerless(ro, a2, boots, st.drift_of, rd, b2, tm, hl)
        by_line = {(r[0], r[1]): r for r in result}
        for i in hidden:
            r = readouts[i]
            row = by_line.get((r["file"], r["first"]))
            truth = assign[i]
            got = row[3] if row else ""
            method = row[4] if row else "not assigned"
            case = "own period" if size[truth] == 1 else "boundary"
            if not got:
                verdict = "undecided"
            else:
                verdict = "right" if got == truth[1][1:] else "wrong"
            details.append([rnd + 1, r["ID"], r["file"], r["first"], case, verdict, truth[1][1:], um._fmt(boots[truth]),
                            got, method])
            rows.append(dict(Test="D header-less", Variant=case, ID=r["ID"], Readout=i, Time=um._fmt(r["header"]),
                             Parameter=method.split(" (")[0], Found=verdict,
                             **{"Error [s]": 0.0, "Other cut points changed": 0}))
        print(f"  test D round {rnd + 1}: {len(hidden)} readouts", flush=True)
    path = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR / "uptime_model_validation_headerless.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Round", "ID", "Log file", "First line", "Case", "Verdict", "True period", "True boot",
                    "Assigned period", "Method"])
        w.writerows(details)
    return rows


def summary_headerless(rows) -> None:
    rows = [r for r in rows if r["Test"].startswith("D")]
    if not rows:
        return
    print(f"{'D header-less':16} {'case':10} {'method':45} {'n':>5} {'right':>6} {'wrong':>6} {'undecided':>9}")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["Variant"], r["Parameter"])].append(r)
    for (case, method), g in sorted(groups.items()):
        c = collections.Counter(r["Found"] for r in g)
        print(f"{'':16} {case:10} {method[:45]:45} {len(g):5} {c['right']:6} {c['wrong']:6} {c['undecided']:9}")
    c = collections.Counter((r["Variant"], r["Found"]) for r in rows)
    for case in ("boundary", "own period"):
        n = sum(v for (cs, _), v in c.items() if cs == case)
        if n:
            print(f"{'':16} {case:10} {'all':45} {n:5} {c[(case, 'right')]:6} {c[(case, 'wrong')]:6} "
                  f"{c[(case, 'undecided')]:9}")


def summary(rows) -> None:
    rows = [r for r in rows if not r["Test"].startswith("D")]
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["Test"], r["Variant"], r["Parameter"] if not r["Test"].startswith("C") else "")].append(r)
    print(f"{'Test':16} {'Variant':16} {'Case':18} {'n':>5} {'found':>6} {'|err| med':>9} {'95%':>7} {'other cuts':>10}")
    for key in sorted(groups):
        g = groups[key]
        err = np.sort(np.abs([r["Error [s]"] for r in g]))
        print(f"{key[0]:16} {key[1]:16} {key[2]:18} {len(g):5} {sum(r['Found'] == 'yes' for r in g) / len(g):6.0%} "
              f"{np.median(err):9.0f} {err[int(0.95 * (len(err) - 1))]:7.0f} "
              f"{sum(r['Other cut points changed'] > 0 for r in g):10}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Synthetic validation of the uptime model.")
    pp.add_common_arguments(parser)
    parser.add_argument("--refit", action="store_true", help="Fit the model again instead of the cached state.")
    parser.add_argument("--per-beacon", type=int, default=3, help="Cases per beacon for tests A and B (default 3).")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--headerless", type=int, default=0, metavar="ROUNDS",
                        help="Test D: rounds of hidden Current Timers (one readout per beacon each); needs a full fit.")
    args = parser.parse_args(argv)
    args.evidence_days = "all"
    t0 = time.time()
    state = load_state(args)
    print(f"state ready, {time.time() - t0:.0f} s", flush=True)
    rng = random.Random(args.seed)
    rows, mismatch = [], 0
    for b in sorted(state["beacons"], key=pp.id_sort_key):
        bc = state["beacons"][b]
        local, h4, cuts = state["inputs"][b]
        base = um.solve_beacon(bc, local, h4, cuts)
        if cuts_of(base) != {a for a, _, _ in state["segments"][b] if a > 0}:
            mismatch += 1
        rows += test_reboot(bc, local, h4, cuts, base, rng, args.per_beacon)
        rows += test_late(bc, local, h4, cuts, base, rng, args.per_beacon)
        rows += test_anchor(bc, local, h4, cuts, base)
    if args.headerless:
        print("test D: full fit for the readouts without Current Timer ...", flush=True)
        rows += test_headerless(um.fit(args), args, rng, args.headerless)
    print(f"{len(rows)} test cases, {time.time() - t0:.0f} s; beacons whose re-solve differs from the fit: {mismatch}")
    out = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR / OUT_CSV
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    summary(rows)
    summary_headerless(rows)
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
