#!/usr/bin/env python3
"""Shared code for the postprocessing of the logger's *.log files.

Paths, parsing of the log lines, validity checks of single records, fake records, the possible
transfer mislabelling and the CSV readers/writers shared by the scripts. Not
run directly: the pipeline is run_pipeline.py, the data for the evaluation generate_output.py
(dated by model_dating.py), the reports data_quality_reports.py (POSTPROCESSING.md).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from lib import logger_protocol, study, study_config

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"                 # times in the outputs (CSV); the logs: LOG_TIME_FORMAT
ROOT = Path(__file__).resolve().parents[1]               # the Postprocessing folder (Output/)
# The inputs of the capture are in the study folder (lib/study.py: --study-dir of run_pipeline.py,
# passed on as DSA_STUDY_DIR); fixed at import.
STUDY_DIR = study.study_dir()
ORIGINAL_LOG_DIR = STUDY_DIR / study.LOGS_SUBDIR       # raw logs, never modified
# The logger's message and line format: <study folder>/logger.toml (lib/logger_protocol.py).
DEFAULT_OUTPUT_DIR_NAME = "Output"
# The automatic pipeline (run_pipeline.py) is the standard: its working copies of the original
# logs (with the decided IDs 0-9 / 48-57) and its output directory are the defaults of all scripts.
PIPELINE_DIR = ROOT / DEFAULT_OUTPUT_DIR_NAME / "pipeline"
DEFAULT_LOG_DIR = PIPELINE_DIR / "logs"
# Subfolders of the output directory
OUTPUT_DATA_DIR = "data"                  # generate_output.py: data for the evaluation
OUTPUT_QUALITY_DIR = "quality"            # checks of the pipeline, data_quality_reports.py
OUTPUT_ID_CORRECTION_DIR = "id_correction"  # automatic ID split of IDs 0-9 / 48-57 (id_split_analysis.py)
OUTPUT_TIMER_ZERO_DIR = "timer_zero"      # timer-zero analysis, contact sync, uptime model
DEFAULT_CONTACTS_CSV = "contacts.csv"     # base name: contacts_YYYYMMDD.csv (contacts_csv_path_for_day)
DEFAULT_SELF_REPORTS_CSV = "self_reports.csv"
DEFAULT_ECO_SESSIONS_CSV = "eco_sessions.csv"
DEFAULT_SANITY_FINDINGS_CSV = "sanity_findings.csv"
DEFAULT_TRANSFER_MISLABEL_MD = "transfer_mislabel.md"
DEFAULT_FAKE_RECORDS_CSV = "fake_records.csv"
DEFAULT_UNRELIABLE_TIME_CSV = "unreliable_time_records.csv"

# Names of the checks of single records that need no dating, as columns of sanity_findings.csv
# (data_quality_reports.py); dated records get a Status in the output of generate_output.py.
CHECK_BEACON_ID_RANGE = "Beacon ID out of range"
# A contact record built by the logger from the bytes of other messages (Background
# Information 3.2.2): see data_quality_reports.find_fake_contacts.
CHECK_CONTACT_FAKE = "Contact fake record (bytes of logger messages)"
CHECK_ID2_RANGE = "Contact ID2 out of range"
CHECK_RSSI_RANGE = "Contact RSSI out of range"
# Flags back-to-back contact lines for manual review (see data_quality_reports.find_adjacent_id_mismatches /
# transfer_mislabel.md) while leaving both entries in the output.
CHECK_TRANSFER_MISLABEL = "Potential contact transfer mislabeling"

# Sanity bounds for this deployment. A beacon occasionally dumps a handful of
# corrupted trailing records (stale/uninitialized flash bytes read past its
# real stored contact count) - these show up as contacts with a wildly
# out-of-range timer (resolving to a timestamp months away) and/or an ID
# that isn't part of the fielded roster (e.g. a 44<->71 record with Timer: 4801652, which the
# former conversion dated to 2026-10-08; corrupt timers are now caught by H1 and the hard limits
# of the data, Postprocessing Method.md 21). The values of the study: study.toml (lib/study_config.py).
STUDY = study_config.config()
DEFAULT_VALID_ID_MIN = STUDY.id_min
DEFAULT_VALID_ID_MAX = STUDY.id_max
DEFAULT_VALID_EXTRA_IDS = STUDY.id_extra
DEFAULT_VALID_RSSI_MIN = STUDY.rssi_valid_min
DEFAULT_VALID_RSSI_MAX = STUDY.rssi_valid_max

# Ceiling on the Timer increase between two back-to-back contact lines
# reporting different beacon IDs for data_quality_reports.find_adjacent_id_mismatches
# to treat it as a potential mislabeling: the Timer must not decrease, and must
# not increase by this much or more. See there for the full rationale.
DEFAULT_MAX_TIMER_INCREASE = 60

# The log lines, all from logger.toml (lib/logger_protocol.py): a line is
# <line time><LOG_SEPARATOR><ID field><LOG_SEPARATOR><text>; the patterns of the texts the scripts
# read have one named group per field (logger_protocol.NEEDED).
LOG = logger_protocol.protocol()
LOG_TIME_FORMAT = LOG.time_format
LOG_TIME_LENGTH = LOG.time_length
LOG_SEPARATOR = LOG.separator
LOG_ID_PREFIX = LOG.id_prefix
ID_FIELD_RE = LOG.id_field_regex()
CURRENT_TIMER_RE = LOG.regex("current_timer")      # timer
VOLTAGE_RE = LOG.regex("voltage")                  # voltage
CONTACT_COUNT_RE = LOG.regex("contact_count")      # count
STATUS_RE = LOG.regex("status")                    # status
CONTACT_RE = LOG.regex("contact")                  # id2, timer, rssi
SELF_REPORT_RE = LOG.regex("self_report")          # timer
ECO_SESSION_RE = LOG.regex("eco_session")          # enter, leave
# The start of a line text: for the readers that only tell the kind of a line.
TEXT_PREFIX = {key: LOG.text_prefix(key) for key in logger_protocol.NEEDED}


@dataclass(frozen=True)
class LogLine:
    timestamp: datetime
    beacon_id: str
    rest: str
    source: Path
    line_number: int


@dataclass(frozen=True)
class ValidTimeSpan:
    start: datetime
    end: datetime


# The observation window of the evaluation (Postprocessing Method.md 7): a scoping decision, not a
# sign of corruption. The scripts use it to label the phase of findings (before / in / after the
# deployment); generate_output.py writes all records whatever their time (the evaluation applies
# the window). The start is the deployment deadline: earlier logs (setup night, handout) are still
# read for the timer-zero analysis, the contact sync and the uptime model.
DEFAULT_VALID_SPAN = ValidTimeSpan(STUDY.observation_start, STUDY.observation_end)
# Hard limits of the data, independent of the observation window: the beacons were flashed with the
# deployed firmware from FLASH_START on, so no boot and no record can be earlier; no record can be
# later than LATEST_RECORD.
FLASH_START = STUDY.flash_start
LATEST_RECORD = STUDY.latest_record
# Readouts logged from DELAY_FREE_FROM on have no logging delay (Postprocessing Method.md 13).
DELAY_FREE_FROM = STUDY.delay_free_from
# The logger's ID bug (BI 3.2.1; study.toml [id_bug]): ID1 48-57 written as 0-9 in the lines logged
# before ID_BUG_UNTIL; None if the study's logger did not have it (the pipeline then splits nothing).
ID_BUG_LOW = frozenset(range(10))           # logged IDs that can be wrong ...
ID_BUG_OFFSET = 48                          # ... the real ID then the logged one + this
ID_BUG_UNTIL = STUDY.id_bug.until if STUDY.id_bug.enabled else None
ID_BUG_WITNESSES_UNTIL = STUDY.id_bug.witnesses_until

# Constants of the method (not of the study), one place each.
DAY_S = 86400                               # seconds per day (drifts in s/day, uptimes in days)
MAX_PLAUSIBLE_TIMER_S = 30 * DAY_S          # contact timers above this come from fake records (BI 3.2.2)
# Contacts with a timer above their readout's Current Timer were recorded after the Current Timer
# message (during the transfer) or are fake records; how far above still counts depends on the question:
READOUT_TIMER_EXCESS_S = 3600               # still a contact of the readout: its real time from the readout
                                            # (id_split_analysis.py; contact_sync_analysis.py, Method 14.1)
MAX_DELAY_S = 8 * 3600                      # longest logging delay assumed (reboot candidates, uptime_model.py; Method 19)
DELAY_BOUND_EXCESS_S = MAX_DELAY_S          # still used for the delay lower bound (timer_zero_analysis.py,
                                            # uptime_model.py; Method 13.1): larger excesses are fake records


def id_bug_applies(t) -> bool:
    """Whether a line logged at t (datetime or timestamp) can carry the ID bug."""
    if ID_BUG_UNTIL is None:
        return False
    return t < (ID_BUG_UNTIL if isinstance(t, datetime) else ID_BUG_UNTIL.timestamp())


def id_sort_key(beacon_id: str):
    try:
        return (0, int(beacon_id))
    except ValueError:
        return (1, beacon_id)


@lru_cache(maxsize=1 << 16)
def parse_timestamp(text: str) -> datetime:
    """datetime of a log line time (LOG_TIME_FORMAT; ValueError if it is none). Cached: the
    logger writes about a thousand lines per second, and strptime is most of the parsing time."""
    return datetime.strptime(text.strip(), LOG_TIME_FORMAT)


def parse_line(raw_line: str, source: Path, line_number: int) -> Optional[LogLine]:
    parts = raw_line.rstrip("\r\n").split(LOG_SEPARATOR, 2)
    if len(parts) < 3:
        return None

    timestamp_str, id_field, rest = parts
    try:
        timestamp = parse_timestamp(timestamp_str)
    except ValueError:
        return None

    id_match = ID_FIELD_RE.match(id_field)
    if not id_match:
        return None

    return LogLine(timestamp, id_match.group(1), rest.strip(), source, line_number)


def format_log_line(line: LogLine) -> str:
    """Reconstruct a LogLine's original raw log-file text (sans newline)."""
    return LOG.log_line(line.timestamp, line.beacon_id, line.rest)


