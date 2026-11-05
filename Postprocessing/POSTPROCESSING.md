# Postprocessing scripts

Turn the `*.log` files written by the logger (`dsa_logger.py`) into the data
for the evaluation and into reports on its quality. The logger does not call
any of these scripts, and they do not use the logger's code: what they need
of it is in `logger.toml` of the study folder (Input). Run them after a
capture. The method is described in detail in
`Postprocessing Method.md` (section numbers below refer to it); the system and
the known issues of the data in `Background Information.md` (BI); open points
in `ToDo.md` (both in the repository root; the logger and firmware points in the firmware
repository, `BeaconNRF54/ToDo.md`).

## Run order

```
pip install -r requirements.txt   # Python >= 3.11; numpy, scipy, pandas
python run_pipeline.py            # original logs -> Output/data/ (+ Output/pipeline/)   (about 18 min)
python run_pipeline.py --reports  # the same + both-sides check and data quality reports (about 1 min more)
python run_pipeline.py --study-dir <folder>   # another study folder (default ../../Studies/DSA2026)
```

One script, from the original logs to the data for the evaluation
(`Output/data/`: contacts, self-reports, eco sessions, reboots, battery,
coverage, lost records). It stops on duplicate original logs. Separately,
when needed:

- `python dating/generate_output.py` - repeat only the dating on the existing pipeline output
  (about 1.5 min; e.g. `--validate-lost N`). Refuses a pipeline output made from other inputs
  (`Output/pipeline/inputs.json`; `--allow-stale` to override).
- `python -m steps.id_split_analysis` - only steps 2 to 4 (fake records, first ID split, working
  copies of the logs), the same code as the pipeline's.
- `python -m unittest discover -s tests` - the tests (seconds): configs, line patterns, fake
  records, ID bug, eps, no module state, cases of other studies (small duplicate files, IDs above
  999, no contact pairs). They use the study folder's configs.
- `python tools/compare_outputs.py <baseline Output> <new Output>` - regression check: two runs
  on the same study folder, file by file (SHA-256); after changes that should not change the data.
- `python validate_model.py --headerless 3` - synthetic validation of the uptime model
  (section 19.1), from which the time precision of the records comes; after model changes. The
  fit is cached and done again when the pipeline's model or `uptime_model.py` changed (`--refit`
  forces it).
- The slides on the state of the postprocessing (`make_presentation.py`) are not part of this
  folder: they live in `../Presentation/` and read this folder's `Output/pipeline/` and
  `Output/data/`; `python Presentation/make_presentation.py` from the repository root (about 2 min;
  writes `Output/presentation/`). Needs a run with `--reports` and `validate_model.py` after it.

## Layout

```
Postprocessing/
  run_pipeline.py        entry point: original logs -> Output/data/ (steps 1-10)
  validate_model.py      tool: synthetic validation of the uptime model
  steps/                 pipeline steps 2-8: ID split, uptime model, its checks -> Output/pipeline/
  dating/                step 9: the data for the evaluation -> Output/data/
  reports/               step 10 (--reports): checks of the data and reports
  lib/postprocessing.py  shared code (parsing, checks, CSV helpers, paths)
  lib/logcache.py        the logs as number arrays, cached: the only code that reads the logs
  lib/study.py           the study folder (--study-dir): where the inputs are
  lib/logger_protocol.py the logger's message and line format, from logger.toml in the study folder
  lib/study_config.py    the values of the study, from study.toml in the study folder
  tests/                 the tests (python -m unittest discover -s tests)
  tools/compare_outputs.py  regression check of two output folders
  requirements.txt       the Python packages
  Output/data/           the data for the evaluation
  Output/pipeline/       working copies of the logs, the uptime model, the checks (diagnostics)
```

Every script in a subfolder can also run on its own, from this folder:
`python steps/uptime_model.py ...` or `python -m steps.uptime_model ...`;
each takes `--study-dir`.

## Scripts

**Pipeline** (`run_pipeline.py`, section 20): from the original logs to
the data, in the order the checks depend on each other. Steps 2-8 are in
`steps/`, step 9 in `dating/`, step 10 in `reports/`.

