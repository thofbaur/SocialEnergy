#!/usr/bin/env python3
"""Split the ID1 values 0-9 of the original logs into the real IDs 0-9 and 48-57.

Until study.toml [id_bug] until the logger wrote ID1 48-57 as 0-9 (Background Information 3.2.1).
Every readout logged under ID1 d (0-9) before that time is either beacon d or beacon
d + 48. ID2 is always correct, and so is every ID1 after the cutoff.

Evidence per disputed readout, strongest first; later steps use the earlier decisions:
A1 Self-exclusion: a beacon never records itself. Own contacts with ID2 = d -> the
   readout is d + 48; with ID2 = d + 48 -> it is d.
B  Timer zero: the readout's timer-zero time (line time - Current Timer) matches a known
   zero of one candidate only. Known zeros: decided readouts and the correctly labelled
   readouts after the cutoff. A logged zero lies at or after the true one (delay >= 0):
   it fits a known zero z' if z' - 60 s <= zero <= z' + 300 s.
A2 Witnesses (undecided readouts only): for each own contact (partner Y, real time T),
   did Y record ID2 = d or ID2 = d + 48 within +-10 min of T? Y must be reliably
   labelled (outside 0-9 / 48-57, or read out after the cutoff). Needs >= 3 exclusive
   hits and >= 60 % for one side. Unreliable when d and d + 48 were close to each other
   (the handout on 2026-08-13), therefore after A1 and B.
B  again with the new decisions.
C  No Current Timer, but contacts: the largest contact timer must fit the uptime of
   exactly one candidate at the readout (<= uptime + 2 min, >= uptime - 6 h).
D  The rest (mostly stubs: a single Status line): the label of the nearest decided
   readout of the pair, if within 15 min and the other candidate's is at least 15 min
   farther.

E  and F (second pass, run_pipeline.py step 6, with the uptime model): Resolver.model_boots and
   Resolver.model_rules.

The split runs only if the study's logger had the bug (study.toml [id_bug]); otherwise no readout
is disputed.

Run alone (python -m steps.id_split_analysis), it does steps 2 to 4 of run_pipeline.py - the same
code (first_split, write_working_logs): the fake records, the contacts of the next beacon under the
previous ID, the first split and the working copies of the logs, without the uptime model (steps
5 and 6). Outputs in --output-dir (default Output/pipeline):
- id_correction/id_split_analysis.csv   one row per disputed readout: lines, logged ID, decided ID,
                                        method, evidence
- id_correction/switch_relabels.csv     contacts of the next beacon under the previous ID
- quality/fake_records_original.csv     fake records of the original logs
- logs/                                 working copies of the original logs
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import logger_protocol
from lib import postprocessing as pp
from lib import study
from steps import timer_zero_analysis as tza

CSV_NAME = "id_split_analysis.csv"
SWITCH_CSV = "switch_relabels.csv"       # contacts of the next beacon under the previous ID (pipeline step 2)

ANALYSIS_END = pp.ID_BUG_WITNESSES_UNTIL      # later readouts are not needed as witnesses / anchors (study.toml)
LOW = set(pp.ID_BUG_LOW)
AMBIGUOUS = LOW | {d + pp.ID_BUG_OFFSET for d in LOW}
MAX_EXCESS_S = pp.READOUT_TIMER_EXCESS_S     # contact timers at most this far above Current Timer
WITNESS_WINDOW_S = 600
WITNESS_MIN_HITS, WITNESS_MIN_SHARE = 3, 0.6
ZERO_EARLY_S, ZERO_LATE_S = 60, 300
MODEL_LATE_S = 1800                # second pass: a readout fits a model boot up to this late
UPTIME_TOL_S, UPTIME_GAP_S = 120, 6 * 3600
NEIGHBOUR_S = 15 * 60


@dataclass
class Readout:
    beacon_id: str
    file: str
    first: int
    last: int
    start: float
    header: Optional[float] = None
    current_timer: Optional[int] = None
    contacts: int = 0
    transfer_complete: bool = False
    # disputed readouts only
    label: Optional[int] = None
    method: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def zero(self) -> Optional[float]:
        return self.header - self.current_timer if self.current_timer is not None else None


# ------------------------------------------------------------------ parsing

def find_fakes(lines: logcache.Lines) -> set[tuple[str, int]]:
    """(file, line) of the fake contact records of the logs (BI 3.2.2, logcache.fake_runs), file by
    file, each file's lines in time order (stable: lines of the same second keep the file order)."""
    order = np.lexsort((lines.t, lines.file))               # stable: ties keep the file order
    cols = [getattr(lines, k)[order] for k in ("file", "line", "id1", "kind", "id2", "timer", "rssi")]
    fakes: set[tuple[str, int]] = set()
    for rows, hit, _ in logcache.fake_runs(*cols):
        fakes.update((lines.paths[f].name, n) for f, n in zip(cols[0][rows[hit]].tolist(), cols[1][rows[hit]].tolist()))
    return fakes


