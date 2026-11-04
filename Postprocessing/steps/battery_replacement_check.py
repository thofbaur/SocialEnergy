#!/usr/bin/env python3
"""Compare the documented battery replacements with the boots of the beacons.

A battery replacement restarts the beacon: it starts a new uptime period whose boot (timer-zero
time) lies at the replacement. Run twice by run_pipeline.py: before the uptime model with the boots
of the timer-zero analysis and the contact sync (the model needs the windows, H7), and with
--model uptime on the final model (after the H4 enforcement): the outputs the dating reads
(unread_uptime_periods.csv) and the hard rule check (H7). Evidence used:

- Boot times per uptime period: --model uptime the final uptime model; otherwise corrected by the
  contact sync where available (timer_zero/contact_sync_periods.csv, Postprocessing Method.md
  section 14), else from the readouts (timer_zero/timer_zero_periods.csv, section 13).
- Readouts with Current Timer of the beacon (timer_zero/readout_overview.csv), at their real time
  (the uptime model's, or line time minus the logging delay of the timer-zero analysis).
- Sightings of the beacon by other beacons and the beacon's own contacts from the logs, each
  converted with the corrected boot time and drift of the recording beacon's uptime period (so
  the logging delay does not enter). During a replacement the beacon is off: the others lose it
  shortly before the boot and see it again right after.

Verdict per replacement (window: the documented time and its accuracy, plus 5 min before and
15 min after):
- match: a boot in the window;
- late: a boot up to 75 min after the window; the note says whether the beacon still recorded
  contacts in its old uptime period after the documented time (then it was changed later);
- change without a readout: no boot in or after the window, but the last readout before the
  window belongs to an older boot, the next readout after it to a later boot, and no readout
  lies in between. The replacement started an uptime period that was never read out before the
  beacon rebooted again; the beacon's own contacts of that period are lost (Background
  Information 3.1.1, 3.4.1). The largest gap in the others' sightings within the window shows
  the replacement;
- beacon silent: the others do not see the beacon after a time before the window (defective);
- no match: none of these.

Outputs (quality/ subfolder of --output-dir):
- battery_replacement_check.csv   one row per documented replacement: verdict and evidence
- unread_uptime_periods.csv       uptime periods started by a replacement and never read out:
                                  beacon, from, to, sightings of the beacon by others in between
(Every boot with its cause: Output/data/reboots.csv of the dating.)
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza

BATTERY_CSV = pp.BATTERY_REPLACEMENTS_CSV   # optional: without it, no documented replacements


def read_replacements(path: Path = BATTERY_CSV) -> list[dict]:
    """The documented battery replacements (ID, Battery replaced, Accuracy, Note); none if the study
    has no battery_replacements.csv (every boot after the first is then a reset)."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))
OUTPUT_CSV = "battery_replacement_check.csv"
UNREAD_CSV = "unread_uptime_periods.csv"
SYNC_PERIODS_CSV = "contact_sync_periods.csv"
UM_PERIODS_CSV = "uptime_model_periods.csv"           # --model uptime (uptime_model.py, after h4_enforce.py)
UM_READOUTS_CSV = "uptime_model_readouts.csv"
UM_HEADERLESS_CSV = "uptime_model_headerless.csv"

BEFORE_S = 5 * 60              # window margin before the replacement window
AFTER_S = 15 * 60              # window margin after the replacement window
LATE_S = 75 * 60               # beyond the window: "late" up to this far
APPROX_S = 15 * 60             # half width for "approx."
UNKNOWN_AFTER_S = 3 * 3600     # "unknown": time is the hand-in, the replacement can be later
SIGHTING_SPAN_S = 2 * pp.DAY_S    # sightings kept around each documented replacement and boot
SILENT_S = 3600                # beacon not seen by others for this long before the window: silent

VERDICT_MATCH = "match"
VERDICT_LATE = "late"
VERDICT_UNREAD = "change without a readout"
VERDICT_SILENT = "beacon silent"
VERDICT_NONE = "no match"



