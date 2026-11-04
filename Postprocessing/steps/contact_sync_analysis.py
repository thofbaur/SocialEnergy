#!/usr/bin/env python3
"""Timer-zero times of all uptime periods aligned by symmetric contacts ("contact sync").

A contact record (ID1 = A, ID2 = B, timer) is stamped with A's own clock. When A sees B,
B usually sees A in the same minutes (both scan 120 ms every 7 s), so every encounter is
recorded on two clocks. Mapped to real time with the zero times of the two uptime periods,

    T = zero(A) + timer_A * (1 + drift_A)        T' = zero(B) + timer_B * (1 + drift_B),

the encounters only line up if the difference of the two zero times is right. The logging
delay does not enter: contact timers are beacon-clock values. The contacts therefore give
the zero times of all uptime periods relative to each other; one constant remains, fixed by
the delay-free readouts (delay = 0).

Steps (Postprocessing Method.md, section 14):
1. Units: every uptime period of timer_zero_analysis.py, and every header-less readout
   that got no timer-zero time there (own unit, start value: start time - largest contact
   timer).
2. Encounter transitions per (unit, partner beacon): contacts on real time in 10-s bins,
   gaps up to 120 s merged; the starts and ends of the encounters, without the first
   start and the last end (they only mark the data coverage). Continuous presence
   carries no timing information; transitions do.
3. Pair offsets: for two units of beacons A and B, the starts (ends) A recorded of B are
   compared with the starts (ends) B recorded of A; the densest +-30 s point of all
   differences is the offset. Pass 1 searches +-8 h and keeps strong peaks (>= 12
   matching transitions); pass 2 searches +-15 min around the pass-1 solution.
4. Solve: iterated weighted median of the offsets implied by the partners (robust
   against wrong pairs), then Huber least squares on the pairs within 10 min.
   A unit counts as synced with >= 2 pairs within 2 min and >= 50 % agreement.
5. Absolute level: median over the delay-free readouts (first readout of a log file,
   readouts after 2026-08-29 19:30) of the synced units.
6. Units without enough pairs: all transitions against all synced partners in one
   histogram (+-8 h); accepted with >= 8 matches from >= 2 partners.
7. Drift per beacon: the transitions of all aligned units are matched one by one
   (nearest partner transition within +-30 s). A wrong drift makes the residuals of a
   unit's matches run away with its timer, so one Huber least-squares fit over all
   matches gives a zero correction per unit and a drift per beacon (the crystal is a
   property of the beacon; the drift holds across battery changes). The delay-free
   readouts enter as equations too: the contacts only fix the drifts relative to each
   other, the readouts the common level. Raw drifts of beacons with few or short
   encounters are noisy; they are shrunk towards the common centre by
   tau^2 / (tau^2 + s^2), with the noise s = k / sqrt(info) (info: sum of the squared
   timer deviations of the beacon's matches) and k and the true spread tau calibrated
   in every run from fits on even and odd days. With these drifts fixed, the zero
   times are solved again.
8. Checks: a period's zero time cannot move later than its own readouts allow (delay
   minus its lower bound from the contacts must not be negative): more than 60 s later
   -> conflict, old zero and drift kept; 10-60 s -> moved to the readouts' edge.
   Header-less readouts: the contact timers must not lie above the uptime at the readout
   (99 % quantile, 2 min tolerance).

Outputs (timer_zero/ subfolder of --output-dir):
- contact_sync_periods.csv    per unit: old and new zero time and drift, shift, method,
                              agreement, status
- contact_sync_drift.csv      per beacon: matches, raw drift, noise, reliability, drift
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import scipy.sparse as sp

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza

PERIODS_CSV = "contact_sync_periods.csv"
DRIFT_CSV = "contact_sync_drift.csv"

BIN_S = 10                     # presence bins
MERGE_BINS = 12                # gaps up to 120 s belong to the same encounter
WIDE_S = 8 * 3600              # pass-1 lag range
NARROW_S = 15 * 60             # pass-2 half window around the pass-1 solution
MATCH_S = 30                   # transitions within +-30 s match
MIN_PEAK_WIDE = 12             # pass 1: weak peaks over +-8 h are mostly spurious
MIN_PEAK_NARROW = 5
WEIGHT_CAP = 50.0
INLIER_S = 600                 # pairs used for the least-squares step
HUBER_S = 30.0
AGREE_S = 120                  # pair "agrees" with the solution
MIN_AGREE_PAIRS, MIN_AGREEMENT = 2, 0.5
UNIT_MIN_PEAK, UNIT_MIN_PARTNERS = 8, 2
LATER_TOL_S, CONFLICT_S = 10.0, 60.0
CT_EXCESS_S = pp.READOUT_TIMER_EXCESS_S     # contacts up to this far above Current Timer belong to the readout
HEADERLESS_TOL_S = 120
DRIFT_WINDOWS_COMMON = (60, 30)  # matching windows [s] with the common drift
DRIFT_WINDOWS_FREE = (60, 30, 30)  # ... with free per-beacon drifts (matches for the raw drifts)
DRIFT_WINDOWS_FINAL = (30, 30)   # ... with the shrunk drifts
DRIFT_HUBER_S = 15.0
DRIFT_IRLS = 8
DRIFT_HOLD_WEIGHT = 1e5        # drift kept at its current value
DRIFT_FREE_WEIGHT = 1.0        # raw drift: weak pull to the common drift (1 s/day costs as much as 1 s)
DRIFT_GOOD_NOISE = 0.7         # s/day: beacons with less noise in both halves define the true spread


# ------------------------------------------------------------------ data

def collect_contacts(lines: logcache.Lines, readouts: list[tza.Readout]) -> tuple:
    """(readout index, ID2, timer) of every contact line with a valid ID2 that the other side
    records as well (pp.symmetric_threshold, BI 2.1), and the eco sessions [(readout index, enter
    timer, leave timer)]; in the merged order of the lines."""
    of = lines.readout_of([(r.source.name, r.first_line, r.last_line) for r in readouts])
    m = lines.merged_index()
    sel = m[((lines.kind == 1) & pp.valid_beacon_ids(lines.id2) & (of >= 0))[m]]
    eco = []
    for n in m[((lines.kind == 0) & (of >= 0))[m]].tolist():
        e = pp.ECO_SESSION_RE.match(lines.rest[lines.other[n]])
        if e:
            eco.append((int(of[n]), int(e.group("enter")), int(e.group("leave"))))
    rd_a, id2_a = of[sel], lines.id2[sel].astype(np.int64)
    id1 = np.array([int(r.beacon_id) if r.beacon_id.isdigit() else -1 for r in readouts], dtype=np.int64)
    keep = lines.rssi[sel] >= pp.symmetric_threshold(id1[rd_a], id2_a)
    return rd_a[keep], id2_a[keep], lines.timer[sel].astype(np.int64)[keep], eco


ECO_SCALE = 1e8            # unit x ECO_SCALE + timer: one sorted axis for all units (timers < 1e8 s)


def eco_sessions_of_units(of_readout: np.ndarray, readouts_ct, eco) -> list:
    """(unit, enter, leave) of the eco sessions: a session belongs to the unit of the readout that
    delivered it, unless its leave timer lies above that readout's Current Timer (+ CT_EXCESS_S):
    then it was recorded before a reboot (BI 3.4.1, kept in flash) and is skipped."""
    out = []
    for j, enter, leave in eco:
        k = int(of_readout[j])
        ct = readouts_ct[j]
        if k >= 0 and leave >= enter and (ct is None or leave <= ct + CT_EXCESS_S):
            out.append((k, enter, leave))
    return out


def eco_flags(u: np.ndarray, t: np.ndarray, sessions) -> np.ndarray:
    """Per contact (unit u, timer t): recorded by its beacon during one of its own eco sessions. In
    eco mode the beacon scans only every 300 s (BI 2.3): its encounter starts and ends are off by
    up to that much, so these records are no edge evidence (the partners' records of it are)."""
    if not len(sessions) or not len(u):
        return np.zeros(len(u), dtype=bool)
    S = np.asarray(sessions, dtype=np.float64)
    a = S[:, 0] * ECO_SCALE + S[:, 1]
    z = S[:, 0] * ECO_SCALE + S[:, 2]
    o = np.argsort(a, kind="stable")
    a, z = a[o], np.maximum.accumulate(z[o])               # overlapping sessions merged
    key = u.astype(np.float64) * ECO_SCALE + t
    idx = np.searchsorted(a, key, "right") - 1
    return (idx >= 0) & (key <= z[np.maximum(idx, 0)])


class Units:
    """Uptime periods plus unresolved header-less readouts, with start zero and drift."""

    def __init__(self, readouts: list[tza.Readout], periods: list[tza.Period]):
        self.key: list[tuple] = []
        zero, drift, beacon = [], [], []
        self.index: dict[tuple, int] = {}
        for p in periods:
            self.index[(p.beacon_id, p.number)] = len(self.key)
            self.key.append((p.beacon_id, p.number))
            zero.append(p.zero_true.timestamp()); drift.append(p.drift); beacon.append(int(p.beacon_id))
        self.of_readout = np.full(len(readouts), -1, dtype=np.int64)
        self.limit = np.zeros(len(readouts), dtype=np.int64)
        for i, r in enumerate(readouts):
            if r.period:
                self.of_readout[i] = self.index[(r.beacon_id, r.period)]
            elif r.contacts and r.max_contact_timer:
                self.of_readout[i] = len(self.key)
                self.index[("headerless", i)] = len(self.key)
                self.key.append(("headerless", i))
                zero.append(r.start_time.timestamp() - r.max_contact_timer)
                drift.append(tza.DRIFT_S_PER_S); beacon.append(int(r.beacon_id))
            self.limit[i] = (r.current_timer + CT_EXCESS_S) if r.current_timer is not None else tza.MAX_PLAUSIBLE_TIMER_S
        self.zero0, self.drift, self.beacon = np.array(zero), np.array(drift), np.array(beacon)
        self.n = len(self.key)
        self.of_beacon: dict[int, list[int]] = collections.defaultdict(list)
        for k in range(self.n):
            self.of_beacon[int(self.beacon[k])].append(k)


def transitions(zero: np.ndarray, drift: np.ndarray, u: np.ndarray, b2: np.ndarray, t: np.ndarray) -> dict:
    """(unit, partner) -> (encounter starts, encounter ends, first bin, last bin) in real time [s]."""
    T = zero[u] + t * (1 + drift[u])
    binidx = np.floor(T / BIN_S).astype(np.int64)
    key = u * 1000 + b2
    if not len(key):
        return {}
    lo = int(binidx.min())
    if int(binidx.max()) - lo < 1 << 32 and 0 <= int(key.min()) and int(key.max()) < 1 << 31:
        both = np.unique((key << 32) | (binidx - lo))          # sorted by (pair, bin), each bin once
        key, binidx = both >> 32, (both & 0xFFFFFFFF) + lo
    else:
        order = np.lexsort((binidx, key)); key, binidx = key[order], binidx[order]
        first = np.ones(len(key), bool); first[1:] = (key[1:] != key[:-1]) | (binidx[1:] != binidx[:-1])
        key, binidx = key[first], binidx[first]
    bounds = np.flatnonzero(np.r_[True, key[1:] != key[:-1], True])
    gap = np.flatnonzero((key[1:] == key[:-1]) & (np.diff(binidx) > MERGE_BINS))
    group = np.searchsorted(bounds, gap, "right") - 1
    starts, ends = binidx[gap + 1] * float(BIN_S), binidx[gap] * float(BIN_S)
    cuts = np.flatnonzero(np.diff(group)) + 1
    events = {}
    for a, b in zip(np.r_[0, cuts].tolist(), np.r_[cuts, len(gap)].tolist()):
        if b == a:
            continue
        s, e = bounds[group[a]], bounds[group[a] + 1]
        events[(int(key[s] // 1000), int(key[s] % 1000))] = (
            starts[a:b], ends[a:b], binidx[s] * float(BIN_S), binidx[e - 1] * float(BIN_S))
    return events


def _diffs(a: np.ndarray, b: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """All a_i - b_j within [lo, hi] (a, b sorted)."""
    j0 = np.searchsorted(b, a - hi, "left"); j1 = np.searchsorted(b, a - lo, "right")
    cnt = j1 - j0
    if cnt.sum() == 0:
        return np.empty(0)
    rep = np.repeat(np.arange(len(a)), cnt)
    off = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    return a[rep] - b[j0[rep] + off]


def _densest(d: np.ndarray) -> tuple[float, int, np.ndarray]:
    """Median of the densest +-MATCH_S cluster, its size, and its mask (d sorted)."""
    n_in = np.searchsorted(d, d + MATCH_S, "right") - np.searchsorted(d, d - MATCH_S, "left")
    i = int(np.argmax(n_in))
    mask = (d >= d[i] - MATCH_S) & (d <= d[i] + MATCH_S)
    return float(np.median(d[mask])), int(n_in[i]), mask


# ------------------------------------------------------------------ pairs and solve

def pair_offsets(units: Units, events: dict, centre: np.ndarray | None) -> list[tuple]:
    """(u, v, offset, weight): c_v - c_u = offset. Wide search if centre is None."""
    edges = []
    for (a_u, bb), (sa, ea, a0, a1) in events.items():
        A = int(units.beacon[a_u])
        for v in units.of_beacon.get(bb, []):
            if v <= a_u or (v, A) not in events:
                continue
            sb, eb, b0, b1 = events[(v, A)]
            if centre is None:
                lo, hi, min_peak = -WIDE_S, WIDE_S, MIN_PEAK_WIDE
            else:
                mid = centre[v] - centre[a_u]
                lo, hi, min_peak = mid - NARROW_S, mid + NARROW_S, MIN_PEAK_NARROW
            reach = max(abs(lo), abs(hi))
            if a0 > b1 + reach or b0 > a1 + reach:
                continue
            d = np.concatenate([_diffs(sa, sb, lo, hi), _diffs(ea, eb, lo, hi)])
            if len(d) < min_peak:
                continue
            d.sort()
            offset, peak, _ = _densest(d)
            background = len(d) * 2 * MATCH_S / (hi - lo)
            if peak < min_peak or peak < 3 * background + 2:
                continue
            edges.append((a_u, v, offset, peak - background))
    return edges


def solve(n: int, edges: list[tuple]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Corrections c (c_v - c_u = offset), weighted agreement share and number of agreeing pairs."""
    neighbours = collections.defaultdict(list)
    for a, b, d, w in edges:
        w = min(w, WEIGHT_CAP)
        neighbours[b].append((a, d, w)); neighbours[a].append((b, -d, w))
    active = [v for v in range(n) if neighbours[v]]
    c = np.zeros(n)
    for _ in range(100):                                   # iterated weighted median
        old = c.copy()
        for v in active:
            vals = np.array([c[x] + d for x, d, _ in neighbours[v]])
            ws = np.array([w for _, _, w in neighbours[v]])
            o = np.argsort(vals); cw = np.cumsum(ws[o])
            c[v] = vals[o][np.searchsorted(cw, cw[-1] / 2)]
        c -= np.median(c[active])
        if np.abs(c - old).max() < 0.5:
            break
    E = np.array([(e[0], e[1]) for e in edges]); D = np.array([e[2] for e in edges])
    W0 = np.minimum(np.array([e[3] for e in edges]), WEIGHT_CAP)
    keep = np.abs(D - (c[E[:, 1]] - c[E[:, 0]])) <= INLIER_S
    E2, D2, W2 = E[keep], D[keep], W0[keep]
    W = W2.copy()
    for _ in range(15):                                    # Huber least squares
        L = np.zeros((n, n)); rhs = np.zeros(n)
        np.add.at(L, (E2[:, 0], E2[:, 0]), W); np.add.at(L, (E2[:, 1], E2[:, 1]), W)
        np.add.at(L, (E2[:, 0], E2[:, 1]), -W); np.add.at(L, (E2[:, 1], E2[:, 0]), -W)
        np.add.at(rhs, E2[:, 1], W * D2); np.add.at(rhs, E2[:, 0], -W * D2)
        c = np.linalg.solve(L + np.eye(n) * 1e-6, rhs)
        r = D2 - (c[E2[:, 1]] - c[E2[:, 0]])
        W = W2 * np.minimum(1.0, HUBER_S / np.maximum(np.abs(r), 1e-9))
    c -= np.median(c[active])
    res = D - (c[E[:, 1]] - c[E[:, 0]])
    good = np.abs(res) <= AGREE_S
    agree, total = np.zeros(n), np.zeros(n)
    for (a, b), g, w in zip(E, good, W0):
        for x in (a, b):
            total[x] += w; agree[x] += w * g
    n_good = np.bincount(E[good].ravel(), minlength=n)
    return c, np.where(total > 0, agree / np.maximum(total, 1e-9), 0.0), n_good


def align_unit(units: Units, events: dict, partners: dict, target: int, synced: np.ndarray, shift: np.ndarray):
    """Correction of one unit against all synced partners, or None / "weak"."""
    A = int(units.beacon[target])
    parts, src = [], []
    for bb in partners.get(target, []):
        sa, ea = events[(target, bb)][:2]
        for v in units.of_beacon.get(bb, []):
            if v == target or not synced[v] or (v, A) not in events:
                continue
            sb, eb = events[(v, A)][0] + shift[v], events[(v, A)][1] + shift[v]
            d = np.concatenate([_diffs(sa, sb, -WIDE_S, WIDE_S), _diffs(ea, eb, -WIDE_S, WIDE_S)])
            parts.append(d); src.append(np.full(len(d), v))
    if not parts:
        return None
    d = np.concatenate(parts); s = np.concatenate(src)
    if len(d) < UNIT_MIN_PEAK:
        return "weak"
    o = np.argsort(d); d, s = d[o], s[o]
    offset, peak, mask = _densest(d)
    background = len(d) * 2 * MATCH_S / (2 * WIDE_S)
    n_partners = len(set(s[mask].tolist()))
    if peak < UNIT_MIN_PEAK or peak < 4 * background + 3 or n_partners < UNIT_MIN_PARTNERS:
        return "weak"
    return -offset, peak, background, n_partners


# ------------------------------------------------------------------ drift per beacon

def match_transitions(units: Units, events: dict, zero: np.ndarray, drift: np.ndarray, window: float):
    """Starts (ends) A recorded of B matched to the nearest start (end) B recorded of A within
    +-window [s] real time: arrays (u, v, timer_u, timer_v)."""
    qa, qb, ua, vb = [], [], [], []                       # per query: A's and B's times, the two units
    for (a, bb), (sa, ea, _, _) in events.items():
        A = int(units.beacon[a])
        for v in units.of_beacon.get(bb, []):
            if v <= a or (v, A) not in events:
                continue
            sb, eb = events[(v, A)][:2]
            qa += [sa, ea]; qb += [sb, eb]; ua += [a, a]; vb += [v, v]
    if not qa:
        return tuple(np.zeros(0, dtype=t) for t in (np.int64, np.int64, float, float))
    # all queries at once: each query's times shifted into a range of its own (times are whole
    # seconds, so the shifted values stay exact), one search; the nearest of the query's own times
    na, nb = np.array([len(x) for x in qa]), np.array([len(x) for x in qb])
    xa, xb = np.concatenate(qa), np.concatenate(qb)
    low = min(xa.min(), xb.min())
    span = 2.0 ** np.ceil(np.log2(max(xa.max(), xb.max()) - low + 2 * window + 2))
    qid_a, qid_b = np.repeat(np.arange(len(qa)), na), np.repeat(np.arange(len(qb)), nb)
    first = np.r_[0, np.cumsum(nb)[:-1]]
    j = np.searchsorted(xb - low + qid_b * span, xa - low + qid_a * span)
    lo = np.clip(j - 1, first[qid_a], first[qid_a] + nb[qid_a] - 1)
    hi = np.clip(j, first[qid_a], first[qid_a] + nb[qid_a] - 1)
    jj = np.where(np.abs(xb[lo] - xa) <= np.abs(xb[hi] - xa), lo, hi)
    ok = np.abs(xb[jj] - xa) <= window
    u, v = np.array(ua)[qid_a][ok], np.array(vb)[qid_a][ok]
    return u, v, (xa[ok] - zero[u]) / (1 + drift[u]), (xb[jj[ok]] - zero[v]) / (1 + drift[v])


def solve_drift(zero, drift, matches, anchors, group, n_groups, prior_weight):
    """Huber least squares for corrections of the zero times (per unit, s) and of the drifts
    (per group, s/day), from the matches (zero_u + t_u (1 + d_u) = zero_v + t_v (1 + d_v)) and
    the delay-free readouts (zero + Current Timer (1 + d) = line time). Each drift is pulled to
    its current value with prior_weight. Returns dz, dd, match residuals, anchor residuals."""
    U, V, TU, TV = matches
    n, m = len(zero), len(U)
    ka, ct, ht = anchors[:, 0].astype(np.int64), anchors[:, 1], anchors[:, 2]
    na = len(ka)
    r = (zero[V] + TV * (1 + drift[V])) - (zero[U] + TU * (1 + drift[U]))
    ra = zero[ka] + ct * (1 + drift[ka]) - ht
    rows = np.r_[np.repeat(np.arange(m), 4), m + np.repeat(np.arange(na), 2), m + na + np.arange(n_groups)]
    cols = np.r_[np.c_[V, U, n + group[V], n + group[U]].ravel(), np.c_[ka, n + group[ka]].ravel(),
                 n + np.arange(n_groups)]
    vals = np.r_[np.c_[np.ones(m), -np.ones(m), TV / pp.DAY_S, -TU / pp.DAY_S].ravel(),
                 np.c_[np.ones(na), ct / pp.DAY_S].ravel(), np.ones(n_groups)]
    A = sp.csr_matrix((vals, (rows, cols)), shape=(m + na + n_groups, n + n_groups))
    b = np.r_[-r, -ra, np.zeros(n_groups)]
    w0 = np.r_[np.ones(m + na), np.full(n_groups, prior_weight)]
    w = w0.copy()
    for _ in range(DRIFT_IRLS):
        N = (A.T @ sp.diags(w) @ A).toarray() + np.eye(n + n_groups) * 1e-9
        x = np.linalg.solve(N, A.T @ (w * b))
        res = A @ x - b
        w = w0 * np.r_[np.minimum(1.0, DRIFT_HUBER_S / np.maximum(np.abs(res[:m + na]), 1e-9)), np.ones(n_groups)]
    return x[:n], x[n:], res[:m], res[m:m + na]


def _info(matches, group, n_groups, n) -> np.ndarray:
    """Per group: sum of the squared deviations [day^2] of the matched timers from their unit's mean."""
    u = np.r_[matches[0], matches[1]]; t = np.r_[matches[2], matches[3]] / pp.DAY_S
    mean = np.bincount(u, t, minlength=n) / np.maximum(np.bincount(u, minlength=n), 1)
    return np.bincount(group, np.bincount(u, (t - mean[u]) ** 2, minlength=n), minlength=n_groups)


class DriftFit:
    """Step 7: zero time and drift of the aligned units, and the per-beacon drift table."""

    def __init__(self, units: Units, u, b2, t, zero, aligned, anchors):
        sel = aligned[u]
        u, b2, t = u[sel], b2[sel], t[sel]
        n = units.n
        self.beacons = np.unique(units.beacon)
        group = np.searchsorted(self.beacons, units.beacon)
        ng = len(self.beacons)
        d0 = tza.DRIFT_S_PER_S
        # a changed drift keeps the time mapping at the unit's median contact timer
        t_med = np.zeros(n)
        o = np.argsort(u, kind="stable"); us, ts = u[o], t[o]
        bounds = np.flatnonzero(np.r_[True, us[1:] != us[:-1], True])
        for s, e in zip(bounds[:-1], bounds[1:]):
            t_med[us[s]] = np.median(ts[s:e])

        def rematch(z, d, windows, weight):
            for w in windows:
                M = match_transitions(units, transitions(z, d, u, b2, t), z, d, w)
                dz, dd, res, res_a = solve_drift(z, d, M, anchors, group, ng, weight)
                z, d = z + dz, d + dd[group] / pp.DAY_S
            return z, d, M, res, res_a

        d = np.full(n, d0)
        z, d, M, res, res_a = rematch(zero + t_med * (units.drift - d), d, DRIFT_WINDOWS_COMMON, DRIFT_HOLD_WEIGHT)
        self.common_res, self.common_res_anchors = res, res_a
        # matches under free per-beacon drifts: with the common drift, the matches of a beacon whose
        # clock deviates leave the window late in its uptime, which pulls its drift to the common one
        zf, df, _, _, _ = rematch(z, d, DRIFT_WINDOWS_FREE, DRIFT_FREE_WEIGHT)
        M = match_transitions(units, transitions(zf, df, u, b2, t), zf, df, DRIFT_WINDOWS_FREE[-1])
        raw = solve_drift(z, d, M, anchors, group, ng, DRIFT_FREE_WEIGHT)[1] + d0 * pp.DAY_S
        # noise model and true spread from fits on even and odd days
        odd = (np.floor((z[M[0]] + M[2]) / pp.DAY_S).astype(np.int64) % 2).astype(bool)
        half, info_half = [], []
        for mask in (~odd, odd):
            Mh = tuple(x[mask] for x in M)
            half.append(solve_drift(z, d, Mh, anchors, group, ng, DRIFT_FREE_WEIGHT)[1] + d0 * pp.DAY_S)
            info_half.append(_info(Mh, group, ng, n))
        (de, do), (ie, io) = half, info_half
        both = (ie > 0) & (io > 0)
        self.k = float(np.median(np.abs(de - do)[both] / np.sqrt(1 / ie[both] + 1 / io[both])) / 0.6745)
        good = both & (self.k / np.sqrt(np.maximum(ie, 1e-12)) < DRIFT_GOOD_NOISE) & \
            (self.k / np.sqrt(np.maximum(io, 1e-12)) < DRIFT_GOOD_NOISE)
        cov = np.mean((de[good] - np.median(de[good])) * (do[good] - np.median(do[good])))
        self.tau = float(np.sqrt(max(cov, 0.0)))
        self.centre = float(np.median(np.r_[de[good], do[good]]))
        self.n_good = int(good.sum())
        # shrinkage
        self.info = _info(M, group, ng, n)
        self.matches = np.bincount(group[np.r_[M[0], M[1]]], minlength=ng)
        self.raw = raw
        self.noise = self.k / np.sqrt(np.maximum(self.info, 1e-12))
        self.reliability = self.tau ** 2 / (self.tau ** 2 + self.noise ** 2)
        self.drift_beacon = self.centre + (raw - self.centre) * self.reliability
        self.drift_sd = np.sqrt(1 / (1 / max(self.tau, 1e-9) ** 2 + 1 / self.noise ** 2))
        d_new = self.drift_beacon[group] / pp.DAY_S
        z, _, M, res, res_a = rematch(z + t_med * (d - d_new), d_new, DRIFT_WINDOWS_FINAL, DRIFT_HOLD_WEIGHT)
        self.zero, self.drift = z, d_new
        self.res, self.res_anchors, self.n_matches = res, res_a, len(M[0])


# ------------------------------------------------------------------ main analysis

def model_units(readouts: list, out_dir: Path):
    """Units from the uptime model (uptime_model_periods.csv, uptime_model_readouts.csv,
    uptime_model_headerless.csv): its periods with their boots and drifts; readouts without Current
    Timer in the period the model assigned, or in a period of their own."""
    boot, drift = {}, {}
    for row in csv.DictReader((out_dir / "uptime_model_periods.csv").open(encoding="utf-8")):
        key = (row["ID"], row["Period"])
        boot[key] = datetime.strptime(row["Boot"], pp.TIMESTAMP_FORMAT).timestamp()
        drift[key] = float(row["Drift [s/day]"]) / pp.DAY_S
    of_line = {(row["Log file"], int(row["First line"])): (row["ID"], row["Period"])
               for row in csv.DictReader((out_dir / "uptime_model_readouts.csv").open(encoding="utf-8"))}
    path = out_dir / "uptime_model_headerless.csv"
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8")):
            line = (row["Log file"], int(row["First line"]))
            if row["Period"] and (row["ID"], row["Period"]) in boot:
                of_line[line] = (row["ID"], row["Period"])
            elif row.get("Own period boot"):
                key = (row["ID"], f"own {line[0]}:{line[1]}")
                boot[key] = datetime.strptime(row["Own period boot"], pp.TIMESTAMP_FORMAT).timestamp()
                drift[key] = tza.DRIFT_S_PER_S
                of_line[line] = key
    keys = sorted(k for k in boot if k[0].isdigit())
    index = {k: n for n, k in enumerate(keys)}
    of_readout = np.full(len(readouts), -1, dtype=np.int64)
    limit = np.zeros(len(readouts), dtype=np.int64)
    for i, r in enumerate(readouts):
        k = of_line.get((r.source.name, r.first_line))
        if k in index:
            of_readout[i] = index[k]
        limit[i] = (r.current_timer + CT_EXCESS_S) if r.current_timer is not None else tza.MAX_PLAUSIBLE_TIMER_S
    beacon = np.array([int(k[0]) for k in keys])
    of_beacon = collections.defaultdict(list)
    for n, b in enumerate(beacon):
        of_beacon[int(b)].append(n)
    return types.SimpleNamespace(key=keys, index=index, of_readout=of_readout, limit=limit,
                                 zero0=np.array([boot[k] for k in keys]), drift=np.array([drift[k] for k in keys]),
                                 beacon=beacon, n=len(keys), of_beacon=of_beacon)


def unit_contacts(units, readouts, lines):
    """Contacts per unit (unit, ID2, timer, readout): own sightings and timers above the readout's
    limit left out, and the records made during the beacon's own eco sessions (eco_flags)."""
    rd, id2, tm, eco = collect_contacts(lines, readouts)
    u = units.of_readout[rd]
    keep = (u >= 0) & (tm >= 0) & (tm <= units.limit[rd]) & (id2 != units.beacon[np.maximum(u, 0)])
    u, b2, t, crd = u[keep], id2[keep], tm[keep].astype(np.float64), rd[keep]
    sessions = eco_sessions_of_units(units.of_readout, [r.current_timer for r in readouts], eco)
    in_eco = eco_flags(u, t, sessions)
    print(f"  {len(sessions)} eco sessions; {int(in_eco.sum())} records made during the beacon's own eco "
          "sessions left out of the edges")
    return u[~in_eco], b2[~in_eco], t[~in_eco], crd[~in_eco]


def run_model_periods(lines: logcache.Lines, out_dir: Path, hints: bool = True) -> None:
    """Drift per beacon on the uptime model's periods and boots (instead of the timer-zero analysis'
    periods, which can mix two boots): the units are aligned already, only DriftFit runs; writes
    contact_sync_drift.csv for the next solve of the model."""
    readouts, _ = tza.analyse(lines, tza.read_reboot_hints(out_dir / tza.REBOOT_HINTS_CSV) if hints else {})
    units = model_units(readouts, out_dir)
    print(f"Contact sync on the uptime model's periods: {len(readouts)} readouts, {units.n} units")
    u, b2, t, _ = unit_contacts(units, readouts, lines)
    anchor_rows = np.array([(int(units.of_readout[i]), float(r.current_timer), r.header_time.timestamp())
                            for i, r in enumerate(readouts)
                            if r.current_timer is not None and units.of_readout[i] >= 0
                            and (r.first_in_file or r.header_time >= tza.ANCHOR_FROM)])
    fit = DriftFit(units, u, b2, t, units.zero0, np.ones(units.n, dtype=bool), anchor_rows)
    write_drift(out_dir / DRIFT_CSV, fit)
    a = fit.res_anchors
    dr = fit.drift_beacon[fit.matches > 0]
    p5, p50, p95 = np.percentile(dr, [5, 50, 95])
    print(f"  drift per beacon: {int((fit.matches > 0).sum())} beacons, {fit.n_matches} matches; true spread "
          f"{fit.tau:.2f} s/day ({fit.n_good} beacons) around {fit.centre:.2f}; reliability > 0.8: "
          f"{int(((fit.matches > 0) & (fit.reliability > 0.8)).sum())}")
    print(f"  drift [s/day] 5/50/95 %: {p5:.1f} / {p50:.1f} / {p95:.1f}; match residual median "
          f"{np.median(np.abs(fit.res)):.1f} s; delay-free readouts {len(a)}, median |residual| "
          f"{np.median(np.abs(a)):.1f} s")
    print(f"  written to {out_dir / DRIFT_CSV}")


def run(lines: logcache.Lines, out_dir: Path, hints: bool = True) -> None:
    """hints: use the reboot hints found so far and add the new ones to them (run_pipeline.py);
    otherwise none are used and the file holds only the hints of this run."""
    hints_csv = out_dir / tza.REBOOT_HINTS_CSV
    if not hints:
        hints_csv.unlink(missing_ok=True)
    readouts, periods = tza.analyse(lines, tza.read_reboot_hints(hints_csv))
    tza.find_issues(readouts, periods)        # assigns periods to header-less readouts
    units = Units(readouts, periods)
    u, b2, t, crd = unit_contacts(units, readouts, lines)
    rd, tm = crd, t
    print(f"Contact sync: {len(readouts)} readouts, {units.n} units, {len(u)} contacts")
    events = transitions(units.zero0, units.drift, u, b2, t)

    edges1 = pair_offsets(units, events, None)
    c1, _, _ = solve(units.n, edges1)
    edges2 = pair_offsets(units, events, c1)
    c2, agree, n_good = solve(units.n, edges2)
    synced = (n_good >= MIN_AGREE_PAIRS) & (agree >= MIN_AGREEMENT)
    print(f"  pairs: pass 1 {len(edges1)}, pass 2 {len(edges2)}; synced by pairs {int(synced.sum())}")

    # absolute level from the delay-free readouts of synced units
    anchors = []
    for r in readouts:
        if r.current_timer is None or not r.period or not (r.first_in_file or r.header_time >= tza.ANCHOR_FROM):
            continue
        k = units.index[(r.beacon_id, r.period)]
        if synced[k]:
            anchors.append(r.corrected_zero().timestamp() - (units.zero0[k] + c2[k]))
    level = float(np.median(anchors))
    shift = np.where(synced, c2 + level, 0.0)
    method = ["pairs" if synced[k] else "" for k in range(units.n)]
    detail = [""] * units.n
    partners = collections.defaultdict(list)
    for (uu, bb) in events:
        partners[uu].append(bb)
    for k in range(units.n):
        if synced[k]:
            continue
        res = align_unit(units, events, partners, k, synced, shift)
        if isinstance(res, tuple):
            shift[k], method[k] = res[0], "all partners"
            detail[k] = f"peak {res[1]}, background {res[2]:.1f}, partners {res[3]}"
        else:
            method[k] = "not synced" + (" (weak)" if res == "weak" else "")

    aligned = np.array([not m.startswith("not synced") for m in method])
    own = collections.defaultdict(list)                    # unit -> readouts with own Current Timer
    for r in readouts:
        if r.current_timer is not None and r.period:
            own[units.index[(r.beacon_id, r.period)]].append(r)
    anchor_rows = np.array([(k, float(r.current_timer), r.header_time.timestamp())
                            for k, rs in own.items() if aligned[k] for r in rs
                            if r.first_in_file or r.header_time >= tza.ANCHOR_FROM])
    fit = DriftFit(units, u, b2, t, units.zero0 + shift, aligned, anchor_rows)
    zero_fit = fit.zero.copy()

    status = [""] * units.n
    for k in range(units.n):
        if not aligned[k]:
            continue
        if units.key[k][0] == "headerless":
            r = readouts[units.key[k][1]]
            uptime = (r.start_time.timestamp() - zero_fit[k]) / (1 + fit.drift[k])
            p99 = float(np.percentile(tm[rd == units.key[k][1]], 99))
            if p99 > uptime + HEADERLESS_TOL_S:
                status[k] = (f"conflict: contact timers (99 % {p99 / 3600:.1f} h) above the uptime at the "
                             f"readout ({uptime / 3600:.1f} h)")
            continue
        # smallest (delay - lower bound) of the period's readouts: negative = zero too late
        slack = min(r.header_time.timestamp() - zero_fit[k] - r.current_timer * (1 + fit.drift[k])
                    - r.delay_lower_bound for r in own[k])
        if slack < -CONFLICT_S:
            status[k] = (f"conflict: contacts put the zero {-slack:.0f} s later than its own readouts "
                         "allow; old zero kept")
        elif slack < -LATER_TOL_S:
            status[k] = f"{-slack:.0f} s later than its own readouts allow; set to their edge"
            zero_fit[k] += slack
    reboot_hints(units, readouts, own, zero_fit, fit.drift, status, hints_csv)
    applied = aligned & np.array([not s.startswith("conflict") for s in status])
    zero_new = np.where(applied, zero_fit, units.zero0)
    drift_new = np.where(applied, fit.drift, units.drift)
    shift = zero_new - units.zero0

    write_periods(out_dir / PERIODS_CSV, units, readouts, zero_new, drift_new, shift, applied, method, agree,
                  status, detail)
    write_drift(out_dir / DRIFT_CSV, fit)
    summary(units, readouts, shift, applied, method, status, fit, anchor_rows)
    print(f"  written to {out_dir / PERIODS_CSV} and {out_dir / DRIFT_CSV}")


def reboot_hints(units: Units, readouts, own, zero_fit, drift, status, path: Path) -> int:
    """Boots confirmed by the contact sync for timer_zero_analysis.py (rule a'').

    A conflict period (the contacts put its boot later than its readouts allow) holds a second boot
    when a readout of the period shows a drift-corrected timer-zero time that fits the boot the
    contacts give (tza.hint_fits: not before it, beyond rounding, and at most tza.REBOOT_HINT_TOL_S
    after it - a readout is not logged before it happens): that readout was not late but belongs to a
    later boot that the rules merged (reboot within 1 h of the edge, too few following readouts).
    Hints are added to the file and kept (after the split the conflict is gone) for the rest of the
    run: run_pipeline.py clears the file at its start (round 1 finds them, round 2 uses them), so a run
    does not depend on the runs before it; run alone without --reboot-hints, the file is written
    anew (run). Returns the number of new ones."""
    known = tza.read_reboot_hints(path)
    rows = []
    for k in range(units.n):
        if units.key[k][0] == "headerless" or not status[k].startswith("conflict"):
            continue
        boot = datetime.fromtimestamp(zero_fit[k])
        support = [r for r in own[k] if tza.hint_fits(r.corrected_zero(), boot)]
        if not support:
            continue
        beacon = units.key[k][0]
        if any(abs((boot - h).total_seconds()) <= tza.REBOOT_HINT_TOL_S for h in known.get(beacon, ())):
            continue
        known.setdefault(beacon, []).append(boot)
        rows.append([beacon, _fmt(zero_fit[k]), units.key[k][1], _fmt(support[0].header_time.timestamp()),
                     f"{status[k]}; readout {_fmt(support[0].header_time.timestamp())} has its timer-zero time "
                     f"{(support[0].corrected_zero() - boot).total_seconds():+.0f} s from it"])
    if rows:
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["ID", "Boot", "Period when found", "Supporting readout", "Evidence"])
            w.writerows(rows)
        print(f"  {len(rows)} new reboot hint(s) written to {path}: rerun timer_zero_analysis.py (or "
              "run_pipeline.py) and contact_sync_analysis.py to apply them")
    return len(rows)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime(pp.TIMESTAMP_FORMAT)


def write_periods(path: Path, units: Units, readouts, zero_new, drift_new, shift, applied, method, agree, status,
                  detail) -> None:
    size = collections.Counter((r.beacon_id, r.period) for r in readouts if r.current_timer is not None and r.period)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Period", "Readouts with Current Timer", "Zero old", "Zero new",
                    "Shift [s] (negative: earlier)", "Drift old [s/day]", "Drift new [s/day]", "Method",
                    "Pair agreement", "Status", "Detail"])
        for k, key in enumerate(units.key):
            if key[0] == "headerless":
                r = readouts[key[1]]
                ident, period, n = r.beacon_id, f"header-less readout {_fmt(r.start_time.timestamp())}", 0
            else:
                ident, period, n = key[0], key[1], size[key]
            w.writerow([ident, period, n, _fmt(units.zero0[k]), _fmt(zero_new[k]) if applied[k] else "",
                        round(shift[k], 1) if applied[k] else "", round(units.drift[k] * pp.DAY_S, 2),
                        round(drift_new[k] * pp.DAY_S, 2) if applied[k] else "", method[k],
                        round(float(agree[k]), 2) if agree[k] else "", status[k], detail[k]])


def write_drift(path: Path, fit: DriftFit) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Matches", "Info [day^2]", "Raw drift [s/day]", "Noise sd [s/day]", "Reliability",
                    "Drift [s/day]", "Drift sd [s/day]"])
        for i, b in enumerate(fit.beacons):
            if fit.matches[i]:
                w.writerow([int(b), int(fit.matches[i]), round(float(fit.info[i])), round(float(fit.raw[i]), 2),
                            round(float(fit.noise[i]), 2), round(float(fit.reliability[i]), 2),
                            round(float(fit.drift_beacon[i]), 2), round(float(fit.drift_sd[i]), 2)])


