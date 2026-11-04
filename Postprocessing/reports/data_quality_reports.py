#!/usr/bin/env python3
"""Reports on the quality of the logs (run_pipeline.py --reports).

Reads the pipeline's working copies of the logs (default Output/pipeline/logs) and writes reports,
not the data for the evaluation (generate_output.py) and not the pipeline's own results: the
timer-zero analysis, the uptime model and the Base mislabelling check on the working copies are
written by run_pipeline.py and left untouched here.

First check: no log file may be contained in another one (identical files, or a copy of the
beginning of a file). Such files would be counted twice; the script stops unless
--allow-duplicate-files is given.

Written to the quality/ subfolder of --output-dir (default Output/pipeline):

- sanity_findings.csv              per log file: lines flagged by the checks that need no dating
                                   (beacon ID / ID2 / RSSI out of range, fake records, possible
                                   transfer mislabelling); dated records get a Status in
                                   generate_output.py instead
- transfer_mislabel.md             possible contacts filed under the wrong beacon
- fake_records.csv                 contact lines the logger built from the bytes of other messages
                                   (BI 3.2.2) that are still in the working copies
- base_mislabel_check_original.csv Base mislabelling check on the uncorrected logs (Original/)
                                   (base_mislabel_check.py; on the working copies:
                                   base_mislabel_check.csv, by run_pipeline.py)

Battery and Contact Count per readout: Output/data/battery.csv (generate_output.py), at the readout's real time.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import base_mislabel_check as bmc
from lib import logcache
from lib import postprocessing as pp
from steps import timer_zero_analysis as tza

SANITY_CHECKS = (pp.CHECK_BEACON_ID_RANGE, pp.CHECK_ID2_RANGE, pp.CHECK_RSSI_RANGE, pp.CHECK_CONTACT_FAKE,
                 pp.CHECK_TRANSFER_MISLABEL)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write data-quality reports for dsa_logger.py *.log files."
    )
    pp.add_common_arguments(parser)
    parser.add_argument(
        "--original-dir",
        type=Path,
        default=bmc.DEFAULT_ORIGINAL_DIR,
        help=(
            "Uncorrected logs for the Base mislabelling check (base_mislabel_check.py). "
            f"Default: {bmc.DEFAULT_ORIGINAL_DIR}"
        ),
    )
    parser.add_argument(
        "--allow-duplicate-files",
        action="store_true",
        help="Report duplicate log files but continue instead of stopping.",
    )
    return parser.parse_args(argv)


def check_duplicate_files(log_dir: Path, allow: bool) -> bool:
    """First check: report log files contained in other log files.

    Returns False if processing must stop.
    """
    log_paths = sorted(log_dir.glob("*.log"))
    print(f"Checking {len(log_paths)} log file(s) in {log_dir} for duplicates ...", flush=True)
    findings = logcache.find_duplicate_log_files(log_paths)
    if not findings:
        print("No duplicate log files.", flush=True)
        return True
    print(f"{len(findings)} duplicate log file finding(s):", file=sys.stderr)
    for kind, files in findings:
        print(f"  {kind}: " + ", ".join(path.name for path in files), file=sys.stderr)
    if allow:
        print("Continuing (--allow-duplicate-files): their lines are counted more than once.", file=sys.stderr)
        return True
    print("Stopped. Remove the duplicates or run with --allow-duplicate-files.", file=sys.stderr)
    return False


def log_line(lines: logcache.Lines, x: int) -> pp.LogLine:
    return pp.LogLine(logcache.line_time(lines.t[x]), lines.ids[lines.id1[x]], lines.text_after_id(x),
                      lines.paths[lines.file[x]], int(lines.line[x]))


def find_fake_contacts(lines: logcache.Lines) -> dict[int, str]:
    """Contact lines the logger built from the bytes of other messages (Background Information 3.2.2).

    When bytes of a data message are lost, the logger fills it up with the bytes of the following
    messages (mostly the Base's 7-byte Status messages) and cuts them into 5-byte contact records;
    its CR LF end check passes by chance. Put back into their 5-byte form, consecutive contact
    lines of one readout (neighbours in time order, same file, consecutive line numbers, same ID1)
    then contain complete logger messages (logcache.fake_runs). Every line that touches such bytes
    is fake, including the mixed line where the real data ends. By chance a complete message
    practically never forms in real data: it needs "ID", a valid message type and CR LF at exactly
    the right distance. Returns line index -> description of the embedded messages of its run."""
    order = lines.merged_index()
    fakes: dict[int, str] = {}
    for rows, hit, types in logcache.fake_runs(*(getattr(lines, k)[order] for k in
                                                 ("file", "line", "id1", "kind", "id2", "timer", "rssi"))):
        text = ", ".join(f"{n} x " + ("Status" if t == 0x08 else f"type 0x{t:02X}") for t, n in sorted(types.items()))
        for x in order[rows[hit]].tolist():
            fakes[x] = f"run of {len(rows)} contact lines with {text}"
    return fakes


def write_fake_records_csv(path: Path, lines: logcache.Lines, fakes: dict[int, str]) -> None:
    """One row per fake contact line (find_fake_contacts), in log order."""
    rank = np.empty(len(lines.t), dtype=np.int64)
    rank[lines.merged_index()] = np.arange(len(lines.t))
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Log file", "Line", "Line time", "ID1", "ID2", "Timer", "RSSI",
                         "Passes ID2 and RSSI checks", "Embedded logger messages"])
        for x in sorted(fakes, key=lambda x: rank[x]):
            id2, timer, rssi = int(lines.id2[x]), int(lines.timer[x]), int(lines.rssi[x])
            valid = pp.is_valid_beacon_id(str(id2)) and pp.is_valid_rssi(rssi)
            writer.writerow([lines.paths[lines.file[x]].name, int(lines.line[x]),
                             logcache.line_time(lines.t[x]).strftime(pp.TIMESTAMP_FORMAT), lines.ids[lines.id1[x]],
                             id2, timer, rssi, "yes" if valid else "no", fakes[x]])


def find_adjacent_id_mismatches(lines: logcache.Lines, max_timer_increase: int = pp.DEFAULT_MAX_TIMER_INCREASE
                                ) -> list[tuple[pp.LogLine, pp.LogLine]]:
    """Pairs of back-to-back contact lines (time order) with different beacon IDs.

    A beacon dumps its whole stored contact list in one connection burst, so consecutive contact
    lines (ID2/Timer/RSSI) normally share the same "ID:" - the reporting beacon. Two contact lines
    reporting different IDs with no line of any kind between them (not even a "Transfer complete",
    "Status", "Current Timer", "Contact Count", or "Voltage" line - those mark a legitimate handover
    to the next connection) is a potential mislabeling: a contact attributed to the wrong beacon,
    e.g. by a connection race in dsa_logger.py. A genuine handover's Timer resets to the next
    beacon's own clock, unrelated to the previous one's, so it's unlikely to land in the narrow
    "held steady or crept up a little" band a real within-dump step would; the Timer must not
    decrease, and must not increase by max_timer_increase or more, to count as a potential
    mislabeling. Nothing is dropped; the pairs are flagged for manual review."""
    order = lines.merged_index()
    kind, id1, timer = lines.kind[order], lines.id1[order], lines.timer[order].astype(np.int64)
    delta = timer[1:] - timer[:-1]
    hit = np.flatnonzero((kind[1:] == 1) & (kind[:-1] == 1) & (id1[1:] != id1[:-1])
                         & (delta >= 0) & (delta < max_timer_increase))
    return [(log_line(lines, int(order[x])), log_line(lines, int(order[x + 1]))) for x in hit.tolist()]


def sanity_counts(lines: logcache.Lines, fakes, mismatches) -> dict[Path, dict[str, int]]:
    """Per log file: lines flagged by the checks that need no dating (SANITY_CHECKS)."""
    out: dict[Path, dict[str, int]] = {}

    def note(source: Path, check: str, n: int = 1) -> None:
        by_check = out.setdefault(source, {})
        by_check[check] = by_check.get(check, 0) + n

    valid = np.array([pp.is_valid_beacon_id(b) for b in lines.ids], dtype=bool)[lines.id1]
    fake = np.zeros(len(lines.t), dtype=bool)
    fake[list(fakes)] = True
    contact = valid & (lines.kind == 1)
    id2_bad = contact & ~fake & ~pp.valid_beacon_ids(lines.id2)
    rssi_bad = contact & ~fake & ~id2_bad & ((lines.rssi < pp.DEFAULT_VALID_RSSI_MIN) | (lines.rssi > pp.DEFAULT_VALID_RSSI_MAX))
    for check, mask in ((pp.CHECK_BEACON_ID_RANGE, ~valid), (pp.CHECK_CONTACT_FAKE, contact & fake),
                        (pp.CHECK_ID2_RANGE, id2_bad), (pp.CHECK_RSSI_RANGE, rssi_bad)):
        for f, n in enumerate(np.bincount(lines.file[mask], minlength=len(lines.paths)).tolist()):
            if n:
                note(lines.paths[f], check, n)
    for source, count in pp.count_mismatches_by_source(mismatches).items():
        note(source, pp.CHECK_TRANSFER_MISLABEL, count)
    return out


def write_sanity_csv(path: Path, log_paths: list[Path], by_source: dict[Path, dict[str, int]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Log file", *SANITY_CHECKS, "Total"])
        for log_path in log_paths:
            by_check = by_source.get(log_path, {})
            values = [by_check.get(name, 0) for name in SANITY_CHECKS]
            w.writerow([log_path.name, *values, sum(values)])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    out = args.output_dir / pp.OUTPUT_QUALITY_DIR
    tz_out = args.output_dir / pp.OUTPUT_TIMER_ZERO_DIR
    out.mkdir(parents=True, exist_ok=True)

    if not check_duplicate_files(args.log_dir, args.allow_duplicate_files):
        return 1

    lines = logcache.load(args.log_dir)
    log_paths = lines.paths

    # fake records left in the working copies (the pipeline reconstructed the known ones, BI 3.2.2)
    fake_contacts = find_fake_contacts(lines)
    fake_csv = out / pp.DEFAULT_FAKE_RECORDS_CSV
    write_fake_records_csv(fake_csv, lines, fake_contacts)

    # Base mislabelling check on the uncorrected logs; the timer-zero times of the pipeline's
    # readout overview serve the header-less readouts (T4)
    mislabel_check_csv, mislabel_findings = bmc.run(args.original_dir, out, tz_out / tza.OVERVIEW_CSV,
                                                    csv_name=bmc.ORIGINAL_CSV_NAME)

    mismatches = find_adjacent_id_mismatches(lines)
    sanity_csv = out / pp.DEFAULT_SANITY_FINDINGS_CSV
    write_sanity_csv(sanity_csv, log_paths, sanity_counts(lines, fake_contacts, mismatches))
    mislabel_md = out / pp.DEFAULT_TRANSFER_MISLABEL_MD
    pp.write_transfer_mislabel_md(mislabel_md, mismatches)

    print(f"Processed {len(log_paths)} log file(s), {len(lines.t)} parsed lines.")
    print(f"Wrote sanity findings for {len(log_paths)} log file(s) to {sanity_csv}")
    print(f"Wrote {len(mismatches)} potential mislabeling occurrence(s) to {mislabel_md}")
    print(f"Wrote {mislabel_findings} Base mislabelling check finding(s) to {mislabel_check_csv}")
    print(f"Wrote {len(fake_contacts)} fake contact record(s) left in the working copies to {fake_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