def count_mismatches_by_source(mismatches: list[tuple[LogLine, LogLine]]) -> dict[Path, int]:
    """Tally data_quality_reports.find_adjacent_id_mismatches occurrences per source log file.

    An occurrence counts once against each distinct file its two lines come
    from (almost always the same file, since the pair is adjacent in the
    merged, chronologically-sorted line stream).
    """
    counts: dict[Path, int] = {}
    for previous, current in mismatches:
        counts[previous.source] = counts.get(previous.source, 0) + 1
        if current.source != previous.source:
            counts[current.source] = counts.get(current.source, 0) + 1
    return counts


def write_transfer_mislabel_md(
    path: Path,
    mismatches: list[tuple[LogLine, LogLine]],
    max_timer_increase: int = DEFAULT_MAX_TIMER_INCREASE,
) -> None:
    """Write a Markdown report listing every adjacent-contact-line ID mismatch.

    One section per occurrence, showing the two offending lines verbatim so
    they can be cross-checked against the raw log files.
    """
    with path.open("w", encoding="utf-8") as md_file:
        md_file.write("# Potential Contact Mislabeling\n\n")
        md_file.write(
            "Every occurrence where two back-to-back contact log lines "
            "(`ID2: ..., Timer: ..., RSSI: ...`, no line of any kind between "
            "them) report a different `ID:` than the line immediately before "
            "them, with the Timer not decreasing and increasing by less than "
            f"{max_timer_increase}. Within one connection dump all contact "
            "lines share the same `ID:` and have Timer values close "
            "together, so a change here is a potential mislabeling.\n\n"
        )
        md_file.write(f"Found {len(mismatches)} occurrence(s).\n\n")
        for index, (previous, current) in enumerate(mismatches, start=1):
            md_file.write(f"## {index}. ID {previous.beacon_id} -> ID {current.beacon_id}\n\n")
            md_file.write(f"{previous.source.name}:{previous.line_number}\n")
            md_file.write("```\n")
            md_file.write(f"{format_log_line(previous)}\n")
            md_file.write("```\n\n")
            md_file.write(f"{current.source.name}:{current.line_number}\n")
            md_file.write("```\n")
            md_file.write(f"{format_log_line(current)}\n")
            md_file.write("```\n\n")