def find_switch_mislabels(lines: logcache.Lines, fakes: set[tuple[str, int]] | None = None) -> list[dict]:
    """Contacts of the next beacon logged under the ID of the previous one, per log file in line order
    (rule: postprocessing.SWITCH_MIN_DROP_S): in a run of consecutive contact lines, the lines of ID A
    right before the first line of ID B are B's if their timers drop by more than SWITCH_MIN_DROP_S
    below A's contact before them, do not decrease from there and continue into B's first timer within
    0 .. SWITCH_CONTINUITY_S. Fake records are left out (they end a run). Returns per run: file, first
    and last line, old and new ID, timers, the timer before the drop and B's first timer."""
    ok = (lines.kind == 1) & ~lines.mask_of(fakes or ())
    line, id1, file = lines.line.astype(np.int64), lines.id1, lines.file
    timer = lines.timer.astype(np.int64)
    joined = np.zeros(len(line), dtype=bool)
    joined[1:] = ok[1:] & ok[:-1] & (file[1:] == file[:-1]) & (line[1:] == line[:-1] + 1)
    change = joined.copy()
    change[1:] &= id1[1:] != id1[:-1]
    # a run starts at a contact line not joined to the one before, and again after each change of ID
    starts = np.maximum.accumulate(np.where(~joined | change, np.arange(len(line)), 0)) if len(line) else line
    out: list[dict] = []
    for j in np.flatnonzero(change).tolist():
        s = int(starts[j - 1])
        drops = np.flatnonzero(timer[s:j - 1] > timer[s + 1:j])          # timer[p - 1] > timer[p], p = s + 1 + x
        k = s + 1 + int(drops[-1]) if len(drops) else s
        if (k > s and timer[k - 1] - timer[k] > pp.SWITCH_MIN_DROP_S
                and 0 <= timer[j] - timer[j - 1] <= pp.SWITCH_CONTINUITY_S):
            out.append(dict(first=int(line[k]), last=int(line[j - 1]), old=lines.ids[id1[j - 1]], new=lines.ids[id1[j]],
                            timer_from=int(timer[k]), timer_to=int(timer[j - 1]), timer_before=int(timer[k - 1]),
                            next_timer=int(timer[j]), file=lines.paths[file[j]].name))
    return out


def write_switch_csv(path: Path, switches: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Log file", "First line", "Last line", "Lines", "ID logged", "ID corrected", "Timer from",
                    "Timer to", "Timer before (ID logged)", "Next timer (ID corrected)"])
        for r in switches:
            w.writerow([r["file"], r["first"], r["last"], r["last"] - r["first"] + 1, r["old"], r["new"],
                        r["timer_from"], r["timer_to"], r["timer_before"], r["next_timer"]])


