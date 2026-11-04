#!/usr/bin/env python3
"""The contacts without the postprocessing pipeline: the logger's own dating against the pipeline's.

During the deployment the logger dated the contacts itself (its postprocessing.py of 2026-08-15,
git 2e1f52d: aggregate_into, run over all logs by its main()). Its rule, applied here to the
original logs in one pass:

- the lines of all files in line-time order (stable); a contact is dated with the latest
  Current Timer line of its logged ID1 before it (none before: the first one of that ID1):
  time = line time of the Current Timer - (Current Timer - contact timer);
- kept if ID1 and ID2 are in 0-170 or 252-254, the RSSI in -110 .. -10, the time not after its
  own line time and on a calendar day 2026-08-12 .. 2026-08-29;
- nothing else: no ID split 0-9 / 48-57, no fake records, no duplicates, no logging delay, no
  drift, no reboot between a readout's records and its Current Timer.

Compared record by record with the dating of the pipeline (dating/model_dating.py on the working
copies, which keep the line numbers of the originals):

- lost: contacts with status ok in the window that the logger's rule drops;
- time error: the logger's time - the pipeline's time of the same record;
- kept in addition: contacts the logger dates into the window that the pipeline does not count
  (duplicates, fake records, undecided ID, dated outside the window, ...).

python -m reports.without_pipeline: prints the numbers.
"""

from __future__ import annotations

import argparse
import collections
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dating import model_dating as mdt
from lib import logcache
from lib import postprocessing as pp
from lib import study

HERE = Path(__file__).resolve().parents[1]
REASONS = ["dated", "ID1 out of range", "ID2 out of range", "RSSI out of range", "no Current Timer of the ID1",
           "dated after its log line", f"dated outside {pp.STUDY.logger_own_dating.first_day:%m-%d} .. "
           f"{pp.STUDY.logger_own_dating.last_day:%m-%d}",
           "only in the working copy (recovered from a fake record)"]
_OWN = pp.STUDY.logger_own_dating        # the logger's own rule (study.toml [logger_own_dating])
LOGGER_ID_MAX, LOGGER_EXTRA_IDS = _OWN.id_max, _OWN.extra
LOGGER_RSSI = (_OWN.rssi_min, _OWN.rssi_max)
LOGGER_DAYS = (_OWN.first_day, _OWN.last_day)
ERROR_BINS = [(0, 10, "$\\leq$10 s"), (10, 60, "10 s -- 1 min"), (60, 600, "1 -- 10 min"), (600, 3600, "10 -- 60 min"),
              (3600, 6 * 3600, "1 -- 6 h"), (6 * 3600, pp.DAY_S, "6 -- 24 h"), (pp.DAY_S, np.inf, "> 1 day")]


def _logger_id(ids: np.ndarray) -> np.ndarray:
    return ((ids >= 0) & (ids <= LOGGER_ID_MAX)) | np.isin(ids, LOGGER_EXTRA_IDS)


def _logger_id_text(s: str) -> bool:
    try:
        v = int(s)
    except ValueError:
        return False
    return 0 <= v <= LOGGER_ID_MAX or v in LOGGER_EXTRA_IDS


def logger_dating(lines: logcache.Lines) -> tuple[np.ndarray, np.ndarray]:
    """Per line: the logger's time of a contact (seconds as lines.t: naive local time) and the
    reason (index into REASONS: 0 dated; -1 not a contact line)."""
    order = lines.merged_index()
    n = len(order)
    t, kind = lines.t[order], lines.kind[order]
    beacon = lines.id1[order].astype(np.int64)
    id_ok = np.array([_logger_id_text(s) for s in lines.ids], dtype=bool)[beacon]
    is_ct, ct_timer = np.zeros(n, dtype=bool), np.zeros(n, dtype=np.int64)
    for x in np.flatnonzero((kind == 0) & id_ok).tolist():
        m = pp.CURRENT_TIMER_RE.match(lines.rest[lines.other[order[x]]])
        if m:
            is_ct[x], ct_timer[x] = True, int(m.group("timer"))
    # per ID1 in line-time order: Current Timer lines up to each line, counted over all ID1s
    g = np.flatnonzero(id_ok)
    g = g[np.argsort(beacon[g], kind="stable")]
    b = beacon[g]
    cum = np.cumsum(is_ct[g])
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    sizes = np.diff(np.r_[starts, len(g)])
    before = np.r_[0, cum][starts]                       # Current Timer lines of the earlier ID1s
    total = np.repeat(cum[starts + sizes - 1] - before, sizes)
    before = np.repeat(before, sizes)
    ct = g[is_ct[g]]                                     # k-th Current Timer line (merged index)
    pc = np.flatnonzero(kind[g] == 1)
    x = g[pc]
    k = np.where(cum[pc] > before[pc], cum[pc] - 1, before[pc])     # latest before, else the first
    ref = ct[np.minimum(k, max(len(ct) - 1, 0))] if len(ct) else np.zeros(len(pc), dtype=np.int64)
    src = order[x]
    timer = lines.timer[src].astype(np.int64)
    when = t[ref] - (ct_timer[ref] - timer) if len(ct) else np.zeros(len(pc), dtype=np.int64)
    day = when // pp.DAY_S
    d0, d1 = ((d - logcache.EPOCH.date()).days for d in LOGGER_DAYS)
    rssi = lines.rssi[src]
    reason = np.select([~_logger_id(lines.id2[src].astype(np.int64)), (rssi < LOGGER_RSSI[0]) | (rssi > LOGGER_RSSI[1]),
                        total[pc] == 0, when > t[x], (day < d0) | (day > d1)], [2, 3, 4, 5, 6], 0)
    out_when = np.zeros(n, dtype=np.int64)
    out_reason = np.full(n, -1, dtype=np.int8)
    out_reason[order[(kind == 1) & ~id_ok]] = 1
    out_when[src], out_reason[src] = when, reason
    return out_when, out_reason