def window(time: datetime, accuracy: str) -> tuple[datetime, datetime]:
    """Earliest and latest plausible timer-zero time of a replacement."""
    before = re.match(r"return time; replaced up to (\d+) min earlier", accuracy)
    plus_minus = re.match(r"±(\d+) min", accuracy)
    if before:
        low, high = int(before.group(1)) * 60, 0
    elif plus_minus:
        low = high = int(plus_minus.group(1)) * 60
    elif accuracy.startswith("approx"):
        low = high = APPROX_S
    elif accuracy == "unknown":
        low, high = 0, UNKNOWN_AFTER_S
    else:
        low = high = 0
    return time - timedelta(seconds=low + BEFORE_S), time + timedelta(seconds=high + AFTER_S)


def _ts(value: str) -> float:
    return datetime.strptime(value, pp.TIMESTAMP_FORMAT).timestamp()


def _fmt(t: Optional[float]) -> str:
    return datetime.fromtimestamp(t).strftime(pp.TIMESTAMP_FORMAT) if t is not None else ""


class Boots:
    """Uptime periods per beacon: (boot, period, source), boot corrected where possible."""

    def __init__(self, tz_dir: Path | None):
        self.unit: dict[tuple[str, str], tuple[float, float]] = {}      # (ID, period) -> (boot, drift s/s)
        self.by_beacon: dict[str, list[tuple[float, str, str]]] = collections.defaultdict(list)
        if tz_dir is None:
            return
        for row in csv.DictReader((tz_dir / tza.PERIODS_CSV).open(encoding="utf-8")):
            self.unit[(row["ID"], row["Period"])] = (_ts(row["Zero true"]), float(row["Drift [s/day]"]) / pp.DAY_S)
        source = {key: "readouts" for key in self.unit}
        sync_csv = tz_dir / SYNC_PERIODS_CSV
        if sync_csv.exists():
            for row in csv.DictReader(sync_csv.open(encoding="utf-8")):
                key = (row["ID"], row["Period"])
                if key in self.unit and row["Zero new"]:
                    self.unit[key] = (_ts(row["Zero new"]), float(row["Drift new [s/day]"]) / pp.DAY_S)
                    source[key] = "contact sync"
        self._index(source)

    @classmethod
    def of_uptime_model(cls, tz_dir: Path) -> "Boots":
        """The periods, boots and drifts of the final uptime model (uptime_model_periods.csv)."""
        boots = cls(None)
        for row in csv.DictReader((tz_dir / UM_PERIODS_CSV).open(encoding="utf-8")):
            boots.unit[(row["ID"], row["Period"])] = (_ts(row["Boot"]), float(row["Drift [s/day]"]) / pp.DAY_S)
        boots._index({key: "uptime model" for key in boots.unit})
        return boots

    def _index(self, source: dict) -> None:
        for (beacon, period), (boot, _) in self.unit.items():
            self.by_beacon[beacon].append((boot, period, source[(beacon, period)]))
        for boots in self.by_beacon.values():
            boots.sort()


def uptime_model_readouts(tz_dir: Path) -> dict[tuple[str, int], tuple[str, Optional[float]]]:
    """(log file, first line) -> (period, real time) of the readouts in the final uptime model: with
    Current Timer (uptime_model_readouts.csv) and without, as assigned (uptime_model_headerless.csv;
    a period of its own is left out, as in the timer-zero analysis' periods)."""
    out = {}
    for row in csv.DictReader((tz_dir / UM_READOUTS_CSV).open(encoding="utf-8")):
        if row["Period"]:
            out[(row["Log file"], int(row["First line"]))] = (row["Period"], _ts(row["Real time"]))
    path = tz_dir / UM_HEADERLESS_CSV
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8")):
            if row["Period"]:
                out[(row["Log file"], int(row["First line"]))] = (row["Period"], None)
    return out


