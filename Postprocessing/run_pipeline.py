#!/usr/bin/env python3
"""From the original logs to the data for the evaluation, in the order the checks depend on each
other (Postprocessing Method.md section 20). The one script to run after a capture.

The scripts of steps 2-8 are in steps/, step 9 in dating/, step 10 in reports/, the shared code
in lib/ (POSTPROCESSING.md, Layout); each runs here as a module (python -m steps.<name>).

Input: the study folder (--study-dir, default ../../Studies/DSA2026 relative to this folder;
lib/study.py): the original logs in Logs/, list_persons.csv, list_rooms.csv,
battery_replacements.csv, logger.toml (the logger's format) and study.toml (the values of the
study: dates, IDs, RSSI, beacons, the logger's ID bug - steps 3 and 6 split nothing if it is
switched off there). The steps get it through the environment (DSA_STUDY_DIR), the cache root
(pipeline/cache) as DSA_CACHE_DIR.

1. The cache of the original logs (lib/logcache.py, Output/pipeline/cache): the only
   step that reads the logs; everything after reads the cache. Duplicate log files (stop) and logger
   clock: line times must not go back within a log file or from one file to the next. The hard
   rules H1 and H4 rely on it (BI 3.2.5).
2. Fake records (BI 3.2.2) on the original logs; left out of everything after. Contacts of the next
   beacon logged under the previous one's ID at a change of beacon without a header
   (id_split_analysis.find_switch_mislabels; id_correction/switch_relabels.csv).
3. First split of ID1 0-9 into 0-9 and 48-57 (id_split_analysis.py), without the fake records.
4. Working copies of the logs with the decided IDs, the switch relabels and the fake records reconstructed (the messages
   hidden in them as normal log lines; Output/pipeline/logs; unchanged files are hard links to the
   originals), made from the cache of the originals, with their cache; a copy is written only
   when its content changed, a copy without an original is deleted.
5. Uptimes on the working copies: timer_zero_analysis.py, contact_sync_analysis.py (the start of
   the uptime model), battery_replacement_check.py (its windows, H7), uptime_model.py (which runs the fake
   record check again while reading), contact_sync_analysis.py --model-periods (drift per beacon on
   the model's periods) and uptime_model.py again with it.
6. Second split: undecided readouts get the label whose boot in the uptime model they fit, or the
   only label under which they break no hard rule of the model (Resolver.model_rules); first
   decisions that fit only the other label are reported. If labels changed or the contact sync
   of round 1 found reboot hints (rule a'', reboot_hints.csv): steps 4 and 5 again.
   Then, on the final uptime model and while steps 7 and 8 run: clock_stop_check.py (beacon
   silent, uptime standing still: shifts for the dating), h4_enforce.py (H4 as a hard rule: the
   smallest change of boots and drifts that puts all readouts in log order), then in parallel
   timer_step_check.py (two clocks in one period not explained by the stops) and
   battery_replacement_check.py --model uptime (the battery replacements and the periods never
   read out on the final boots, for the dating) followed by hard_rule_check.py --model uptime;
   nothing in steps 5 to 8 reads their outputs. When they are done, the inputs of the run are
   recorded (pipeline/inputs.json, postprocessing.input_fingerprints): the dating refuses a
   pipeline output made from other inputs.
7. Result of the split against the original logs (id_correction/id_split_analysis.csv).
   Steps 2 to 4 are id_split_analysis.first_split and write_working_logs; id_split_analysis.py run
   alone does them with the same code.
8. Base mislabelling check (base_mislabel_check.py, BI 3.3.1, 3.3.2, 6.4 items 12-13) on the working
   copies: contacts filed under the wrong beacon. A check only, nothing is changed; findings that
   need cleaning are reported (quality/base_mislabel_check.csv).
9. Dating of all records with the final uptime model (generate_output.py, model_dating.py) into
   --output-dir/data: contacts per day, self-reports, eco sessions, reboots, battery, coverage,
   lost records, logger stalls.
10. Only with --reports, in parallel: both_sides_check.py (check of the dating) and
   data_quality_reports.py (state of the beacons, sanity findings).

Outputs in --output-dir (default Output): data/ (the data for the evaluation) and pipeline/:
logs/ (working copies), quality/ (log_clock_check.csv, fake_records_original.csv,
hard_rule_check_uptime_model.csv, ...), id_correction/id_split_analysis.csv, timer_zero/ (all
timer-zero, contact sync and uptime model outputs for the working copies).
"""

