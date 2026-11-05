#!/usr/bin/env python3
"""Uptime periods and boots of all beacons from one model: hard rules as constraints, the evidence
as costs (Postprocessing Method.md section 19).

Per beacon, its readouts with Current Timer in log order are cut into uptime periods; each period
gets a boot. Among all cuttings and boots that obey the hard rules (hard_rule_check.py, section
18) the model takes the one with the lowest cost. "Each readout its own period" always obeys the
rules, so a solution always exists.

For readout r of beacon b in period k (drift d_b from the contact sync):
    z_r = line time of Current Timer - C_r (1 + d_b)       observed timer-zero time
    delay_r = z_r - B_k                                   >= lower bound lb_r from its contacts

Hard constraints per period (boot B):
    H1  B <= z_r - lb_r + eps_r for all its readouts (lb_r: contacts recorded during the readout,
        fake records and invalid contacts left out)
    H3  Current Timer does not decrease within the period
    H5  B >= real time of the previous period's last readout - eps
    H6  B >= z_a - eps_a - ANCHOR_MARGIN_S for its delay-free readouts (delay at most ANCHOR_MARGIN_S + eps)
    H7  no period holds readouts both before and after the window of a documented battery
        replacement (the replacement's boot may be a period without readout)
    H9  B >= FLASH_START (the beacons were flashed from then on)
    H4c a period without a reliable boot from its own contacts: none of its readouts lies after the next
        readout in log order of another beacon whose period has reliable contact boots and lies on the
        consensus sequence below (R <= R_ref + eps). Such boots rest on the delay cost alone, which
        places them late (section 19, log-order cap); the cap only moves a boot earlier and gives way
        to H1, H5, H6 and H9.
Costs:
    delay       sum over the readouts of log(1 + |delay| / DELAY_TAU_S): a readout is not logged before it
                happens, eps is only rounding (a cost on positive delays alone placed the boots from the
                delay at the H1 limit, delay -eps: 12 s late, section 19); a delay-free readout: of its
                distance outside 0 .. ANCHOR_MARGIN_S (it was logged within that of its real time)
    contacts    per readout with a reliable boot from its own contacts (peak >= MIN_PEAK matched
                encounter edges, peak >= MIN_RATIO x second peak): weight x min(((B - boot) / SIGMA)^2, CAP)
    H4          per readout, against the nearest readouts of other beacons before and after it in log
                order that lie on the longest log-ordered sequence of real times (the consensus of all
                beacons, as in hard_rule_check.py): H4_WEIGHT x min((violation / 60 s)^2, CAP)
    reboot      REBOOT_PENALTY per period boundary, by beacon type (list_persons.csv / list_rooms.csv)
    log order   a period without a reliable boot from its own contacts: per readout, its real time
                interpolated by its position in log order between the nearest readouts of other
                beacons before and after it that the H4c references would accept (same log file,
                at most INTERP_MAX_SPAN_S apart): INTERP_WEIGHT x min(((R - prediction) / sigma)^2, CAP),
                sigma = INTERP_SIGMA0_S + INTERP_SIGMA_SLOPE x their distance (section 19: on readouts
                with reliable contact boots the prediction is unbiased, 90 % within 21 s for brackets
                below 120 s; the delay cost alone places these boots about 11 s late)

The hard rules H1, H5, H6 and H7 are the strongest costs, not absolute: violating one costs
HARD_PENALTY (+1 per 6 s of violation), more than any evidence. A solution therefore always exists;
a violation remains only where the data contradicts itself (e.g. a readout under a wrong ID) and is
reported. H3 within a period stays absolute: a cut is always possible.

Solution: candidate cut points (current boundaries, falling Current Timer, jumps in the reliable
contact boots, readouts after which a reboot is physically possible, battery windows); dynamic
programming over them per beacon; per period the boot with the lowest cost in its allowed interval.
Iterated: the contact boots and the H4 neighbours are recomputed with the new periods.

Outputs (timer_zero/ subfolder of --output-dir):
- uptime_model_periods.csv    per period: beacon, number, first and last readout, readouts, boot,
                              drift, reliable contact readouts, readouts with a log-order prediction,
                              periods before, change to before, violated hard rules
- uptime_model_readouts.csv   per readout with Current Timer: period, boot, real time, delay,
                              boot from its contacts and its reliability
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import math
import os
import sys
import time
import types
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import contact_sync_analysis as cs
from steps import hard_rule_check as hrc
from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza

PERIODS_CSV = "uptime_model_periods.csv"
READOUTS_CSV = "uptime_model_readouts.csv"
FAKES_CSV = "uptime_model_fake_records.csv"
HEADERLESS_CSV = "uptime_model_headerless.csv"
STATE_PKL = "uptime_model_state.pkl"       # final state for label_test (second ID split)
FLASH_START_TS = pp.FLASH_START.timestamp()
FIT_BIN_S = 10                     # boot from the partners' records: bin of the record times
FIT_MAX_WIDTH_S = 1500             # ... accepted if the 90 % width of the peak is at most this,
FIT_MIN_RATIO = 1.1                # ... the peak at least this times the best score more than FIT_AWAY_S away,
FIT_AWAY_S = 600
FIT_MATCH_S = 600                  # a candidate boot within this of the peak matches it (the record match runs a few
                                   # minutes early: it sees co-presence, not the encounter edge; test D)
FIT_MIN_COUNT, FIT_BACKGROUND = 50, 4.0  # ... at least this many matches and this times the median
MOVE_MAX_S = 90                    # ... a candidate's boot may move at most this much earlier to fit it
HL_LOW_QUANTILE = 10               # %: the "oldest" contact of a readout without Current Timer (robust against
                                   # contacts sent twice after a lost confirmation, BI 3.1.4)
HL_GRID_MIN = 10                   # scan times: a candidate period is impossible if one of its readouts holds at least
                                   # this many scan times (contact timers) in the readout's timer range and none of
                                   # them is one of the readout's (one uptime stretch, one set of scan times)
HL_BACKOFF_S = 1800                # a readout's oldest contacts (HL_LOW_QUANTILE) lie at most this below the newest
                                   # contact the period's previous readout delivered (contacts are sent oldest first;
                                   # a lost confirmation resends the last batch: at most 1 111 s, 99 % within 84 s)
COP_WINDOW_S = 600                 # co-presence: a partner recorded the beacon within this of the contact's time
COP_BIN_S = 60                     # ... record times binned to this
COP_MIN_ACTIVE = 50                # ... judged on at least this many contacts whose partner was recording then,
COP_MIN_SHARE = 0.10               # ... the period rejected below this share (own period: 99 % above 0.71,
                                   # neighbouring periods 28 % below 0.10; 2026-10-25, 2 163 / 2 772 readouts)
OWN_MAX_WIDTH_S = 3600             # own period forced by the hard rules (no period possible): the partners' fit is
                                   # accepted up to this 90 % width and without the rival test (FIT_MIN_RATIO); the
                                   # records get the precision of half the width
PERSONS_CSV = pp.LIST_PERSONS_CSV
ROOMS_CSV = pp.LIST_ROOMS_CSV

# delay-free readouts (first of a log file, H6) are logged within this of their real time; readouts of
# periods with reliable contact boots show 0 .. 12 s (5 / 95 %, median 1 s; 2026-10-24); before 60 s
ANCHOR_MARGIN_S = hrc.ANCHOR_MARGIN_S
LB_MAX_EXCESS_S = pp.DELAY_BOUND_EXCESS_S   # contacts at most this far above Current Timer count for the lower bound
DELAY_TAU_S = 60.0
SIGMA_S = 20.0
CAP = 16.0
MIN_PEAK, MIN_RATIO = 15, 1.5
MIN_BACKGROUND = 4.0               # ... and the peak at least this times the mean count per window in its range
H4_WEIGHT, H4_SEARCH = 2.0, 50     # H4: weight; readouts searched for the nearest neighbour of another beacon
# H4c references only in periods with reliable contact boots. Tested 2026-10-22 with every readout of
# another beacon on the consensus sequence instead (boots without contact evidence are late, never early,
# so they can only loosen a cap): the nearest one is often such a late readout, H4 in the deployment
# 17 -> 24 and the readouts without contact evidence after their next reliable neighbour 10 -> 13 %.
H4C_RELIABLE_ONLY = True
# log-order position as evidence for periods without reliable contact boots (see the docstring)
INTERP_MAX_SPAN_S = 600
INTERP_SIGMA0_S, INTERP_SIGMA_SLOPE = 5.0, 0.25
INTERP_WEIGHT = 1.0
REBOOT_PENALTY = {"person": 8.0, "room": 30.0, "other": 8.0}
CONTACT_JUMP_S = 120
MAX_DELAY_S = pp.MAX_DELAY_S       # longest logging delay assumed for the reboot candidates (also the delay bound's limit)
DELAY_NOTE_S = 3600                # readouts logged within this of MAX_DELAY_S are reported (the bound may have cut them)
LOG_DELAY_S = 3 * 3600             # longest logger delay expected (BI); a readout without Current Timer needs no more ...
LOG_DELAY_WINDOW_S = 3600          # ... unless the readouts with Current Timer logged within this of its line time
LOG_DELAY_MARGIN_S = 1800          # show a longer delay (backlog): then the largest delay that at least two
                                   # different beacons reach (one misdated readout cannot widen it) plus this margin
WIDE_S, NARROW_S, MATCH_S = 12 * 3600, 600, 30
ITERATIONS = 3
INF = float("inf")
HARD_PENALTY = 1000.0              # a violated hard rule costs more than any evidence (data contradicts itself)


def _ts(value: str) -> float:
    return datetime.strptime(value, pp.TIMESTAMP_FORMAT).timestamp()


def _fmt(t: Optional[float]) -> str:
    return datetime.fromtimestamp(t).strftime(pp.TIMESTAMP_FORMAT) if t is not None else ""


# ------------------------------------------------------------------ inputs

def beacon_types(persons_csv: Path, rooms_csv: Path) -> dict[str, str]:
    types = {}
    for path, kind in ((persons_csv, "person"), (rooms_csv, "room")):
        if path.exists():
            text = path.read_text(encoding="utf-8-sig")
            delimiter = ";" if ";" in text.splitlines()[0] else ","
            for row in csv.DictReader(text.splitlines(), delimiter=delimiter):
                if row.get("ID", "").strip():
                    types[row["ID"].strip()] = kind
    return types


def read_drifts(tz_dir: Path) -> tuple[dict[str, float], float]:
    """Per beacon drift (s/s) from the contact sync; the centre for the others."""
    drifts, values = {}, []
    path = tz_dir / "contact_sync_drift.csv"
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8")):
            drifts[row["ID"]] = float(row["Drift [s/day]"]) / pp.DAY_S
            values.append(drifts[row["ID"]])
    centre = float(np.median(values)) if values else tza.DRIFT_S_PER_S
    return drifts, centre


def read_contacts(log_dir: Path, readouts: list[dict], drift_of, hl_limit: dict | None = None,
                  eco_out: list | None = None) -> tuple:
    """Contacts per readout (index, ID2, timer), the delay lower bound of each readout from the
    contacts recorded during it, and the fake records found (file, line). The contacts returned are
    the ones the other side records as well (pp.symmetric_threshold): they serve as encounter
    evidence (edges, partners' records).

    The lines come from the cache of the logs (lib/logcache.py), which holds the fake records found
    while reading: runs of consecutive contact lines of one ID1 are put back into their 5-byte form
    and searched for complete logger messages (logcache.fake_runs, BI 3.2.2). Fake
    records and contacts that fail the validity checks are left out of everything else.

    hl_limit (optional, filled): readout without Current Timer -> (latest boot its contacts allow,
    largest timer, low timer): every contact is recorded before its own log line, so boot <= line
    time - timer x (1 + drift) (H1 for the single contact); low timer: the HL_LOW_QUANTILE % quantile
    of its contact timers, its "oldest" contact.

    eco_out (optional, filled): the eco sessions [(readout index, enter, leave)]."""
    paths = [log_dir / name for name in sorted({r["file"] for r in readouts}) if (log_dir / name).exists()]
    lines = logcache.Lines(paths)
    of = lines.readout_of([(r["file"], r["first"], r["last"]) for r in readouts])
    fakes = [(lines.paths[f].name, n) for f, n in zip(lines.file[lines.fake].tolist(), lines.line[lines.fake].tolist())]
    # file after file, lines in file order
    idx = np.flatnonzero((lines.kind == 1) & ~lines.fake & pp.valid_beacon_ids(lines.id2)
                         & (lines.rssi >= pp.DEFAULT_VALID_RSSI_MIN) & (lines.rssi <= pp.DEFAULT_VALID_RSSI_MAX)
                         & (lines.timer <= tza.MAX_PLAUSIBLE_TIMER_S) & (of >= 0))
    rd, t = of[idx], lines.timer[idx].astype(np.int64)
    ct = np.array([np.nan if r["ct"] is None else r["ct"] for r in readouts], dtype=np.float64)
    header = np.array([np.nan if r["header"] is None else r["header"] for r in readouts], dtype=np.float64)
    scale = np.array([1 + drift_of(r["ID"]) for r in readouts], dtype=np.float64)
    has_ct = ~np.isnan(ct[rd])
    # delay lower bound: contacts recorded during the readout (timer above Current Timer)
    lb = np.zeros(len(readouts))
    sel = np.flatnonzero(has_ct & (ct[rd] < t) & (t <= ct[rd] + LB_MAX_EXCESS_S))
    if len(sel):
        i = rd[sel]
        excess = (t[sel] - ct[i]) * scale[i] - (logcache.local_timestamps(lines.t[idx[sel]]) - header[i])
        best = np.full(len(readouts), -np.inf)
        np.maximum.at(best, i, excess)
        lb[best > 0.0] = best[best > 0.0]
    if hl_limit is not None:
        sel = np.flatnonzero(~has_ct)
        i, ts = rd[sel], t[sel]
        lim = logcache.local_timestamps(lines.t[idx[sel]]) - ts * scale[i]
        cuts = np.flatnonzero(np.diff(i)) + 1          # the contacts of a readout are consecutive lines
        timers = collections.defaultdict(list)
        for a, b in zip(np.r_[0, cuts].tolist(), np.r_[cuts, len(i)].tolist()):
            k, low, top = int(i[a]), float(lim[a:b].min()), int(ts[a:b].max())
            old = hl_limit.get(k)
            hl_limit[k] = (min(low, old[0]), max(top, old[1])) if old else (low, top)
            timers[k].append(ts[a:b])
        for k, parts in timers.items():
            hl_limit[k] = hl_limit[k][:2] + (float(np.percentile(np.concatenate(parts), HL_LOW_QUANTILE)),)
    if eco_out is not None:
        for n in np.flatnonzero((lines.kind == 0) & (of >= 0)).tolist():
            rest = lines.rest[lines.other[n]]
            e = pp.ECO_SESSION_RE.match(rest) if rest.startswith(pp.TEXT_PREFIX["eco_session"]) else None
            if e:
                eco_out.append((int(of[n]), int(e.group("enter")), int(e.group("leave"))))
    b2, tm = lines.id2[idx].astype(np.int64), t.astype(np.float64)
    # only records with a counterpart on the other side (BI 2.1: a person-worn beacon records a location
    # tag down to -100 dBm, the tag records it only down to -80 dBm); the delay lower bound and hl_limit
    # above use all records
    id1 = np.array([int(r["ID"]) if r["ID"].isdigit() else -1 for r in readouts], dtype=np.int64)
    keep = lines.rssi[idx] >= pp.symmetric_threshold(id1[rd], b2)
    return rd[keep], b2[keep], tm[keep], lb, fakes


def battery_windows(path: Path) -> dict[str, list[tuple[float, float]]]:
    out = collections.defaultdict(list)
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8")):
            if row["Verdict"] != "beacon silent":
                out[row["ID"]].append((_ts(row["Window from"]), _ts(row["Window to"])))
    return out


# ------------------------------------------------------------------ contact boot per readout

def contact_reliable(c) -> bool:
    """A boot from contacts (boot, peak, second peak, background) is reliable: at least MIN_PEAK
    matched edges, MIN_RATIO times the second peak and MIN_BACKGROUND times the background."""
    return (c is not None and c[1] >= MIN_PEAK and c[1] >= MIN_RATIO * max(c[2], 1)
            and c[1] >= MIN_BACKGROUND * max(c[3], 1.0))


def edge_offsets(queries: list[tuple[int, int, np.ndarray, int, int]], edges: list[np.ndarray],
                 chunk: int = 20_000_000) -> dict[int, np.ndarray]:
    """queries: (readout, index into edges, the readout's own edge times, lo, hi), the queries of a
    readout consecutive; edges: sorted edge times. Per readout with a query: all edges[g] - own
    within [lo, hi] of its queries, sorted (contact_sync_analysis._diffs of each query, joined).
    Edge times and bounds are whole seconds (edge times are multiples of the bin), so the shifted
    search and the shifted sort below are exact."""
    out: dict[int, np.ndarray] = {}
    if not queries:
        return out
    E = np.concatenate(edges)
    first = np.r_[0, np.cumsum([len(e) for e in edges])]
    rid = np.array([q[0] for q in queries], dtype=np.int64)
    g = np.array([q[1] for q in queries], dtype=np.int64)
    qlo = np.array([q[3] for q in queries], dtype=float)
    qhi = np.array([q[4] for q in queries], dtype=float)
    n = np.array([len(q[2]) for q in queries], dtype=np.int64)
    x_all = np.concatenate([q[2] for q in queries])
    reach = float(max(np.abs(qlo).max(), np.abs(qhi).max()))
    low = min(float(E.min()), float(x_all.min()))
    span = 2.0 ** np.ceil(np.log2(max(float(E.max()), float(x_all.max())) - low + 2 * reach + 2))
    key = (E - low) + np.repeat(np.arange(len(edges)), np.diff(first)) * span    # one range per edge array
    width = 2.0 ** np.ceil(np.log2(2 * reach + 2))                              # one range per readout
    group = np.cumsum(np.r_[True, rid[1:] != rid[:-1]]) - 1                     # readout number per query
    xq = np.repeat(np.arange(len(queries)), n)
    xs = np.r_[0, np.cumsum(n)]
    # chunks of whole readouts, by the most offsets they can give
    load = np.cumsum((first[g + 1] - first[g]) * n)
    heads = np.flatnonzero(np.r_[True, rid[1:] != rid[:-1]])
    before = np.r_[0, load][heads]
    breaks = heads[np.r_[True, np.diff(before // chunk) > 0]].tolist() + [len(queries)]
    for qa, qb in zip(breaks[:-1], breaks[1:]):
        q = xq[xs[qa]:xs[qb]]
        x = x_all[xs[qa]:xs[qb]]
        base = (x - low) + g[q] * span
        j0 = np.searchsorted(key, base + qlo[q], "left")
        cnt = np.maximum(np.searchsorted(key, base + qhi[q], "right") - j0, 0)
        rep = np.repeat(np.arange(len(x)), cnt)
        d = E[j0[rep] + (np.arange(len(rep)) - np.repeat(np.cumsum(cnt) - cnt, cnt))] - x[rep]
        local = group[q][rep] - group[qa]
        v = np.sort((d + reach) + local * width)
        local = np.floor(v / width)
        d = v - local * width - reach
        cut = np.flatnonzero(local[1:] != local[:-1]) + 1
        names = rid[heads[(heads >= qa) & (heads < qb)]]
        for s, e in zip(np.r_[0, cut].tolist(), np.r_[cut, len(d)].tolist()):
            if e > s:
                out[int(names[int(local[s])])] = d[s:e]
        for r in names.tolist():                        # readouts without an offset in range
            out.setdefault(r, np.empty(0))
    return out


def contact_boots(readouts, assign, boots, drift, rd, b2, tm, diffs_out: dict | None = None, eco=()
                  ) -> dict[int, tuple[float, int, int, float]]:
    """Boot of each readout from its own contacts against the partners' current periods:
    readout -> (boot, peak, second peak, background: mean count per +-MATCH_S window in the searched
    range). assign: readout -> period key; boots: key -> boot. Searched only over the physically
    possible boots: not before the flashing (H9) nor the boot of the period before (a boot earlier
    than that would merge two periods; daily routines give look-alike peaks about a day apart), not
    after the readout's own upper limit (H1).
    diffs_out (optional): for the readouts given as its keys, filled with (boot of the assigned
    period, sorted offsets of the matched encounter edges).
    eco: the eco sessions [(readout index, enter timer, leave timer)] (read_contacts, eco_out): the
    beacon's own records during them are left out of the edges (none given: all records count)."""
    keys = sorted(boots)
    kidx = {k: i for i, k in enumerate(keys)}
    p_boot = np.array([boots[k] for k in keys]); p_drift = np.array([drift(k[0]) for k in keys])
    r_unit = np.array([kidx.get(assign.get(i), -1) for i in range(len(readouts))])
    u = r_unit[rd]
    sel = u >= 0
    u, crd, cb2, ctm = u[sel], rd[sel], b2[sel], tm[sel]
    beacon = np.array([int(k[0]) for k in keys])
    own = beacon[u] != cb2
    u, crd, cb2, ctm = u[own], crd[own], cb2[own], ctm[own]
    # records made during the beacon's own eco sessions (one scan per 300 s, BI 2.3) are no edges
    if eco:
        sessions = cs.eco_sessions_of_units(r_unit, [r["ct"] for r in readouts], eco)
        keep = ~cs.eco_flags(u, ctm, sessions)
        u, crd, cb2, ctm = u[keep], crd[keep], cb2[keep], ctm[keep]
    ev_p = cs.transitions(p_boot, p_drift, u, cb2, ctm)
    r_boot = np.where(r_unit >= 0, p_boot[np.maximum(r_unit, 0)], 0.0)
    r_drift = np.where(r_unit >= 0, p_drift[np.maximum(r_unit, 0)], 0.0)
    ev_r = cs.transitions(r_boot, r_drift, crd, cb2, ctm)
    prev_boot = {}                                       # period -> boot of the period before it
    for b in {k[0] for k in keys}:
        ks = sorted((k for k in keys if k[0] == b), key=lambda k: boots[k])
        for n, k in enumerate(ks):
            prev_boot[k] = boots[ks[n - 1]] if n else -INF
    units_of = collections.defaultdict(list)
    for k, i in kidx.items():
        units_of[int(k[0])].append(i)
    cache = {}

    def partner(B, A):
        if (B, A) not in cache:
            s = [ev_p[(v, A)][0] for v in units_of.get(B, []) if (v, A) in ev_p]
            e = [ev_p[(v, A)][1] for v in units_of.get(B, []) if (v, A) in ev_p]
            cache[(B, A)] = (np.sort(np.concatenate(s)) if s else np.empty(0), np.sort(np.concatenate(e)) if e else np.empty(0))
        return cache[(B, A)]

    partners_of = collections.defaultdict(list)
    for (i, bb) in ev_r:
        partners_of[i].append(bb)
    # per readout: the offsets partner edge - own edge of all its partners within +-WIDE_S
    # only offsets that can pass the readout's bounds below are searched (bounds widened to whole
    # seconds; the exact filter follows)
    segment, partner_edges, queries = {}, [], []
    for i, partners in partners_of.items():
        A = int(readouts[i]["ID"])
        lo, hi = -WIDE_S, WIDE_S
        if diffs_out is None or i not in diffs_out:
            r = readouts[i]
            hi_boot = (r["header"] - r["ct"] * (1 + r_drift[i]) + hrc.eps(r["ct"])
                       if r["ct"] is not None and r["header"] is not None else INF)
            lo_boot = max(FLASH_START_TS, prev_boot.get(assign.get(i), -INF) - MATCH_S)
            lo = max(lo, math.floor(lo_boot - r_boot[i]))
            hi = min(hi, math.ceil(hi_boot - r_boot[i])) if math.isfinite(hi_boot) else hi
            lo, hi = min(lo, WIDE_S + 1), max(hi, -WIDE_S - 1)       # empty range if lo > hi
        for bb in partners:
            sb, eb = partner(bb, A)
            if len(sb):
                for k, own, other in ((0, ev_r[(i, bb)][0], sb), (1, ev_r[(i, bb)][1], eb)):
                    if (bb, A, k) not in segment:
                        segment[(bb, A, k)] = len(partner_edges)
                        partner_edges.append(other)
                    queries.append((i, segment[(bb, A, k)], own, lo, hi))
    offsets = edge_offsets(queries, partner_edges)
    out = {}
    for i in partners_of:
        if i not in offsets:
            continue
        d = offsets[i]
        if diffs_out is not None and i in diffs_out:
            diffs_out[i] = (r_boot[i], d)
        # only physically possible boots: not before the flashing (H9), not after the readout's
        # own upper limit (H1: line time of Current Timer - Current Timer + eps)
        r = readouts[i]
        hi_boot = (r["header"] - r["ct"] * (1 + r_drift[i]) + hrc.eps(r["ct"])
                   if r["ct"] is not None and r["header"] is not None else INF)
        lo_boot = max(FLASH_START_TS, prev_boot.get(assign.get(i), -INF) - MATCH_S)
        d = d[(d >= lo_boot - r_boot[i]) & (d <= hi_boot - r_boot[i])]
        if len(d) < MIN_PEAK:
            continue
        span = (min(hi_boot, r_boot[i] + d.max()) - max(lo_boot, r_boot[i] + d.min())) if len(d) else 0
        background = len(d) * 2 * cs.MATCH_S / max(span, 2 * cs.MATCH_S)
        c, peak, mask = cs._densest(d)
        rest = d[np.abs(d - c) > 120]
        second = cs._densest(rest)[1] if len(rest) else 0
        near = d[(d >= c - NARROW_S) & (d <= c + NARROW_S)]
        c = cs._densest(near)[0] if len(near) else c
        out[i] = (r_boot[i] + c, peak, second, background)
    return out


# ------------------------------------------------------------------ segmentation

class Beacon:
    def __init__(self, beacon_id: str, items: list[int], readouts, lb, drift: float, kind: str, windows):
        self.id, self.idx, self.drift, self.kind = beacon_id, items, drift, kind
        self.ct = np.array([readouts[i]["ct"] for i in items], dtype=float)
        self.header = np.array([readouts[i]["header"] for i in items])
        self.z = self.header - self.ct * (1 + drift)
        self.lb = np.array([lb[i] for i in items])
        self.eps = hrc.eps_array(self.ct)
        self.anchor = np.array([readouts[i]["first_in_file"] or readouts[i]["header"] >= tza.ANCHOR_FROM.timestamp()
                                for i in items])
        self.windows = windows
        self.cap = np.full(len(items), INF)       # latest boot from assigned readouts without Current Timer


def segment_cost(bc: Beacon, a: int, b: int, lo_h5: float, contact, h4) -> tuple[float, float, str]:
    """Cost and boot of readouts a..b-1 as one period; INF if a hard rule cannot be met."""
    ct = bc.ct[a:b]
    if np.any(np.diff(ct) < 0):
        return INF, 0.0, "H3"
    hi = min(float(np.min(bc.z[a:b] - bc.lb[a:b] + bc.eps[a:b])), float(np.min(bc.cap[a:b])))
    lo = max(lo_h5, FLASH_START_TS)                # H9: no boot before the beacons were flashed
    anchors = bc.anchor[a:b]
    if anchors.any():
        lo = max(lo, float(np.max((bc.z[a:b] - bc.eps[a:b] - ANCHOR_MARGIN_S)[anchors])))
    hard, flag = 0.0, ""
    no_contact = not any(j in contact for j in range(a, b))
    if lo > hi:                                   # the data contradicts itself: keep H1, pay for the rest
        hard, flag = HARD_PENALTY * (1 + (lo - hi) / 360), f"H5/H6 violated by {lo - hi:.0f} s"
        lo = hi
    elif no_contact:
        # H4c: no reliable contact boot in the period: not after the next reliable readout in log order
        order_cap = min((h4[j][2] + bc.eps[j] - bc.ct[j] * (1 + bc.drift)
                         for j in range(a, b) if len(h4.get(j, ())) > 2), default=INF)
        hi = max(lo, min(hi, order_cap))
    cands = {hi, lo} if lo > -INF else {hi}
    for j in np.flatnonzero(anchors):            # delay-free readouts: boots at delay 0 and ANCHOR_MARGIN_S
        for d0 in (0.0, ANCHOR_MARGIN_S / 2, ANCHOR_MARGIN_S):
            v = float(bc.z[a + j] - d0)
            if lo <= v <= hi:
                cands.add(v)
    if no_contact:                               # the delay cost is lowest at a readout's delay 0
        cands.update(float(v) for v in bc.z[a:b] if lo <= v <= hi)
    # log-order predictions (periods without a reliable contact boot): (position, predicted R, sigma)
    interp = [(j, h4[j][3], h4[j][4]) for j in range(a, b)
              if no_contact and len(h4.get(j, ())) > 4 and math.isfinite(h4[j][3])]
    if interp:
        vs = [(Rp - bc.ct[j] * (1 + bc.drift), sg) for j, Rp, sg in interp]
        w = sum(1 / sg ** 2 for _, sg in vs)
        cands.update(v for v, _ in vs if lo <= v <= hi)
        cands.add(min(max(sum(v / sg ** 2 for v, sg in vs) / w, lo), hi))
    for j in range(a, b):
        c = contact.get(j)
        if c is not None and lo <= c[0] <= hi:
            cands.add(c[0])
        for bound in h4.get(j, ())[:3]:
            if not math.isfinite(bound):
                continue
            v = bound - bc.ct[j] * (1 + bc.drift)
            if lo <= v <= hi:
                cands.add(v)
    best, best_b, best_flag = INF, hi, flag
    for B in cands:
        R = B + ct * (1 + bc.drift)
        cost, f = hard, flag
        # H7: no battery window inside the period's readouts
        if any(np.any(R < w0) and np.any(R > w1) for w0, w1 in bc.windows):
            cost += HARD_PENALTY
            f = (f + "; " if f else "") + "H7 violated"
        delay = bc.z[a:b] - B
        off = np.where(anchors, np.maximum(-delay, 0) + np.maximum(delay - ANCHOR_MARGIN_S, 0), np.abs(delay))
        cost += float(np.sum(np.log1p(off / DELAY_TAU_S)))
        for j in range(a, b):
            c = contact.get(j)
            if c is not None:
                cost += c[1] * min(((B - c[0]) / SIGMA_S) ** 2, CAP)
            nb = h4.get(j)
            if nb:
                Rj = B + bc.ct[j] * (1 + bc.drift)
                before, after = nb[:2]
                v = max(0.0, before - Rj - bc.eps[j]) + max(0.0, Rj - after - bc.eps[j])
                cost += H4_WEIGHT * min((v / 60.0) ** 2, CAP)
        for j, Rp, sg in interp:
            cost += INTERP_WEIGHT * min(((B + bc.ct[j] * (1 + bc.drift) - Rp) / sg) ** 2, CAP)
        if cost < best:
            best, best_b, best_flag = cost, B, f
    return best, best_b, best_flag


def solve_beacon(bc: Beacon, contact, h4, current_cuts: set[int]) -> list[tuple[int, int, float, str]]:
    """Cheapest cutting into periods: list of (first, end, boot, violated hard rules) over positions of bc."""
    n = len(bc.idx)
    cand = {0, n} | {p for p in current_cuts if 0 < p < n}
    for j in range(1, n):
        if bc.ct[j] < bc.ct[j - 1]:
            cand.add(j)
        if bc.z[j] - bc.lb[j] + bc.eps[j] > bc.header[j - 1] - MAX_DELAY_S:
            cand.add(j)                     # a boot after the previous readout is physically possible (its
                                            # real time is at most MAX_DELAY_S before its line time)
        for w0, w1 in bc.windows:
            if bc.header[j - 1] < w1 and bc.header[j] > w0:
                cand.add(j)
    reliable = [j for j in range(n) if j in contact]
    for x, y in zip(reliable, reliable[1:]):
        if abs(contact[x][0] - contact[y][0]) > CONTACT_JUMP_S:
            cand.update(range(x + 1, y + 1))
    pos = sorted(cand)
    penalty = REBOOT_PENALTY.get(bc.kind, REBOOT_PENALTY["other"])
    best = {0: (0.0, None, None, "")}            # position -> (cost, previous position, boot, flag of last period)
    for q in pos[1:]:
        for p in pos:
            if p >= q or p not in best:
                continue
            prev_cost, _, prev_boot, _ = best[p]
            lo_h5 = -INF
            if p > 0:
                lo_h5 = prev_boot + bc.ct[p - 1] * (1 + bc.drift) - bc.eps[p - 1]
            cost, boot, flag = segment_cost(bc, p, q, lo_h5, contact, h4)
            if cost == INF:
                continue
            total = prev_cost + cost + (penalty if p > 0 else 0.0)
            if q not in best or total < best[q][0]:
                best[q] = (total, p, boot, flag)
    if n not in best:                               # cannot happen: single-readout periods always fit
        raise RuntimeError(f"no solution for beacon {bc.id}")
    out, q = [], n
    while q:
        _, p, boot, flag = best[q]
        out.append((p, q, boot, flag))
        q = p
    return out[::-1]


WORKERS = min(6, os.cpu_count() or 1)


def _solve(job) -> list[tuple[int, int, float, str]]:
    return solve_beacon(*job)


def solve_beacons(jobs: list[tuple], pool: Optional[ProcessPoolExecutor] = None) -> list[list[tuple[int, int, float, str]]]:
    """solve_beacon(*job) of each job, in parallel on pool (each beacon is solved from its own inputs
    only), in the order of jobs; the largest beacons first. Without a pool (or with one job) in this
    process."""
    if pool is None or len(jobs) < 2:
        return [solve_beacon(*job) for job in jobs]
    order = sorted(range(len(jobs)), key=lambda k: -len(jobs[k][0].idx))
    done = dict(zip(order, pool.map(_solve, [jobs[k] for k in order])))
    return [done[k] for k in range(len(jobs))]


# ------------------------------------------------------------------ driver

def fit(args):
    """Reads the inputs and iterates the model to convergence. Returns the state (readouts, beacons,
    periods, boots, evidence and, per beacon, the inputs of its last solve: contact boots, H4
    bounds and cut points) for write_outputs and validate_model.py. The beacons are solved on one
    pool of processes, closed when the fit is done."""
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        return _fit(args, pool)


def _fit(args, pool: ProcessPoolExecutor):
    t0 = time.time()
    tz_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    model = hrc.load_model(tz_dir)
    readouts = hrc.load_readouts(tz_dir)
    drifts, centre = read_drifts(tz_dir)
    drift_of = lambda b: drifts.get(b, centre)
    kinds = beacon_types(PERSONS_CSV, ROOMS_CSV)
    windows = battery_windows(args.output_dir / pp.OUTPUT_QUALITY_DIR / "battery_replacement_check.csv")
    print(f"Uptime model: {len(readouts)} readouts; reading the contacts (fake record check first) ...", flush=True)
    hl_limit: dict = {}
    eco: list = []                                  # eco sessions, for the edges of contact_boots
    rd, b2, tm, lb, fakes = read_contacts(args.log_dir, readouts, drift_of, hl_limit, eco)
    with (tz_dir / FAKES_CSV).open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["Log file", "Line"], *fakes])
    print(f"  {len(rd)} contacts, {len(fakes)} fake records left out ({tz_dir / FAKES_CSV}), "
          f"{time.time() - t0:.0f} s", flush=True)

    with_ct = [i for i, r in enumerate(readouts) if r["ct"] is not None and r["header"] is not None]
    by_beacon = collections.defaultdict(list)
    for i in with_ct:
        by_beacon[readouts[i]["ID"]].append(i)
    beacons = {b: Beacon(b, items, readouts, lb, drift_of(b), kinds.get(b, "other"), windows.get(b, []))
               for b, items in by_beacon.items()}
    # start: current periods and boots
    assign = {i: (r["ID"], r["period"]) for i, r in enumerate(readouts) if r["period"] and (r["ID"], r["period"]) in model}
    boots = {k: v[0] for k, v in model.items()}
    before = {k: v[0] for k, v in model.items()}
    order = sorted(with_ct)                                   # log order

    def iterate(it: int, label: str = ""):
        nonlocal assign, boots
        cb = contact_boots(readouts, assign, boots, lambda b: drift_of(b), rd, b2, tm, eco=eco)
        reliable = {i: (v[0], min(math.log(v[1]), 5.0)) for i, v in cb.items()
                    if contact_reliable(v)
                    and (args.evidence_days == "all" or (readouts[i]["header"] is not None and
                         datetime.fromtimestamp(readouts[i]["header"]).toordinal() % 2 == (args.evidence_days == "odd")))}
        # real times of all readouts under the current solution, for H4
        real = {}
        for i in order:
            k = assign.get(i)
            if k in boots:
                real[i] = boots[k] + readouts[i]["ct"] * (1 + drift_of(readouts[i]["ID"]))
        # H4 bounds: the longest log-ordered sequence of real times (as hard_rule_check.py, steps of
        # H4_STEP_S) is the consensus of all beacons; each readout must lie between the steps of the
        # nearest readouts of other beacons on it, before and after it in log order
        ordered = [i for i in order if i in real]
        on_seq = sorted(ordered[x] for x in hrc._longest_ordered([int(real[i] // hrc.H4_STEP_S) for i in ordered]))
        # H4c references: readouts on the consensus sequence (H4C_RELIABLE_ONLY: only in periods with
        # reliable contact boots)
        reliable_periods = {assign[i] for i in reliable if i in assign}
        ref_seq = [i for i in on_seq if assign.get(i) in reliable_periods] if H4C_RELIABLE_ONLY else on_seq
        new_assign, new_boots, changed, flags, inputs = dict(assign), {}, 0, {}, {}
        for b, bc in beacons.items():                # the inputs of each beacon's solve
            local = {j: reliable[i] for j, i in enumerate(bc.idx) if i in reliable}
            h4 = {}
            for j, i in enumerate(bc.idx):
                k = bisect.bisect_left(on_seq, i)
                lo_b = hi_b = None
                for x in range(k - 1, max(-1, k - 1 - H4_SEARCH), -1):
                    if readouts[on_seq[x]]["ID"] != b:
                        lo_b = (real[on_seq[x]] // hrc.H4_STEP_S) * hrc.H4_STEP_S
                        break
                for x in range(k + (k < len(on_seq) and on_seq[k] == i), min(len(on_seq), k + 1 + H4_SEARCH)):
                    if readouts[on_seq[x]]["ID"] != b:
                        hi_b = (real[on_seq[x]] // hrc.H4_STEP_S + 1) * hrc.H4_STEP_S
                        break
                cap_r = INF                # H4c: real time of the next reference of another beacon
                nxt = prv = None
                for x in range(bisect.bisect_right(ref_seq, i), min(len(ref_seq), bisect.bisect_right(ref_seq, i) + H4_SEARCH)):
                    if readouts[ref_seq[x]]["ID"] != b:
                        nxt = ref_seq[x]
                        cap_r = real[nxt]
                        break
                for x in range(bisect.bisect_left(ref_seq, i) - 1, max(-1, bisect.bisect_left(ref_seq, i) - 1 - H4_SEARCH), -1):
                    if readouts[ref_seq[x]]["ID"] != b:
                        prv = ref_seq[x]
                        break
                pred = sigma = math.nan     # log-order prediction between the references before and after
                if (prv is not None and nxt is not None
                        and readouts[prv]["file"] == readouts[i]["file"] == readouts[nxt]["file"]
                        and 0 < real[nxt] - real[prv] <= INTERP_MAX_SPAN_S):
                    span = real[nxt] - real[prv]
                    pred = real[prv] + (i - prv) / (nxt - prv) * span
                    sigma = INTERP_SIGMA0_S + INTERP_SIGMA_SLOPE * span
                if lo_b is not None or hi_b is not None or cap_r < INF or math.isfinite(pred):
                    h4[j] = (-INF if lo_b is None else lo_b, INF if hi_b is None else hi_b, cap_r, pred, sigma)
            cuts = {j for j in range(1, len(bc.idx)) if assign.get(bc.idx[j]) != assign.get(bc.idx[j - 1])}
            inputs[b] = (local, h4, cuts)
        solved = solve_beacons([(bc, *inputs[b]) for b, bc in beacons.items()], pool)
        moved = []                                   # |boot change| of the beacons whose cut points stay
        for (b, bc), segs in zip(beacons.items(), solved):
            cuts = inputs[b][2]
            keys = []
            for num, (a, e, boot, flag) in enumerate(segs, start=1):
                key = (b, f"m{num}")
                keys.append(key)
                new_boots[key] = boot
                flags[key] = flag
                for j in range(a, e):
                    new_assign[bc.idx[j]] = key
            new_cuts = {a for a, _, _, _ in segs if a > 0}
            changed += len(new_cuts ^ cuts)
            if new_cuts == cuts:
                moved += [abs(new_boots[k] - boots[k]) for k in keys if k in boots]
        assign, boots = new_assign, new_boots
        boots_moved = (f"; boots of unchanged cuttings moved: {int(np.count_nonzero(np.array(moved) > 0.5))} "
                       f"(max {max(moved):.0f} s)") if moved else ""
        print(f"  iteration {it + 1}{label}: {len(boots)} periods, {changed} cut points changed, "
              f"{len(reliable)} reliable contact boots{boots_moved}, {time.time() - t0:.0f} s", flush=True)
        return changed, reliable, cb, flags, inputs

    def converge(label: str = ""):
        """Iterates until no cut point changes (at least twice: the first iteration starts from other
        periods); warns if ITERATIONS is reached before."""
        for it in range(ITERATIONS):
            result = iterate(it, label)
            if it and result[0] == 0:
                return result
        print(f"  WARNING: not converged after {ITERATIONS} iterations{label} ({result[0]} cut points changed "
              "in the last): raise ITERATIONS", flush=True)
        return result

    changed, reliable, cb, flags, inputs = converge()
    headerless = []
    if args.evidence_days == "all":
        # readouts without Current Timer: assign, let them bound their period's boot, solve again
        headerless, attach = assign_headerless(readouts, assign, boots, drift_of, rd, b2, tm, hl_limit)
        if apply_caps(beacons, attach, hl_limit):
            changed, reliable, cb, flags, inputs = converge(" (bounded by the readouts without Current Timer)")
            headerless, attach = assign_headerless(readouts, assign, boots, drift_of, rd, b2, tm, hl_limit)
    return types.SimpleNamespace(tz_dir=tz_dir, readouts=readouts, beacons=beacons, assign=assign, boots=boots,
                                 lb=lb, reliable=reliable, cb=cb, drift_of=drift_of, model=model, before=before,
                                 flags=flags, inputs=inputs, segments=segs_of(beacons, assign),
                                 contacts=(rd, b2, tm), headerless=headerless,
                                 hl_limit=hl_limit)


def assign_headerless(readouts, assign, boots, drift_of, rd, b2, tm, hl_limit):
    """Period of each readout without Current Timer (a lost header, BI 3.3.1 / 3.2.3).

    Candidates: the periods of the beacon's readouts with Current Timer right before and after it
    in log order.
    1. Both the same: that period. Only one of them (before the beacon's first or after its last
       readout with Current Timer): the beacon can have rebooted in between, so the candidate is
       checked as in 2 (no contacts: that period).
    1b. Logger delay: a candidate is impossible if the beacon rebooted into its next period more
       than the allowed logger delay before the readout's first line time (the readout happened
       before that reboot). Allowed: LOG_DELAY_S, or, if the readouts with Current Timer logged
       within LOG_DELAY_WINDOW_S show a backlog, the largest delay reached by at least two different
       beacons + LOG_DELAY_MARGIN_S (a backlog delays all beacons; a single misdated readout does not
       widen the allowance).
       One candidate left: that one.
    2. Possible are the candidates under which its contacts lie between the beacon's last
       complete readout before it and their own log lines: the boot not after the latest boot its
       contacts allow (hl_limit, + eps), and its oldest contacts (HL_LOW_QUANTILE % quantile of
       the timers) not before the real time of that readout (- eps): a complete readout empties
       the storage (BI 2.1), so everything this readout sends was recorded after it. Complete:
       Transfer complete and all stored contacts of Contact Count received; an incomplete readout
       leaves contacts for the next one, so it gives no bound in real time - but it sends its
       contacts oldest first: under a candidate, the readout's oldest contacts lie at most
       HL_BACKOFF_S below the newest stored contact of the candidate's previous readout
       (the last one with stored contacts). One possible: that one. None
       possible with the current boots: a candidate whose boot fits after moving at most
       MOVE_MAX_S earlier (within the precision of a boot; the cap then moves it). Otherwise
       undecided - probably a period of its own, with a reboot before it and one after it, never
       read out with Current Timer.
    2b. Both possible: a candidate is impossible if one of its readouts holds at least HL_GRID_MIN
       scan times (contact timers) within the readout's timer range and none of them is one of the
       readout's scan times - in one uptime period a stretch of timers is scanned once (contacts
       sent twice after a lost confirmation repeat the same timers). One left: that one.
    3. Still undecided (no period possible, or both possible): the boot from the partners' records
       (fit_from_partners), searched from the lowest boot the bound of 2 allows to the latest boot
       its contacts allow (no period possible: the peak only after the beacon's last readout before
       it, H5 - a look-alike a day early must not win, 102 on 2026-08-20 in test D). Both possible: the candidate whose boot lies in the peak. No candidate
       in the peak: a period of its own with the fitted boot (never read out with Current Timer;
       a reboot before and after it), if the boot lies after the beacon's last readout before it
       (H5) and its newest contact not after the boot of the beacon's next period (own_fits; the
       same condition everywhere an own period is made). (Matching encounter edges, the former step 3, decided 4 of 7 test cases wrongly,
       validate_model.py test D.) No period possible and no confident fit: the period of
       its own is forced by the hard rules, only its boot is open - the best fit is accepted up to
       OWN_MAX_WIDTH_S without the rival test (its records get the precision of half the width).
    3b. Cross-check of "only possible period" (2): the same fit; a confident boot away from the
       chosen period's boot withdraws the decision - own period, if the boot lies after the beacon's
       last readout (H5); a fitted boot before it is impossible and withdraws nothing (75 on
       2026-08-29: a fit a day early against timers that end 4 s before the log line). A readout
       alone in its own period can fit a neighbour by its timers.
    3c. Co-presence (all decisions except "same period before and after"): dated with the chosen
       period, the readout's contacts whose partner was recording then (any record within
       COP_WINDOW_S) must mostly have a record of the beacon by that partner within COP_WINDOW_S -
       an encounter is seen on both sides. Below COP_MIN_SHARE of at least COP_MIN_ACTIVE such
       contacts the period is rejected; the readout then needs a period of its own (boot as for
       the forced own period of 3), else undecided.
    Returns the rows for uptime_model_headerless.csv and, per assigned readout, (readout, period,
    neighbouring readout of that period) for apply_caps."""
    with_period = collections.defaultdict(list)
    for i in sorted(assign):
        if assign[i] in boots:
            with_period[readouts[i]["ID"]].append(i)
    real = {i: boots[assign[i]] + readouts[i]["ct"] * (1 + drift_of(readouts[i]["ID"]))
            for items in with_period.values() for i in items}
    last_real = collections.defaultdict(lambda: -INF)        # period -> real time of its last readout
    for i, t in real.items():
        last_real[assign[i]] = max(last_real[assign[i]], t)
    floor, next_boot = {}, {}                                 # period -> its boot's lower limit (H5), next boot
    for b in with_period:
        keys = sorted({assign[i] for i in with_period[b]}, key=lambda k: boots[k])
        for n, k in enumerate(keys):
            floor[k] = last_real[keys[n - 1]] if n else -INF
            next_boot[k] = boots[keys[n + 1]] if n + 1 < len(keys) else INF
    logged = sorted((readouts[i]["header"], readouts[i]["header"] - t, readouts[i]["ID"]) for i, t in real.items()
                    if readouts[i]["header"] is not None)
    logged_t = [x[0] for x in logged]

    def allowed_delay(t):
        lo = bisect.bisect_left(logged_t, t - LOG_DELAY_WINDOW_S)
        hi = bisect.bisect_right(logged_t, t + LOG_DELAY_WINDOW_S)
        local, seen = -INF, set()
        for _, d, b in sorted(logged[lo:hi], key=lambda x: -x[1]):
            seen.add(b)
            if len(seen) >= 2:                  # the largest delay reached by two different beacons
                local = d
                break
        return max(LOG_DELAY_S, local + LOG_DELAY_MARGIN_S)
    by_rd = np.argsort(rd, kind="stable")
    rd_sorted = rd[by_rd]

    def scan_contacts(i):
        m = by_rd[np.searchsorted(rd_sorted, i):np.searchsorted(rd_sorted, i, side="right")]
        return b2[m], tm[m]

    def scan_times(i):
        return np.unique(tm[by_rd[np.searchsorted(rd_sorted, i):np.searchsorted(rd_sorted, i, side="right")]])

    def other_scan_times(h, k):
        """A readout of period k holds HL_GRID_MIN scan times in h's timer range, none of them h's."""
        th = scan_times(h)
        if len(th) < HL_GRID_MIN:
            return False
        for x in with_period[readouts[h]["ID"]]:
            if assign[x] != k:
                continue
            tx = scan_times(x)
            if not len(tx):
                continue
            lo, hi = max(th[0], tx[0]), min(th[-1], tx[-1])
            a, b = th[(th >= lo) & (th <= hi)], tx[(tx >= lo) & (tx <= hi)]
            if len(a) >= HL_GRID_MIN and len(b) >= HL_GRID_MIN and not np.isin(a, b).any():
                return True
        return False
    def newest_before(items, j, k):
        """Newest stored contact timer of the last readout of period k before position j that holds
        stored contacts (-INF: none; a readout without contacts bounds nothing, 102 on 2026-08-20)."""
        for x in reversed(items[:j]):
            if assign[x] == k:
                t = scan_times(x)
                t = t[t <= readouts[x]["ct"]]
                if len(t):
                    return float(t[-1])
        return -INF

    def own_fits(h, f_lo):
        """A period of its own with a boot from f_lo lies between the beacon's periods: after its last
        readout before it (H5) and with its newest contact not after the boot of the period of its next
        readout (+ eps): else two clocks at once (90 on 2026-08-13: a fit 14 min before p1's boot,
        contacts up to 2.5 h after it)."""
        if f_lo < last_before.get(h, -INF) - MATCH_S:
            return False
        items = with_period[readouts[h]["ID"]]
        j = bisect.bisect_left(items, h)
        if j >= len(items) or h not in hl_limit:
            return True
        top = hl_limit[h][1]
        return f_lo + top * (1 + drift_of(readouts[h]["ID"])) <= boots[assign[items[j]]] + hrc.eps(top) + MATCH_S
    decided, rows, attach, refit, lower, last_before = {}, {}, [], [], {}, {}
    for h, r in enumerate(readouts):
        if r["ct"] is not None or not r["ID"].isdigit() or r["ID"] not in with_period:
            continue
        items = with_period[r["ID"]]
        j = bisect.bisect_left(items, h)
        near = [x for x in (items[j - 1] if j else None, items[j] if j < len(items) else None) if x is not None]
        keys = list(dict.fromkeys(assign[x] for x in near))
        limit = hl_limit.get(h)
        lim_txt = _fmt(limit[0]) if limit else ""
        last_before[h] = real[items[j - 1]] if j else -INF
        if len(keys) > 1 and r.get("start") is not None:
            late = [k for k in keys if r["start"] - next_boot[k] > allowed_delay(r["start"])]
            if 0 < len(late) < len(keys):
                keys = [k for k in keys if k not in late]
                if len(keys) == 1:
                    decided[h] = (keys[0], "only possible period (logger delay)")
        if h in decided:
            pass
        elif len(keys) == 1 and len(near) == 2:
            decided[h] = (keys[0], "same period before and after")
        elif len(keys) == 1 and limit is None:
            decided[h] = (keys[0], "only neighbouring period (no contacts)")
        else:
            possible, movable = keys, []
            if limit is not None:
                e = hrc.eps(limit[1])
                # an incomplete previous readout leaves contacts for this one: no lower bound then
                prev_real = -INF
                for x in reversed(items[:j]):          # last complete readout before it
                    if readouts[x]["complete"]:
                        prev_real = real[x]
                        break
                t_low = limit[2] if len(limit) > 2 else limit[1]
                lower[h] = prev_real - t_low * (1 + drift_of(r["ID"])) - e     # lowest boot allowed
                after_prev = [k for k in keys
                              if boots[k] + t_low * (1 + drift_of(r["ID"])) >= prev_real - e
                              and t_low >= newest_before(items, j, k) - HL_BACKOFF_S]
                possible = [k for k in after_prev if boots[k] <= limit[0] + e]
                movable = [k for k in after_prev
                           if boots[k] - MOVE_MAX_S <= limit[0] + e and floor.get(k, -INF) <= limit[0] + e]
            clash = [k for k in possible if other_scan_times(h, k)] if len(possible) > 1 else []
            if len(possible) > 1 and len(clash) == len(possible) - 1:
                k = next(k for k in possible if k not in clash)
                decided[h] = (k, f"only possible period (other scan times in period {clash[0][1][1:]})")
            elif len(possible) == 1:
                decided[h] = (possible[0], "only possible period")
            elif not possible and len(movable) == 1:
                decided[h] = (movable[0], "only possible period (its boot moves earlier)")
            elif not possible:
                rows[h] = [r["file"], r["first"], r["ID"], "",
                           "undecided: no period possible (probably a period of its own)", lim_txt, "", ""]
                if limit is not None:
                    refit.append((h, []))
            elif limit is None:
                rows[h] = [r["file"], r["first"], r["ID"], "", "undecided: both periods possible, no contacts",
                           "", "", ""]
            else:
                rows[h] = [r["file"], r["first"], r["ID"], "",
                           "undecided: both periods possible, partners' records do not decide", lim_txt, "", ""]
                refit.append((h, keys))
        if h in decided:
            k = decided[h][0]
            attach.append((h, k, next(x for x in near if assign[x] == k)))
            rows[h] = [r["file"], r["first"], r["ID"], k[1][1:], decided[h][1], lim_txt, "", ""]
    # 3. boot from the partners' records
    own_periods = {}
    if refit:
        # no period possible: only a period of its own is left, its boot after the last readout (H5)
        floor = {h: last_before.get(h, -INF) - MATCH_S for h, keys in refit if not keys}
        fits = fit_from_partners(readouts, assign, boots, drift_of, rd, b2, tm,
                                 [(h, lower.get(h, -INF), hl_limit[h][0] + hrc.eps(hl_limit[h][1]), floor.get(h, -INF))
                                  for h, _ in refit])
        for h, keys in refit:
            f = fits.get(h)
            if f is None:
                continue
            boot, f_lo, f_hi, count, ratio = f
            r = readouts[h]
            inside = [k for k in keys if f_lo - FIT_MATCH_S <= boots[k] <= f_hi + FIT_MATCH_S]
            fit_txt = f"partners' records: peak {count}, ratio {ratio:.2f}, boot {_fmt(f_lo)} .. {_fmt(f_hi)}"
            if len(inside) == 1:
                k = inside[0]
                rows[h][3:5] = [k[1][1:], f"partners' records ({fit_txt})"]
                items = with_period[r["ID"]]
                j = bisect.bisect_left(items, h)
                near = [x for x in (items[j - 1] if j else None, items[j] if j < len(items) else None) if x is not None]
                attach.append((h, k, next(x for x in near if assign[x] == k)))
            elif not inside and own_fits(h, f_lo):
                rows[h][3:5] = ["", f"own period ({fit_txt})"]
                own_periods[h] = (boot, f_lo, f_hi)
        # no period possible and no confident fit: the own period is forced, accept a broad peak
        forced = [(h, lower.get(h, -INF), hl_limit[h][0] + hrc.eps(hl_limit[h][1]), floor[h])
                  for h, keys in refit if not keys and h not in fits]
        if forced:
            wide = fit_from_partners(readouts, assign, boots, drift_of, rd, b2, tm, forced,
                                     max_width=OWN_MAX_WIDTH_S, min_ratio=0.0)
            for h, f in wide.items():
                boot, f_lo, f_hi, count, ratio = f
                if own_fits(h, f_lo):
                    rows[h][3:5] = ["", f"own period (forced by the hard rules; partners' records, broad peak "
                                        f"{count}, ratio {ratio:.2f}, boot {_fmt(f_lo)} .. {_fmt(f_hi)})"]
                    own_periods[h] = (boot, f_lo, f_hi)
    # 3b. cross-check of "only possible period" with the partners' records: a readout alone in its
    # own uptime period can fit a neighbouring period by its timers (that period's last complete
    # readout early in its uptime); a confident fit away from the chosen boot withdraws the decision
    check = [h for h, (k, how) in decided.items() if how == "only possible period" and h in lower and h in hl_limit]
    if check:
        fits = fit_from_partners(readouts, assign, boots, drift_of, rd, b2, tm,
                                 [(h, lower[h], hl_limit[h][0] + hrc.eps(hl_limit[h][1])) for h in check])
        drop = set()
        for h in check:
            f = fits.get(h)
            k = decided[h][0]
            if f is None or f[1] - FIT_MATCH_S <= boots[k] <= f[2] + FIT_MATCH_S:
                continue
            boot, f_lo, f_hi, count, ratio = f
            if not own_fits(h, f_lo):
                continue                        # an own period with the fitted boot breaks H5 or overlaps the
                                                # next period: no evidence against the timers
            fit_txt = f"partners' records: peak {count}, ratio {ratio:.2f}, boot {_fmt(f_lo)} .. {_fmt(f_hi)}"
            drop.add(h)
            rows[h][3:5] = ["", f"own period ({fit_txt}; not period {k[1][1:]}, the only one its timers allow)"]
            own_periods[h] = (boot, f_lo, f_hi)
        attach = [a for a in attach if a[0] not in drop]
    # 3c. co-presence: the partners recorded the beacon at its contacts' times under the chosen period
    check = [(h, k) for h, k, _ in attach if not rows[h][4].startswith("same period")]
    if check:
        cop = copresence_index(readouts, assign, boots, drift_of, rd, b2, tm)
        drop, own_fit = {}, []
        for h, k in check:
            act, rec = copresence(cop, readouts[h]["ID"], *scan_contacts(h), boots[k], drift_of(readouts[h]["ID"]))
            if act >= COP_MIN_ACTIVE and rec < COP_MIN_SHARE * act:
                drop[h] = (k, rec / act, act)
                if h in hl_limit:
                    own_fit.append((h, lower.get(h, -INF), hl_limit[h][0] + hrc.eps(hl_limit[h][1])))
        wide = (fit_from_partners(readouts, assign, boots, drift_of, rd, b2, tm, own_fit,
                                  max_width=OWN_MAX_WIDTH_S, min_ratio=0.0) if own_fit else {})
        for h, (k, share, act) in drop.items():
            why = f"period {k[1][1:]}: its partners recorded it at {share:.0%} of {act} contacts"
            f = wide.get(h)
            if f is not None and own_fits(h, f[1]):
                boot, f_lo, f_hi, count, ratio = f
                rows[h][3:5] = ["", f"own period (co-presence rejects {why}; partners' records: peak {count}, "
                                    f"ratio {ratio:.2f}, boot {_fmt(f_lo)} .. {_fmt(f_hi)})"]
                own_periods[h] = (boot, f_lo, f_hi)
            else:
                rows[h][3:5] = ["", f"undecided: co-presence rejects {why}"]
        attach = [a for a in attach if a[0] not in drop]
    for h, row in rows.items():
        row += [_fmt(own_periods[h][0]), _fmt(own_periods[h][1]), _fmt(own_periods[h][2])] if h in own_periods else ["", "", ""]
    return [rows[h] for h in sorted(rows)], attach


def copresence_index(readouts, assign, boots, drift_of, rd, b2, tm) -> tuple[np.ndarray, np.ndarray, int]:
    """Sorted keys of the dated records in COP_BIN_S bins: (partner, beacon seen, bin) and (partner, bin),
    and the base of the ID pair (partner * base + beacon seen: above every ID)."""
    owner = np.array([int(r["ID"]) if r["ID"].isdigit() else -1 for r in readouts], dtype=np.int64)
    unit_boot = np.array([boots.get(assign.get(i), np.nan) for i in range(len(readouts))])
    unit_drift = np.array([drift_of(r["ID"]) for r in readouts])
    ok = ~np.isnan(unit_boot[rd]) & (owner[rd] >= 0)
    o = owner[rd[ok]]
    t_bin = ((unit_boot[rd[ok]] + tm[ok] * (1 + unit_drift[rd[ok]])) // COP_BIN_S).astype(np.int64)
    base = int(max(owner.max(initial=0), b2.max(initial=0))) + 1
    seen = np.unique(((o * base + b2[ok].astype(np.int64)) << 32) + t_bin)
    active = np.unique((o << 32) + t_bin)
    return seen, active, base


def copresence(index, beacon: str, partners: np.ndarray, timers: np.ndarray, boot: float, drift: float) -> tuple[int, int]:
    """Of a readout's contacts dated with boot: how many fall where their partner was recording (any record
    within COP_WINDOW_S), and of those how many where it recorded the beacon (within COP_WINDOW_S)."""
    seen, active, base = index
    w = COP_WINDOW_S // COP_BIN_S
    t_bin = ((boot + timers * (1 + drift)) // COP_BIN_S).astype(np.int64)
    p = partners.astype(np.int64)
    a_key = (p << 32) + t_bin
    act = np.searchsorted(active, a_key + w, side="right") > np.searchsorted(active, a_key - w)
    s_key = ((p * base + int(beacon)) << 32) + t_bin      # p: IDs of contacts, below base
    rec = act & (np.searchsorted(seen, s_key + w, side="right") > np.searchsorted(seen, s_key - w))
    return int(act.sum()), int(rec.sum())


def fit_from_partners(readouts, assign, boots, drift_of, rd, b2, tm, targets, max_width=None, min_ratio=None,
                      min_count=None, background=None) -> dict:
    """Boot of readouts from the partners' records of the beacon.

    A record of partner P at the readout's own timer t and P's record of the beacon at real time T
    (P's period boot + timer x (1 + drift)) from the same encounter give boot = T - t. Per partner
    the record times are binned (FIT_BIN_S) and all pairs counted by their lag; the sum over the
    partners peaks at the boot (with neighbouring bins, +-1). Searched only within the allowed
    range (lo .. hi); daily routines give look-alike peaks about a day apart, a long joint stay a
    plateau. Accepted if the peak's 90 % width is at most max_width, the peak is at least
    min_ratio times the best score more than FIT_AWAY_S outside its 90 % band, at least min_count
    and background times the median (defaults FIT_*). targets: [(readout, lo, hi)] or
    [(readout, lo, hi, floor)]: the peak only at or after floor (H5), the median and the rivals
    still over lo .. hi (a narrower range would raise the median of a broad peak: 112 on
    2026-08-13; a look-alike before floor stays a rival). Returns
    readout -> (boot, 90 % from, 90 % to, count, ratio)."""
    max_width = FIT_MAX_WIDTH_S if max_width is None else max_width
    min_ratio = FIT_MIN_RATIO if min_ratio is None else min_ratio
    min_count = FIT_MIN_COUNT if min_count is None else min_count
    background = FIT_BACKGROUND if background is None else background
    ids = np.array(sorted({int(readouts[t[0]]["ID"]) for t in targets}), dtype=np.int64)
    owner = np.array([int(r["ID"]) if r["ID"].isdigit() else -1 for r in readouts], dtype=np.int64)
    unit_boot = np.array([boots.get(assign.get(i), np.nan) for i in range(len(readouts))])
    unit_drift = np.array([drift_of(r["ID"]) for r in readouts])
    sel = np.isin(b2, ids) & ~np.isnan(unit_boot[rd]) & (owner[rd] >= 0)
    p_b2, p_owner = b2[sel].astype(np.int64), owner[rd[sel]]
    p_real = unit_boot[rd[sel]] + tm[sel] * (1 + unit_drift[rd[sel]])
    base = int(max(owner.max(initial=0), b2.max(initial=0))) + 1     # above every ID of a key
    key = p_b2 * base + p_owner                          # partner records grouped by (beacon seen, partner)
    srt = np.lexsort((p_real, key))
    key, p_real = key[srt], p_real[srt]
    order = np.argsort(rd, kind="stable")
    rd_sorted = rd[order]
    out = {}
    for h, lo, hi, *fl in targets:
        a = int(readouts[h]["ID"])
        mine = order[np.searchsorted(rd_sorted, h):np.searchsorted(rd_sorted, h, side="right")]
        if not len(mine) or not np.isfinite(hi):
            continue
        lo = max(lo, FLASH_START_TS)
        b0, b1 = int(lo // FIT_BIN_S), int(hi // FIT_BIN_S) + 1
        if b1 <= b0:
            continue
        score = np.zeros(b1 - b0 + 2)
        own_p, own_t = b2[mine], tm[mine]
        for p in np.unique(own_p):
            ot = np.unique(own_t[own_p == p] // FIT_BIN_S).astype(np.int64)
            k0, k1 = np.searchsorted(key, a * base + p), np.searchsorted(key, a * base + p, side="right")
            if k1 <= k0:
                continue
            t_lo, t_hi = lo + ot.min() * FIT_BIN_S - 3600, hi + ot.max() * FIT_BIN_S + 3600
            pt = p_real[k0 + np.searchsorted(p_real[k0:k1], t_lo):k0 + np.searchsorted(p_real[k0:k1], t_hi, side="right")]
            if not len(pt):
                continue
            pt = np.unique((pt // FIT_BIN_S).astype(np.int64))
            if len(pt) * len(ot) > 5e7:
                continue
            d = (pt[None, :] - ot[:, None]).ravel()
            d = d[(d >= b0 - 1) & (d <= b1)] - (b0 - 1)
            score += np.bincount(d, minlength=len(score))[:len(score)]
        sm = score[:-2] + score[1:-1] + score[2:]
        f0 = max(0, int(fl[0] // FIT_BIN_S) - b0) if fl and np.isfinite(fl[0]) else 0
        if f0 >= len(sm) or not sm[f0:].any():
            continue
        k = f0 + int(np.argmax(sm[f0:]))
        peak = sm[k]
        top = f0 + np.flatnonzero(sm[f0:] >= 0.9 * peak)
        idx = np.arange(len(sm))                   # rivals: outside the peak's own 90 % band
        away = (idx < top.min() - FIT_AWAY_S // FIT_BIN_S) | (idx > top.max() + FIT_AWAY_S // FIT_BIN_S)
        second = sm[away].max() if away.any() else 0.0
        ratio = peak / max(second, 1.0)
        width = (top.max() - top.min()) * FIT_BIN_S
        if (width <= max_width and ratio >= min_ratio and peak >= min_count
                and peak >= background * max(np.median(sm), 1.0)):
            out[h] = ((b0 + k) * FIT_BIN_S, (b0 + top.min()) * FIT_BIN_S, (b0 + top.max()) * FIT_BIN_S, int(peak), ratio)
    return out


def apply_caps(beacons, attach, hl_limit) -> bool:
    """Assigned readouts without Current Timer bound the boot of their period: their latest boot
    (hl_limit, + eps) becomes a cap at the neighbouring readout of that period (H1 for their
    contacts). Returns whether any cap was set."""
    for bc in beacons.values():
        bc.cap[:] = INF
    pos = {i: (b, j) for b, bc in beacons.items() for j, i in enumerate(bc.idx)}
    n = 0
    for h, _, near in attach:
        if h in hl_limit and near in pos:
            b, j = pos[near]
            cap = hl_limit[h][0] + hrc.eps(hl_limit[h][1])
            if cap < beacons[b].cap[j]:
                beacons[b].cap[j] = cap
                n += 1
    print(f"  {n} caps on the boots from {len(attach)} assigned readouts without Current Timer", flush=True)
    return n > 0


def label_test(state: dict, i: int, labels: tuple[str, ...]) -> dict[str, list[str]]:
    """Which label of readout i keeps the hard rules: for each label, the readout is put into that
    beacon's readouts (and taken out of the other's), both beacons are solved again with the
    model's evidence (contact boots, H4 bounds, cut points; the readout's own contact boot is
    dropped, it was matched under the logged ID), and the violated hard rules are collected.
    Returns label -> violated hard rules (empty: the label keeps all of them)."""
    readouts, lb, inputs = state["readouts"], state["lb"], state["inputs"]
    beacons = {b: types.SimpleNamespace(**v) for b, v in state["beacons"].items()}
    any_bc = next(iter(beacons.values()))
    out = {}
    for lab in labels:
        violated = []
        for b in labels:
            bc0 = beacons.get(b)
            items = [x for x in (bc0.idx if bc0 else []) if x != i]
            if b == lab:
                items = sorted(items + [i])
            if not items:
                continue
            drift = bc0.drift if bc0 else any_bc.drift
            bc = Beacon(b, items, readouts, lb, drift, bc0.kind if bc0 else "other", bc0.windows if bc0 else [])
            local0, h40, cuts0 = inputs.get(b, ({}, {}, set())) if bc0 else ({}, {}, set())
            by_idx = lambda d: {bc0.idx[j]: v for j, v in d.items()} if bc0 else {}
            loc_i, h4_i = by_idx(local0), by_idx(h40)
            src = beacons.get(readouts[i]["ID"])            # H4 bounds of the readout under its logged ID
            if src is not None and i in src.idx:
                h4_i.setdefault(i, inputs[readouts[i]["ID"]][1].get(src.idx.index(i)))
            cut_i = {bc0.idx[j] for j in cuts0} if bc0 else set()
            local = {p: loc_i[x] for p, x in enumerate(items) if x in loc_i and x != i}
            h4 = {p: h4_i[x] for p, x in enumerate(items) if h4_i.get(x) is not None}
            cuts = {p for p, x in enumerate(items) if x in cut_i and p > 0}
            violated += [f"{b}: {flag}" for *_, flag in solve_beacon(bc, local, h4, cuts) if flag]
        out[lab] = violated
    return out


def segs_of(beacons, assign) -> dict[str, list[tuple[int, int, tuple]]]:
    """Per beacon its periods as (first, end, key) over the positions of its readouts."""
    out = {}
    for b, bc in beacons.items():
        segs, a = [], 0
        for j in range(1, len(bc.idx) + 1):
            if j == len(bc.idx) or assign.get(bc.idx[j]) != assign.get(bc.idx[j - 1]):
                segs.append((a, j, assign.get(bc.idx[a])))
                a = j
        out[b] = segs
    return out


def run(args) -> None:
    st = fit(args)
    tz_dir, readouts, flags = st.tz_dir, st.readouts, st.flags
    suffix = "" if args.evidence_days == "all" else f"_{args.evidence_days}_days"
    write_outputs(tz_dir, readouts, st.assign, st.boots, st.beacons, st.lb, st.reliable, st.cb, st.drift_of, st.model,
                  st.before, flags, suffix, st.inputs)
    print(f"  periods with a violated hard rule (the data contradicts itself): {sum(1 for f in flags.values() if f)}")
    # MAX_DELAY_S bounds where a reboot can lie (candidate cut points): a delay close to it may have
    # been cut by the bound instead of the data
    near = sorted(((r["header"] - st.boots[st.assign[i]] - r["ct"] * (1 + st.drift_of(r["ID"])), i)
                   for i, r in enumerate(readouts)
                   if r["ct"] is not None and st.assign.get(i) in st.boots), reverse=True)
    near = [(d, i) for d, i in near if d > MAX_DELAY_S - DELAY_NOTE_S]
    if near:
        print(f"  NOTE: {len(near)} readouts logged within {DELAY_NOTE_S / 3600:.0f} h of the longest delay "
              f"assumed for reboots (MAX_DELAY_S {MAX_DELAY_S / 3600:.0f} h; largest {near[0][0] / 3600:.2f} h): "
              + ", ".join(f"{readouts[i]['ID']} {_fmt(readouts[i]['header'])}" for _, i in near[:5]), flush=True)
    print(f"  written to {tz_dir / PERIODS_CSV.replace('.csv', suffix + '.csv')} and "
          f"{tz_dir / READOUTS_CSV.replace('.csv', suffix + '.csv')}")
    if suffix:
        return
    import pickle
    with (tz_dir / STATE_PKL).open("wb") as f:
        pickle.dump(dict(readouts=readouts, lb=st.lb, inputs=st.inputs,
                         beacons={b: dict(idx=list(bc.idx), drift=bc.drift, kind=bc.kind, windows=bc.windows)
                                  for b, bc in st.beacons.items()}), f)
    rows = st.headerless
    with (tz_dir / HEADERLESS_CSV).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Log file", "First line", "ID", "Period", "Method", "Latest boot from its contacts",
                    "Support before", "Support after", "Own period boot", "Own period boot from", "Own period boot to"])
        w.writerows(rows)
    methods = collections.Counter(r[4] for r in rows)
    print(f"  readouts without Current Timer: {len(rows)}; " + ", ".join(f"{k} {v}" for k, v in sorted(methods.items()))
          + f" ({tz_dir / HEADERLESS_CSV})")


def write_outputs(tz_dir, readouts, assign, boots, beacons, lb, reliable, cb, drift_of, model, before, flags,
                  suffix: str = "", inputs: dict | None = None) -> None:
    # readouts with a log-order prediction (references of other beacons before and after in the same file)
    log_order = {bc.idx[j] for b, bc in beacons.items() for j, v in (inputs or {}).get(b, ({}, {}, set()))[1].items()
                 if math.isfinite(v[3])}
    members = collections.defaultdict(list)
    for i, k in assign.items():
        if k in boots:
            members[k].append(i)
    old_of = {}
    for i, r in enumerate(readouts):
        if r["period"]:
            old_of[i] = (r["ID"], r["period"])
    with (tz_dir / PERIODS_CSV.replace(".csv", suffix + ".csv")).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Period", "First readout", "Last readout", "Readouts", "Boot", "Drift [s/day]",
                    "Reliable contact boots", "Log-order readouts", "Periods before", "Boot before", "Boot - before [s]",
                    "Violated hard rules"])
        for k in sorted(boots, key=lambda k: (pp.id_sort_key(k[0]), int(k[1][1:]))):
            ms = sorted(members[k], key=lambda i: readouts[i]["header"])
            olds = sorted({old_of[i][1] for i in ms if i in old_of}, key=int)
            ob = [before[(k[0], p)] for p in olds if (k[0], p) in before]
            w.writerow([k[0], k[1][1:], _fmt(readouts[ms[0]]["header"]) if ms else "", _fmt(readouts[ms[-1]]["header"]) if ms else "",
                        len(ms), _fmt(boots[k]), round(drift_of(k[0]) * pp.DAY_S, 2), sum(1 for i in ms if i in reliable),
                        sum(1 for i in ms if i in log_order),
                        " ".join(olds), _fmt(ob[0]) if ob else "", round(boots[k] - ob[0], 1) if ob else "",
                        flags.get(k, "")])
    with (tz_dir / READOUTS_CSV.replace(".csv", suffix + ".csv")).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Log file", "First line", "Line time of Current Timer", "Current Timer", "Period", "Boot",
                    "Real time", "Delay [s]", "Delay lower bound [s]", "Boot from its contacts", "Contact peak",
                    "Second peak", "Reliable", "Period before"])
        for i, r in enumerate(readouts):
            k = assign.get(i)
            if r["ct"] is None or k not in boots:
                continue
            d = drift_of(r["ID"])
            R = boots[k] + r["ct"] * (1 + d)
            c = cb.get(i)
            w.writerow([r["ID"], r["file"], r["first"], _fmt(r["header"]), r["ct"], k[1][1:], _fmt(boots[k]), _fmt(R),
                        round(r["header"] - R, 1), round(lb[i], 1), _fmt(c[0]) if c else "", c[1] if c else "",
                        c[2] if c else "", "yes" if i in reliable else "", r["period"]])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Uptime periods and boots from one model (hard rules + evidence).")
    pp.add_common_arguments(parser)
    parser.add_argument("--evidence-days", choices=("all", "even", "odd"), default="all",
                        help="Use the contact boots of readouts on all days, or only even / odd days "
                             "(stability test; outputs get the suffix _even_days / _odd_days). Default: all")
    args = parser.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