def read_readouts(overview_csv: Path, model: dict | None = None
                  ) -> tuple[dict[str, list[tuple[float, str]]], dict[str, list]]:
    """Readouts with Current Timer per beacon: (real time, period); and line ranges per log file
    -> (first, last, ID, period) of all readouts assigned to a period. Periods and real times of the
    timer-zero analysis (section 13: line time minus the logging delay), or of the uptime model
    (model: uptime_model_readouts)."""
    with_ct: dict[str, list[tuple[float, str]]] = collections.defaultdict(list)
    ranges: dict[str, list] = collections.defaultdict(list)
    for row in csv.DictReader(overview_csv.open(encoding="utf-8")):
        if model is not None:
            period, real = model.get((row["Log file"], int(row["First line"])), ("", None))
        else:
            period = row["Period"]
            # real time of the readout: line time minus its logging delay (section 13)
            real = _ts(row["Start time"]) - (float(row["Delay [s]"]) if row["Delay [s]"] else 0.0)
        if row["Current Timer present"] == "yes" and period and real is not None:
            with_ct[row["ID"]].append((real, period))
        if period:
            ranges[row["Log file"]].append((int(row["First line"]), int(row["Last line"]), row["ID"], period))
    for v in with_ct.values():
        v.sort()
    for v in ranges.values():
        v.sort()
    return with_ct, ranges


def read_sightings(log_dir: Path, ranges, boots: Boots, spans: dict[str, list[tuple[float, float]]]):
    """Real times at which other beacons saw each beacon of interest, and of its own contacts,
    within the given spans (converted with the recording beacon's corrected boot and drift)."""
    seen: dict[str, list[float]] = collections.defaultdict(list)
    own: dict[str, list[float]] = collections.defaultdict(list)
    if not spans:                       # no documented replacements (battery_replacements.csv optional)
        return seen, own
    lo = min(a for v in spans.values() for a, _ in v) - pp.DAY_S
    hi = max(b for v in spans.values() for _, b in v) + pp.DAY_S
    lines = logcache.Lines([p for p in sorted(log_dir.glob("*.log")) if ranges.get(p.name)])
    items = [(name, first, last, (beacon, period)) for name, rs in ranges.items() for first, last, beacon, period in rs]
    of = lines.readout_of([x[:3] for x in items])
    unit = [boots.unit.get(x[3]) for x in items]
    boot = np.array([u[0] if u else np.nan for u in unit] + [np.nan])     # [-1]: no readout
    scale = np.array([1 + u[1] if u else np.nan for u in unit] + [np.nan])
    id1 = np.array([int(b) if b.isdigit() else -1 for b in lines.ids] or [0], dtype=np.int64)[lines.id1]
    sel = np.flatnonzero((lines.kind == 1) & (id1 >= 0) & ~np.isnan(boot[of]))
    a, b = id1[sel], lines.id2[sel].astype(np.int64)
    t = boot[of[sel]] + lines.timer[sel].astype(np.int64) * scale[of[sel]]
    inside = (lo <= t) & (t <= hi)
    for x, v in spans.items():
        if not x.isdigit():
            continue
        for times, mask in ((seen, b == int(x)), (own, a == int(x))):
            tx = t[mask & inside]
            hit = np.zeros(len(tx), dtype=bool)
            for y, z in v:
                hit |= (y <= tx) & (tx <= z)
            if hit.any():
                times[x].extend(tx[hit].tolist())
    for d in (seen, own):
        for v in d.values():
            v.sort()
    return seen, own


def _around(times: list[float], t: float) -> tuple[Optional[float], Optional[float]]:
    """Last time before t and first time at or after t."""
    k = bisect.bisect_left(times, t)
    return (times[k - 1] if k else None), (times[k] if k < len(times) else None)


def _largest_gap(times: list[float], a: float, b: float) -> tuple[float, Optional[float], Optional[float]]:
    w = times[bisect.bisect_left(times, a):bisect.bisect_right(times, b)]
    if len(w) < 2:
        return 0.0, None, None
    g = max(range(len(w) - 1), key=lambda k: w[k + 1] - w[k])
    return w[g + 1] - w[g], w[g], w[g + 1]