from __future__ import annotations

import argparse
import collections
import csv
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pickle

import numpy as np

from lib import study
from steps import base_mislabel_check as bmc
from steps import id_split_analysis as isa
from steps import timer_zero_analysis as tza
from lib import logcache
from lib import postprocessing as pp
from steps import uptime_model as um

HERE = Path(__file__).resolve().parent
# (script, extra arguments), scripts relative to HERE, each run as a module (python -m steps.<name>);
# the drift is measured again on the uptime model's periods (the
# timer-zero analysis' periods can mix two boots) and the model solved again with it.
# --reboot-hints: the hints of this run (the file is cleared at its start) are used and added to.
MODEL_STEPS = [("steps/timer_zero_analysis.py", ["--reboot-hints"]), ("steps/contact_sync_analysis.py", ["--reboot-hints"]),
               ("steps/battery_replacement_check.py", []), ("steps/uptime_model.py", []),
               ("steps/contact_sync_analysis.py", ["--model-periods", "--reboot-hints"]), ("steps/uptime_model.py", [])]
# checks of the final uptime model after the last round, in parallel with steps 7 and 8: chains run
# in parallel, the steps of a chain one after the other; a list inside a chain is a group of chains
# run in parallel. After the H4 enforcement (which rewrites the periods) the timer step check (reads
# the periods and the clock stops) and the battery check -> hard rule check (reads the battery check)
# are independent.
CHECK_STEPS = [[("steps/clock_stop_check.py", []), ("steps/h4_enforce.py", []),
                [[("steps/battery_replacement_check.py", ["--model", "uptime"]),
                  ("steps/hard_rule_check.py", ["--model", "uptime"])],
                 [("steps/timer_step_check.py", [])]]]]
DATING_STEP = "dating/generate_output.py"
REPORT_STEPS = ["reports/both_sides_check.py", "reports/data_quality_reports.py"]


def check_clock(lines: logcache.Lines) -> list[list]:
    """Line times going back within a file (more than 1 s) or a file starting before the previous ended;
    every line with a line time counts, also those the parsers skip."""
    rows, prev_name, prev_end = [], None, None
    local = logcache.local_timestamps(lines.t)
    for f, path in enumerate(lines.paths):
        number, t = lines.line[lines.start[f]:lines.start[f + 1]].astype(np.int64), local[lines.start[f]:lines.start[f + 1]]
        extra = []
        for n, raw in lines.unparsed[f].items():
            try:
                extra.append((n, pp.parse_timestamp(raw[:pp.LOG_TIME_LENGTH]).timestamp()))
            except ValueError:
                pass
        if extra:
            number = np.r_[number, [e[0] for e in extra]]
            t = np.r_[t, [e[1] for e in extra]]
            order = np.argsort(number, kind="stable")
            number, t = number[order], t[order]
        if not len(t):
            continue
        last = np.maximum.accumulate(t)                     # latest line time up to each line
        for k in (np.flatnonzero(t[1:] < last[:-1] - 1) + 1).tolist():
            rows.append([path.name, int(number[k]), "line time goes back within the file", round(float(last[k - 1] - t[k]))])
        first = float(t[0])
        if prev_end is not None and first < prev_end - 1:
            rows.append([path.name, 1, f"file starts before the end of {prev_name}", round(prev_end - first)])
        prev_name, prev_end = path.name, float(last[-1])
    return rows


def module_command(script: str, args: list[str]) -> list[str]:
    """Run a script of a subfolder as a module (cwd HERE): steps/x.py -> python -m steps.x."""
    return [sys.executable, "-u", "-m", script.removesuffix(".py").replace("/", "."), *args]