def parse_logs(lines: logcache.Lines, skip: frozenset | set = frozenset(), files: Optional[set[str]] = None
               ) -> tuple[list[Readout], dict[str, np.ndarray]]:
    """Readouts per file (same split as timer_zero_analysis.collect_readouts) up to ANALYSIS_END and
    all their contacts; files: the log file names to read (all if None); contact lines in skip
    ((file, line), e.g. fake records) are left out of the contacts. Contact lines come in runs of one
    ID1, taken at once: of a run only its first line can start a readout."""
    readouts: list[Readout] = []
    rd, id2, timer = [], [], []
    end = ANALYSIS_END.timestamp()
    local = logcache.local_timestamps(lines.t)
    skipped = lines.mask_of(skip)
    for f, path in enumerate(lines.paths):
        if files is not None and path.name not in files:
            continue
        a, b = int(lines.start[f]), int(lines.start[f + 1])
        late = np.flatnonzero(local[a:b] >= end)
        b = a + int(late[0]) if len(late) else b
        kind, id1, number = lines.kind[a:b], lines.id1[a:b], lines.line[a:b]
        new = np.ones(b - a, dtype=bool)
        new[1:] = (kind[1:] == 0) | (kind[:-1] == 0) | (id1[1:] != id1[:-1])
        starts = np.flatnonzero(new)
        cur: Optional[Readout] = None
        for x, y in zip(starts.tolist(), np.r_[starts[1:], b - a].tolist()):
            beacon = lines.ids[id1[x]]
            if kind[x] == 1:
                rest, ct = "", None
            else:
                rest = lines.rest[lines.other[a + x]]
                ct = pp.CURRENT_TIMER_RE.match(rest)
            has_content = cur is not None and (cur.current_timer is not None or cur.contacts or cur.transfer_complete)
            if (cur is None or beacon != cur.beacon_id or (rest.startswith(pp.TEXT_PREFIX["status"]) and has_content)
                    or (ct and cur.current_timer is not None) or cur.transfer_complete):
                cur = Readout(beacon, path.name, int(number[x]), int(number[x]), float(local[a + x]))
                readouts.append(cur)
            cur.last = int(number[y - 1])
            if ct:
                cur.current_timer, cur.header = int(ct.group("timer")), float(local[a + x])
                continue
            if kind[x] == 1:
                cur.contacts += y - x
                keep = ~skipped[a + x:a + y]
                rd.extend([len(readouts) - 1] * int(keep.sum()))
                id2.extend(lines.id2[a + x:a + y][keep].tolist())
                timer.extend(lines.timer[a + x:a + y][keep].tolist())
            elif rest.startswith(pp.TEXT_PREFIX["transfer_complete"]):
                cur.transfer_complete = True
        print(f"  {path.name}: {len(readouts)} readouts, {len(rd)} contacts so far", flush=True)
    return readouts, dict(rd=np.array(rd, dtype=np.int64), id2=np.array(id2, dtype=np.int64),
                          timer=np.array(timer, dtype=np.int64))


def is_disputed(r: Readout) -> bool:
    return r.beacon_id.isdigit() and int(r.beacon_id) in LOW and pp.id_bug_applies(r.start)


def is_reliable(r: Readout) -> bool:
    return r.beacon_id.isdigit() and (int(r.beacon_id) not in AMBIGUOUS or not pp.id_bug_applies(r.start))


# ------------------------------------------------------------------ evidence and decisions

