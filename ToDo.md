# ToDo – Postprocessing

Open and done points of the postprocessing and of the evaluation of its data (`Postprocessing/`).
Section numbers (e.g. 3.2.3) refer to `Background Information.md`, "Method" to
`Postprocessing/Postprocessing Method.md`. The logger and firmware points (1, 21, 22, 23, 24, 28)
are in the firmware repository, `../../BeaconNRF54/ToDo.md`; the numbers are shared, so a
number is used only once in both files.

| # | Area | Task | Status |
|---|---|---|---|
| 2 | Postprocessing | Set the final observation window | Done: 2026-08-13 18:00 to 2026-08-29 09:00 |
| 3 | Postprocessing | Flag incomplete readouts | Done 2026-10-23: missing tails are gaps between readouts or `erased by reboot` (coverage ends `recorded` at the newest delivered contact); holes inside a readout are located (`lost_records.csv`, `Where` = `inside a readout`, 96 holes, 289 h on 2026-10-24; Method 21). Start of a readout (2026-10-26): gaps before a readout whose `Contact Count` shows missing contacts although its newest contact was delivered are located without the partner rule (52 gaps, 2 h); 76 readouts with a deficit but no gap stay unlocated (probably contacts counted twice) |
| 4 | Postprocessing | Correct late line times | Done: `generate_output.py` dates by the uptime model (method 21) |
| 5 | Postprocessing | Remove duplicate contacts | Done: status `duplicate` in the output of `generate_output.py` (same contact in an earlier readout of the same uptime period) |
| 6 | Postprocessing | Mark data gaps after resets | Done: `data/coverage.csv` (per beacon: recorded, erased by reboot, never read out, not read out, no data; eco) |
| 7 | Postprocessing | Mark logger outages | Done as far as observable: stalls with buffered lines in `pipeline/quality/logger_stalls.csv`; their cost (readouts lost) in the lost-records columns of `data/coverage.csv` (method 21) |
| 8 | Postprocessing | Mark periods with reduced or no recording | Done for eco mode: column `Eco` of `data/coverage.csv`. Tracking off by command: not marked (the experiment log is not used for the coverage) |
| 9 | Postprocessing | Flag readouts with full storage | Done: coverage state `dropped (storage full)` (`Contact Count` ≥ 215 000 between two readouts, lost-records columns of `data/coverage.csv`) |
| 10 | Postprocessing | Flag readouts with a lost header | Done: assigned to a period by the uptime model or left undecided (`timer_zero/uptime_model_headerless.csv`); undecided records `not datable` |
| 11 | Evaluation | Exclude development kits | Open |
| 12 | Evaluation | Treat beacons without readout as missing data | Open |
| 13 | Postprocessing | Detect contacts mislabelled by the Base before 2026-08-15 18:43 | Done 2026-11-05: check in `base_mislabel_check.py`; no mislabelling found in the deployment; 3 cases before it (T6: a single `Transfer complete` line under another ID, no contacts or self-reports affected; checked 2026-11-04, nothing to clean) |
| 14 | Postprocessing | Check for a reboot before using another readout's `Current Timer` | Done with the model dating (periods of the uptime model; method 21) |
| 15 | Postprocessing | Mark readouts whose ID 0–9 / 48–57 stayed undecided | Done: status `ID undecided (0-9 / 48-57)` from the automatic split (`run_pipeline.py`) |
| 16 | Postprocessing | Refuse duplicate log files | Done in `run_pipeline.py`, `generate_output.py` and `data_quality_reports.py` |
| 17 | Postprocessing / Evaluation | Complete the validity checks (RSSI threshold, timer limit) | Done for the postprocessing (2026-11-05): timer: status `invalid: timer after its log line (H1)` in `generate_output.py`. RSSI threshold: moved to the evaluation (with ToDo 47) |
| 18 | Postprocessing | Recognise fake records by their byte pattern | Done: `find_fake_contacts` (embedded logger messages); in the pipeline the fakes are reconstructed in the working copies (`recover_fake_lines`: the hidden messages as normal log lines), listed in `quality/fake_records_original.csv` |
| 19 | Evaluation | Contact rate only over clean intervals | Open; moved to the evaluation (`contact_rate_report.py` retired 2026-10-24) |
| 20 | Tools | Repair the `resolve-id-mislabel` skill after the move of `postprocessing.py` | Done: retired with the manual ID correction (`Postprocessing/retired/`), not to be repaired |
| 25 | Postprocessing | Readouts without `Current Timer` that are alone in their uptime period | Done 2026-10-25: co-presence check (Method 19): test D 0 of 62 wrong (before 4); 86 on 2026-08-16 stays undecided in the test. Found with it: 42 p8 (2026-08-25 08:42), header and contacts do not fit one clock — open |
| 26 | Postprocessing | Bounds of a lost interval | Done: from the partners' first to their last sighting of the beacon in the gap (± 60 s); the full gap stays in the lost-records columns of `coverage.csv` (`Gap from` / `Gap to`) |
| 27 | Postprocessing | Beacon 3 offset by +50 s in the both-sides check | Done: checked; period 10 (one readout, boot from the delay only), within its 90 s precision; no change |
| 29 | Postprocessing | Gaps with two causes (full storage, then readouts not logged) | Done 2026-11-05 (Method 20, 21): new coverage state `dropped (storage full) and read out, not logged`: 9 gaps, 800 h, all location tags (118 among them) |
| 31 | Postprocessing | Speed up the pipeline | Done 2026-10-22 – 2026-10-29 (here and ToDo 40): full run 104 min → about 20 min (1 173 s with `--reports` on 2026-11-02), outputs byte-identical at every step; the logs are read once into a cache of number arrays (40). The checks after round 2 run in two parallel branches since 2026-11-02 (about 20 s). Left (see details): round 2 and the model iterations not cut (would change results) |
| 32 | Postprocessing | Checks not yet done | Done 2026-10-26 (Method 19): drift residuals show no daily or type effect above about 2 s; reboot penalty: 30 for all changes nothing, 8 for all gives two physically impossible extra periods of room tags on the setup night; test A per type 100 % found |
| 33 | Postprocessing | Log-order cap (H4c) on boots without contact evidence | Done 2026-10-22 (`uptime_model.py`, Postprocessing Method.md 19): H4 in the deployment 30 → 23; pipeline and output regenerated |
| 34 | Postprocessing | H4 as a hard rule | Done 2026-10-24 (`h4_enforce.py`, Method 19.4): smallest change of boots and drifts, 193 → 0 pairs out of order, 0 hard-rule violations |
| 35 | Postprocessing | Time precision of the records re-validated | Done 2026-10-24 (Method 19.1, 21): classes delay-free 13 s, contact boots 16 s, log-order predictions 28 s, delay alone 90 s (before 16 / 48 / 90 s); H6 delay of delay-free readouts 0–10 s (before 60 s). 2026-10-26: log-order class split by type, persons 40 s (test A 95 % 38 s), room tags 28 s (21 s); a boot moved by the H4 enforcement gets at least the move |
| 36 | Postprocessing | Records without a time (readouts of 61 and 71 without `Current Timer`) | Done 2026-10-25 (`uptime_model.py`, Method 19): scan times decide 71 (p12); 61 gets the period of its own the hard rules force (boot from a broad peak of the partners' records, precision 1 131 s); no record without a time left |
| 37 | Evaluation | One-sided visibility: stretches where a beacon recorded its partners but they did not record it (or the reverse), although both were recording, also at the RSSI both sides record (e.g. 96 on 2026-08-15, 75 on 2026-08-17, 52 after its boot on 2026-08-15, 55 on 2026-08-13/14, 42 on the evening of 2026-08-24 in both directions around a reboot; Method 19) | Open: handled in the evaluation; the records stay dated |
| 38 | Postprocessing | Data scan 2026-10-27: 112's readout without `Current Timer` misdated by about 10 h; beacons seen by their partners before their first boot | Done 2026-10-27 (`uptime_model.py`, `model_dating.py`, Method 19, 21): a single candidate period is checked like two (112 → own period, boot 10:16, precision about 20 min); the previous-readout bound skips readouts without contacts and an own-period boot is searched after the last readout (H5; test D 0 wrong); a fitted boot that breaks H5 withdraws nothing (75 stays in p5); seen by at least 3 partners before the first boot → `erased by reboot` (`Where`: `before the first boot`, 9 beacons, 13 h, all 2026-08-13) |
| 39 | Postprocessing | One pipeline from the original logs to the data, with reboots and battery | Done 2026-10-28 (Method 20, 21): `run_pipeline.py` runs the dating as step 9 (`--reports`: both-sides check and data quality reports as step 10); `Output/data/` gets `reboots.csv` (every boot with precision, cause and the own records it erased) and `battery.csv` (voltage and `Contact Count` per readout at its real time; replaces `quality/measurements.csv`, `timer_zero_times.csv` and the delta histogram, now in `retired/`); `generate_output.py --run-pipeline` removed. Scripts sorted by role: entry points at the top (`run_pipeline.py`, was `uptime_pipeline.py`; `validate_model.py`, was `uptime_model_validation.py`; `make_presentation.py`), `steps/`, `dating/`, `reports/`, `lib/` (`POSTPROCESSING.md`, Layout) |
| 40 | Postprocessing | Run time of the pipeline | Done 2026-10-29 (Method 20): the lines of the working copies cached as number arrays (`lib/logcache.py`), read by all steps instead of the text; contact sync transitions and a copy per search in the lost-records check removed; full run 6 262 s → 3 248 s, outputs byte-identical. Since the same day every log is read once (the originals in step 1, into the cache; the working copies are made from it), all scripts read the cache, `data_quality_reports.py` included; full run with `--reports` 3 248 s → 2 212 s; then contact sync matching and the uptime model's edge offsets batched, its beacons solved in parallel: 1 482 s; dating (an array copy per search, daily files in parallel) and the timer-zero plots in parallel: 1 124 s; outputs byte-identical |
| 41 | Postprocessing | Drift of 147 p1: its contacts want about −17 s/day, the H4 enforcement −19 to −20 s/day (about 30 s at the end of its 14-day period, within its precision) | Open: clock not steady over the 14 days, or some of its log-order readouts off (Method, second recheck at the end of section 20) |
| 42 | Postprocessing | Lost records: a gap `read out, not logged` needs a readout of the beacon in it (the partner rule assumed that a beacon seen by its partners recorded too) | Done 2026-11-02 (`model_dating.py`, Method 21, readout test): threshold = smallest `Contact Count` of the readouts logged around the gap, expected own records from the partners' records and the beacon's own ratio, factor calibrated on the logged readouts; 60 gaps (59 h) → `not recorded (no readout possible)` (new coverage state), mostly 5-min eco gaps and sparse stretches; under a low threshold not decidable |
| 43 | Postprocessing | Drift term of the time precision per beacon | Done 2026-11-02 (`model_dating.py`, Method 21): 1.5 s/day or twice the beacon's drift sd where larger (22 beacons); 1.0 % of the `ok` contacts get at most 2.3 s more |
| 44 | Postprocessing | Reboot hints (rule a″) only from readouts not before the boot | Done 2026-11-02 (`timer_zero_analysis.hint_fits`, Method 13.2, 14.8): window −13 … +300 s instead of ±300 s; 9 → 3 hints, uptime model unchanged |
| 45 | Postprocessing | Convergence of the uptime model: the boots do not reach a fixed point (210 of 1 087 jitter by a few seconds per iteration, the cut points are stable) | Done 2026-11-05: accepted as it is (Method 19): within the precision classes; guards print the boot movement per iteration, unconverged cut points, round-2 hints and delays near `MAX_DELAY_S` |
| 46 | Postprocessing | Precision classes from one seed; the class `delay` (90 s) not tested since the log-order evidence | Done 2026-11-02 (`validate_model.py`, Method 19.1): seeds 1–4 and test A without log order; pooled 95 %: delay-free 10 s, contacts 14 s, log order persons 23 s / rooms 22 s, delay 51 s (persons 72, rooms 35); classes unchanged, contacts 17 s in one seed |
| 48 | Postprocessing | `erased by reboot` assumed the beacon recorded until the reboot; where it was off (outdoor tags before their activation on 2026-08-15, unused beacons like 69 for 15 days) nothing was erased and its partners have no records either | Done 2026-11-02 (`model_dating.py`, Method 21): erased only where the partners saw the beacon (sightings ≤ 30 min apart), the rest `not seen (off or alone)`; 11 656 h → 8 391 h erased, 3 265 h not seen; `reboots.csv` own records erased 11 777 → 8 476 h. For the evaluation: `not seen` is no evidence of a person without contacts |
| 49 | Postprocessing | `clock_stop_check.py` not idempotent: run again after the pipeline it read the boots after the H4 enforcement | Done 2026-11-02 (Method 19.3): reads `Boot before H4` where present; pipeline results unchanged, reruns byte-identical |
| 47 | Postprocessing / BI | RSSI below the default thresholds on the setup night: until 2026-08-13 10:55 person beacons recorded persons and tags recorded persons below −80 dBm (none from 2026-08-14) | Open: harmless for the contact sync (the symmetric filter caps at −80 dBm); to note in the method doc / BI and for the RSSI filter of the evaluation (ToDo 17) |
| 50 | Postprocessing | Output files that could be combined: three empty `not_dated_*.csv`, the both-sides summary, `contact_sync_readouts.csv` without a reader | Done 2026-11-03 (Method 20): `not_dated.csv`, totals in `both_sides_check.csv`, contact sync readouts retired; 59 → 55 result files, content unchanged. Second step: `lost_records.csv` into `coverage.csv` (columns `Lost from` … `Readout threshold`), the dating's checks (`logger_stalls`, `lost_records_validation`, `both_sides_check`) to `pipeline/quality/`; 54 result files, `Output/data/` only the evaluation data. Third step: `timer_zero_readouts.csv` into `readout_overview.csv` (5 columns added); 53 result files |
| 51 | Postprocessing | End-to-end regression test: today a change is checked by a full run and a byte comparison of all outputs by hand (`tools/compare_outputs.py`) | Done 2026-11-05: `Postprocessing/regression/regression_test.py` (local only, `.git/info/exclude`): `save` / `check` / `run [--save]` against a baseline of SHA-256, CSV headers and line counts |
| 52 | Postprocessing | `timer_step_check.py` calls `uptime_model.contact_boots` without the eco sessions, so its encounter edges include the beacon's own records during them, unlike the uptime model (results unchanged since the eco sessions became a parameter on 2026-11-04) | Done 2026-11-05 (Method 19.2, 20): the eco sessions are passed (each block gets those of its readout); the one step found (15 p2, 8 550 s, explained by its clock stops) stays, its second block now timer 68 150 – 82 550 s (peaks 148 / 79, before 82 550 – 96 950 s, 88 / 37); all data unchanged |

## Details

### 2 – Final observation window (`Postprocessing/Postprocessing Method.md`, 7)

Done: 2026-08-13 18:00 to 2026-08-29 09:00 (the defaults of
`--valid-date-start` / `--valid-date-end`). Since 2026-10-20 the conversion
writes all records whatever their time; the window is applied by the
evaluation.

### 3 – Incomplete readouts (3.2.3, 3.3.2)

Per readout, compare `Contact Count` with the number of contact lines
received. Allow a small excess (contacts recorded during the readout). Flag
readouts with fewer contact lines, and readouts without
`Transfer complete. Disconnecting`.

### 4 – Late line times (3.2.3)

Within one uptime period of a beacon the timer-zero time
(`line time − Current Timer`) is constant. A readout whose timer-zero time is
later than the others of the same period was stamped late by the difference.
Use the earliest timer-zero time of the period as the reference for
converting event times, instead of the readout's own `Current Timer` line.

### 5 – Duplicate contacts (3.1.4)

Remove identical contact records (ID1, ID2, timer, RSSI) that appear in two
**different** readouts of the same uptime period. Do not remove identical
records within one readout; they can be genuine.

### 6 – Data gaps after resets (3.4.1)

For every detected reset (timer-zero jump), mark the interval from the last
readout before the reset to the reset time as missing data for that beacon.

### 7 – Logger outages (3.2.4)

Mark periods without any log lines. Exclude readout intervals that span such
a period from contact rates (in the evaluation, task 19; the coverage marks
them as `read out, not logged`).

### 8 – Reduced or no recording (3.4.4)

Use `eco_sessions.csv` to mark periods of reduced recording per beacon, so
that few contacts in these periods are not read as absence.

### 9 – Full storage (3.4.2)

Flag readouts with `Contact Count` of about 215 000 or more. The beacon's
storage was full, and the oldest contacts of the interval are missing.

### 10 – Lost header (3.3.1, 3.2.3)

Flag readouts that have contact lines but no `Current Timer` of their own.
Before 2026-08-15 18:43 the Base could lose the header; later, data lost on
the logger PC (3.2.3) has the same effect. If the beacon rebooted before
such a readout, its event times are placed too early (see task 14).

### 11 – Development kits (3.5)

Exclude IDs 252–254 (as ID1 and ID2) from the evaluation. They are test
devices, not participants.

### 12 – Beacons without readout (2.1)

A beacon is only read out when its stored contacts reach the Base's minimum
level (button 0: more than 5 000 contacts; button 2: 2 or more; button 3:
1 or more; see the experiment log for which was used). Missing data of a
beacon means "not read out", not "no contacts".

### 13 – Contacts mislabelled by the Base (3.3.1)

Until 2026-08-15 18:43 the Base labelled late messages of beacon A with the
ID of the next beacon B. The existing detector (`Postprocessing/Postprocessing Method.md`, 5) does not find these,
because B's header lies between the two contacts. In readouts before the fix,
check the first contacts after each header: if their timers continue the
previous readout's timers (beacon A) instead of fitting B's `Current Timer`,
reassign them to A (the manual tool `relabel_id_range.py` is retired; an
automatic reassignment would belong to `base_mislabel_check.py`).

### 14 – Reboot check before using another readout's reference (6.3)

The former conversion used, for a readout without `Current Timer`, the most
recent earlier one of the same beacon (or the next later one), which is only
correct without a reboot in between. Done: the dating uses the uptime model,
which assigns such readouts to a period or leaves them undecided (Method 19,
21).

### 15 – Undecided IDs 0–9 / 48–57 (5)

Readouts whose beacon the automatic split could not decide keep their
ambiguous label. IDs 1–9 are valid IDs, so the scripts would process them as
if they were correct. Done: their records get the status `ID undecided
(0-9 / 48-57)` (from `id_correction/id_split_analysis.csv`).

### 16 – Duplicate log files (6.1)

All `*.log` files of the log folder are read. A copy of a file (e.g. names
ending in " 1" or " (1)" in `Original/`) would count every line twice. Detect
files with identical or overlapping content before processing and stop with
an error.

### 17 – Validity checks (6.2)

- RSSI: today only −110 … −10 dBm. Also reject contacts weaker than the
  recording threshold (−80 dBm for person-worn beacons, −100 dBm for
  location tags, unless changed by command).
- Event timer: done, status `invalid: timer after its log line (H1)`; records
  dated outside the hard limits of the data (flashing 2026-08-12 23:00 to
  2026-08-30 00:05) go to `not_dated.csv`.

### 18 – Fake records (3.2.2, 6.4 item 14)

Records built from the bytes of following messages have ID2 values equal to
the character codes of `I` (73), `D` (68), CR (13) and LF (10) and timers
that are mostly implausible. Some pass the range checks. Recognise the
pattern (e.g. timer 524301 = Status message type 0x08, status 0, CR) and drop
these records.

### 19 – Contact rate only over clean intervals

The contact rate is computed in the evaluation from `Output/data/`: the
contacts per day (status `ok`) per beacon, divided by the hours the beacon's
own records exist (`coverage.csv`: state `recorded`, outside eco sessions if
wanted). Intervals that are lost, erased by a reboot, dropped by full
storage, silent (clock stopped) or never read out are not part of the
recorded time. The former `contact_rate_report.py` (`Contact Count` divided
by the time since the previous logged readout) is retired.

### 20 – `resolve-id-mislabel` skill

Retired (2026-10-19) together with the manual ID correction; moved to
`Postprocessing/retired/resolve-id-mislabel-skill/`. Not to be repaired.

### 31 – Speed up the pipeline

Done 2026-10-22, outputs byte-identical to before (`Output/pipeline`, `Output/data`):

- line times parsed once per distinct second (`postprocessing.parse_timestamp`, cached); was most of
  the parsing time, also in the logger clock check (step 1: 16 min → 36 s) and `model_dating.py`;
- fake record check (step 2) and the contact reading of `uptime_model.py` (4× per run, also used by
  `timer_step_check.py`) file by file in 6 processes: 201 → 69 s and about 220 → 37 s per call;
- timer-zero analysis and contact sync in one process on the same lines (one load of the logs,
  about 2 min and 17 GB, instead of two) and the readout split cached between them;
- hard rule check and timer step check only once, on the final model, in parallel with steps 7–8
  (were run in both rounds; nothing in between reads them);
- smaller: contact lines skip the header regexes in `collect_readouts`, `is_valid_beacon_id` cached,
  fake byte streams built with numpy.

Measured run (before → after, s per round): timer-zero + contact sync 931 → 714, uptime model
378 / 448 → 254 / 448, contact sync on the model's periods 521 → 482.

Then done 2026-10-29 (ToDo 40, Method 20): the columnar cache proposed here (`lib/logcache.py`, every
log read once, all steps read the number arrays), batched contact sync matching and edge offsets,
the uptime model's beacons solved in parallel, the dating's array copies removed: full run with
`--reports` 6 262 s → 1 124 s. `reboot_hints.csv` is cleared at the start of each run since
2026-10-30, so a run depends only on the original logs.

Run of 2026-11-02 (1 173 s with `--reports`): steps 1–4 44 s; round 1 420 s, round 2 390 s (each:
timer-zero analysis 10–35 s, contact sync about 80 s, battery check 10 s, uptime model 115 s,
contact sync on the model's periods about 65 s, uptime model again 115 s); checks after round 2
about 110 s; dating about 100 s; reports about 110 s. The four runs of the uptime model are about
460 s, the four contact syncs about 290 s.

Left, and why not done:

- ~~**Checks after round 2 in parallel**~~ Done 2026-11-02: after the H4 enforcement the timer
  step check (reads the periods it rewrites and the clock stops) and the battery check → hard rule
  check (reads the battery check) run in parallel; check phase 120–123 s → 102–106 s (measured
  twice each on the same outputs), outputs byte-identical.
- **Round 2** (about 390 s): runs the whole model again although usually only a few readouts change
  label; it is needed when the second split decides a readout or round 1 found reboot hints (in
  this data at least the hints: 3), and a cheaper partial round would change results. Not worth special-casing
  (2026-10-30).
- **Fewer model iterations** (the second iteration of each loop, or a warm start of the second
  model run of a round from the first, together about 1–2 min): the boots do not converge exactly
  (ToDo 45), so either changes boots by a few seconds. Not done.