def summary(units: Units, readouts, shift, applied, method, status, fit: DriftFit, anchor_rows) -> None:
    per = [k for k in range(units.n) if units.key[k][0] != "headerless"]
    hl = [k for k in range(units.n) if units.key[k][0] == "headerless"]
    print(f"  periods {len(per)}: by pairs {sum(method[k] == 'pairs' for k in per)}, by all partners "
          f"{sum(method[k] == 'all partners' for k in per)}, not synced {sum(method[k].startswith('not') for k in per)}, "
          f"conflicts {sum(status[k].startswith('conflict') for k in per)}")
    sh = -shift[[k for k in per if applied[k]]]
    print("  zero earlier by > 60 s: %d, > 10 min: %d, > 1 h: %d (median %.0f s, max %.0f s)" % (
        (sh > 60).sum(), (sh > 600).sum(), (sh > 3600).sum(), np.median(sh), sh.max()))
    a = fit.res_anchors
    print(f"  drift per beacon: {int((fit.matches > 0).sum())} beacons, {fit.n_matches} matches; noise "
          f"{fit.k:.1f} / sqrt(info), true spread {fit.tau:.2f} s/day ({fit.n_good} beacons) around {fit.centre:.2f}; "
          f"reliability > 0.8: {int(((fit.matches > 0) & (fit.reliability > 0.8)).sum())}")
    dr = fit.drift_beacon[fit.matches > 0]
    print("  drift [s/day] 5/50/95 %%: %.1f / %.1f / %.1f; match residual median %.1f s (common drift %.1f s)" % (
        *np.percentile(dr, [5, 50, 95]), np.median(np.abs(fit.res)), np.median(np.abs(fit.common_res))))
    print(f"  delay-free readouts: {len(a)}, within +-30 s: {int((np.abs(a) <= 30).sum())}, median |residual| "
          f"{np.median(np.abs(a)):.1f} s (common drift {np.median(np.abs(fit.common_res_anchors)):.1f} s)")
    print(f"  header-less readouts without zero time: {len(hl)}, now with zero time: {int(sum(applied[k] for k in hl))}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Align the timer-zero times of all uptime periods by symmetric contacts.")
    pp.add_common_arguments(parser)
    parser.add_argument("--model-periods", action="store_true",
                        help="Only the drift per beacon, on the uptime model's periods and boots (run after "
                             "uptime_model.py); writes contact_sync_drift.csv.")
    tza.add_reboot_hints_argument(parser)
    args = parser.parse_args(argv)
    out_dir = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = logcache.load(args.log_dir)
    if args.model_periods:
        run_model_periods(lines, out_dir, args.reboot_hints)
    else:
        run(lines, out_dir, args.reboot_hints)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
