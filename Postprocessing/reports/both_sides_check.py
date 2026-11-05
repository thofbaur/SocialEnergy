#!/usr/bin/env python3
"""Both-sides check of the dated contacts on encounter edges (Postprocessing Method.md section 21).

Every encounter is recorded by both beacons on their own clocks (BI 2.1). Dated independently, the
start and the end of an encounter must agree on both sides. The nearest reverse record alone is no
check: during a long joint stay a reverse record lies within seconds of any time, whatever the
offset. Edges carry the timing:

- records with status ok and at an RSSI both sides record (pp.symmetric_threshold);
- per directed pair (A recorded B) and day: records in 10-s bins, gaps up to 120 s belong to one
  encounter; starts and ends, without the first start and the last end of the day (they mark the
  data coverage, not an encounter edge);
- only clean edges: a start with at least CLEAR_S without a record of the pair before it, an end
  with CLEAR_S after it, of an encounter at least MIN_LEN_S long (records flickering at the
  threshold give edges on one side only and match by chance);
- each start (end) of A->B is matched with the nearest start (end) of B->A within +-MATCH_S;
  dt = A's time - B's time. Chance level: the same with B's edges shifted by CHANCE_SHIFT_S.

Reported per precision class of A's record at the edge and per beacon the median signed dt (a
shifted boot shows as a systematic offset). The class is read from the record's precision
(classify: the largest class of model_dating.PRECISION_S it reaches): the class of the readout that
fixes its time plus the drift term from that readout (section 21), so a record far from it counts
in a higher class.
Output: <quality dir>/both_sides_check.csv (per beacon and class: edges, median dt, median |dt|, 90 % |dt|,
share within 30 s; ID1 "all": the totals per class).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dating import model_dating as mdt
from lib import postprocessing as pp
from lib import study

BIN_S = 10
MERGE_S = 120
MATCH_S = 120
CLEAR_S = 300           # a start needs this long without a record of the pair before it, an end after it
MIN_LEN_S = 60          # ... and an encounter at least this long (flickering at the threshold is no edge)
CHANCE_SHIFT_S = 3600   # chance baseline: the partner's edges shifted by this
OUT_CSV = "both_sides_check.csv"


def classify(prec: np.ndarray) -> np.ndarray:
    p = mdt.PRECISION_S
    return np.select([prec >= p["delay"], prec >= p["log order, person"], prec >= p["log order"], prec >= p["contacts"]],
                     [f"delay only ({p['delay']:.0f} s)", f"log order, person ({p['log order, person']:.0f} s)",
                      f"log order ({p['log order']:.0f} s)", f"contacts ({p['contacts']:.0f} s)"],
                     f"delay-free ({p['delay-free']:.0f} s)")


def edges(c: pd.DataFrame) -> pd.DataFrame:
    """Starts and ends per directed pair: columns ID1, ID2, kind, t, prec."""
    c = c.assign(bin=(c.t // BIN_S).astype(np.int64)).sort_values(["ID1", "ID2", "bin"])
    c = c.drop_duplicates(["ID1", "ID2", "bin"])
    same = (c.ID1.values[1:] == c.ID1.values[:-1]) & (c.ID2.values[1:] == c.ID2.values[:-1])
    gap = np.diff(c.bin.values) * BIN_S > MERGE_S
    new_enc = np.r_[True, ~same | gap]           # first bin of an encounter
    last_enc = np.r_[~same | gap, True]           # last bin of an encounter
    first_pair = np.r_[True, ~same]
    last_pair = np.r_[~same, True]
    t = c.bin.values * float(BIN_S)
    before = np.r_[np.inf, np.diff(t)]            # time since the pair's previous record
    after = np.r_[np.diff(t), np.inf]
    enc_id = np.cumsum(new_enc)
    first_t = pd.Series(t).groupby(enc_id).transform("min").values
    last_t = pd.Series(t).groupby(enc_id).transform("max").values
    long_enough = (last_t - first_t) >= MIN_LEN_S
    starts = new_enc & ~first_pair & (before >= CLEAR_S) & long_enough
    ends = last_enc & ~last_pair & (after >= CLEAR_S) & long_enough
    out = [pd.DataFrame({"ID1": c.ID1.values[m], "ID2": c.ID2.values[m], "kind": k, "t": t[m],
                         "prec": c.prec.values[m]}) for m, k in ((starts, "start"), (ends, "end"))]
    return pd.concat(out, ignore_index=True)


def check_day(path: Path) -> pd.DataFrame:
    c = pd.read_csv(path, usecols=["ID1", "ID2", "RSSI", "Contact Local Time", "Time precision [s]", "Status"])
    c = c[c.Status == "ok"]
    c = c[c.RSSI.values >= pp.symmetric_threshold(c.ID1.values, c.ID2.values)]
    c = pd.DataFrame({"ID1": c.ID1.values, "ID2": c.ID2.values, "prec": c["Time precision [s]"].values,
                      "t": pd.to_datetime(c["Contact Local Time"]).astype("int64").values // 10**9})
    e = edges(c)
    a = e.sort_values("t")
    b = e.rename(columns={"ID1": "ID2", "ID2": "ID1", "t": "tb", "prec": "prec_b"}).sort_values("tb")
    m = pd.merge_asof(a, b, left_on="t", right_on="tb", by=["ID1", "ID2", "kind"], direction="nearest",
                      tolerance=MATCH_S).dropna(subset=["tb"])
    m["dt"] = m.t - m.tb
    m["cls"] = classify(m.prec.values)
    bs = b.assign(tb=b.tb + CHANCE_SHIFT_S).sort_values("tb")
    chance = pd.merge_asof(a, bs, left_on="t", right_on="tb", by=["ID1", "ID2", "kind"], direction="nearest",
                           tolerance=MATCH_S).dropna(subset=["tb"])
    m.attrs["edges"], m.attrs["chance"] = len(a), len(chance)
    return m[["ID1", "ID2", "kind", "t", "dt", "cls"]]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Both-sides check of the dated contacts on encounter edges.")
    study.add_argument(parser)
    parser.add_argument("--data-dir", type=Path, default=pp.ROOT / pp.DEFAULT_OUTPUT_DIR_NAME / pp.OUTPUT_DATA_DIR)
    parser.add_argument("--quality-dir", type=Path, default=pp.PIPELINE_DIR / pp.OUTPUT_QUALITY_DIR)
    parser.add_argument("--days", nargs="*", help="YYYYMMDD; default: all contacts_*.csv")
    args = parser.parse_args(argv)
    paths = sorted(args.data_dir.glob("contacts_*.csv"))
    if args.days:
        paths = [p for p in paths if p.stem.split("_")[1] in args.days]
    parts = []
    for p in paths:
        m = check_day(p)
        parts.append(m)
        print(f"  {p.name}: {m.attrs['edges']} clean edges, {len(m)} matched, by chance (partner shifted "
              f"{CHANCE_SHIFT_S} s) {m.attrs['chance']}", flush=True)
    m = pd.concat(parts, ignore_index=True)
    stats = dict(edges="size", median="median", median_abs=lambda x: x.abs().median(),
                 p90=lambda x: x.abs().quantile(0.9), within_30s=lambda x: (x.abs() <= 30).mean())
    total = m.groupby("cls").dt.agg(**stats).reset_index()
    print(f"Both-sides check on encounter edges ({len(paths)} days, {len(m)} matched edges, within +-{MATCH_S} s):")
    print(total.round(2).to_string(index=False))
    per = m.groupby(["ID1", "cls"]).dt.agg(**stats).reset_index()
    per = per[per.edges >= 30]
    print("beacons (>= 30 edges) with |median dt| > 30 s:")
    print(per[per["median"].abs() > 30].to_string(index=False) or "  none")
    out = args.quality_dir / OUT_CSV
    pd.concat([per, total.assign(ID1="all")], ignore_index=True).round(
        {"median": 1, "median_abs": 1, "p90": 1, "within_30s": 3}).to_csv(out, index=False)
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