def contact_records_bytes(id2s, timers, rssis) -> bytes:
    """The bytes each contact line of a run was decoded from, joined (vectorised: the fake record
    checks build this stream for every contact line of the logs; layout from logger.toml)."""
    return logger_protocol.protocol().contact_bytes(id2s, timers, rssis)


def embedded_messages(stream: bytes):
    """Yield (start, end) of every complete logger message inside a byte stream (logger.toml)."""
    return logger_protocol.protocol().message_spans(stream)


FAKE_REMOVED_TEXT = "# fake record removed (BI 3.2.2)"     # marker line: not in the log line format


# A run of contact lines at the end of a readout whose timers drop by more than this below the
# readout's own contacts and continue within SWITCH_CONTINUITY_S into the next ID's contacts belongs
# to the next beacon (id_split_analysis.find_switch_mislabels)
SWITCH_MIN_DROP_S = 60
SWITCH_CONTINUITY_S = 60


def read_unreliable_time_csv(path: Path) -> set[tuple[str, int]]:
    """(log file name, line) of the records listed by timer_step_check.py as dated on a clock that
    stepped without reboot; empty if the list does not exist."""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as csv_file:
        return {(row["Log file"], int(row["Line"])) for row in csv.DictReader(csv_file)}


@lru_cache(maxsize=4096)       # called for every line and contact; few distinct IDs
def is_valid_beacon_id(
    beacon_id: str,
    id_min: int = DEFAULT_VALID_ID_MIN,
    id_max: int = DEFAULT_VALID_ID_MAX,
    extra_ids: frozenset[int] = DEFAULT_VALID_EXTRA_IDS,
) -> bool:
    """Whether beacon_id is part of this deployment's fielded device roster."""
    try:
        id_int = int(beacon_id)
    except ValueError:
        return False
    return id_min <= id_int <= id_max or id_int in extra_ids


