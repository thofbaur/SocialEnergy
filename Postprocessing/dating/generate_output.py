#!/usr/bin/env python3
"""Generate the data used for the evaluation from the automatic uptime pipeline.

Step 9 of run_pipeline.py, which runs it; run it alone only to repeat the dating on an existing
pipeline output (e.g. --validate-lost, or --valid-date-start / --valid-date-end). The input is the output of run_pipeline.py (default Output/pipeline): the working copies of the
original logs with the decided IDs 0-9 / 48-57 and the fake records reconstructed (logs/), the uptime
model with its boots and drifts after the H4 enforcement and the readout overview (timer_zero/), the
clock stops and the records of periods with two unexplained clocks (quality/), and the undecided IDs
(id_correction/).

Before writing, the inputs are checked: the study folder (--study-dir) has its inputs, there are
no duplicate original log files (BI 6.1), and the pipeline output is up to date - its working
copies are the same files as the original logs, and it was made from the inputs there are now
(the original logs, study.toml, logger.toml, the beacon lists, the battery replacements:
<pipeline dir>/inputs.json, written by run_pipeline.py when its checks are done).

All records are written whatever their time; the observation window is applied by the
evaluation (--valid-date-start / --valid-date-end restrict the output if wanted).

Written into --data-dir (default Output/data):

- contacts_YYYYMMDD.csv           one file per day: ID1, ID2, RSSI, Contact Local Time,
                                  Time precision [s], Status
- self_reports.csv                ID, Local Time, Time precision [s], Status
- eco_sessions.csv                ID, Enter Local Time, Exit Local Time, Time precision [s], Status
- reboots.csv                     every boot: ID, Beacon type, Period, Boot Local Time, Time
                                  precision [s], Cause (first boot / battery replacement / reset;
                                  without battery_replacements.csv no replacements), own records
                                  erased by it
- battery.csv                     ID, Local Time, Time precision [s], Battery (mV), Contact Count,
                                  Status - per readout
- not_dated.csv                   records without an uptime period or outside the hard limits:
                                  no time; kind (contact / self-report / eco session), log file,
                                  line, timer and reason
- coverage.csv                    per beacon, intervals over the whole data: recorded, erased by
                                  reboot, read out not logged, dropped (storage full), both of
                                  them, not recorded
                                  (no readout possible), not seen (off or alone), silent (clock
                                  stopped), never read out, not read out, before first boot, no data;
                                  column Eco; on the pieces of a gap with own records missing
                                  although the partners recorded the beacon, its evidence (lost
                                  interval, partner records, readouts around it ...)
In <pipeline dir>/quality/ (diagnostics of the dating):
- logger_stalls.csv               times the logger stopped reading
- lost_records_validation.csv     test E of the lost-records check (--validate-lost N)

Dating with the uptime model, every record kept (model_dating.py, Postprocessing Method.md
section 21). Status: ok, fake, unreliable time (two clocks in one period), ID undecided (0-9 /
48-57; only with the ID bug, study.toml [id_bug]), invalid: ..., duplicate; the evaluation selects
the status it uses.

Reports on the quality of the data are written by data_quality_reports.py, the both-sides check of
the dating by both_sides_check.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):        # run as a file (python <folder>/<script>.py): make the packages importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import study
from dating import model_dating
from lib import logcache
from lib import postprocessing as pp
from steps import uptime_model as um

DEFAULT_PIPELINE_DIR = pp.PIPELINE_DIR
DEFAULT_DATA_DIR = pp.ROOT / pp.DEFAULT_OUTPUT_DIR_NAME / pp.OUTPUT_DATA_DIR


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write contacts, self-reports and eco sessions from the output of run_pipeline.py."
    )
    study.add_argument(parser)
    parser.add_argument("--pipeline-dir", type=Path, default=DEFAULT_PIPELINE_DIR,
                        help=f"Output of run_pipeline.py. Default: {DEFAULT_PIPELINE_DIR}")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help=f"Where the data for the evaluation is written. Default: {DEFAULT_DATA_DIR}")
    parser.add_argument("--valid-date-start", type=pp.parse_time,
                        default=None,
                        help='Optional: write only records from this time on ("YYYY-MM-DD HH:MM:SS"). Default: no '
                             "limit - the observation window is applied by the evaluation, not here.")
    parser.add_argument("--valid-date-end", type=pp.parse_time,
                        default=None, help="Optional: write only records up to this time. Default: no limit.")
    parser.add_argument("--allow-duplicate-files", action="store_true",
                        help="Write the data although the original logs contain duplicate files.")
    parser.add_argument("--validate-lost", type=int, default=0, metavar="N",
                        help="Synthetic test of the lost-records check: N complete readouts treated as not logged "
                             "(lost_records_validation.csv).")
    parser.add_argument("--allow-stale", action="store_true",
                        help="Write the data although the pipeline output was made from other inputs "
                             "(logs, study.toml, logger.toml, beacon lists, battery replacements).")
    args = parser.parse_args(argv)
    args.original_dir = pp.ORIGINAL_LOG_DIR              # the study folder's Logs/ (study.study_dir)
    args.valid_span = None
    if args.valid_date_start or args.valid_date_end:
        args.valid_span = pp.ValidTimeSpan(args.valid_date_start or datetime(2000, 1, 1),
                                           args.valid_date_end or datetime(2100, 1, 1))
    return args


def check_inputs(args: argparse.Namespace) -> list[str]:
    """Problems that stop the conversion (empty: ok)."""
    problems = [f"study folder {pp.STUDY_DIR} lacks {name}" for name in study.missing(pp.STUDY_DIR)]
    originals = sorted(args.original_dir.glob("*.log"))
    if not originals:
        return [f"no log files in {args.original_dir}"]
    duplicates = logcache.find_duplicate_log_files(originals)     # from the cache of the originals
    if duplicates and not args.allow_duplicate_files:
        for kind, files in duplicates:
            problems.append(f"duplicate log files ({kind}): {', '.join(p.name for p in files)}")
    logs = args.pipeline_dir / "logs"
    model_csv = args.pipeline_dir / pp.OUTPUT_TIMER_ZERO_DIR / um.PERIODS_CSV
    if not model_csv.exists():
        return problems + [f"{model_csv} is missing: run run_pipeline.py"]
    if not args.allow_stale:
        names = {p.name for p in originals}
        copies = {p.name for p in logs.glob("*.log")}
        if names != copies:
            problems.append(f"the working copies in {logs} are not the original logs "
                            f"({len(names - copies)} missing, {len(copies - names)} extra): run run_pipeline.py")
        changed = pp.changed_inputs(args.pipeline_dir)
        if changed:
            problems.append(f"the pipeline output was made from other inputs ({', '.join(changed)}; "
                            f"{pp.INPUTS_JSON}): run run_pipeline.py")
    return problems


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logcache.set_root(args.pipeline_dir)
    problems = check_inputs(args)
    if problems:
        print("Not converted:")
        for p in problems:
            print(f"  {p}")
        return 1
    args.data_dir.mkdir(parents=True, exist_ok=True)
    model_dating.run(args.pipeline_dir / "logs", args.pipeline_dir, args.data_dir, args.valid_span, args.validate_lost)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