def check(battery: list[dict], boots: Boots, readouts, seen, own) -> tuple[list[dict], list[dict]]:
    rows, unread = [], []
    for b in battery:
        x = b["ID"]
        time = datetime.strptime(b["Battery replaced"], "%Y-%m-%d %H:%M")
        low, high = (t.timestamp() for t in window(time, b["Accuracy"]))
        noted = time.timestamp()
        beacon_boots = boots.by_beacon.get(x, [])
        in_window = [p for p in beacon_boots if low <= p[0] <= high]
        late = [p for p in beacon_boots if high < p[0] <= high + LATE_S]
        s = seen.get(x, [])
        row: dict = dict(ID=x, time=b["Battery replaced"], accuracy=b["Accuracy"], low=low, high=high,
                   verdict=VERDICT_NONE, boot=None, period="", source="", off=None, on=None,
                   gap=(0.0, None, None), own_after=0, own_until=None, last_readout=None, next_readout=None,
                   next_boot=None, note="")
        boot = (in_window or late or [None])[0]
        if boot:
            row.update(boot=boot[0], period=boot[1], source=boot[2])
            row["off"], row["on"] = _around(s, boot[0])
            row["verdict"] = VERDICT_MATCH if in_window else VERDICT_LATE
            if not in_window:
                o = own.get(x, [])
                after = o[bisect.bisect_left(o, noted + 60):bisect.bisect_left(o, boot[0] - 60)]
                row["own_after"], row["own_until"] = len(after), (after[-1] if after else None)
                row["note"] = (f"still recorded {len(after)} contacts in its old uptime period until "
                               f"{_fmt(after[-1])}: changed later than documented" if after else
                               "the data cannot tell whether the change was later than documented or a second "
                               "boot followed the change before any readout")
            rows.append(row)
            continue
        rds = readouts.get(x, [])
        after_k = bisect.bisect_right(rds, (high, "~"))
        before = rds[after_k - 1] if after_k else None          # last readout up to the window end
        after = rds[after_k] if after_k < len(rds) else None     # first readout after it
        row["last_readout"], row["next_readout"] = before, after
        # the change lies after the last readout of the old boot
        row["gap"] = _largest_gap(s, max(low, before[0]) if before else low, high)
        prev_boot = boots.unit.get((x, before[1]), (None,))[0] if before else None
        next_boot = boots.unit.get((x, after[1]), (None,))[0] if after else None
        row["next_boot"] = next_boot
        last_seen = s[bisect.bisect_left(s, high) - 1] if bisect.bisect_left(s, high) else None
        if last_seen is not None and last_seen < low - SILENT_S and not any(t > high for t in s):
            row["verdict"] = VERDICT_SILENT
            row["note"] = f"last seen by other beacons {_fmt(last_seen)}"
        elif (before and after and prev_boot is not None and next_boot is not None
              and prev_boot < low and next_boot > high):
            row["verdict"] = VERDICT_UNREAD
            start = row["gap"][1] if row["gap"][1] else noted
            seen_between = sum(1 for t in s if start <= t < next_boot)
            unread.append(dict(ID=x, start=start, end=next_boot, cause=f"battery replacement {b['Battery replaced']}",
                               seen=seen_between, last_readout=before[0], next_readout=after[0]))
            row["note"] = (f"last readout {_fmt(before[0])} (boot {_fmt(prev_boot)}), next readout {_fmt(after[0])} "
                           f"(boot {_fmt(next_boot)}), none in between: the uptime period started by the "
                           f"replacement was never read out ({(next_boot - start) / 3600:.1f} h)")
        else:
            nearest = min(beacon_boots, key=lambda p: abs(p[0] - noted), default=None)
            row["note"] = f"nearest boot {_fmt(nearest[0])} (period {nearest[1]})" if nearest else "no boots"
        rows.append(row)
    return rows, unread