def valid_beacon_ids(ids):
    """is_valid_beacon_id for a numpy array of numeric IDs."""
    import numpy as np
    return ((ids >= DEFAULT_VALID_ID_MIN) & (ids <= DEFAULT_VALID_ID_MAX)) | np.isin(ids, sorted(DEFAULT_VALID_EXTRA_IDS))


def is_valid_rssi(
    rssi: int,
    rssi_min: int = DEFAULT_VALID_RSSI_MIN,
    rssi_max: int = DEFAULT_VALID_RSSI_MAX,
) -> bool:
    """Whether rssi (dBm, negative) falls within the radio's plausible range."""
    return rssi_min <= rssi <= rssi_max


# Recording thresholds by the type of the beacon seen (BI 2.1; study.toml [rssi]): location tags (DSL)
# are recorded down to the first, all other beacons (DSA) down to the second.
RSSI_THRESHOLD_LOCATION_TAG = STUDY.rssi_threshold_location_tag
RSSI_THRESHOLD_BEACON = STUDY.rssi_threshold_beacon
LIST_ROOMS_CSV = STUDY_DIR / study.LIST_ROOMS        # location tags (rooms)
LIST_PERSONS_CSV = STUDY_DIR / study.LIST_PERSONS    # beacons worn by persons
BATTERY_REPLACEMENTS_CSV = STUDY_DIR / study.BATTERY_REPLACEMENTS   # documented battery replacements