class Resolver:
    def __init__(self, readouts: list[Readout], contacts: dict[str, np.ndarray]):
        self.readouts = readouts
        rd, id2, timer = contacts["rd"], contacts["id2"], contacts["timer"]
        header = np.array([r.header if r.header is not None else np.nan for r in readouts])
        ct = np.array([r.current_timer if r.current_timer is not None else np.nan for r in readouts])
        valid = ~np.isnan(ct[rd]) & (timer >= 0) & (timer <= ct[rd] + MAX_EXCESS_S)
        real = np.where(valid, header[rd] - (ct[rd] - timer), np.nan)
        reliable = np.array([is_reliable(r) for r in readouts])
        rid = np.array([int(r.beacon_id) if r.beacon_id.isdigit() else -1 for r in readouts])
        # witness index: (witness, seen ambiguous ID) -> sorted real times
        m = valid & reliable[rd] & np.isin(id2, list(AMBIGUOUS))
        index = collections.defaultdict(list)
        for w, s, t in zip(rid[rd[m]], id2[m], real[m]):
            index[(int(w), int(s))].append(t)
        self.witness = {k: np.sort(np.array(v)) for k, v in index.items()}
        order = np.argsort(rd, kind="stable")
        bounds = np.searchsorted(rd[order], np.arange(len(readouts) + 1))
        self.own = {}                        # disputed readout -> (ID2s, timers, real times, valid)
        self.disputed = [i for i, r in enumerate(readouts) if is_disputed(r)]
        for i in self.disputed:
            sel = order[bounds[i]:bounds[i + 1]]
            self.own[i] = (id2[sel], timer[sel], real[sel], valid[sel])
        self.anchors = collections.defaultdict(list)   # d -> [(label, zero, time)] after the cutoff
        for r in readouts:
            if r.current_timer is not None and not pp.id_bug_applies(r.start) and r.beacon_id.isdigit():
                b = int(r.beacon_id)
                if b in AMBIGUOUS:
                    self.anchors[b % 48].append((b, r.zero, r.header))

    def _seen(self, key, t) -> bool:
        a = self.witness.get(key)
        if a is None:
            return False
        j = np.searchsorted(a, t)
        return (j < len(a) and a[j] - t <= WITNESS_WINDOW_S) or (j > 0 and t - a[j - 1] <= WITNESS_WINDOW_S)

    def _decide(self, i: int, label: int, method: str, note: str = "") -> None:
        r = self.readouts[i]
        r.label, r.method = label, method
        if note:
            r.evidence.append(note)

    def _known(self, d: int) -> list[tuple[int, float, float]]:
        out = list(self.anchors[d])
        out += [(r.label, r.zero, r.header) for r in (self.readouts[i] for i in self.disputed)
                if r.label is not None and int(r.beacon_id) == d and r.current_timer is not None]
        return out

    def self_exclusion(self) -> None:
        for i in self.disputed:
            d = int(self.readouts[i].beacon_id)
            id2 = self.own[i][0]
            n_low, n_high = int(np.sum(id2 == d)), int(np.sum(id2 == d + 48))
            self.readouts[i].evidence.append(f"own contacts with ID2 {d} / {d + 48}: {n_low} / {n_high}")
            if n_low != n_high:
                self._decide(i, d if n_high > n_low else d + 48, "A1 self-exclusion")

    def witnesses(self) -> None:
        for i in self.disputed:
            r = self.readouts[i]
            d = int(r.beacon_id)
            id2, _, real, valid = self.own[i]
            low = high = 0
            for y, t, ok in zip(id2, real, valid):
                if not ok or int(y) in AMBIGUOUS:
                    continue
                a, b = self._seen((int(y), d), t), self._seen((int(y), d + 48), t)
                low += a and not b; high += b and not a
            r.evidence.append(f"witnesses saw {d} / {d + 48}: {low} / {high}")
            if r.label is None and low + high >= WITNESS_MIN_HITS and \
                    max(low, high) >= WITNESS_MIN_SHARE * (low + high) and low != high:
                self._decide(i, d if low > high else d + 48, "A2 witnesses")

    def timer_zero(self) -> None:
        for _ in range(5):
            changed = False
            for i in self.disputed:
                r = self.readouts[i]
                if r.label is not None or r.current_timer is None:
                    continue
                d = int(r.beacon_id)
                fits: dict[int, float] = {}
                for lab, z, _ in self._known(d):
                    if z - ZERO_EARLY_S <= r.zero <= z + ZERO_LATE_S:
                        fits[lab] = min(fits.get(lab, 1e18), abs(r.zero - z))
                if len(fits) == 1:
                    (lab, dz), = fits.items()
                    self._decide(i, lab, "B timer zero",
                                 f"zero {_fmt(r.zero)} fits only {lab} ({dz:.0f} s from a known zero)")
                    changed = True
            if not changed:
                break

    def contact_timers(self) -> None:
        for i in self.disputed:
            r = self.readouts[i]
            timers = self.own[i][1]
            timers = timers[timers <= tza.MAX_PLAUSIBLE_TIMER_S]
            if r.label is not None or r.current_timer is not None or not len(timers):
                continue
            m, d = int(timers.max()), int(r.beacon_id)
            fits = {lab for lab, z, _ in self._known(d)
                    if r.start - z - UPTIME_GAP_S <= m <= r.start - z + UPTIME_TOL_S}
            if len(fits) == 1:
                lab = fits.pop()
                self._decide(i, lab, "C contact timers", f"largest contact timer {m} s fits only {lab}")
            elif fits:
                r.evidence.append(f"largest contact timer {m} s fits both")

    def neighbours(self) -> None:
        decided = [(self.readouts[i].start, int(self.readouts[i].beacon_id), self.readouts[i].label)
                   for i in self.disputed if self.readouts[i].label is not None]
        for i in self.disputed:
            r = self.readouts[i]
            if r.label is not None:
                continue
            d = int(r.beacon_id)
            best: dict[int, float] = {}
            for t, dd, lab in decided:
                if dd == d:
                    best[lab] = min(best.get(lab, 1e18), abs(t - r.start))
            for lab, _, t in self.anchors[d]:
                best[lab] = min(best.get(lab, 1e18), abs(t - r.start))
            lo, hi = best.get(d, 1e18), best.get(d + 48, 1e18)
            if min(lo, hi) <= NEIGHBOUR_S and abs(lo - hi) >= NEIGHBOUR_S:
                self._decide(i, d if lo < hi else d + 48, "D neighbour",
                             f"nearest decided readout {d}: {lo / 60:.1f} min, {d + 48}: {hi / 60:.1f} min")

    def model_rules(self, state: dict) -> tuple[int, list[str]]:
        """Second pass with the hard rules of the uptime model: an undecided readout with Current
        Timer gets the label under which the uptime model keeps all hard rules, if exactly one
        label does (uptime_model.label_test: both candidate beacons solved again with the readout
        under each label). Returns the number of new decisions and notes on the others."""
        from steps import uptime_model as um
        index = {(r["file"], r["first"]): n for n, r in enumerate(state["readouts"])}
        new, notes = 0, []
        for i in self.disputed:
            r = self.readouts[i]
            if r.label is not None or r.current_timer is None:
                continue
            n = index.get((r.file, r.first))
            if n is None:
                continue
            d = int(r.beacon_id)
            res = um.label_test(state, n, (str(d), str(d + 48)))
            keep = [lab for lab, v in res.items() if not v]
            text = "; ".join(f"{lab}: {', '.join(v) if v else 'keeps all hard rules'}" for lab, v in res.items())
            if len(keep) == 1:
                self._decide(i, int(keep[0]), "F uptime model rules", text)
                new += 1
            else:
                notes.append(f"{r.file} line {r.first}: {text}")
        return new, notes

    def model_boots(self, boots: dict[int, list[float]]) -> tuple[int, list[str]]:
        """Second pass with the boots of the uptime model (label -> boots of its periods, without
        periods made only of undecided readouts): an undecided readout with Current Timer gets the
        label whose boot its observed timer-zero time fits (at most ZERO_EARLY_S before and
        MODEL_LATE_S after it), if exactly one fits. Decisions of the first pass that fit only the
        other label are reported, not changed. Returns the number of new decisions and the reports."""
        new, reports = 0, []
        for i in self.disputed:
            r = self.readouts[i]
            if r.current_timer is None:
                continue
            d = int(r.beacon_id)
            fits = {lab for lab in (d, d + 48)
                    if any(b - ZERO_EARLY_S <= r.zero <= b + MODEL_LATE_S for b in boots.get(lab, ()))}
            if r.label is None and len(fits) == 1:
                lab = fits.pop()
                self._decide(i, lab, "E uptime model", f"timer-zero time {_fmt(r.zero)} fits only a boot of {lab} "
                             "in the uptime model")
                new += 1
            elif r.label is not None and fits and r.label not in fits:
                reports.append(f"{r.file} line {r.first} {_fmt(r.start)}: decided {r.label} ({r.method}), "
                               f"but its timer-zero time {_fmt(r.zero)} fits only {sorted(fits)} in the uptime model")
        return new, reports

    def run(self) -> None:
        self.self_exclusion()
        self.timer_zero()
        self.witnesses()
        self.timer_zero()
        self.contact_timers()
        self.neighbours()