def write_outputs(out_dir: Path, rows, unread) -> None:
    with (out_dir / OUTPUT_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Battery replaced", "Accuracy", "Window from", "Window to", "Verdict", "Boot",
                    "Boot - battery [min]", "Period", "Boot from", "Others last saw it", "Others saw it again",
                    "Largest gap in others' sightings in the window [s]", "Gap from", "Gap to",
                    "Own contacts in the old period after the documented time", "Last readout before",
                    "Next readout after", "Note"])
        for r in rows:
            noted = datetime.strptime(r["time"], "%Y-%m-%d %H:%M").timestamp()
            g, ga, gb = r["gap"]
            w.writerow([r["ID"], r["time"], r["accuracy"], _fmt(r["low"]), _fmt(r["high"]), r["verdict"],
                        _fmt(r["boot"]), round((r["boot"] - noted) / 60, 1) if r["boot"] else "", r["period"],
                        r["source"], _fmt(r["off"]), _fmt(r["on"]), round(g) if ga else "", _fmt(ga), _fmt(gb),
                        r["own_after"] or "", _fmt(r["last_readout"][0]) if r["last_readout"] else "",
                        _fmt(r["next_readout"][0]) if r["next_readout"] else "", r["note"]])
    with (out_dir / UNREAD_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "From", "To", "Hours", "Cause", "Last readout before", "Next readout after",
                    "Sightings of the beacon by others in between"])
        for u in unread:
            w.writerow([u["ID"], _fmt(u["start"]), _fmt(u["end"]), round((u["end"] - u["start"]) / 3600, 1),
                        u["cause"], _fmt(u["last_readout"]), _fmt(u["next_readout"]), u["seen"]])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Compare battery replacements with the boots of the beacons.")
    pp.add_common_arguments(parser)
    parser.add_argument("--battery-csv", type=Path, default=BATTERY_CSV,
                        help=f"Documented battery replacements (optional). Default: {BATTERY_CSV}")
    parser.add_argument("--model", choices=("timer-zero", "uptime"), default="timer-zero",
                        help="Boots, periods and readout times of the timer-zero analysis and the contact sync "
                             "(before the uptime model, which needs the windows) or of the final uptime model "
                             "(run_pipeline.py after the H4 enforcement: the outputs the dating reads).")
    args = parser.parse_args(argv)
    tz_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    if not (tz_dir / tza.PERIODS_CSV).exists() or not (tz_dir / tza.OVERVIEW_CSV).exists():
        print(f"{tz_dir} lacks the timer-zero analysis; run run_pipeline.py first.", file=sys.stderr)
        return 1
    if args.model == "uptime" and not (tz_dir / UM_PERIODS_CSV).exists():
        print(f"{tz_dir / UM_PERIODS_CSV} not found: run uptime_model.py first.", file=sys.stderr)
        return 1
    if args.model == "timer-zero" and not (tz_dir / SYNC_PERIODS_CSV).exists():
        print(f"{SYNC_PERIODS_CSV} not found: boot times from the readouts only (run contact_sync_analysis.py "
              "first for corrected ones).")
    out_dir = args.output_dir / pp.OUTPUT_QUALITY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    battery = read_replacements(args.battery_csv)
    if not args.battery_csv.exists():
        print(f"{args.battery_csv} not found: no documented battery replacements")
    if args.model == "uptime":
        boots = Boots.of_uptime_model(tz_dir)
        readouts, ranges = read_readouts(tz_dir / tza.OVERVIEW_CSV, uptime_model_readouts(tz_dir))
    else:
        boots = Boots(tz_dir)
        readouts, ranges = read_readouts(tz_dir / tza.OVERVIEW_CSV)
    spans: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for b in battery:
        t = datetime.strptime(b["Battery replaced"], "%Y-%m-%d %H:%M").timestamp()
        spans[b["ID"]].append((t - SIGHTING_SPAN_S, t + SIGHTING_SPAN_S))
    print(f"Battery replacements ({args.model} boots): reading the sightings in {args.log_dir} ...", flush=True)
    seen, own = read_sightings(args.log_dir, ranges, boots, spans)

    rows, unread = check(battery, boots, readouts, seen, own)
    write_outputs(out_dir, rows, unread)

    counts = Counter(r["verdict"] for r in rows)
    print(f"{len(rows)} battery replacements: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    for r in rows:
        if r["verdict"] != VERDICT_MATCH:
            noted = datetime.strptime(r["time"], "%Y-%m-%d %H:%M").timestamp()
            delta = f"{(r['boot'] - noted) / 60:+.0f} min" if r["boot"] else ""
            print(f"  {r['ID']:>4} {r['time']}  {r['verdict']} {delta}  {r['note']}")
    print(f"{len(unread)} uptime period(s) started by a replacement and never read out: " + "; ".join(
        f"{u['ID']} {_fmt(u['start'])[5:16]} to {_fmt(u['end'])[5:16]}" for u in unread))
    print(f"Written to {out_dir / OUTPUT_CSV} and {out_dir / UNREAD_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
