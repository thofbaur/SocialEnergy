#!/usr/bin/env python3
"""Check the logs for contacts the Base filed under the wrong beacon (Background Information 3.3.1).

Until 2026-08-15 18:43 the Base labelled every forwarded message with the ID of the beacon it
had connected to last. A message of beacon A still in transit when the Base connected to
beacon B was logged under B's ID, right after B's header. Such contacts continue A's timers
and do not fit B's Current Timer. The check runs on every readout of the logs, without a time
limit (the Base fix is used only to report the results per phase).

Tests per readout (split as in timer_zero_analysis.collect_readouts):
Fake records (T7) and records with an invalid ID2 or RSSI are no contacts and are left out of T1
and T2 (e.g. 137 at dsa_20260812_2350.log line 126658, ID2 0, a fake of the T7 block before it).
T1 Timer above Current Timer: contacts with a timer more than EXCESS_S above the readout's
   own Current Timer cannot be the beacon's own (contacts recorded during the readout exceed
   it only by the readout duration). Consecutive ones form a block, classified as
   - "continues the previous readout": its first timer lies between the last contact timer
     and the Current Timer (+ EXCESS_S) of the previous readout of another beacon in the
     same file - the Base mislabelling;
   - "fake record pattern": a timer of 524301 (the bytes of a Status message) or above
     30 days (BI 3.2.2);
   - "recorded during a long readout": the excess is covered by the logged time since the
     header (+ EXCESS_S);
   - "not explained".
T2 Self-sighting: a beacon never records itself; contacts with ID2 = ID1 are foreign. In logs
   without the ID correction (BI 3.2.1), ID1 0-9 before 2026-08-15 22:00 can be beacon d + 48,
   which may record d: not testable, counted separately.
T3 Contacts before the header: contact lines between the Status line and the Current Timer
   line of the same readout.
T4 Header-less readout directly after another beacon's readout (not an ID switch, T5) whose first contact timers
   continue that readout (between its last contact timer and its Current Timer + EXCESS_S).
   Without a header, the readout's own uptime comes from the timer-zero analysis
   (readout_overview.csv, Postprocessing Method.md 13.5): timers above it cannot be the
   beacon's own (Base mislabelling); timers within it fit the beacon itself (beacons with
   similar uptimes, a coincidence); without a known timer-zero time the case stays open.

T5 ID switch between two consecutive contact lines (no line between): the part after the switch
   is B's own (A's end and B's header lost, BI 3.2.3) or A's contacts under B's ID. Decided by
   self-exclusion (sightings of A and none of B: B's own; of B and none of A: A's), else by B's
   uptime from the timer-zero analysis, else by A's timers and Contact Count (A's contacts
   before and after the switch add up to it).
T6 Transfer complete directly after another beacon's lines: before the Base fix A's end of
   transfer filed under the next ID (the old Base wrote it with the current ID) or the readout
   lost except its Transfer complete; after the fix only data loss. No contacts affected.
T7 Fake records (BI 3.2.2): a data message whose payload was truncated (bytes lost on the logger
   PC) is filled up by the logger's parser with the bytes of the following messages (mostly the
   Base's Status messages); the CR LF check at its computed end passes by chance. Put back into
   their 5-byte form (ID2, timer, RSSI), consecutive contact records then contain complete logger
   messages ("ID" <beacon> <flag> <payload> CR LF). Every record touching such bytes is fake;
   those that pass the validity checks and are not above Current Timer must be dropped (listed
   in "Lines to drop"). A timer of 524301 alone is no sign: it is a genuine uptime of 6.07 days.

Every finding gets "Cleaning needed": yes (lines under the wrong ID), drop (fake records),
check (undecided), no.

For each phase the number of readouts in which T1 could detect a foreign block is reported:
those whose previous readout (another beacon, same file) has a larger Current Timer
(+ EXCESS_S), so that its continuing contacts would exceed this readout's Current Timer.

Output (quality/ subfolder of --output-dir):
- base_mislabel_check.csv   one row per finding: readout, test, contacts affected, lines,
                            timers, previous readout, assessment, cleaning needed
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import logcache
from lib import postprocessing as pp
from lib import study

CSV_NAME = "base_mislabel_check.csv"                     # on the pipeline's working copies
ORIGINAL_CSV_NAME = "base_mislabel_check_original.csv"   # on the uncorrected logs (data_quality_reports.py)
DEFAULT_ORIGINAL_DIR = pp.ORIGINAL_LOG_DIR

BASE_FIX = pp.STUDY.base_fix                       # BI 3.3.1, 3.3.2 (study.toml)
DEPLOYMENT_START = pp.DEFAULT_VALID_SPAN.start
EXCESS_S = 600                                       # the tests' margin above a Current Timer (T1 and the others)
MAX_TIMER_S = pp.MAX_PLAUSIBLE_TIMER_S
FAKE_TIMER = pp.LOG.status_timer()                   # Status message bytes read as a contact (BI 3.2.2; 524301)

LOW_IDS = {str(i) for i in pp.ID_BUG_LOW}

class Contacts:
    """The contact lines of a readout, in line order: arrays line, id2, timer, t (line time, seconds
    since logcache.EPOCH), rssi, fake (fake record, BI 3.2.2: logcache, the runs of consecutive
    contact lines of the readout). An item is (line, ID2, timer, line time, RSSI); a slice is
    Contacts."""
    __slots__ = ("line", "id2", "timer", "t", "rssi", "fake")

    def __init__(self, line, id2, timer, t, rssi, fake):
        self.line, self.id2, self.timer, self.t, self.rssi, self.fake = line, id2, timer, t, rssi, fake

    def __len__(self) -> int:
        return len(self.line)

    def __getitem__(self, k):
        if isinstance(k, slice):
            return Contacts(*(getattr(self, a)[k] for a in self.__slots__))
        return (int(self.line[k]), int(self.id2[k]), int(self.timer[k]), logcache.line_time(self.t[k]),
                int(self.rssi[k]))

    def __iter__(self):
        return (self[k] for k in range(len(self)))

    def items(self, mask) -> list[tuple[int, int, int, datetime, int]]:
        return [self[k] for k in np.flatnonzero(mask).tolist()]


NO_CONTACTS = Contacts(*(np.zeros(0, dtype=np.int64) for _ in range(5)), np.zeros(0, dtype=bool))


@dataclass
class Readout:
    beacon_id: str
    file: str
    first: int
    start: datetime
    last: int = 0
    current_timer: Optional[int] = None
    header_time: Optional[datetime] = None
    status_seen: bool = False
    transfer_complete: bool = False
    contact_count: Optional[int] = None
    first_kind: str = ""                # kind of the first and last line: contact, status, header, transfer, other
    last_kind: str = ""
    leading: int = 0                    # contacts before the first line of another kind
    contacts: Contacts = NO_CONTACTS
    before_header: list[int] = field(default_factory=list)              # lines of contacts before Current Timer


@dataclass
class Finding:
    readout: Readout
    test: str
    count: int
    first_line: int
    last_line: int
    timers: str
    previous: Optional[Readout]
    assessment: str
    clean: str = "no"                  # yes: contacts under the wrong ID; drop: fake records; check: undecided
    drop_lines: str = ""               # line numbers of the records to drop


def phase(t: datetime) -> str:
    if t < DEPLOYMENT_START:
        return "before the deployment"
    return "deployment, before the Base fix" if t < BASE_FIX else "after the Base fix"


def readouts_of(data: dict, name: str):
    """Yield the readouts of one log file (data: logcache.read) in order (split as in
    timer_zero_analysis.collect_readouts). Contact lines come in runs of one ID1, taken at once: of a
    run only its first line can start a readout."""
    cur: Optional[Readout] = None
    ranges: list[tuple[int, int]] = []              # rows of cur's contact lines
    n_contacts = 0

    def done(r: Readout) -> Readout:
        if ranges:
            rows = np.concatenate([np.arange(a, b) for a, b in ranges])
            r.contacts = Contacts(data["line"][rows].astype(np.int64), data["id2"][rows].astype(np.int64),
                                  data["timer"][rows].astype(np.int64), data["t"][rows], data["rssi"][rows].astype(np.int64),
                                  data["fake"][rows])
        return r

    kind, id1, number, t, ids = data["kind"], data["id1"], data["line"], data["t"], data["ids"]
    valid = [pp.is_valid_beacon_id(b) for b in ids]
    rest_of = np.full(len(kind), -1, dtype=np.int64)
    rest_of[kind == 0] = np.arange(len(data["rest"]))
    new = np.ones(len(kind), dtype=bool)
    new[1:] = (kind[1:] == 0) | (kind[:-1] == 0) | (id1[1:] != id1[:-1])
    starts = np.flatnonzero(new)
    for a, b in zip(starts.tolist(), np.r_[starts[1:], len(kind)].tolist()):
        beacon = ids[id1[a]]
        if not valid[id1[a]]:
            if cur is not None:
                yield done(cur)
            cur = None
            continue
        contact = kind[a] == 1
        rest = "" if contact else data["rest"][rest_of[a]]
        is_status = not contact and pp.STATUS_RE.match(rest) is not None
        ct = None if contact else pp.CURRENT_TIMER_RE.match(rest)
        has_content = cur is not None and (cur.current_timer is not None or n_contacts or cur.transfer_complete)
        if (cur is None or beacon != cur.beacon_id or (is_status and has_content)
                or (ct and cur.current_timer is not None) or cur.transfer_complete):
            if cur is not None:
                yield done(cur)
            cur = Readout(beacon, name, int(number[a]), logcache.line_time(t[a]))
            ranges, n_contacts = [], 0
        cur.last = int(number[b - 1])
        kind_ = ("contact" if contact else "status" if is_status
                 else "header" if ct or rest.startswith(("Contact Count", "Voltage"))
                 else "transfer" if rest.startswith(pp.TEXT_PREFIX["transfer_complete"]) else "other")
        if not cur.first_kind:
            cur.first_kind = kind_
        if contact:
            if cur.first_kind == "contact":     # the run's first line counts if nothing else came before it
                cur.leading += b - a - (cur.last_kind not in ("", "contact"))
            cur.last_kind = kind_
            ranges.append((a, b))
            n_contacts += b - a
            if cur.current_timer is None and cur.status_seen:
                cur.before_header.extend(number[a:b].tolist())
            continue
        cur.last_kind = kind_
        count = pp.CONTACT_COUNT_RE.match(rest)
        if count:
            cur.contact_count = int(count.group("count"))
        if is_status:
            cur.status_seen = True
        elif ct:
            cur.current_timer, cur.header_time = int(ct.group("timer")), logcache.line_time(t[a])
        elif rest.startswith(pp.TEXT_PREFIX["transfer_complete"]):
            cur.transfer_complete = True
    if cur is not None:
        yield done(cur)


def _continues(timer: int, prev: Optional[Readout]) -> bool:
    if prev is None or prev.current_timer is None or not prev.contacts:
        return False
    return prev.contacts[-1][2] - EXCESS_S <= timer <= prev.current_timer + EXCESS_S


def read_zero_times(overview_csv: Path) -> dict[tuple[str, str], list[tuple[int, datetime]]]:
    """(log file, ID) -> [(first line, timer-zero time)] from readout_overview.csv."""
    zeros: dict[tuple[str, str], list[tuple[int, datetime]]] = collections.defaultdict(list)
    if not overview_csv.exists():
        return zeros
    with overview_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["Zero time"]:
                zeros[(row["Log file"], row["ID"])].append(
                    (int(row["First line"]), datetime.strptime(row["Zero time"], pp.TIMESTAMP_FORMAT)))
    return zeros


def _zero_of(r: Readout, zeros) -> Optional[datetime]:
    """Timer-zero time of the readout (first line within one line); in the working copies, on which
    readout_overview.csv is made, IDs 0-9 with the ID bug may have become 48-57."""
    split = r.beacon_id in LOW_IDS and pp.id_bug_applies(r.start)
    for beacon in {r.beacon_id, str(int(r.beacon_id) + pp.ID_BUG_OFFSET)} if split else {r.beacon_id}:
        for first, zero in zeros.get((r.file, beacon), ()):
            if abs(first - r.first) <= 1:
                return zero
    return None


@dataclass(frozen=True, eq=False)
class Undecided:
    """Which readouts can still carry the ID bug. Uncorrected logs (corrected False): every readout
    with an ID 0-9 before the cutoff. ID-corrected logs (the pipeline's working copies): only the
    readouts the ID split left undecided, spans = (log file) -> [(first line, last line)] from
    id_split_analysis.csv."""
    corrected: bool = False
    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @classmethod
    def load(cls, id_split_csv: Optional[Path]) -> "Undecided":
        """From id_split_analysis.csv; None: the logs are not ID-corrected."""
        if id_split_csv is None:
            return cls()
        spans: dict[str, list[tuple[int, int]]] = {}
        for row in csv.DictReader(id_split_csv.open(encoding="utf-8")):
            if not row["ID1 analysis"]:
                spans.setdefault(row["Log file"], []).append((int(row["First line"]), int(row["Last line"])))
        return cls(True, spans)

    def ambiguous(self, r: Readout, *ids: str) -> bool:
        """IDs 0-9 before the ID correction cutoff can be d + 48 (BI 3.2.1): in uncorrected logs
        always, in corrected logs only where the split stayed undecided."""
        if not pp.id_bug_applies(r.start) or not any(i in LOW_IDS for i in ids):
            return False
        if self.corrected:
            return any(a <= r.last and r.first <= z for a, z in self.spans.get(r.file, []))
        return True


UNCORRECTED = Undecided()


def _t1(r: Readout, other_prev: Optional[Readout], findings: list[Finding], stats, ph: str,
        skip=None) -> None:
    """skip: per contact, left out (fake record, invalid ID2 or RSSI)."""
    c = r.contacts
    kept = np.flatnonzero(~skip) if skip is not None else np.arange(len(c))
    above = c.timer[kept] > r.current_timer + EXCESS_S
    edges = np.flatnonzero(np.diff(np.r_[False, above, False].astype(np.int8)))
    for x, y in zip(edges[0::2].tolist(), edges[1::2].tolist()):        # blocks of consecutive contacts above
        block = [c[k] for k in kept[x:y].tolist()]
        first_timer = block[0][2]
        logged = max((b[3] - r.header_time).total_seconds() for b in block)
        if any(b[2] > MAX_TIMER_S or b[2] == FAKE_TIMER for b in block):
            what, clean = ("fake record pattern (timer 524301 or above 30 days, BI 3.2.2); above Current Timer, "
                           "so the conversion drops them (time in the future)"), "no"
        elif all(b[2] - r.current_timer <= (b[3] - r.header_time).total_seconds() + EXCESS_S for b in block):
            what, clean = f"recorded during a long readout (logged {logged:.0f} s after the header)", "no"
        elif _continues(first_timer, other_prev):
            what, clean = f"continues the previous readout of beacon {other_prev.beacon_id}: Base mislabelling", "yes"
        else:
            what, clean = "not explained", "check"
        findings.append(Finding(r, "T1 timer above Current Timer", len(block), block[0][0], block[-1][0],
                                f"{min(b[2] for b in block)}-{max(b[2] for b in block)}", other_prev, what, clean))
        stats[(ph, "T1 findings")] += 1


def _t5(r: Readout, a: Readout, zeros, findings: list[Finding], stats, ph: str,
        undecided: Undecided = UNCORRECTED) -> None:
    """ID switch between two consecutive contact lines: is the part after the switch B's own or A's?"""
    part = r.contacts[:r.leading] or r.contacts[:1]
    timers = [c[2] for c in part]
    sees_a = sum(1 for c in part if str(c[1]) == a.beacon_id)
    sees_b = sum(1 for c in part if str(c[1]) == r.beacon_id)
    zero = _zero_of(r, zeros)
    uptime = (r.start - zero).total_seconds() if zero is not None else None
    continues = (a.current_timer is not None and _continues(timers[0], a)
                 and max(timers) <= a.current_timer + EXCESS_S)
    total = len(a.contacts) + len(part)
    count_fits = (a.contact_count is not None and abs(total - a.contact_count) <= max(5, 0.02 * a.contact_count)
                  and abs(total - a.contact_count) < abs(len(a.contacts) - a.contact_count))
    testable = not undecided.ambiguous(r, a.beacon_id, r.beacon_id)
    if testable and sees_a and not sees_b:
        what, clean = (f"B's own contacts: they contain {sees_a} sightings of A and none of B; "
                       "A's end and B's header lost (BI 3.2.3)"), "no"
    elif testable and sees_b and not sees_a:
        what, clean = f"A's contacts under B's ID: they contain {sees_b} sightings of B and none of A", "yes"
    elif uptime is not None and timers[0] <= uptime + EXCESS_S:
        what, clean = f"timers fit B's own uptime ({uptime:.0f} s): B's own contacts, header lost", "no"
    elif uptime is not None and continues:
        what, clean = f"timers above B's uptime ({uptime:.0f} s) and continue A: A's contacts under B's ID", "yes"
    elif uptime is not None:
        what, clean = f"timers above B's uptime ({uptime:.0f} s), not continuing A: not explained", "check"
    elif continues and count_fits:
        what, clean = ("no timer-zero time for B; timers continue A and A + B contacts match A's Contact Count: "
                       "possibly A's contacts"), "check"
    else:
        what, clean = "no timer-zero time for B; timers do not continue A: probably B's own, header lost", "no"
    if not testable:
        what += " (self-exclusion not testable: ID 0-9 before the ID correction)"
    findings.append(Finding(r, "T5 ID switch between contact lines", len(part), part[0][0], part[-1][0],
                            f"{min(timers)}-{max(timers)}", a, what, clean))
    stats[(ph, "T5 ID switches")] += 1


def _t4(r: Readout, other_prev: Readout, zeros, findings: list[Finding], stats, ph: str) -> None:
    n = 0
    for timer in r.contacts.timer.tolist():
        if not _continues(timer, other_prev):
            break
        n += 1
    zero = _zero_of(r, zeros)
    if zero is None:
        what, clean = (f"first contacts continue beacon {other_prev.beacon_id}: possible mislabelling "
                       "(no timer-zero time for this readout)"), "check"
    else:
        uptime = (r.start - zero).total_seconds()
        if r.contacts[0][2] > uptime + EXCESS_S:
            what, clean = (f"first contacts continue beacon {other_prev.beacon_id} and exceed this beacon's "
                           f"uptime ({uptime:.0f} s): Base mislabelling"), "yes"
        else:
            what, clean = (f"timers fit this beacon's own uptime ({uptime:.0f} s): similar uptimes, "
                           "not a mislabelling"), "no"
    findings.append(Finding(r, "T4 header-less after another beacon", n, r.contacts[0][0], r.contacts[n - 1][0],
                            f"{r.contacts[0][2]}-{r.contacts[n - 1][2]}", other_prev, what, clean))
    stats[(ph, "T4 findings")] += 1


def _invalid(c) -> bool:
    return (not pp.is_valid_beacon_id(str(c[1])) or not pp.is_valid_rssi(c[4]) or c[2] > MAX_TIMER_S)


def _t7(r: Readout, other_prev: Optional[Readout], findings: list[Finding], stats, ph: str) -> None:
    """Fake records (BI 3.2.2): a data message whose payload was truncated (bytes lost on the logger
    PC) was filled up by the parser with the bytes of the following messages; the CR LF check at its
    computed end passed by chance. Such records contain complete logger messages when their 5-byte
    form is put back together (runs of consecutive contact lines, logcache.fake_runs). Every record
    that touches such bytes is fake."""
    c = r.contacts
    if not c.fake.any():
        return
    zeros, ones = np.zeros(len(c), dtype=np.int8), np.ones(len(c), dtype=np.int8)
    for rows, hit, kinds in logcache.fake_runs(zeros, c.line, zeros, ones, c.id2, c.timer, c.rssi):
        records = [c[k] for k in rows[hit].tolist()]
        survive = [x for x in records if not _invalid(x)
                   and (r.current_timer is None or x[2] <= r.current_timer + EXCESS_S)]
        names = ", ".join(f"{n} x {'Status' if f == 0x08 else f'type 0x{f:02X}'}" for f, n in sorted(kinds.items()))
        what = (f"fake records: their bytes contain complete logger messages ({names}); "
                + (f"{len(survive)} pass the validity checks and get a valid-looking time: must be dropped"
                   if survive else "all are dropped by the validity checks or the conversion"))
        findings.append(Finding(r, "T7 fake records", len(records), records[0][0], records[-1][0],
                                f"{min(x[2] for x in records)}-{max(x[2] for x in records)}", other_prev, what,
                                "drop" if survive else "no", " ".join(str(x[0]) for x in survive)))
        stats[(ph, "T7 fake records")] += len(records)
        stats[(ph, "T7 fake records to drop")] += len(survive)


def check_file(path: Path, findings: list[Finding], stats: collections.Counter, zeros,
               undecided: Undecided = UNCORRECTED) -> None:
    prev: Optional[Readout] = None          # last readout with contacts
    last: Optional[Readout] = None          # readout directly before
    for r in readouts_of(logcache.read(path), path.name):
        ph = phase(r.start)
        if r.contacts or r.current_timer is not None:
            stats[(ph, "readouts")] += 1
        other_prev = prev if prev is not None and prev.beacon_id != r.beacon_id else None
        adjacent = last is not None and last.beacon_id != r.beacon_id and r.first == last.last + 1
        # fake records (T7) and records with invalid ID2 / RSSI are no contacts: left out of T1 and T2
        c = r.contacts
        skip = c.fake | ~pp.valid_beacon_ids(c.id2) | (c.rssi < pp.DEFAULT_VALID_RSSI_MIN) \
            | (c.rssi > pp.DEFAULT_VALID_RSSI_MAX)
        # T1
        if r.current_timer is not None and r.contacts:
            stats[(ph, "readouts with Current Timer and contacts")] += 1
            if other_prev is not None and other_prev.current_timer is not None \
                    and other_prev.current_timer > r.current_timer + EXCESS_S:
                stats[(ph, "T1 detectable (previous beacon has the larger uptime)")] += 1
            _t1(r, other_prev, findings, stats, ph, skip)
        # T2
        if r.contacts:
            own = int(r.beacon_id) if r.beacon_id.isdigit() else None
            if undecided.ambiguous(r, r.beacon_id):
                stats[(ph, "T2 not testable (ID1 0-9 before the ID correction, undecided)")] += 1
            else:
                selfs = c.items((c.id2 == own) & ~skip) if own is not None else []
                if selfs:
                    fake = any(c[2] > MAX_TIMER_S or c[2] == FAKE_TIMER for c in selfs)
                    findings.append(Finding(r, "T2 self-sighting", len(selfs), selfs[0][0], selfs[-1][0],
                                            f"{min(c[2] for c in selfs)}-{max(c[2] for c in selfs)}", other_prev,
                                            "fake record pattern (BI 3.2.2); see T1/T7" if fake else
                                            "contacts with ID2 = ID1 are not the beacon's own",
                                            "no" if fake else "yes"))
                    stats[(ph, "T2 findings")] += 1
        # T3
        if r.before_header and r.current_timer is not None:
            findings.append(Finding(r, "T3 contacts before the header", len(r.before_header), r.before_header[0],
                                    r.before_header[-1], "", other_prev,
                                    "contact lines between Status and Current Timer", "check"))
            stats[(ph, "T3 findings")] += 1
        # T5: ID switch between two consecutive contact lines; T4: other header-less readouts
        if r.current_timer is None and r.contacts:
            stats[(ph, "header-less readouts with contacts")] += 1
        if adjacent and last.last_kind == "contact" and r.first_kind == "contact" and r.contacts:
            _t5(r, last, zeros, findings, stats, ph, undecided)
        elif r.current_timer is None and r.contacts and other_prev is not None \
                and r.first == other_prev.last + 1 and _continues(r.contacts[0][2], other_prev):
            _t4(r, other_prev, zeros, findings, stats, ph)
        # T6
        if adjacent and r.first_kind == "transfer" and last.last_kind in ("contact", "other"):
            if r.start >= BASE_FIX:
                what, clean = ("data loss: the end of beacon " + last.beacon_id + "'s readout and all of this "
                               "readout except its Transfer complete were not logged (BI 3.2.3); no contacts "
                               "affected"), "no"
            else:
                what, clean = (f"Transfer complete of beacon {last.beacon_id} under this ID (Base mislabelling) or "
                               "this readout lost except its Transfer complete; no contacts affected"), "check"
            findings.append(Finding(r, "T6 Transfer complete under another ID", 0, r.first, r.first, "", last,
                                    what, clean))
            stats[(ph, "T6 findings")] += 1
        # T7: fake blocks
        _t7(r, other_prev, findings, stats, ph)
        if r.contacts:
            prev = r
        last = r


def write_csv(path: Path, findings: list[Finding]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Log file", "First line", "Last line", "Start time", "Phase", "ID1", "Current Timer", "Contacts",
                    "Transfer complete", "Test", "Contacts affected", "First affected line", "Last affected line",
                    "Timers affected", "Previous readout ID", "Previous Current Timer", "Previous last contact timer",
                    "Previous readout lines", "Assessment", "Cleaning needed", "Lines to drop"])
        for x in findings:
            r, p = x.readout, x.previous
            w.writerow([r.file, r.first, r.last, r.start.strftime(pp.TIMESTAMP_FORMAT), phase(r.start), r.beacon_id,
                        r.current_timer if r.current_timer is not None else "", len(r.contacts),
                        "yes" if r.transfer_complete else "no", x.test, x.count, x.first_line, x.last_line, x.timers,
                        p.beacon_id if p else "", p.current_timer if p and p.current_timer is not None else "",
                        p.contacts[-1][2] if p and p.contacts else "", f"{p.first}-{p.last}" if p else "",
                        x.assessment, x.clean, x.drop_lines])


def run(log_dir: Path, out_dir: Path, overview_csv: Path, id_split_csv: Optional[Path] = None,
        csv_name: str = CSV_NAME) -> tuple[Path, int]:
    """Check all logs of log_dir; overview_csv (readout_overview.csv) gives the timer-zero times for T4.
    id_split_csv (id_split_analysis.csv): log_dir holds ID-corrected logs; the readouts it lists as
    undecided stay ambiguous for the self-exclusion (T2, T5), all others are testable. The result is
    written to out_dir / csv_name (the pipeline: the working copies, CSV_NAME; data_quality_reports.py:
    the original logs, ORIGINAL_CSV_NAME)."""
    undecided = Undecided.load(id_split_csv)
    paths = sorted(log_dir.glob("*.log"))
    zeros = read_zero_times(overview_csv)
    findings: list[Finding] = []
    stats: collections.Counter = collections.Counter()
    for path in paths:
        check_file(path, findings, stats, zeros, undecided)
    csv_path = out_dir / csv_name
    write_csv(csv_path, findings)
    print(f"Base mislabelling check (BI 3.3.1) on {len(paths)} log file(s) in {log_dir}:")
    for ph in ("before the deployment", "deployment, before the Base fix", "after the Base fix"):
        keys = sorted(k for k in stats if k[0] == ph)
        print(f"  {ph}: " + ", ".join(f"{k[1]} {stats[k]}" for k in keys))
    kinds = collections.Counter((phase(x.readout.start), x.test,
                                 re.sub(r"\d+", "N", x.assessment.split(";")[0]))
                                for x in findings)
    for (ph, test, what), n in sorted(kinds.items()):
        print(f"    {ph} | {test} | {what}: {n}")
    need = [x for x in findings if x.clean != "no"]
    print("  cleaning needed: " + (", ".join(f"{k} {v}" for k, v in sorted(collections.Counter(
        (phase(x.readout.start), x.clean) for x in need).items())) or "none"))
    for x in need:
        print(f"    [{x.clean}] {x.readout.file}:{x.first_line}-{x.last_line} {x.readout.start:%Y-%m-%d %H:%M:%S} "
              f"ID {x.readout.beacon_id} | {x.test} | {x.count} contacts | {x.assessment}")
    print(f"  written to {csv_path} ({len(findings)} findings)")
    return csv_path, len(findings)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check the logs for contacts the Base filed under the wrong beacon.")
    study.add_argument(parser)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_ORIGINAL_DIR,
                        help=f"Logs to check. Default: the uncorrected logs in {DEFAULT_ORIGINAL_DIR}")
    parser.add_argument("--output-dir", type=Path,
                        default=pp.PIPELINE_DIR,
                        help="Output directory; the result goes into its quality/ subfolder.")
    parser.add_argument("--id-split-csv", type=Path, default=None,
                        help="id_split_analysis.csv of the ID correction: --log-dir holds ID-corrected logs "
                             "(e.g. the pipeline's working copies); only its undecided readouts stay ambiguous.")
    args = parser.parse_args(argv)
    out_dir = args.output_dir / pp.OUTPUT_QUALITY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    run(args.log_dir, out_dir, args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR / "readout_overview.csv", args.id_split_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