def _fmt(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime(pp.TIMESTAMP_FORMAT) if ts is not None else ""


# ------------------------------------------------------------------ output

def write_csv(path: Path, readouts: list[Readout], disputed: list[int]) -> None:
    """One row per disputed readout."""
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Log file", "First line", "Last line", "Start time", "ID1 logged", "Current Timer",
                    "Contacts", "ID1 analysis", "Method", "Evidence"])
        for i in disputed:
            r = readouts[i]
            w.writerow([r.file, r.first, r.last, _fmt(r.start), r.beacon_id,
                        r.current_timer if r.current_timer is not None else "", r.contacts,
                        r.label if r.label is not None else "", r.method or "undecided", "; ".join(r.evidence)])


def _changes(readouts: list[Readout], disputed: list[int]) -> dict[str, dict[int, tuple[str, str]]]:
    """file -> line number -> (logged ID1, decided ID1) of the relabelled readouts."""
    changes = collections.defaultdict(dict)
    for i in disputed:
        r = readouts[i]
        if r.label is not None and r.label != int(r.beacon_id):
            for n in range(r.first, r.last + 1):
                changes[r.file][n] = (r.beacon_id, str(r.label))
    return changes


def recover_fake_lines(data: dict) -> tuple[dict[int, str], int]:
    """Reconstruction of the fake records of one log file (BI 3.2.2; data: logcache.read).

    A fake block is a run of consecutive contact lines of one ID1 whose records, put back into
    their bytes, contain complete logger messages: the messages that followed a truncated
    data message and that the logger cut into records. Each complete embedded message is decoded
    as the logger would have written it (logger.toml, lib/logger_protocol.py; the ID from the message's own
    byte, so IDs 48-57 are right even before the logger fix), and its lines replace the block's
    fake lines in order, with their line time; the remaining fake lines become a marker line
    (postprocessing.FAKE_REMOVED_TEXT) that the parsers skip. The line numbers of the file stay the
    same (a block never yields more lines than it has fake lines). Returns line number (1-based) ->
    new line text (without line end), and the number of recovered lines."""
    proto = logger_protocol.protocol()
    out: dict[int, str] = {}
    recovered = 0
    cols = [data[k] for k in ("line", "id1", "kind", "id2", "timer", "rssi")]
    for rows, positions, _ in logcache.fake_runs(np.zeros(len(cols[0]), dtype=np.int8), *cols):
        stream = pp.contact_records_bytes(data["id2"][rows], data["timer"][rows], data["rssi"][rows]) \
            + proto.terminator
        texts: list[tuple[int, str]] = []
        for start, end in pp.embedded_messages(stream):
            beacon, message_type, payload = proto.split(stream[start:end])
            texts.extend((beacon, text) for text in proto.decode(message_type, payload))
        if len(texts) > len(positions):          # cannot happen with these message sizes
            texts = texts[:len(positions)]
        for k, pos in enumerate(positions):
            row = int(rows[pos])
            stamp = logcache.line_time(data["t"][row])
            out[int(data["line"][row])] = proto.log_line(stamp, *texts[k]) if k < len(texts) else \
                f"{stamp.strftime(proto.time_format)},{pp.FAKE_REMOVED_TEXT}"
        recovered += min(len(texts), len(positions))
    return out, recovered