| Step | Script | Purpose |
|---|---|---|
| 1 | (`run_pipeline.py`) | Cache of the original logs (the only step that reads the logs), duplicate log files (stop), logger clock |
| 2–4, 6–7 | `steps/id_split_analysis.py` | Fake records (BI 3.2.2), contacts of the next beacon under the previous ID (section 5a), automatic split of ID1 0–9 / 48–57 (section 15; only with the ID bug, `study.toml`), working copies of the logs with the decided IDs, the switch relabels and the fake records reconstructed; alone: steps 2–4 |
| 5 | `steps/timer_zero_analysis.py` | Readouts, timer-zero time per uptime period (the start of the uptime model), delay per readout, contact accounting per readout (section 13) |
| 5 | `steps/contact_sync_analysis.py` | Timer-zero times aligned via symmetric contacts; drift per beacon (section 14; `--model-periods`: on the uptime model's periods) |
| 5, after 6 | `steps/battery_replacement_check.py` | Documented battery replacements against the boots, periods never read out (section 17): before the uptime model for its windows, after the H4 enforcement (`--model uptime`) on the final model for the dating |
| 5 | `steps/uptime_model.py` | Uptime periods and boots of all beacons from one model: hard rules as constraints, evidence as costs (section 19) |
| after 6 | `steps/clock_stop_check.py` | Clock stops: beacon silent while its uptime stood still; shifts for the dating (section 19.3) |
| after 6 | `steps/h4_enforce.py` | H4 as a hard rule: smallest change of boots and drifts under which all readouts lie in log order (section 19.4); keeps the model's own values in two columns, so a second run gives the same result |
| after 6 | `steps/timer_step_check.py` | Two clocks in one uptime period not explained by the clock stops: all records of the period unreliable (section 19.2) |
| after 6 | `steps/hard_rule_check.py` | Check of the final model against the hard rules H1–H9 (section 18) |
| 8 | `steps/base_mislabel_check.py` | Contacts the Base filed under the wrong beacon, on the working copies (section 16) |
| 9 | `dating/generate_output.py` | Data for the evaluation: contacts per day, self-reports, eco sessions, reboots, battery, coverage, lost records, logger stalls (section 21); alone: repeats the dating; `--validate-lost N`: test E of the lost-records check |
| 9 | `dating/model_dating.py` | The dating with the uptime model (used by `generate_output.py`) |
| 10 (`--reports`) | `reports/both_sides_check.py` | Both sides of an encounter: the dated contacts against the partners' records on clean encounter edges, per precision class and beacon (section 21) |
| 10 (`--reports`) | `reports/data_quality_reports.py` | Sanity findings per log file, fake records left in the working copies, possible transfer mislabelling, Base mislabelling check on the uncorrected logs (section 11); does not touch the pipeline's results |

**Tools** (top level) **and shared code:**

| Script | Purpose |
|---|---|
| `validate_model.py` | Synthetic validation of the uptime model: injected reboots (with contacts, without, and without log order: the precision classes), late readouts, delay-free readouts as ground truth, hidden `Current Timer` (section 19.1); `--seed N` for another sample |
| `reports/without_pipeline.py` | The contacts without the pipeline: the logger's own dating rule during the deployment against the pipeline's, record by record: contacts lost, under the wrong beacon, time error (section 22); used by the slides (`../Presentation/make_presentation.py`), alone prints the numbers |
| `lib/postprocessing.py` | Shared code: parsing, validity checks, fake records, CSV readers and writers, paths. Not run directly |
| `lib/logcache.py` | The log files as number arrays (contact lines as numbers, other lines as text, the fake records found while reading, the file's SHA-256 for the duplicate check), one cache file per log; the file can be rebuilt from it byte for byte. Each log is read once: the originals in step 1 (cache in `Output/pipeline/cache/`: one subfolder per log folder, named by the folder and a digest of its full path, never inside a log folder; `run_pipeline.py` and `generate_output.py` put it into their pipeline folder and pass it on as `DSA_CACHE_DIR`), the working copies are made from that cache with their own. Written again whenever a log file or the logger's format (`logger.toml`) changed. Every script reads the cache, none the text. Stops with an error if a line does not have the logger's format. Not run directly |
| `lib/study.py`, `lib/study_config.py`, `lib/logger_protocol.py` | The study folder and its two configs (Input). Not run directly |

All scripts default to the pipeline: `--log-dir Output/pipeline/logs`,
`--output-dir Output/pipeline` (`generate_output.py`: `--pipeline-dir`,
`--data-dir Output/data`).

## Input

The study folder (`--study-dir` of every script; default
`../../Studies/DSA2026` relative to this folder; `lib/study.py`). A script
passes it to the scripts it starts as the environment variable
`DSA_STUDY_DIR`; without `--study-dir` a script reads that variable or uses
the default. The pipeline stops if one of these is missing
(except `battery_replacements.csv`, which is optional):

| In the study folder | Contents |
|---|---|
| `Logs/*.log` | The original log files, never modified |
| `list_persons.csv`, `list_rooms.csv` | Beacons worn by persons, location tags (beacon types, RSSI thresholds) |
| `battery_replacements.csv` | Optional: documented battery replacements (section 17). Without it there are none: every boot after the first has the cause `reset`, the battery check lists nothing |
| `study.toml` | The values of the study (`lib/study_config.py`): hard limits of the data (flashing, latest record), observation window, start of the delay-free readouts, the Base fix, the fielded IDs, valid RSSI and the recording thresholds, the storage limit and the drift of the beacons, the logger's ID bug, the logger's own dating rule (section 22). The code holds no value of a particular study |
| `logger.toml` | The logger's format (`lib/logger_protocol.py`): log file names, the layout of a log line (line time format, ID field, separator), the frame of a message on the wire and every message type with its payload layout and the log lines written for it, each under the key the scripts read it by. All patterns of the log lines are made from these texts (no line text in the code); used also to find the fake records and write back the messages hidden in them (BI 3.2.2) |

The original log files are never modified. `run_pipeline.py` writes working
copies with the decided IDs 0–9 / 48–57 and the fake records reconstructed to
`Output/pipeline/logs/`; everything else is generated from these. When its
checks are done it records the inputs it used (`Output/pipeline/inputs.json`:
the two configs as read, the beacon lists, the battery replacements, the
names, sizes and times of the logs); the dating refuses a pipeline output made
from other inputs.

**The logger's ID bug** (BI 3.2.1): in the logs of DSA2026, beacons 48–57 were
logged as ID1 0–9 in the lines logged before 2026-08-15 22:00; a logged 48–57
is always right. The pipeline splits these readouts (steps 3 and 6, section
15). For a study whose logger does not have the bug, set `enabled = false` in
`[id_bug]` of `study.toml`: then no readout is disputed, the working copies
keep every logged ID, and no record gets the status `ID undecided (0-9 /
48-57)`.

In DSA2026 each line has the form (`logger.toml`)

```
<line time>,ID: <beacon_id>,<text>
```

with `<text>` one of `Current Timer: N`, `Voltage: N`, `Status: N`,
`Contact Count: N`, `ID2: X, Timer: N, RSSI: -N` (a contact),
`Self-report time: N`, `Eco Session Enter: N, Leave: N`,
`Transfer complete. Disconnecting` (BI 2.6). Beacons report events as a timer
of their own uptime; the real time comes from the uptime model:
boot of the record's uptime period + timer × (1 + drift of the period), plus
the shifts of the period's clock stops before the timer (section 21).

## Output

**`Output/data/`** (the data for the evaluation; `generate_output.py`):

| File | Contents |
|---|---|
| `contacts_YYYYMMDD.csv` | One file per calendar day: `ID1` (reporting beacon), `ID2` (seen beacon), `RSSI`, `Contact Local Time`, `Time precision [s]` (95 % bound), `Status` |
| `self_reports.csv` | `ID`, `Local Time`, `Time precision [s]`, `Status` |
| `eco_sessions.csv` | `ID`, `Enter Local Time`, `Exit Local Time`, `Time precision [s]`, `Status` |
| `reboots.csv` | Every boot: `ID`, `Beacon type`, `Period`, `Boot Local Time`, `Time precision [s]` (empty for a period never read out), `Cause` (`first boot`, `battery replacement <documented time>` - with `(period never read out)` if no readout followed -, `reset`), `Own records erased from`, `Own records erased [h]` (only where the partners saw the beacon: as `erased by reboot` in the coverage) |
| `battery.csv` | `ID`, `Local Time` (real time of the readout), `Time precision [s]`, `Battery (mV)`, `Contact Count` (contacts stored at its start), `Status` - one row per readout with `Current Timer` |
| `not_dated.csv` | Records without an uptime period or with a time outside the hard limits: kind (`Record`: contact / self-report / eco session), IDs, timer(s), log file, line, reason |
| `coverage.csv` | Per beacon, non-overlapping intervals over the whole data: `recorded`, `erased by reboot`, `read out, not logged`, `dropped (storage full)`, `dropped (storage full) and read out, not logged` (both: the flash ran full and a readout not logged emptied it), `not recorded (no readout possible)`, `not seen (off or alone)` (within `erased by reboot`: the partners did not see the beacon, so it had no contacts to erase), `silent (clock stopped)`, `never read out`, `not read out`, `before first boot` (up to the first sighting by the partners, from there `erased by reboot`), `no data`; column `Eco` for reduced scanning (section 21). On the pieces of a lost-records gap, its evidence (columns `Lost from` … `Readout threshold`, per gap, repeated on each of its pieces; empty elsewhere): own records missing although the partners recorded the beacon: gaps between two readouts of a period, holes inside one readout and gaps at the start of a readout (column `Where`: `between readouts`, `inside a readout` - a readout with `Current Timer` only if its `Contact Count` shows missing contacts -, `start of a readout` - proven by the `Contact Count`, without partner records -, `before the first boot` - recorded by at least 3 partners before the beacon's first boot: erased by a reboot before its first readout with `Current Timer`); lost interval, cause (the `State`: `read out, not logged`, `dropped (storage full)`, `erased by reboot`, or `not recorded (no readout possible)`: a gap between readouts in which no readout was possible — the own records the beacon would have made there, times a factor calibrated on the logged readouts, stay below the smallest `Contact Count` of the readouts logged around it), partner records, readouts before and after, the full gap, `Own records expected`, `Readout threshold` (the readout test, Method 21). The hours of a gap are the sum of its pieces' `Hours` |

`Status`: `ok`; `fake` (BI 3.2.2); `unreliable time (two clocks in one
period)`; `ID undecided (0-9 / 48-57)`; `invalid: ID2 out of range` /
`RSSI out of range` / `timer after its log line (H1)`; `duplicate` (the same
contact already received in an earlier readout of the same uptime period).
The evaluation selects the status it uses (normally `ok`). All records are
written whatever their time; the observation window (2026-08-13 18:00 to
2026-08-29 09:00) is applied by the evaluation. Records dated before the
flashing (2026-08-12 23:00) or after 2026-08-30 00:05 have a corrupt timer
and go to `not_dated.csv`.

**`Output/pipeline/`** (`run_pipeline.py`, `data_quality_reports.py`; the dating's diagnostics, `both_sides_check.py`):

| File | Written by | Contents |
|---|---|---|
| `inputs.json` | pipeline | The inputs this output was made from (fingerprints; written when the checks are done, removed at the start of a run) |
| `logs/` | pipeline | Working copies of the original logs (unchanged files are hard links) |
| `cache/` | pipeline | Cache of the original logs and of the working copies, one subfolder each (`lib/logcache.py`, about 1.5 GB each; the unchanged working copies share the originals' cache files as hard links); can be deleted, is written again when needed (reading all logs once, about 70 s) |
| `id_correction/id_split_analysis.csv` | pipeline | Per disputed readout (ID1 0–9 before 2026-08-15 22:00): decided ID, method, evidence |
| `id_correction/switch_relabels.csv` | pipeline | Runs of contact lines written under the ID of the beacon that follows them: file, lines, logged and corrected ID, timers (section 5a) |
| `timer_zero/readout_overview.csv` | pipeline | One row per readout in log order: file, ID, times, `Current Timer`, `Transfer complete`, contact accounting (expected, received, lost at least / at most), self-reports, delay, timer-zero time, header values, eco sessions, lines, first in file, period, delay lower bound, ID split state; line and line time of the `Current Timer`, observed timer-zero time, reason for a new period, flags (section 13) |
| `timer_zero/timer_zero_periods.csv` | pipeline | Timer-zero analysis: periods (section 13) |
| `timer_zero/contact_sync_periods.csv`, `contact_sync_drift.csv`, `reboot_hints.csv` | pipeline | Contact sync: aligned timer-zero times, drift per beacon, confirmed reboots for round 2 (section 14; the hints are cleared at the start of each run; `timer_zero_analysis.py` and `contact_sync_analysis.py` run alone use them only with `--reboot-hints`, without it the contact sync writes the file anew) |
| `timer_zero/uptime_model_periods.csv`, `uptime_model_readouts.csv`, `uptime_model_headerless.csv`, `uptime_model_fake_records.csv`, `uptime_model_state.pkl` | pipeline | The uptime model: periods with boot and drift (after the H4 enforcement; the model's own in `Boot before H4`, `Drift before H4 [s/day]`), readouts with period, real time and delay, readouts without `Current Timer` and their period (section 19) |
| `timer_zero/uptime_model_validation*.csv` | `validate_model.py` | Synthetic validation (section 19.1) |
| `quality/log_clock_check.csv`, `fake_records_original.csv` | pipeline | Logger clock; fake records of the original logs |
| `quality/battery_replacement_check.csv`, `unread_uptime_periods.csv` | pipeline | Battery replacements, periods never read out, on the final uptime model (section 17) |
| `quality/clock_stops.csv` | pipeline | Every unexpected silence of a worn beacon with its verdict; accepted stops with timer and shift (section 19.3) |
| `quality/h4_enforce.csv` | pipeline | Periods whose boot or drift the H4 enforcement changed; contradictions (section 19.4) |
| `quality/timer_step_check.csv`, `unreliable_time_records.csv` | pipeline | Two clocks in one period; records marked unreliable (section 19.2) |
| `quality/hard_rule_check_uptime_model.csv` | pipeline | Violations of the hard rules by the final model (section 18) |
| `quality/base_mislabel_check.csv` | pipeline | Base mislabelling check on the working copies (section 16) |
| `quality/sanity_findings.csv` | reports | Per log file: lines flagged by the checks that need no dating (beacon ID, ID2, RSSI out of range, fake records, possible transfer mislabelling) (section 11) |
| `quality/fake_records.csv` | reports | Fake contact records still in the working copies (the known ones are reconstructed by the pipeline) |
| `quality/transfer_mislabel.md` | reports | Adjacent contact lines of different ID1 with a timer step of 0–59 s: possible mislabelling, for review (section 5) |
| `quality/base_mislabel_check_original.csv` | reports | Base mislabelling check on the uncorrected logs (`Original/`) |
| `quality/logger_stalls.csv` | dating | Times the logger stopped reading (buffered lines logged on waking up, BI 3.2.3 / 3.2.4) |
| `quality/lost_records_validation.csv` | dating | Test E of the lost-records check (`--validate-lost N` only) |
| `quality/both_sides_check.csv` | reports | Both-sides check of the dating per beacon and precision class (beacons with at least 30 edges), and in total per class (`ID1` = `all`) (section 21) |

## CLI flags

Run `python <script> --help` for the full list. The most relevant:

- `--study-dir` (every script) - the study folder with the inputs.
- `--reboot-hints` (`timer_zero_analysis.py`, `contact_sync_analysis.py`) - use the reboot hints found
  so far (the pipeline passes it); without it a script run alone does not depend on earlier runs.
- `--log-dir` / `--output-dir` - the log and output folders (default: the pipeline's).
- `--valid-date-start` / `--valid-date-end` - the observation window (`"YYYY-MM-DD HH:MM:SS"`, in every
  script); used to label the phase of
  findings (before / in / after the deployment) and, in `generate_output.py`, to restrict the
  output if wanted (default there: no limit).
- `--allow-duplicate-files` - continue despite duplicate log files.
- `--reports`, `--validate-lost N` (`run_pipeline.py`); `--allow-stale`, `--validate-lost N` (`generate_output.py`).
- `--model uptime` (`battery_replacement_check.py`, `hard_rule_check.py`) - check the final uptime model.
- `--refit`, `--per-beacon N`, `--headerless ROUNDS` (`validate_model.py`).