def pipeline_contacts(pipeline_dir: Path) -> dict:
    """The pipeline's dating per contact line of the working copies (dating/model_dating.py)."""
    keep: dict = {}
    mdt.run(pipeline_dir / "logs", pipeline_dir, pipeline_dir.parent / pp.OUTPUT_DATA_DIR, keep=keep)
    return keep


def compare(original: logcache.Lines, keep: dict, w0: datetime, w1: datetime) -> dict:
    """The numbers of the slide: base = contacts with status ok in the window (pipeline)."""
    b_when, b_reason = logger_dating(original)
    b_real = np.full(len(b_when), np.nan)
    b_real[b_reason == 0] = logcache.local_timestamps(b_when[b_reason == 0])   # the others: no time
    f_of = {p.name: f for f, p in enumerate(original.paths)}
    fo = np.array([f_of[name] for name in keep["names"]], dtype=np.int64)[keep["file"]]
    idx = np.empty(len(fo), dtype=np.int64)
    for f in np.unique(fo).tolist():
        sel = np.flatnonzero(fo == f)
        a = int(original.start[f])
        numbers = original.line[a:original.start[f + 1]].astype(np.int64)
        idx[sel] = a + np.searchsorted(numbers, keep["line"][sel].astype(np.int64))
    idx = np.minimum(idx, len(original.line) - 1)
    same = (original.line[idx] == keep["line"]) & (original.kind[idx] == 1) & (original.id2[idx] == keep["id2"]) \
        & (original.timer[idx] == keep["timer"]) & (original.rssi[idx] == keep["rssi"])
    lo, hi = w0.timestamp(), w1.timestamp()
    when = keep["when"]
    tw = np.round(np.nan_to_num(when, nan=-1.0))
    p_win = ~np.isnan(when) & (tw >= lo) & (tw <= hi)
    ok_code = keep["statuses"].index(mdt.OK)
    base = p_win & (keep["status"] == ok_code)
    r = np.where(same, b_reason[idx], len(REASONS) - 1)
    lost = base & (r != 0)
    m = base & (r == 0)
    dt = b_real[idx[m]] - when[m]
    ad = np.abs(dt)
    logged = np.array([int(s) if s.isdigit() else -1 for s in original.ids], dtype=np.int64)[original.id1[idx]]
    other_id1 = m & (logged != keep["id1"])
    # contacts the logger dates into the window, by the pipeline's view of the same record
    b_win = (b_reason == 0) & (np.round(b_real) >= lo) & (np.round(b_real) <= hi)
    view = np.full(len(original.line), "not a contact line of the working copies", dtype=object)
    status = np.array(keep["statuses"], dtype=object)[keep["status"]]
    view[idx[same]] = np.where(np.isnan(when[same]), "not dated", np.where(p_win[same], status[same],
                                                                            "dated outside the window"))
    extra = collections.Counter(view[b_win].tolist())
    n_base = int(base.sum())
    bins = [int(((ad > a) & (ad <= z)).sum()) if a else int((ad <= z).sum()) for a, z, _ in ERROR_BINS]
    return dict(
        records=len(idx), unmatched=int((~same).sum()), base=n_base,
        lost=int(lost.sum()), lost_by=dict(collections.Counter(REASONS[i] for i in r[lost].tolist())),
        dated=int(m.sum()), other_id1=int(other_id1.sum()),
        mean=float(dt.mean()), mean_abs=float(ad.mean()), median_abs=float(np.median(ad)),
        median=float(np.median(dt)), p90_abs=float(np.percentile(ad, 90)), p99_abs=float(np.percentile(ad, 99)),
        later=float(np.mean(dt > 0)), within60=float(np.mean(ad <= 60)), over_hour=float(np.mean(ad > 3600)),
        over_day=float(np.mean(ad > pp.DAY_S)), out_of_window=int((m & ~((np.round(b_real[idx]) >= lo)
                                                                     & (np.round(b_real[idx]) <= hi))).sum()),
        logger_in_window=int(b_win.sum()), logger_extra=dict(extra), error_bins=bins,
        error_labels=[s for _, _, s in ERROR_BINS])


def numbers(pipeline_dir: Path, w0: datetime, w1: datetime, original: logcache.Lines | None = None) -> dict:
    original = original or logcache.Lines(sorted(pp.ORIGINAL_LOG_DIR.glob("*.log")))
    return compare(original, pipeline_contacts(pipeline_dir), w0, w1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    study.add_argument(parser)
    parser.add_argument("--output-dir", type=Path, default=HERE / pp.DEFAULT_OUTPUT_DIR_NAME)
    args = parser.parse_args(argv)
    n = numbers(args.output_dir / "pipeline", pp.DEFAULT_VALID_SPAN.start, pp.DEFAULT_VALID_SPAN.end)
    for k, v in n.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