def write_working_logs(directory: Path, original_dir: Path, readouts: list[Readout], disputed: list[int],
                       fakes: set[tuple[str, int]] | None = None,
                       switches: list[dict] | None = None) -> tuple[int, int, int]:
    """All original logs under their own names, with the decided IDs written in and the fake
    records reconstructed (recover_fake_lines: the messages hidden in them as normal log lines, the
    rest as marker lines; for the files that contain fakes), made from the cache of the originals
    (logcache.derive), each with its cache; files without a change are hard links to the original
    (a copy where links are not possible). A copy whose content did not change is not written
    again; a copy without an original (left from an earlier run) is deleted, with its cache.
    Returns the numbers of changed files, relabelled lines and recovered lines. switches
    (find_switch_mislabels): runs of contact lines written under the ID of the beacon that follows
    them (its decided label, if the split relabelled it)."""
    import shutil
    changes = _changes(readouts, disputed)
    for sw in switches or ():
        target = changes[sw["file"]].get(sw["last"] + 1, (None, sw["new"]))[1]
        for n in range(sw["first"], sw["last"] + 1):
            changes[sw["file"]][n] = (changes[sw["file"]].get(n, (sw["old"],))[0], target)
    directory.mkdir(parents=True, exist_ok=True)
    fake_files = {f for f, _ in (fakes or ())}
    replace: dict[str, dict[int, str]] = {}
    recovered = 0
    for name in sorted(fake_files):
        replace[name], n_rec = recover_fake_lines(logcache.read(original_dir / name))
        recovered += n_rec
    lines = 0
    originals = sorted(original_dir.glob("*.log"))
    names = {p.name for p in originals}
    stale = [p for p in sorted(directory.glob("*.log")) if p.name not in names]
    for target in stale:
        logcache.drop(target)
        target.unlink()
    if stale:
        print(f"   {len(stale)} working copies without an original deleted: "
              + ", ".join(p.name for p in stale[:5]) + (" ..." if len(stale) > 5 else ""))
    for path in originals:
        target = directory / path.name
        st = path.stat()
        linked = target.exists() and (target.samefile(path) or (target.stat().st_size, target.stat().st_mtime_ns)
                                      == (st.st_size, st.st_mtime_ns))       # link, or copy (copy2) of the original
        if path.name not in changes and not replace.get(path.name):
            if not linked:
                target.unlink(missing_ok=True)
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
            logcache.link_cache(path, target)
            continue
        relabel, new_lines = changes.get(path.name, {}), replace.get(path.name, {})
        if not linked and target.exists() and logcache.cache_file(target).exists():
            made = logcache.meta(target)
            if made.get("recipe") == logcache.recipe(path, relabel, new_lines):
                lines += made["relabelled"]              # made so in an earlier round or run
                continue
        data, content, n = logcache.derive(path, relabel, new_lines)
        lines += n
        target.unlink(missing_ok=True)                   # never write into a link to the original
        target.write_bytes(content)
        logcache.store(target, data)
    return len(set(changes) | {f for f, r in replace.items() if r}), lines, recovered