def step_command(script: str, extra: list[str], log_dir: Path, out_dir: Path) -> list[str]:
    return module_command(script, ["--log-dir", str(log_dir), "--output-dir", str(out_dir), *extra])


def run_model(log_dir: Path, out_dir: Path) -> None:
    for script, extra in MODEL_STEPS:
        t = time.time()
        print(f"--- {script} {' '.join(extra)}", flush=True)
        result = subprocess.run(step_command(script, extra, log_dir, out_dir), cwd=HERE)
        if result.returncode:
            raise SystemExit(f"{script} failed")
        print(f"    ({time.time() - t:.0f} s)", flush=True)


def _run_chain(chain: list, out_dir: Path, results: list) -> None:
    """Runs (name, command) steps one after the other; a list item is a group of chains run in
    parallel, the chain goes on when all of them are done. Results in chain order."""
    for item in chain:
        if isinstance(item, list):
            group = [[] for _ in item]
            threads = [threading.Thread(target=_run_chain, args=(sub, out_dir, res)) for sub, res in zip(item, group)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            for res in group:
                results.extend(res)
            if any(code for res in group for _, code, _ in res):
                return
            continue
        name, command = item
        log = out_dir / f"{Path(name.split()[0]).stem}.out.txt"
        with log.open("w", encoding="utf-8") as fh:
            code = subprocess.run(command, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT).returncode
        results.append((name, code, log))
        if code:
            return


def start_chains(chains: list[list[tuple[str, list[str]]]], out_dir: Path) -> list[tuple[threading.Thread, list]]:
    """Start chains of (name, command) in parallel; their output goes to a file per step, printed
    by finish_checks."""
    started = []
    for chain in chains:
        results: list = []
        thread = threading.Thread(target=_run_chain, args=(chain, out_dir, results))
        thread.start()
        started.append((thread, results))
    return started


def start_checks(log_dir: Path, out_dir: Path) -> list[tuple[threading.Thread, list]]:
    """Start the chains of CHECK_STEPS in parallel."""
    def commands(chain):
        return [[commands(sub) for sub in item] if isinstance(item, list) else
                (f"{item[0]} {' '.join(item[1])}".strip(), step_command(item[0], item[1], log_dir, out_dir))
                for item in chain]
    return start_chains([commands(chain) for chain in CHECK_STEPS], out_dir)


def finish_checks(started: list[tuple[threading.Thread, list]]) -> None:
    failed = []
    for thread, results in started:
        thread.join()
        for name, code, log in results:
            print(f"--- {name}", flush=True)
            print(log.read_text(encoding="utf-8", errors="replace"), end="", flush=True)
            log.unlink()
            if code:
                failed.append(name)
    if failed:
        raise SystemExit(f"{', '.join(failed)} failed")


def model_boots(out_dir: Path, readouts, resolver) -> dict[int, list[float]]:
    """Boots of the uptime model per beacon, without periods made only of undecided disputed readouts."""
    tz = out_dir / pp.OUTPUT_TIMER_ZERO_DIR
    undecided = {(readouts[i].file, readouts[i].first) for i in resolver.disputed if readouts[i].label is None}
    members = collections.defaultdict(list)
    for row in csv.DictReader((tz / "uptime_model_readouts.csv").open(encoding="utf-8")):
        members[(row["ID"], row["Period"])].append((row["Log file"], int(row["First line"])))
    boots = collections.defaultdict(list)
    for row in csv.DictReader((tz / "uptime_model_periods.csv").open(encoding="utf-8")):
        key = (row["ID"], row["Period"])
        if members[key] and all(m in undecided for m in members[key]):
            continue
        if row["ID"].isdigit():
            boots[int(row["ID"])].append(datetime.strptime(row["Boot"], pp.TIMESTAMP_FORMAT).timestamp())
    return boots


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="From the original logs to the data for the evaluation.")
    study.add_argument(parser)
    parser.add_argument("--output-dir", type=Path, default=HERE / pp.DEFAULT_OUTPUT_DIR_NAME,
                        help="pipeline/ (working copies, model, checks) and data/ (the evaluation data) go here")
    parser.add_argument("--allow-duplicate-files", action="store_true")
    parser.add_argument("--reports", action="store_true",
                        help="Also run both_sides_check.py and data_quality_reports.py (about 1 min more).")
    parser.add_argument("--validate-lost", type=int, default=0, metavar="N",
                        help="Test E of the lost-records check in the dating (generate_output.py --validate-lost).")
    args = parser.parse_args(argv)
    missing = study.missing(pp.STUDY_DIR)
    if missing:
        print(f"study folder {pp.STUDY_DIR} lacks: {', '.join(missing)}")
        return 1
    args.original_dir = pp.ORIGINAL_LOG_DIR
    print(f"study folder {pp.STUDY_DIR}", flush=True)
    t0 = time.time()
    out = args.output_dir / "pipeline"
    logcache.set_root(out)                          # inherited by the steps
    data = args.output_dir / pp.OUTPUT_DATA_DIR
    quality, idc, logs = out / pp.OUTPUT_QUALITY_DIR, out / pp.OUTPUT_ID_CORRECTION_DIR, out / "logs"
    for d in (quality, idc, logs):
        d.mkdir(parents=True, exist_ok=True)
    paths = sorted(args.original_dir.glob("*.log"))
    # the reboot hints of the contact sync (rule a'' of the timer-zero analysis) hold for this run only:
    # found in round 1, used in round 2; kept from an earlier run they made the result depend on it
    (out / pp.OUTPUT_TIMER_ZERO_DIR / tza.REBOOT_HINTS_CSV).unlink(missing_ok=True)
    # the output is stale until this run's checks are done (an aborted run leaves no fingerprint)
    (out / pp.INPUTS_JSON).unlink(missing_ok=True)

    print(f"1. {len(paths)} original log files: cache, duplicates and logger clock", flush=True)
    cached = logcache.ensure(paths)
    print(f"   cache of the original logs: {cached} files read ({time.time() - t0:.0f} s)", flush=True)
    duplicates = logcache.find_duplicate_log_files(paths)
    for kind, files in duplicates:
        print(f"   duplicate log files ({kind}): {', '.join(p.name for p in files)}")
    if duplicates and not args.allow_duplicate_files:
        return 1
    original = logcache.Lines(paths)
    clock = check_clock(original)
    with (quality / "log_clock_check.csv").open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["Log file", "Line", "Problem", "Seconds"], *clock])
    print(f"   logger clock: {len(clock)} problem(s){': ' + '; '.join(f'{r[0]} line {r[1]}: {r[2]} ({r[3]} s)' for r in clock[:5]) if clock else ''}")

    print(f"2. fake records ({time.time() - t0:.0f} s)", flush=True)
    # steps 2 and 3: the same code as id_split_analysis.py run alone
    fakes, switches, readouts, resolver = isa.first_split(original, paths, out)
    del original

    hints: dict = {}                       # reboot hints of round 1 (set at its end)
    for round_ in (1, 2):
        print(f"4. working copies of the logs ({time.time() - t0:.0f} s)", flush=True)
        files, lines, recovered = isa.write_working_logs(logs, args.original_dir, readouts, resolver.disputed, fakes,
                                                          switches)
        print(f"   {files} files changed: {lines} lines relabelled, {recovered} lines recovered from "
              f"{len(fakes)} fake records (BI 3.2.2)")
        if logcache.ensure(sorted(logs.glob("*.log"))):         # made with the copies; only a copy changed by hand
            print("   working copies changed outside the pipeline: their cache written again", flush=True)
        print(f"5. uptimes on the working copies, round {round_} ({time.time() - t0:.0f} s)", flush=True)
        run_model(logs, out)
        if round_ == 2:
            # hints found only now would need a third round: none is run, so say so
            found = tza.read_reboot_hints(out / pp.OUTPUT_TIMER_ZERO_DIR / tza.REBOOT_HINTS_CSV)
            late = {b: [h for h in v if h not in hints.get(b, [])] for b, v in found.items()}
            late = {b: v for b, v in late.items() if v}
            if late:
                print(f"   WARNING: the contact sync of round 2 found {sum(map(len, late.values()))} new reboot "
                      "hint(s), not used (no round 3): " + "; ".join(f"{b} {h:%Y-%m-%d %H:%M:%S}" for b, v in
                                                                    sorted(late.items()) for h in v), flush=True)
            break
        print(f"6. second split with the uptime model ({time.time() - t0:.0f} s)", flush=True)
        new, reports = resolver.model_boots(model_boots(out, readouts, resolver))
        print(f"   {new} undecided readouts decided by the uptime model's boots; {len(reports)} first decisions "
              "fit only the other label:")
        for line in reports:
            print(f"     {line}")
        with (out / pp.OUTPUT_TIMER_ZERO_DIR / um.STATE_PKL).open("rb") as f:
            new_rules, notes = resolver.model_rules(pickle.load(f))
        print(f"   {new_rules} undecided readouts decided by the hard rules of the uptime model; "
              f"{len(notes)} with Current Timer stay undecided:")
        for line in notes:
            print(f"     {line}")
        new += new_rules
        # round 2 also when only hints were found: they are used only by the timer-zero analysis of round 2
        hints = tza.read_reboot_hints(out / pp.OUTPUT_TIMER_ZERO_DIR / tza.REBOOT_HINTS_CSV)
        print(f"   {sum(map(len, hints.values()))} reboot hints from the contact sync")
        if not new and not hints:
            break
        isa.write_csv(idc / isa.CSV_NAME, readouts, resolver.disputed)

    print(f"   clock stops, H4 enforcement, battery check, timer step and hard rule check on the final uptime model, "
          f"in parallel with "
          f"steps 7 and 8 ({time.time() - t0:.0f} s)", flush=True)
    checks = start_checks(logs, out)

    print(f"7. result of the ID split ({time.time() - t0:.0f} s)", flush=True)
    isa.write_csv(idc / isa.CSV_NAME, readouts, resolver.disputed)
    methods = collections.Counter(readouts[i].method or "undecided" for i in resolver.disputed)
    print("   " + ", ".join(f"{k} {v}" for k, v in sorted(methods.items())))
    changed = sum(1 for i in resolver.disputed if readouts[i].label is not None and readouts[i].label != int(readouts[i].beacon_id))
    print(f"   against the original logs: {changed} readouts relabelled to 48-57, "
          f"{sum(1 for i in resolver.disputed if readouts[i].label is None)} undecided")

    print(f"8. Base mislabelling check on the working copies ({time.time() - t0:.0f} s)", flush=True)
    bmc.run(logs, quality, out / pp.OUTPUT_TIMER_ZERO_DIR / "readout_overview.csv", idc / isa.CSV_NAME)
    print(f"   waiting for the checks ({time.time() - t0:.0f} s)", flush=True)
    finish_checks(checks)
    pp.write_input_fingerprints(out)                # the dating checks that its inputs are still these

    print(f"9. dating of all records: the data for the evaluation ({time.time() - t0:.0f} s)", flush=True)
    command = module_command(DATING_STEP, ["--study-dir", str(pp.STUDY_DIR),
                                           "--pipeline-dir", str(out), "--data-dir", str(data)])
    if args.allow_duplicate_files:
        command.append("--allow-duplicate-files")
    if args.validate_lost:
        command += ["--validate-lost", str(args.validate_lost)]
    if subprocess.run(command, cwd=HERE).returncode:
        raise SystemExit(f"{DATING_STEP} failed")

    if args.reports:
        print(f"10. both-sides check and data quality reports, in parallel ({time.time() - t0:.0f} s)", flush=True)
        finish_checks(start_chains([
            [(REPORT_STEPS[0], module_command(REPORT_STEPS[0], ["--data-dir", str(data), "--quality-dir",
                                                                str(out / pp.OUTPUT_QUALITY_DIR)]))],
            [(REPORT_STEPS[1], step_command(REPORT_STEPS[1], [], logs, out))]], out))
    print(f"done ({time.time() - t0:.0f} s); data for the evaluation in {data}, diagnostics in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