@lru_cache(maxsize=None)
def location_tags(path: Path = LIST_ROOMS_CSV) -> frozenset:
    """IDs (int) of the location tags (list_rooms.csv; read once per file, location_tags.cache_clear()
    to read it again)."""
    ids = set()
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
        delimiter = ";" if ";" in text.splitlines()[0] else ","
        for row in csv.DictReader(text.splitlines(), delimiter=delimiter):
            if row.get("ID", "").strip().isdigit():
                ids.add(int(row["ID"].strip()))
    return frozenset(ids)


def symmetric_threshold(id1, id2):
    """RSSI from which a sighting is recorded in both directions: the stricter of the two thresholds
    (BI 2.1). A person-worn beacon records a location tag down to -100 dBm, the tag records it only
    down to -80 dBm: weaker records of such a pair have no counterpart on the other side. Works on
    ints and on numpy arrays."""
    import numpy as np
    tags = np.array(sorted(location_tags()), dtype=np.int64)
    thr1 = np.where(np.isin(id1, tags), RSSI_THRESHOLD_LOCATION_TAG, RSSI_THRESHOLD_BEACON)
    thr2 = np.where(np.isin(id2, tags), RSSI_THRESHOLD_LOCATION_TAG, RSSI_THRESHOLD_BEACON)
    return np.maximum(thr1, thr2)


def contacts_csv_path_for_day(base_path: Path, day: date) -> Path:
    """Derive the per-day contacts CSV path from the base --contacts-csv path.

    e.g. base "contacts.csv" + 2026-08-14 -> "contacts_20260814.csv" in the
    same directory.
    """
    return base_path.with_name(f"{base_path.stem}_{day.strftime('%Y%m%d')}{base_path.suffix}")