def first_split(original: logcache.Lines, paths: list[Path], out: Path, log=print):
    """Steps 2 and 3 of run_pipeline.py on the cache of the original logs: the fake records
    (quality/fake_records_original.csv), the contacts of the next beacon under the previous ID
    (id_correction/switch_relabels.csv) and the first split of ID1 0-9 (id_correction/
    id_split_analysis.csv), in the output folder out. Returns fakes, switches, readouts, resolver."""
    quality, idc = out / pp.OUTPUT_QUALITY_DIR, out / pp.OUTPUT_ID_CORRECTION_DIR
    for d in (quality, idc):
        d.mkdir(parents=True, exist_ok=True)
    fakes = find_fakes(original)
    with (quality / "fake_records_original.csv").open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["Log file", "Line"], *sorted(fakes)])
    log(f"   {len(fakes)} fake records")
    switches = find_switch_mislabels(original, fakes)
    write_switch_csv(idc / SWITCH_CSV, switches)
    log(f"   contacts of the next beacon under the previous ID: {len(switches)} run(s), "
        f"{sum(r['last'] - r['first'] + 1 for r in switches)} lines"
        + "".join(f"; {r['file']}:{r['first']}-{r['last']} {r['old']} -> {r['new']}" for r in switches[:5]))
    log("3. first split of ID1 0-9", flush=True)
    if pp.ID_BUG_UNTIL is None:
        log("   no ID bug in this study (study.toml [id_bug] enabled = false): no readout is disputed")
    end_name = logger_protocol.protocol().log_file_name(ANALYSIS_END)
    readouts, contacts = parse_logs(original, fakes, {p.name for p in paths if p.name < end_name})
    resolver = Resolver(readouts, contacts)
    resolver.run()
    methods = collections.Counter(readouts[i].method or "undecided" for i in resolver.disputed)
    log(f"   {len(resolver.disputed)} disputed readouts: " + ", ".join(f"{k} {v}" for k, v in sorted(methods.items())))
    write_csv(idc / CSV_NAME, readouts, resolver.disputed)            # read by the timer-zero analysis
    return fakes, switches, readouts, resolver


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Steps 2 to 4 of run_pipeline.py: fake records, contacts of the "
                                                 "next beacon under the previous ID, first split of ID1 0-9, "
                                                 "working copies of the logs.")
    study.add_argument(parser)
    parser.add_argument("--output-dir", type=Path, default=pp.PIPELINE_DIR,
                        help=f"Pipeline output folder. Default: {pp.PIPELINE_DIR}")
    args = parser.parse_args(argv)
    paths = sorted(pp.ORIGINAL_LOG_DIR.glob("*.log"))
    print(f"2. {len(paths)} original log files in {pp.ORIGINAL_LOG_DIR}: fake records", flush=True)
    logcache.ensure(paths)
    fakes, switches, readouts, resolver = first_split(logcache.Lines(paths), paths, args.output_dir)
    print("4. working copies of the logs", flush=True)
    files, lines, recovered = write_working_logs(args.output_dir / "logs", pp.ORIGINAL_LOG_DIR, readouts,
                                                 resolver.disputed, fakes, switches)
    print(f"   {files} files changed: {lines} lines relabelled, {recovered} lines recovered from {len(fakes)} "
          f"fake records; written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