def parse_time(text: str) -> datetime:
    """A time given on the command line: the full YYYY-MM-DD HH:MM:SS (TIMESTAMP_FORMAT)."""
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected "YYYY-MM-DD HH:MM:SS", got "{text}"') from None


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by the scripts of the pipeline and the reports."""
    study.add_argument(parser)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=f"Directory containing *.log files. Default: {DEFAULT_LOG_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PIPELINE_DIR,
        help=(
            "Output directory; each script writes into its own subfolder of it "
            f"({OUTPUT_DATA_DIR}, {OUTPUT_QUALITY_DIR}, {OUTPUT_ID_CORRECTION_DIR}, "
            f"{OUTPUT_TIMER_ZERO_DIR}), created if missing. "
            f"Default: {PIPELINE_DIR}."
        ),
    )
    parser.add_argument(
        "--valid-date-start",
        type=parse_time,
        default=DEFAULT_VALID_SPAN.start,
        help=(
            "Start of the observation window (\"YYYY-MM-DD HH:MM:SS\"); labels the phase "
            "of findings (before / in / after the deployment). "
            f"Default: {DEFAULT_VALID_SPAN.start.isoformat(sep=' ')}"
        ),
    )
    parser.add_argument(
        "--valid-date-end",
        type=parse_time,
        default=DEFAULT_VALID_SPAN.end,
        help=(
            "End of the observation window (\"YYYY-MM-DD HH:MM:SS\"). "
            f"Default: {DEFAULT_VALID_SPAN.end.isoformat(sep=' ')}"
        ),
    )


INPUTS_JSON = "inputs.json"     # in the pipeline folder: the inputs the pipeline output was made from


def input_fingerprints() -> dict[str, str]:
    """What the pipeline output depends on besides the code: the two configs as read (not their
    comments), the beacon lists, the battery replacements (optional) and the original logs (name,
    size, modification time). Written by run_pipeline.py when its checks are done; the dating refuses
    a pipeline output made from other inputs (generate_output.py)."""
    def file(path: Path) -> str:
        return _file_digest(path) if path.exists() else "missing"
    logs = hashlib.sha256(repr([(p.name, p.stat().st_size, p.stat().st_mtime_ns)
                                for p in sorted(ORIGINAL_LOG_DIR.glob("*.log"))]).encode("utf-8")).hexdigest()
    return {"study.toml": hashlib.sha256(repr(STUDY).encode("utf-8")).hexdigest(), "logger.toml": LOG.digest,
            "list_persons.csv": file(LIST_PERSONS_CSV), "list_rooms.csv": file(LIST_ROOMS_CSV),
            "battery_replacements.csv": file(BATTERY_REPLACEMENTS_CSV), "logs": logs}


def changed_inputs(pipeline_dir: Path) -> list[str]:
    """The inputs that differ from those the pipeline output in pipeline_dir was made from (all, if
    it records none)."""
    import json
    path = pipeline_dir / INPUTS_JSON
    now = input_fingerprints()
    if not path.exists():
        return sorted(now)
    made = json.loads(path.read_text(encoding="utf-8"))
    return sorted(k for k in now if made.get(k) != now[k])


def write_input_fingerprints(pipeline_dir: Path) -> None:
    import json
    (pipeline_dir / INPUTS_JSON).write_text(json.dumps(input_fingerprints(), indent=1), encoding="utf-8")


def _file_digest(path: Path, limit: Optional[int] = None, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of the file, or of its first `limit` bytes."""
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while remaining is None or remaining > 0:
            chunk = handle.read(chunk_size if remaining is None else min(chunk_size, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def find_duplicate_log_files(log_paths: list[Path], head_bytes: int = 1 << 16,
                             digest=_file_digest) -> list[tuple[str, list[Path]]]:
    """Find log files whose content is contained in another log file.

    Returns a list of (kind, files):
    - ("identical", [a, b, ...]): files with byte-identical content;
    - ("copy of the beginning", [shorter, longer]): the shorter file is
      byte-identical to the beginning of the longer one (e.g. a copy taken
      while the logger was still writing).

    Every line of such files would be processed more than once. digest(path, limit): SHA-256 of
    the file or of its first limit bytes (logcache.find_duplicate_log_files: from the cache).
    """
    sizes = {path: path.stat().st_size for path in log_paths}
    heads = {path: digest(path, head_bytes) for path in log_paths}

    findings: list[tuple[str, list[Path]]] = []
    by_content: dict[str, list[Path]] = {}
    for path in log_paths:
        by_content.setdefault(digest(path), []).append(path)
    unique = []
    for files in by_content.values():
        if len(files) > 1:
            findings.append(("identical", sorted(files)))
        unique.append(files[0])

    # A shorter file can only be the beginning of a longer one if their first
    # head_bytes agree, so only those pairs are read in full.
    by_head: dict[str, list[Path]] = {}
    for path in unique:
        if sizes[path] >= head_bytes:
            by_head.setdefault(heads[path], []).append(path)
    for files in by_head.values():
        files.sort(key=lambda path: sizes[path])
        for index, shorter in enumerate(files):
            shorter_digest = digest(shorter)
            for longer in files[index + 1:]:
                if sizes[longer] > sizes[shorter] and digest(longer, sizes[shorter]) == shorter_digest:
                    findings.append(("copy of the beginning", [shorter, longer]))
    return findings


if __name__ == "__main__":
    print(
        "postprocessing.py is the shared library. Run (POSTPROCESSING.md):\n"
        "  python run_pipeline.py [--reports]   # original logs -> Output/data (+ Output/pipeline)\n"
        "  (the slides: ../Presentation/make_presentation.py)",
        file=sys.stderr,
    )
    raise SystemExit(2)
