# Postprocessing Method

This document describes what the scripts in `Postprocessing/` do with the
log files and why. The meaning of the log lines and the known issues of the
data are described in `Background Information.md` (repository root);
section numbers written as "BI 3.2.1" refer to it. How to run the scripts and
what they write: `POSTPROCESSING.md`.

| Script | Purpose | Section |
|---|---|---|
| `run_pipeline.py` | From the original logs to the data for the evaluation, in the order the checks depend on each other | 20 |
| `id_split_analysis.py` | Fake records, contacts of the next beacon under the previous ID, automatic split of ID1 0–9 / 48–57, working copies of the logs (alone: pipeline steps 2–4) | 2, 5a, 15 |
| `timer_zero_analysis.py` | Timer-zero time per uptime period, delay and contact accounting per readout | 13 |
| `contact_sync_analysis.py` | Timer-zero times aligned via symmetric contacts; drift per beacon | 14 |
| `base_mislabel_check.py` | Contacts the Base filed under the wrong beacon | 16 |
| `battery_replacement_check.py` | Battery replacements against the boots; resets; periods never read out | 17 |
| `hard_rule_check.py` | The hard rules of the system, checked on a solution | 18 |
| `uptime_model.py` | Uptime periods and boots of all beacons from one model | 19 |
| `validate_model.py` | Synthetic validation of the uptime model | 19.1 |
| `timer_step_check.py` | Two clocks in one uptime period | 19.2 |
| `clock_stop_check.py` | Clock stops: beacon silent, uptime standing still | 19.3 |
| `h4_enforce.py` | H4 as a hard rule on the final model | 19.4 |
| `generate_output.py`, `model_dating.py` | Dating of all records with the uptime model; coverage, lost records | 21 |
| `both_sides_check.py` | Both sides of an encounter as a check of the dating | 21 |
| `data_quality_reports.py` | Quality of the logs | 1, 2, 5, 11 |
| `without_pipeline.py` | The contacts as the logger dated them itself, against the pipeline's | 22 |
| `postprocessing.py` | Shared code: parsing, validity checks, fake records, CSV readers and writers | 1, 2 |
| `logcache.py` | The logs as number arrays, cached: the only code that reads the logs | 1, 20 |
| `study.py`, `study_config.py`, `logger_protocol.py` | The study folder; its values (`study.toml`) and the logger's format (`logger.toml`) | 1, 7 |

Part A (sections 1–7) covers reading the logs and the checks of single
lines; Part B (8–22) the reports, the reconstruction of the beacons' clocks
and the dating. Section 21 describes the data for the evaluation.

The inputs are in the study folder (`--study-dir` of `run_pipeline.py`,
default `../../Studies/DSA2026`): the original logs (`Logs/`), the beacon
lists, optionally the documented battery replacements, `study.toml` (every
value of the study: times, IDs, RSSI limits and thresholds, the beacons'
storage and drift, the logger's ID bug) and `logger.toml` (the logger's
message and line format). The numbers and dates in this document are those
of DSA2026.

---

## Part A – Reading the logs

### 1. Parsing and merging

1. All `*.log` files of the log folder are read: in the pipeline first the
   original logs (`Logs/` of the study folder, never modified),
   then its working copies (`Output/pipeline/logs/`, section 20). Lines that
   do not match the line format (BI 2.6; `logger.toml`) – incomplete lines, debug output,
   corrupted text – are skipped.
2. Where the lines of all files are needed in one sequence (timer-zero
   analysis, contact sync) they are merged and sorted by line time. The sort
   is **stable**: lines with the same time keep their order, so a readout's
   `Current Timer` line stays ahead of the records that use it. A new log
   file always starts a new readout.

If the log folder contains the same log twice (e.g. copies of a file), every
line would be processed twice. `run_pipeline.py`, `generate_output.py` and
`data_quality_reports.py` therefore first check the folder for byte-identical
files and for files that are a copy of the beginning of another file, and
stop if they find any.

### 2. Validity checks of single records

| Check | Rule | Reason |
|---|---|---|
| ID1 | 1–170 or 252–254 | Corrupted bytes, unknown devices (255) |
| Fake record | Contact line built from the bytes of logger messages (below) | Truncated data message filled up with the following messages (BI 3.2.2) |
| ID2 | same rule as ID1 | Corrupted contact records, fake records (BI 3.2.2) |
| RSSI | −110 … −10 dBm | Corrupted contact records |

A record that fails a check is kept in the output with a `Status` (section
21); the uptime model and the checks of its solution leave such records out
of their evidence. Checks that need a time (a record after its own log line,
H1; dated outside the hard limits of the data) belong to the dating (section
21). `data_quality_reports.py` counts the checks above per log file (section
11).

**Fake records** (`find_fake_contacts` in `postprocessing.py`). Consecutive
contact lines of one readout (same file, consecutive line numbers, same ID1)
are put back into the 5 bytes each was decoded from: ID2, timer (3 bytes,
big-endian), RSSI magnitude. If the bytes contain a complete logger message
(`ID` ⟨beacon⟩ ⟨type⟩ ⟨payload of the length the type requires⟩ `CR LF`),
every line touching it is fake, including the mixed line where the real data
ends. The `CR LF` the logger found after the data message can be the end of
the last embedded message, so the search runs with `CR LF` appended. A single
value is no sign (a timer of 524301 is also a genuine uptime of 6.07 days); a
complete message by chance is practically impossible: over all logs about
2 700 message starts (`ID` + valid type) occur by chance, each completed by
`CR LF` at the right place with a probability of about 5·10⁻⁵, i.e. about 0.1
chance hits in total. The fake records found consist of up to 28 consecutive
messages and are the Base's `Status` messages that follow the readout in the
log. The pipeline finds them on the original logs and reconstructs the
messages hidden in them as normal log lines in its working copies (section
20, step 4); the check runs again on the working copies while their cache
is written (`lib/logcache.py`), and `uptime_model.py` leaves out what it finds.

### 3. Time conversion

Replaced. The records are dated with the uptime model (section 21): boot of
the record's uptime period + timer × (1 + drift of the period), plus the
shifts of the period's clock stops. The former conversion with the nearest
`Current Timer` of the beacon (line time taken as the readout time, no
drift) was removed on 2026-10-22.

### 4. Duplicates

- **Contacts:** the same contact (ID2, timer, RSSI) in an earlier readout of
  the same uptime period gets the status `duplicate` (sent twice after a lost
  confirmation, BI 3.1.4); identical records within one readout are kept as
  they are, they can be genuine.
- **Self-reports and eco sessions:** written once per beacon and time.
- **Beacon summary:** read back and updated (section 8).

### 5. Contacts filed under the wrong beacon: adjacent lines

A beacon sends all its contacts in one uninterrupted sequence. A switch to
the next beacon starts that beacon's own, unrelated timer. A pair of lines is
flagged as a possible mislabeling when **all** of the following hold:

1. The two lines are neighbours in the merged line list (section 1).
2. Both are contact lines (`ID2: …, Timer: …, RSSI: …`).
3. Their ID1 differs.
4. `0 ≤ timer of the second line − timer of the first line < 60 s`.

Any parsed line between the two (header, self-report, eco session,
`Transfer complete`, unknown text) breaks the neighbourhood; neighbours can
come from different files; ID2, RSSI, the line-time gap and the validity of
the IDs are not checked. Such pairs are **not removed**: they are listed in
`quality/transfer_mislabel.md` for review and counted in the sanity findings
(`data_quality_reports.py`). The mislabeling caused by the Base before
2026-08-15 18:43 (BI 3.3.1) is not found this way (the new beacon's header
lies between the two contacts); it is the subject of section 16. Genuine
switches between two beacons booted at about the same time are flagged as
well.

### 5a. Contacts of the next beacon under the previous ID

At a change of beacon whose lines in between were not logged (the end of
A's readout with its `Transfer complete`, B's header), the label can switch
some lines after the true boundary. Found once: `dsa_20260822_1818.log`
2026-08-22 18:53:55, 68's readout (`Current Timer` 87 428, contacts up to
timer 83 772) ends in 15 lines labelled 68 with timers 21 041–21 062, which
continue without a gap into 4 750 lines labelled 55 (21 062–27 943, then
55's self-reports and `Transfer complete`). They are 55's: 8 of the 10
beacons in them are in 55's next contacts and none in 68's last ones;
dated as 55 (2026-08-22 16:39) six of them recorded 55 within the hour,
dated as 68 (00:04) none recorded 68 (the nearest: 103, an hour earlier).

Rule (`find_switch_mislabels` in `postprocessing.py`, pipeline step 2): in a
run of consecutive contact lines (no other line between them), the lines of
ID A right before the first line of ID B are B's if their timers drop by
more than 60 s below A's contact before them, do not decrease from there and
continue into B's first timer within 0 … 60 s. The working copies carry B's
ID (its decided label, if the ID split relabelled B); the runs are listed in
`id_correction/switch_relabels.csv`. On all logs the rule finds only the
run above. Not affected: a change of beacon where both timers happen to lie
close together (128 → 127 on 2026-08-13 00:35, 158 → 99 at 09:50, section
5): there A's timers do not drop, both labels fit their own beacon's clock,
and only lines between the two readouts are missing.

Effect (pipeline run 2026-10-24): the 15 contacts are dated as 55 at
2026-08-22 16:39 instead of as 68 at 00:04; 55's lost interval before them
ends at 16:39:04 (before: 16:39:26); 68 has no records left in the night
of 2026-08-21/22 after 23:12 (its coverage there: `read out, not logged`).
`transfer_mislabel.md` lists 2 pairs (before 3).

### 6. Output

The data for the evaluation and its columns: section 21 and
`POSTPROCESSING.md`.

### 7. Settings

The values of a study are in `study.toml` of its study folder
(`lib/study_config.py`), the logger's format in `logger.toml`
(`lib/logger_protocol.py`); the code holds no value of a particular study.
In DSA2026:

| Setting | DSA2026 (`study.toml`) | Used in |
|---|---|---|
| Valid IDs | 1–170, 252–254 (`[ids]`) | 2 |
| Valid RSSI | −110 … −10 dBm (`[rssi]`) | 2 |
| Recording thresholds | location tags −100 dBm, other beacons −80 dBm (`[rssi]`) | 14, 21 |
| Mislabel limit | 60 s (code) | 5 |
| Observation window | 2026-08-13 18:00 – 2026-08-29 09:00 (`[times]`; `--valid-date-start` / `--valid-date-end`) | phases of the findings; applied by the evaluation |
| Hard limits of the data | flashing 2026-08-12 23:00 – 2026-08-30 00:05 (`[times]`) | 19, 21 |
| Delay-free readouts | from 2026-08-29 19:30 (`[times]`) | 13, 18, 19 |
| Base fix | 2026-08-15 18:43 (`[times]`) | 16 |
| ID bug | on: ID1 48–57 logged as 0–9 before 2026-08-15 22:00; witnesses until 2026-08-17 23:00 (`[id_bug]`) | 15 |
| Beacons | storage full from `Contact Count` 215 000; drift −21 s/day, own drift −30 … −15 s/day (`[beacon]`) | 13, 21 |
| Battery replacements | `battery_replacements.csv` (optional) | 17, 21 |

The observation window starts at the deployment deadline: only events from
then on are evaluated. The logs before it (setup night 2026-08-12/13, handout
on the morning of 2026-08-13) are still read by the timer-zero analysis
(section 13), the contact sync (section 14) and the uptime model, where they
improve the boots and drifts. The window ends at 2026-08-29 09:00 (BI,
chapter 0).

---

## Part B – Evaluation steps

### 8. Beacon summary, 9. Current issues

Removed 2026-10-30: `quality/beacon_summary.csv`
(per beacon the last line, last voltage and the fault bits of the last
`Status`, seeded from its own previous output) and `quality/current_issues.csv`
(never seen, not seen within 36 h, last voltage below 2 650 mV, fault bits).
They served the fleet maintenance during the deployment. Afterwards they
said nothing the data does not, and they were not reproducible: the
summary depended on the runs before it, and "not seen" was measured against
the date of the run (after the deployment, all 168 beacons). The voltage of
every readout is in `Output/data/battery.csv` (section 21).

### 10. Readout measurements

Removed 2026-10-28: `quality/measurements.csv`
(voltage, `Contact Count` and timer-zero time = logger time − `Current
Timer` per readout) and the timer-zero times and delta histogram built from
it. Voltage and `Contact Count` per readout are in `Output/data/battery.csv`
at the readout's real time (section 21); the boots in
`Output/data/reboots.csv` and the uptime model (section 19).

### 11. Data quality report (`sanity_findings.csv`)

One row per log file of the working copies (also files without findings),
one column per check that needs no dating: beacon ID out of range, contact
ID2 out of range, RSSI out of range, fake record still in the working copy
(the known ones are reconstructed by the pipeline), possible transfer
mislabeling (section 5). Records with a problem that needs a time get a
`Status` in the data for the evaluation instead (section 21).

### 12. Contact rate

Removed (2026-10-24): `Contact Count`
divided by the time since the previous readout says little where readouts
were interrupted, lost or erased. The contact rate belongs to the evaluation,
from the contacts per day and the coverage (`coverage.csv`: which time of a
beacon is recorded, lost, erased or silent; ToDo 19).

### 13. Timer-zero analysis (`timer_zero_analysis.py`)

Determines the **true timer-zero time** (boot time) of every uptime period of
every beacon, and the logging delay of every readout (BI 2.5, 3.4.3, 3.6).
Run by `run_pipeline.py` (step 5, together with the contact sync on the
same lines), or on its own with `python timer_zero_analysis.py`. Its periods
and timer-zero times are the starting point of the contact sync and the
uptime model (sections 14, 19), which decides the periods itself; its readout
overview (readouts, delay lower bounds, `Contact Count`) is used by the later
steps and the dating (section 21).

**Model.** For every readout n:

```
z_observed(n) = line time − Current Timer
              = z_true + drift × uptime(n) + delay(n)   (± 1 s rounding)
```

- `z_true`: boot time of the uptime period;
- `drift`: the beacon clock runs fast, so the timer-zero time moves earlier
  with uptime; common value −21 s per day of uptime (measured, task 21 of the
  firmware ToDo, `BeaconNRF54/ToDo.md`);
- `delay(n) ≥ 0`: the line can only be stamped late (BI 3.6).

The drift-corrected timer-zero time `z_observed − drift × Current Timer` is
therefore `z_true + delay`: the true value lies at the **lower edge** of the
corrected values of a period.

**13.1 Readouts.** The merged log lines (section 1) are split into readouts:
the uninterrupted lines of one ID1, from `Status`/`Current Timer` to
`Transfer complete` or the next ID. Per readout: line time and value of its
`Current Timer`, first readout of its log file or not, contacts, largest
plausible contact timer (at most 30 days), and the **delay lower bound** from
its contacts: the largest `(contact timer − Current Timer) − (contact line
time − header line time)` over contacts at most 8 h above `Current Timer`, the
longest logging delay assumed (`MAX_DELAY_S`, section 19; larger excesses come
from fake records, BI 3.2.2; up to 2026-11-04 the limit was 1 day). A contact recorded during
the readout cannot be stamped before it was recorded, so the header was at
least that late.

**13.2 Uptime periods.** Per beacon, readouts with their own `Current Timer`
in time order. The *true time* of the previous readout is estimated from the
period's edge so far, `edge + Current Timer × (1 + drift)`; its line time is
not used, because it can be late. Readout n starts a new period if:

| Rule | Condition |
|---|---|
| b | `Current Timer` decreased |
| a | Its corrected timer-zero time lies after the previous readout's true time (booted after it) **and** more than 1 h after the edge |
| a′ | Booted after the previous readout, but within 1 h of the edge: a reboot only if the next 2 readouts also lie after the previous readout's true time; otherwise a late readout |
| a″ | As a′, but confirmed by the contact sync: `reboot_hints.csv` holds a boot of the beacon that the readout's corrected timer-zero time fits — at most 5 min after it and not before it beyond 13 s (`hint_fits`; a readout is not logged before it happens; until 2026-11-02 ±5 min) (14.8). The contacts do not depend on the logging delay, so a readout that agrees with them was not late |

**13.3 Drift per period.**

| Situation | Drift used |
|---|---|
| Period contains readouts after 2026-08-29 19:30 (delay-free anchors, BI 3.2.3) and at least 3 days of uptime before them | Beacon's own drift: the steepest line through the anchor that no earlier readout falls below |
| No anchor; period spans at least 3 days of uptime with at least 5 readouts | Beacon's own drift: the best line below all readouts (lower convex hull, smallest sum of distances) |
| Otherwise | Common −21 s/day |

An own drift outside −30 … −15 s/day is not used; the period gets the
common −21 s/day instead and is marked "common (… outside the plausible
range)". Outside this range the fit follows the delays rather than the clock:
the delays grew over the deployment, so in short periods with few on-time
readouts the lowest points lie at one end only (fits of −530 … +730 s/day,
positive values being physically impossible). The points
used are the readouts' latest possible timer-zero times (observed minus the
delay lower bound). Reason: the drifts of the beacons differ by about
±2 s/day; with the common value, a period of 15 days can be off by up to about
30 s. The contact sync (14.7) measures each beacon's drift from the contacts
and replaces these values for all periods it aligns.

**13.4 Edge (true timer-zero time).** Per period, with its drift:

- with delay-free anchors: the mean of their corrected timer-zero times;
- otherwise: the minimum over all readouts of (corrected timer-zero time −
  delay lower bound). If the bound lowered the edge, the period is marked
  "minimum, lowered by the contact bound".

`Delay` of a readout = corrected timer-zero time − edge.

**13.5 Readouts without their own `Current Timer`.**

| Case | Treatment |
|---|---|
| Previous and next readout of the beacon lie in the same period | That period's timer-zero time applies |
| A reboot lies between them, or one of them is missing | The largest contact timer is about the beacon's uptime at the readout, because contacts are recorded up to the readout. For each candidate period, the uptime at the readout's line time is `(line time − edge) / (1 + drift)`. A candidate fits if the largest contact timer is not above that uptime and at most 6 h below it (line time late, last contact some time before the readout). Exactly one fitting candidate: the readout is assigned to it. None or both: no timer-zero time |

**13.6 Checks and flags.**

| Flag | Meaning |
|---|---|
| below the edge | Delay < −2 s: impossible with delays ≥ 0 |
| delay below the lower bound from the contacts | Contradicts the contacts |
| first readout in file is delayed | Delay > 60 s for a readout that should be on time |
| delay-free readout far above the edge (period: persistent step) | A first-in-file or anchor readout more than 1 h above the edge: the period's readouts do not fit one continuously running beacon (exchanged tag, two tags with one ID, wrong ID) |
| long logger stall (period) | 3 or more consecutive readouts more than 1 h late; information only |
| `Current Timer` decreased without boot after previous readout | Impossible for one continuously running beacon |
| possible reboot undecided / late readout looked like a reboot | Rule a′ could not be decided / resolved as a late readout |
| single readout in its uptime period | Timer-zero time cannot be checked |

**13.7 Output.**

| File | Content |
|---|---|
| `readout_overview.csv` | One row per readout in log order: number, log file, ID, start and end time, `Current Timer` value and presence, `Transfer complete` presence, expected and received contacts with their accounting (13.8), self-reports, delay, timer-zero time (13.4, 13.5); voltage, status, eco sessions, first/last line, first in file, period and its size, source of the timer-zero time, delay lower bound, state of the automatic ID split of IDs 0–9 / 48–57 (section 15); line and line time of the `Current Timer`, observed timer-zero time, reason for a new period, flags (until 2026-11-03 in `timer_zero_readouts.csv`) |
| `timer_zero_periods.csv` | One row per period: first and last readout, number of readouts, true timer-zero time, edge source, drift and its source, largest delay, number of first-in-file readouts, flags |

**13.8 Expected and received contacts.** Per readout with `Current Timer`,
`Contact Count` is the number of contacts stored at its start (BI 2.4). A
readout that stops early keeps the rest on the beacon, which sends it with
the next readout (BI 2.1, step 4); contacts confirmed by the Base but not
written by the logger are lost (BI 3.2.3); a reboot deletes everything still
stored (BI 3.1.1).

| Column | Meaning |
|---|---|
| Expected contacts | `Contact Count` |
| Received contacts | Contact lines of the readout |
| Received stored contacts | Those with timer ≤ `Current Timer` (recorded before the readout; comparable with `Contact Count`) |
| Recorded during the readout | Those with timer > `Current Timer` |
| Not received | Expected − received stored contacts; negative: more arrived than were stored |
| Received in later readouts | Contacts of later readouts of the same uptime period with timer ≤ this `Current Timer`: left on the beacon and sent later |
| Lost at most | Not received − received in later readouts |
| Lost at least | Not received − (`Contact Count` of the next readout of the period − its contacts recorded after this readout); if the beacon rebooted before its next readout: all not received |
| Note | Reboot before the next readout, next readout without header, no later readout, more received than expected |

Contacts sent twice (BI 3.1.4) count as received each time. Readouts without
`Current Timer` have no `Contact Count`; their contacts count as received in
later readouts of the period they were assigned to (13.5).

The dating (section 21) computes the same accounting again on the uptime
model's periods (`Model.set_accounting`): "later readouts of the period" and
"rebooted before the next readout" as the model has them (2026-10-30; the
columns of `readout_overview.csv` stay on the timer-zero analysis' periods,
which differ from the model's at 12 of 8 418 consecutive pairs of readouts).

**13.9 Settings** (constants in `timer_zero_analysis.py`).

| Setting | Value |
|---|---|
| Common drift | −21 s/day |
| Own drift: minimum span / readouts, plausible range (outside: common drift) | 3 days / 5 readouts, −30 … −15 s/day |
| Reboot margin (rule a) | 1 h |
| Following readouts for rule a′ | 2 |
| Delay-free from | 2026-08-29 19:30 |
| Rounding tolerance | 2 s |
| Contacts used for the delay lower bound | at most 8 h above `Current Timer` (`MAX_DELAY_S`) |
| Plausible contact timer / largest gap for header-less readouts | 30 days / 6 h |
| Long logger stall | 3 readouts more than 1 h late |

### 14. Contact sync (`contact_sync_analysis.py`)

Aligns the timer-zero times of all uptime periods (section 13) with each other
using the contacts, independently of the logging delay.

**Idea.** A contact record carries the timer of the beacon that recorded it.
When A sees B, B usually sees A in the same minutes (both scan 120 ms every
7 s, BI 2.1). Every encounter is therefore recorded on two clocks. Mapped to
real time with the timer-zero times of the two uptime periods, the encounters
only line up if the *difference* of the two timer-zero times is right. Contact
timers are beacon-clock values, so the logging delay does not enter. One
constant for all periods remains; it is fixed by the delay-free readouts.

**14.1 Units.** Every uptime period of section 13, and every readout without
own `Current Timer` that got no timer-zero time there (13.5), as a unit of its
own (start value: start time − largest contact timer). Contacts with a timer
above `Current Timer` + 1 h are not used.

**14.2 Encounter transitions.** Records a beacon made during its own eco
sessions are left out (BI 2.3: one scan per 300 s, its encounter starts and
ends are off by up to that much; the partners' records of it stay). A session
belongs to the period of the readout that delivered it, unless its leave
timer lies above that readout's `Current Timer` + 1 h (recorded before a
reboot, kept in flash): then it is skipped. The same filter applies to the
contact boots of the uptime model (section 19). Only records the other side records as well:
RSSI at least the stricter of the two recording thresholds (BI 2.1;
`pp.symmetric_threshold`). A person-worn beacon records a location tag down to
−100 dBm, the tag records it only down to −80 dBm; the weaker records of such
a pair have no counterpart, so the person's encounter with the tag would
start earlier and end later than the tag's with the person, and the edges
would not match (2026-08-20: 62 % of the records of location tags were weaker
than −80 dBm). Location tag – location tag (−100 dBm both ways) and person –
person (−80 dBm both ways) are symmetric already. The same filter applies to
the contacts of the uptime model (section 19: contact boots, partners'
records, timer step check) and to the partner records of the lost-records
check (section 21); the delay lower bound and the upper limit of header-less
readouts use all records. Effect (2026-10-21, full pipeline against the run
before): delay-free readouts fit the synced zero times to 1.8 s median
instead of 3.7 s (76 of 78 within ±30 s, before 69 of 73); drift matches
325 028 → 358 871, beacons with drift reliability > 0.8 97 → 121; reliable
contact boots of the uptime model 2 590 → 2 725; H4 in the deployment 40 → 38,
on the setup day 34 → 23; same cut points, period boots moved by a median of
0 s (room tags, 99 % within 9 s) and 1 s (persons, 99 % within 12 s), 2
periods by 61 s. Per unit and partner beacon, the contacts on
real time in 10-s bins; gaps up to 120 s belong to the same encounter. Used
are the starts and ends of the encounters, without the first start and the
last end (they only mark the data coverage). Continuous presence (e.g. two
location tags in range of each other all the time) carries no timing
information.

**14.3 Pair offsets.** For two units of beacons A and B: the starts (ends) A
recorded of B against the starts (ends) B recorded of A; the densest ±30 s
point of all differences is the offset of the two timer-zero times.
Pass 1 searches ±8 h and keeps only strong peaks (≥ 12 matching transitions,
weaker peaks over ±8 h are mostly coincidences of daily routines); pass 2
searches ±15 min around the pass-1 solution (≥ 5 matches).

**14.4 Solve.** Iterated weighted median of the offsets implied by the
partners (robust against wrong pairs), then Huber least squares on the pairs
within 10 min. A unit is synced with ≥ 2 pairs within 2 min of the solution
and ≥ 50 % agreement (weighted).

**14.5 Absolute level.** Median over the delay-free readouts (first readout of
a log file, readouts after 2026-08-29 19:30) of the synced units.

**14.6 Units without enough pairs.** All transitions of the unit against all
synced partners (on their new time) in one histogram, ±8 h; accepted with
≥ 8 matches from ≥ 2 partners.

**14.7 Drift per beacon.** The common −21 s/day (13.3) is replaced by each
beacon's own drift, measured from the contacts. A wrong drift makes a unit's
encounters run away from its partners' with growing uptime; the slope gives
the drift difference.

1. All aligned units (14.4, 14.6): every encounter start (end) A recorded of B
   is matched to the nearest start (end) B recorded of A within ±30 s (first
   round ±60 s), and one Huber least-squares fit over all matches gives a zero
   correction per unit and a drift per **beacon** (the crystal belongs to the
   beacon: units of the same beacon across battery changes agree to 0.15 s/day,
   beacons differ by up to 4 s/day). The delay-free readouts (14.5) enter as
   equations: the contacts fix the drifts only relative to each other, the
   readouts the common level.
2. The matches for the drift come from a fit with free drifts. With the common
   drift, the matches of a beacon whose clock deviates leave the window late in
   its uptime, which pulls its drift to the common value.
3. Raw drifts of beacons with few or short encounters are noisy. Noise per
   beacon `s = k / √info`, with `info` the sum of the squared deviations
   (days²) of its matched timers from their unit's mean. `k` and the true
   spread `τ` of the drifts are calibrated in every run from two fits, on even
   and on odd days: `k` from the differences of the two, `τ` from their
   covariance over the beacons with noise < 0.7 s/day in both halves.
4. Each drift is shrunk towards the centre of these beacons:
   `drift = centre + (raw − centre) × τ² / (τ² + s²)`. Beacons with long
   encounter histories keep their own value; beacons with little data stay
   near the centre.
5. With these drifts fixed, the matches and the zero times are solved again.

Validation (2026-10-14): drifts fitted on even days predict those on odd days
to 0.35 s/day median (common drift: 1.11); the per-period drifts from the
readouts (13.3, independent of the contacts) differ from the contact drift by
0.41 s/day median (common drift: 1.06); the delay-free readouts fit to
2.4–3.4 s median instead of 3.7–4.2 s. The zero times move by a few seconds
(at most about 30 s).

**14.7a Drift on the uptime model's periods (`--model-periods`).** The
units of 14.1 are the periods of the timer-zero analysis, which can mix two
boots (the conflicts of 14.8); their matches can bias a beacon's drift. After
the first solve of the uptime model, the drift fit (14.7) runs again on the
model's periods and boots (`uptime_model_periods.csv`, `uptime_model_readouts.csv`,
header-less readouts as assigned, own periods as fitted); only
`contact_sync_drift.csv` is written, and the model is solved again with it
(section 20, step 5). First run (2026-10-21, on the outputs of the previous
pipeline run): 168 beacons with a drift (before 158), 131 with reliability
> 0.8 (121), delay-free readouts 1.6 s median residual (103 readouts);
24 drifts change by more than 1 s/day, mostly former outliers moving to the
centre (144 −14.2 → −21.4, 92 −27.0 → −20.2, 47 −15.5 → −21.6, 35 −15.0 →
−20.7, 44 −16.1 → −20.3 s/day); 215 689 records left out as made during the
beacon's own eco sessions (2 275 sessions).

**14.8 Checks.**

| Check | Consequence |
|---|---|
| A readout of the period (own `Current Timer`) gets a delay more than 60 s below its lower bound from the contacts (13.1): the new timer-zero time is later than its readouts allow | Conflict, old timer-zero time and drift kept; the period probably mixes two clocks |
| 10–60 s | Timer-zero time moved to the readouts' edge |
| Readout without own `Current Timer`: 99 % of its contact timers more than 2 min above the uptime at the readout | Conflict, no timer-zero time |

**Reboots merged into a period.** A conflict period holds a second boot when
one of its readouts has a corrected timer-zero time that fits the boot the
contacts give (at most 5 min after it, not before it beyond 13 s, as rule a″):
that readout was not late, it belongs to a later boot
that rule a′ could not confirm (reboot within 1 h of the edge, too few
following readouts; e.g. the restart of a room tag in the setup night). The
boot is added to `reboot_hints.csv`; the timer-zero analysis splits the
period there (rule a″) in its next run: in the pipeline, round 1 finds the
hints and round 2 uses them. Since 2026-10-30 `run_pipeline.py` clears the
file at the start of each run; before, it was kept from run to run, so a run
depended on the runs before it (recheck, end of section 20). Round 2 runs
when the second split decides a readout or round 1 found hints (before, only
in the first case: with no new label the hints would have stayed unused).
Hints the contact sync of round 2 finds would need a third round, which is
not run: `run_pipeline.py` prints a warning with them (2026-11-02; none in
this data).

One-sided window (2026-11-02). The window was ±5 min; but a readout whose
timer-zero time lies before the boot would have a negative delay under it.
6 of the 9 hints were "supported" by such readouts, 64 – 292 s before the
boot (112, 134, 135, 141, 156, 157, all on the setup night 2026-08-13): the
contact boot of a period that mixes two clocks is off by minutes, and the
readout belongs to the period's first boot. With the one-sided window 3
hints remain (5 on 2026-08-25, 6 on 2026-08-27, 129 on 2026-08-13; readouts
0, +269 and +97 s after the boot). Effect: the uptime model and everything
after it unchanged byte for byte — the 6 hints only split periods of the
timer-zero analysis, which the model cut the same way itself.

**14.9 Output** (`timer_zero/`).

| File | Content |
|---|---|
| `contact_sync_periods.csv` | Per unit: old and new timer-zero time, shift, old and new drift, method (pairs / all partners / not synced), agreement, status |
| `contact_sync_drift.csv` | Per beacon: matches, info, raw drift, noise, reliability `τ²/(τ²+s²)`, drift and its uncertainty |
| `reboot_hints.csv` | Boots confirmed by the contact sync, for rule a″ of the timer-zero analysis (13.2): beacon, boot, period when found, supporting readout, evidence. Kept for the rest of the run (cleared by `run_pipeline.py` at its start) |

### 15. Automatic ID split (`id_split_analysis.py`)

Until 2026-08-15 22:00 the logger wrote ID1 48–57 as 0–9 (BI 3.2.1). The
script decides for every readout of the **original** logs (folder
`Original/`) logged under ID1 d (0–9) before that time whether it is beacon d
or d + 48, and writes corrected copies. Used are the original logs up to
2026-08-17 23:00: ID2 is always correct, ID1 after the cutoff too.

**15.1 Readouts.** Per file, split as in 13.1. A readout is *disputed* if its
ID1 is 0–9 and it starts before the cutoff.

**15.2 Evidence**, strongest first; each step uses the decisions of the
earlier ones:

| Step | Evidence | Decides |
|---|---|---|
| A1 Self-exclusion | A beacon never records itself: own contacts with ID2 = d → the readout is d + 48; with ID2 = d + 48 → it is d | Any count, if the two counts differ |
| B Timer zero | Timer-zero time of the readout against the known zeros of both candidates (decided readouts, and readouts of both IDs after the cutoff). The logged zero lies at or after the true one (delay ≥ 0): it fits a known zero z′ if z′ − 60 s ≤ zero ≤ z′ + 300 s | Exactly one candidate fits |
| A2 Witnesses | Per own contact (partner Y, real time T): did Y record ID2 = d or d + 48 within ±10 min of T? Y must be reliably labelled (outside 0–9 / 48–57, or read out after the cutoff) | ≥ 3 exclusive hits, ≥ 60 % for one side; only undecided readouts |
| B again | With the new decisions | |
| C Contact timers | No `Current Timer`: the largest contact timer fits the uptime of one candidate only (≤ uptime + 2 min, ≥ uptime − 6 h) | |
| D Neighbour | Mostly stubs (a single `Status` line): label of the nearest decided readout of the pair | Within 15 min, the other candidate's at least 15 min farther |

Witnesses come after A1 and B: when d and d + 48 were close to each other
(handout on 2026-08-13) they see both and contradict the self-exclusion in
about half of the readouts; in the hand-cleaned files they were wrong in 2 of
81 readouts, the self-exclusion in none of 61.

**15.3 Check against the hand correction** (BI 5, six `_cleaned` files; a
one-time check, the hand-cleaned logs and the comparison are no longer part of
the postprocessing). The
original and hand-cleaned lines are paired on time stamp and record (a line
removed by hand is skipped). Result (2026-10-15): all 87 decided readouts
with contacts or `Current Timer` agree (53 changed, 34 kept); 16 of 8.8
million lines differ, all stubs: 5 where the analysis finds 48–57 and the
hand cleaning kept 0–9 (nearest readouts of the high ID within minutes, of
the low ID hours away), 11 that stay undecided.

**15.4 Output** (`id_correction/`).

| File | Content |
|---|---|
| `id_split_analysis.csv` | Per disputed readout: file, line range, start time, logged ID1, `Current Timer`, contacts, decided ID1, method, evidence |
| `switch_relabels.csv` | Contacts of the next beacon under the previous ID (5a) |

The working copies of the logs with the decided IDs are written in the
pipeline's step 4 (section 20); `id_split_analysis.py` run on its own does
steps 2 to 4 with the same code. The split runs only if the study's logger
had the bug (`study.toml` `[id_bug]`): with `enabled = false` no readout is
disputed.

### 16. Base mislabelling check (`base_mislabel_check.py`)

Until 2026-08-15 18:43 the Base labelled every forwarded message with the ID
of the beacon it had connected to last (BI 3.3.1): a message of beacon A still
in transit when the Base connected to beacon B was logged under B's ID, right
after B's header. The check runs on all logs (no time limit) and tests every
readout (split as in 13.1): in the pipeline on the working copies, with the
decided IDs 0–9 / 48–57 (step 8, `quality/base_mislabel_check.csv`), and in
`data_quality_reports.py` on the uncorrected logs in `Original/`
(`quality/base_mislabel_check_original.csv`):

| Test | Finding |
|---|---|
| T1 Timer above `Current Timer` | Contacts more than 600 s above the readout's own `Current Timer`, grouped into consecutive blocks. Classified: *continues the previous readout* (first timer between the last contact timer and the `Current Timer` + 600 s of the previous readout of another beacon in the file: Base mislabelling); *fake record pattern* (a timer of 524301, the bytes of a `Status` message, or above 30 days; BI 3.2.2); *recorded during a long readout* (excess covered by the logged time since the header + 600 s); *not explained* |
| T2 Self-sighting | Contacts with ID2 = ID1: a beacon never records itself. Not testable for ID1 0–9 before 2026-08-15 22:00 in the uncorrected logs (it can be beacon d + 48, which may see d); counted separately |
| T3 Contacts before the header | Contact lines between `Status` and `Current Timer` of the same readout |
| T4 Header-less readout after another beacon | Readout without `Current Timer` right after another beacon's readout, whose first timers continue that readout. The readout's uptime comes from its timer-zero time (13.5, `readout_overview.csv`): timers above it are a Base mislabelling; timers within it fit the beacon itself (similar uptimes); without timer-zero time the case stays open |
| T5 ID switch between two consecutive contact lines | No line between A's and B's contacts. The part after the switch is B's own (A's end and B's header lost, BI 3.2.3) or A's contacts under B's ID. Decided by self-exclusion (sightings of A and none of B: B's own; of B and none of A: A's), else by B's uptime (timer-zero time), else by A's timers and `Contact Count` (A's contacts before and after the switch add up to it) |
| T6 `Transfer complete` under another ID | Directly after another beacon's lines. Before the Base fix: A's end of transfer filed under B (the old Base wrote it with the current ID) or B's readout lost except its `Transfer complete`; after it: data loss only. No contacts affected |
| T7 Fake records | Consecutive contact records of a readout put back into their 5-byte form (ID2, timer as 3 bytes big-endian, RSSI magnitude) and searched for complete logger messages: `ID` ⟨beacon⟩ ⟨type⟩ ⟨payload of the length the type requires⟩ `CR LF`. Every record that touches such bytes is fake (BI 3.2.2: a truncated data message filled up with the following messages, mostly the Base's `Status` messages). Records that pass the validity checks and are not above `Current Timer` are listed as lines to drop. A value alone is no sign: a timer of 524301 (`08 00 CR`) is also a genuine uptime of 6.07 days (location tags) |

Every finding gets *Cleaning needed*: **yes** (lines under the wrong ID),
**drop** (fake records that pass the validity checks: valid ID2 and RSSI,
timer not above `Current Timer`), **check** (undecided), **no**. In the
pipeline the fake records are reconstructed in the working copies (section
20, step 4); any left get the status `fake` (section 21).

Sensitivity of T1: a foreign block exceeds the receiving beacon's `Current
Timer` when the previous beacon has the larger uptime. The number of such
readouts is reported per phase (before the deployment, deployment before the
Base fix, after it).

Result (2026-10-15):

- No contacts under a wrong ID in any phase. Deployment before the Base fix:
  no finding (306 readouts where T1 would have detected a foreign block).
- T5: 220 ID switches (61 before the deployment, 159 after the Base fix,
  none in between); all are the next beacon's own contacts with A's end and
  B's header lost (self-exclusion, uptime, or timers not continuing A).
- T7: fake records also after the logger fix of 2026-08-15 13:53 (the fix
  only catches the framing error, BI 3.2.2). In the deployment 69 fake
  records in 3 blocks (beacon 22 on 2026-08-26 22:51, 37 and 54 on
  2026-08-29 00:03 and 02:32), built from 48 `Status` messages; 18 of them
  pass all validity checks and must be dropped (including the mixed record at
  the start of each block, whose last bytes belong to the next message).
- Before the deployment: 63 fake records, 4 to drop; 3 T6 and 1 T1 case to
  check (setup night, handout).

**Output:** `quality/base_mislabel_check.csv`, one row per finding: readout
(file, lines, start time, phase, ID1, `Current Timer`, contacts, `Transfer
complete`), test, contacts affected with their lines and timers, the
previous readout (ID, `Current Timer`, last contact timer, lines), assessment, cleaning needed, lines to drop.

### 17. Battery replacements and resets (`battery_replacement_check.py`)

A battery replacement restarts the beacon (BI 3.4.1): it starts a new uptime
period whose boot lies at the replacement. The documented replacements
(`battery_replacements.csv`) are checked twice in the pipeline (section 20):
before the uptime model, which needs their windows (H7), and with
`--model uptime` on the final model after the H4 enforcement; the outputs of
the second run are the ones the dating (`unread_uptime_periods.csv`) and the
hard rule check read (since 2026-10-30; before, the dating used the periods
never read out from the boots before the model, which differed from the
model's by up to 10 s). They are checked against:

- the boot of each uptime period: of the final uptime model, or in the first
  run corrected by the contact sync (section 14) where available, otherwise
  from the readouts (section 13);
- the readouts with `Current Timer` of the beacon, at their real time (the
  model's, or line time minus the logging delay of section 13);
- the sightings of the beacon by other beacons and its own contacts, each
  converted with the corrected boot and drift of the recording beacon's uptime
  period. During a replacement the beacon is off: the others lose it shortly
  before the boot and see it again right after.

Window of a replacement: the documented time with its accuracy, plus 5 min
before and 15 min after.

| Verdict | Condition |
|---|---|
| match | A boot in the window |
| late | A boot up to 75 min after the window. If the beacon still recorded contacts in its old uptime period after the documented time, it was changed later; otherwise the data cannot tell this from a second boot that followed the change before any readout |
| change without a readout | No boot in or after the window, but the last readout up to the window end belongs to an older boot and the next readout to a later one. The replacement started an uptime period that was never read out before the beacon rebooted again; the beacon's own contacts of that period are lost (BI 3.1.1). Its start is the largest gap in the others' sightings within the window after the last readout |
| beacon silent | The others do not see the beacon after a time more than 1 h before the window (defective) |
| no match | None of these |

**Resets** (until 2026-10-30, `beacon_resets.csv`, `reset_histogram.png`;
removed, replaced by `Output/data/reboots.csv`
of the dating, section 21, on the final model). Every boot in the observation window is listed with what explains
it: a battery replacement, the reset after a replacement whose uptime period
was never read out, the first boot of the beacon, or a *reset without a
documented battery replacement*. For each boot the time since the beacon's
last readout is given: the contacts it stored in that time are erased by the
boot (BI 3.1.1). The encounters themselves are not lost: a reboot takes only
seconds, the neighbouring beacons keep recording the beacon and hold the
encounter from their side (BI 2.1) — as long as the beacon ran until the
reboot; where it was off before (not yet activated, unused, battery out) its
partners have no records of it either and nothing was erased (since
2026-11-02 `not seen (off or alone)`, section 21). For the same reason the others' sightings
cannot show a reboot; they show a battery replacement only because the beacon
is off for the swap.

Result (2026-10-16): 34 replacements: 23 match, 5 late, 5 changes without a
readout (4, 37, 60, 24, 36; 10–45 h never read out each), 1 beacon silent
(39, last seen 2026-08-20 15:17). 812 boots in the observation window, 778 of
them resets without a documented replacement on 108 beacons (median 7 per
beacon, nearly all worn beacons). Own records erased by the boots (last
readout to boot, without double counting): about 9 700 h of the beacons' own
recording; the encounters remain in the partners' records.
By beacon type: worn by persons 96 beacons, 759 reboots (median 8 per
beacon, up to 16); room tags 66 beacons, 11 reboots (median 0); replacement
beacons (in neither list) 3 beacons, 13 reboots.

**Output** (`quality/`):

| File | Content |
|---|---|
| `battery_replacement_check.csv` | Per documented replacement: window, verdict, boot (corrected) and its period, when the others last saw the beacon and saw it again, the largest gap in their sightings in the window, own contacts in the old uptime period after the documented time, last readout before and next readout after, note |
| `unread_uptime_periods.csv` | Uptime periods started by a replacement and never read out: beacon, from, to, hours, cause, last readout before, next readout after, sightings of the beacon by others in between (the encounters can be reconstructed from the other side, BI 2.1) |

### 18. Hard rules (`hard_rule_check.py`)

Every solution for the uptime periods and boots must obey rules that follow
from how the system works; the soft evidence (delay distribution, contacts,
reboot frequency) may only choose between solutions that do. The check tests
a solution: by default per period the boot and drift of the contact sync
where applied, otherwise of the timer-zero analysis; in the pipeline the
final uptime model (`--model uptime`, section 19). For a readout r of beacon b in
period k, R_r = B_k + C_r (1 + d_b) is the real time at which the beacon read
its uptime; tolerance ε = 13 s (rounding, precision of the boots) + 1.5 s per
day of uptime (drift).

| Rule | Statement | Basis |
|---|---|---|
| H1 | Nothing is logged before it happens: the real time of every header and contact is not later than its line time (+ ε) | BI 2.6, 3.6 |
| H2 | One readout, one clock: `Current Timer` and contacts of a readout share the boot (not self-reports and eco sessions, which survive a reboot) | BI 3.1.1 |
| H3 | Uptime only grows: within a period `Current Timer` does not decrease | BI 3.4.1 |
| H4 | Readouts happen in log order, across all beacons: one Base, one connection at a time, the order is kept up to the log (enforced on the final model, 19.4) | BI 2.1, 3.2.3 |
| H5 | A new boot lies after the real time of the previous period's last readout | BI 3.4.1 |
| H6 | Delay-free readouts (first of a log file, after 2026-08-29 19:30) are logged at their real time (delay 0 … 10 s ± ε, 19). First of a file: the very first readout, also if it has no `Current Timer` (a file starting in the middle of a readout: that readout's `finished` starts the first pause, so the next one is not delay-free) | BI 3.2.3, 3.6 |
| H7 | A documented battery replacement has a boot in its window (also a period without readout) | `battery_replacements.csv` |
| H8 | Uptime starts at the boot (covered by H1 for the contacts) | definition |
| H9 | No boot before the beacons were flashed (2026-08-12 23:00) | flashing |

H2 holds by construction. Fake records and contacts that fail the validity
checks are not checked. H4: a single readout with a wrong real time would make
many others look wrong, so the violators are the readouts outside the longest
non-decreasing sequence of R in log order (compared in 1-min steps): the
smallest set whose removal restores the order. A period with all its readouts
outside has a shifted boot; one with only some outside probably mixes two
boots.

Result (2026-10-16), deployment: H4 174 of about 8 600 readouts in 93
periods (30 of them with only some readouts outside, among them the conflict
periods 5 p9, 76 p7, 6 p13); H5 5 periods; H6 1 readout (after the
deployment); H7 the 5 late battery replacements; H1 none. H1 to H3 and H6
hold for the rest; H4 holds for 98 % of the readouts.

**Output:** `quality/hard_rule_check.csv` (`--model uptime`:
`quality/hard_rule_check_uptime_model.csv`), one row per violation: rule,
beacon, period, readout (file, line, line time), value, limit, excess,
phase, note.

### 19. Uptime model (`uptime_model.py`)

Decides the uptime periods and their boots in one step for all beacons, by a
general rule instead of thresholds per case. Per beacon, its readouts with
`Current Timer` in log order are cut into periods; each period gets a boot.
Among all cuttings and boots the model takes the one with the lowest cost.

**Evidence per readout.** Observed timer-zero time z = line time −
`Current Timer` × (1 + drift); delay lower bound from the contacts recorded
during the readout (fake records and invalid contacts left out); the **boot
from its own contacts**: its encounter edges against the edges its partners
recorded of it, on the partners' current boots (as in 14.2–14.3, per readout
instead of per period). Such a boot is reliable when its peak has at least 15
matched edges and is at least 1.5 times the second highest peak (a spurious
peak from daily routines has rivals of similar height; room tags rarely
reach it).

Since 2026-10-20 the search covers only the physically possible boots: not
before the flashing (H9) nor before the boot of the period before (daily
routines give look-alike peaks about a day apart), not after the readout's
own upper limit (H1: line time of `Current Timer` − `Current Timer` + eps).
Reliable additionally requires the peak to be at least 4 times the
background, the mean count per ±30 s window in that range: on the setup day,
with all beacons together, 19 chance matches had passed the fixed threshold.
Trigger: 25 at 2026-08-13 12:14:08 (`Current Timer` 7 938, boot at most
10:01:50) had a "reliable" contact boot at 15:21, three hours after the
readout; now 10:00:20 (not reliable), and 144 p1 (−6 min) is no longer
reliable either. Effect: reliable contact boots 2 547 → 2 590; both-sides
check unchanged (median 6 s, 90 % 51 s); synthetic validation unchanged
(injected reboots found 96–100 %, delay-free readouts 7 s / 18 s); H4 in
the deployment 40, on the setup day 34 (before 10; all ≤ 67 s, median 19 s).
The validation now moves the bounds from readouts without `Current Timer`
with an injected reboot (otherwise they forced impossible solutions).

**Tested and switched off: the partners' records as boot evidence for all
readouts** (`USE_RECORD_EVIDENCE`, 2026-10-20; the code was removed on
2026-11-05, entry of that day in section 20). For readouts with `Current
Timer` but without a reliable boot from encounter edges, the boot from the
partners' records (the match used for the readouts without `Current Timer`,
strict: peak ≤ 2 min wide, ratio 1.3, ≥ 30 matches, 4 × background) was
added as evidence. 570 readouts got such a boot (332 on the setup day); it
lay a median of 30 s and in a quarter of the cases more than 3 min before the
model's boot, 94 by more than 5 min. Result: H4 in the deployment 40 → 74,
on the setup day 34 → 245; both-sides check unchanged (median 6 s, 90 %
51 s); delay-free readouts 7 s / 18 s → 9 s / 19 s. The record match mostly
sees co-presence, not the moment of an encounter's start or end: it is too
coarse as evidence for a boot and is used only to decide between candidate
periods (readouts without `Current Timer`).

**Hard rules (section 18) as the strongest costs.** H1 (boot not after any
readout's upper limit), H5 (boot after the previous period's last readout),
H6 (delay-free readouts) and H7 (no period across a battery window; the
replacement may start a period without readout) cost 1 000 + 1 per 6 s of
violation, more than all evidence; H3 (`Current Timer` falling within a
period) is absolute. A solution therefore always exists; a violation remains
only where the data contradicts itself.

**Costs.** Delay: Σ log(1 + |delay| / 60 s) (absolute value since 2026-10-25, below). Contact boot: per reliable readout
min(log peak, 5) × min(((boot − its contact boot) / 20 s)², 16). H4: per
readout, against the nearest readouts of other beacons before and after it
in log order that lie on the longest log-ordered sequence of real times (the
consensus of all beacons, in 1-min steps, as in the check of section 18),
2 × min((violation / 60 s)², 16), violation beyond the timer precision eps.
(First version: the median of the 3 readouts of other beacons logged right
before and after it. Where readouts are sparse, e.g. at the start of a log
file, that median reached into the next hour and missed 14 min of delay.) Reboot:
8 per period boundary for worn and replacement beacons, 30 for room tags.

**Checks of the reboot penalty and the drift (2026-10-25/26, ToDo 32).**
Sensitivity: the model fitted with 8 for all beacons and with 30 for all.
With 30 for all, nothing changes (1 087 periods, the same cut points, the
same assignments of readouts without `Current Timer`). With 8 for all, two
room tags get an extra period, both on the setup night and both physically
impossible: 124 at 2026-08-13 00:14:24 (its `Current Timer` runs 50 → 64
in the 14 s from 00:14:10, one clock without a reset) and 167 at 00:54:17
(after the reboot at 00:51 that its falling `Current Timer`, 134 → 117,
shows, a second one 5 min later although `Current Timer` runs on, 118 → 159
in 59 s). The penalty for room tags prevents such cuts; the penalty for
persons decides nothing in this data. Injected reboots (test A) are found
100 % for both types, and late readouts (test B) never create a period.
Drift: on 2 919 readouts with reliable contact boots the residual of the
contact boot against the period's boot is 0 s (median) in every 4-hour bin
of the day, for persons and room tags (means −2 … 0 s), and shows no
curvature along long periods — no daily or temperature effect above about
2 s.

**Log-order cap (H4c, 2026-10-22).** A period without a reliable contact boot
has its boot from the delay cost alone, which places it as late as H1
allows. The data shows that these boots are late, never early: against the
readouts of periods with reliable contact boots in log order (where two of
them lie less than 2 min apart in real time), 26 % of their readouts lay
after the next reliable one by more than 13 s and none before the previous
one; median offset from the middle of that bracket +14 s (readouts of
reliable periods: 0 s, 0.6 % outside on either side). The both-sides check
(section 21) saw the same independently (+10 s). It shows most where the
logger falls behind: the Base reads the beacons seconds apart in real time
while the lines are logged up to hours later (H4 per 1 000 readouts: 0–5
in log files with a median delay below 60 min, 50 above). Rule: in a period
without a reliable contact boot, no readout may lie after the next readout
in log order of another beacon whose period has reliable contact boots and
lies on the consensus sequence (R ≤ R_ref + eps). The cap is an upper bound
on the boot, like H1; it only moves a boot earlier and gives way to H1, H5,
H6 and H9 (the boot is then their lower limit). Tested on the pipeline
output of 2026-10-22 (`uptime_model.py` and `hard_rule_check.py` only):
periods unchanged (1 086, the same readouts in each); boots of periods
without reliable contact boots in the deployment earlier in 52 of 90
(5 % quantile −38 s), of the others within −5 / +3 s (5 / 95 %); H4 in the
deployment 30 → 17 readouts (8 of them new, 18–39 s, against neighbours
without contact evidence), before the deployment 31 → 24; readouts without
contact evidence after their next reliable neighbour 26 → 10 %, median
offset +14 → +11 s (the cap bounds the boot, the delay cost still pushes it
to the bound). Tested and not used: every readout of another beacon on the
consensus sequence as reference (`H4C_RELIABLE_ONLY = False`): the nearest
one is often a late readout without contact evidence itself, H4 17 → 24.

Full pipeline run with the cap (2026-10-22): 1 086 periods, no
contradiction in the data; H4 in the deployment 30 → 23 readouts (median
23 s; beyond 45 s only 15 p2, section 19.2, and 147 p1, 72–100 s), before
the deployment 31 → 17; readouts without contact evidence after their next
reliable neighbour 26 → 13 %, median offset +14 → +11 s. Both-sides check:
delay-free readouts median |Δt| 30 → 20 s, within 30 s 61 → 64 %; delay
only 56 → 57 %; contact boots unchanged. Second ID split: 9 at 2026-08-13
09:54:22 (`Current Timer` 238, no contacts or other records) keeps all hard
rules under both labels now and stays undecided (before: 57 by method F).
Output (status counts) unchanged. The timer step check then accepted a
spurious step (13 p1, below); fixed.

**Log-order position as evidence (2026-10-24).** The cap bounds the boot
from above; between its bounds the delay cost still placed boots without
reliable contact evidence as late as allowed. The log order also predicts
where such a readout lies: interpolated by its position among the readouts
(readout count, not log lines) between the nearest readouts of other
beacons before and after it that the cap accepts as references (on the
consensus sequence, with reliable contact boots, in the same log file).
Tested leave-one-out on readouts with reliable contact boots: for
references less than 120 s apart, R − prediction median +0.0 s, |·| median
5.3 s, 90 % 21 s (by log lines: 90 % 24 s); 120–600 s apart: +2.2 s, 30 s,
112 s. The boots without contact evidence lay a median +10.7 s after it.
Rule: for a period without a reliable contact boot, per readout with
references at most 600 s apart, the cost min(((R − prediction) / σ)², 16)
with σ = 5 s + 0.25 × the distance of the references (about the spread
above); the predictions' boots and their weighted mean are candidates of
the boot search. Result (full pipeline run): boots of such periods in the
deployment earlier by a median 15 s; the largest moves (up to 318 s) are
periods with a single readout, whose boot the delay cost hardly fixes
(e.g. delays of −13 s → 111 s in a log file running minutes behind).
Both-sides check, mean signed offset of the encounter edges (|Δt| ≤ 60 s):
delay only +7.5 → +3.5 s (± 0.5), anchors +6.4 → +5.6 s, reliable contact
boots −0.2 → −0.1 s; no beacon off by more than 30 s any more (before:
beacon 3, +50 s). H4 in the deployment 23 → 12 readouts (two of them the
repaired clock stops of 15 p2, 19.3; beyond 35 s only 147 p1, 81 s), before
the deployment 17 → 26 (setup night, 15–50 s). The remaining bias of the
delay-only boots lies where no bracket exists (references in another log
file or more than 600 s apart); the anchors (delay-free readouts, H6) keep
about +6 s.

**Symmetric delay cost (2026-10-25).** The delay cost charged only positive
delays: any boot from delay 0 down to the H1 limit (delay −eps) cost the
same, and the boots fixed by the delay alone sat at that limit (median
delay −12 s over their 39 readouts in 24 periods; both-sides check +10 s).
A readout is not logged before it happens; eps is only rounding. The cost
is now log(1 + |delay| / 60 s), and for periods without a reliable contact
boot each readout's boot at delay 0 is a candidate of the boot search.
Tested and not used: the delay predicted from other beacons' readouts
logged around the same time (the logger backlog) — on readouts with
reliable contact boots off by a median 40 s (±5 min) to 73 s (±15 min),
and only 11 of the 39 readouts have such neighbours. Result (full pipeline
run): both-sides check, delay only median Δt +10 → 0 s; H4 before the
enforcement 281 → 168 pairs; validation: delay-free readouts 10 s / 13 s →
1 s / 10 s outside the window, 4 s / 12 s → 2 s / 10 s inside, injected
reboots without contacts 4 s / 27 s → 4 s / 25 s (19.1).

**H6: delay of a delay-free readout 0 … 10 s (2026-10-24).** Before, H6
allowed a delay-free readout up to 60 s after its real time, and the delay
cost placed the anchors' boots late within it (the +6 s above). Readouts of
periods with reliable contact boots that are delay-free show 0 … 12 s
(5 / 95 %, median 1 s). `ANCHOR_MARGIN_S` = 10 s: H6 is B ≥ line time −
C (1 + d) − eps − 10 s; within the margin the delay costs nothing, the
candidates of the boot search include the boots at delay 0, 5 and 10 s.
Both-sides check (final run against 2026-10-21): delay-free readouts
median Δt +10 → 0 s, median |Δt| 30 → 20 s, within 30 s 61 → 70 %.

**Solution.** Candidate cut points: current boundaries, falling `Current
Timer`, jumps of more than 2 min between reliable contact boots, readouts
after which a reboot is physically possible (their latest boot after the
previous readout's line time − 8 h), battery windows. Dynamic programming
over them per beacon; per period the boot with the lowest cost in its
allowed interval. Iterated: contact boots and H4 neighbours are recomputed
with the new periods until no cut point changes (at least two iterations, at
most `ITERATIONS` = 3; a warning if the cut points still change then).

**Convergence of the boots (2026-11-02).** The stop rule looks at the cut
points only; each iteration now also prints how many boots of unchanged
cuttings moved. In the final solve 140 – 180 boots still move between the
last two iterations (up to 120 s). Run on with 6 iterations per loop
(diagnostic, nothing written): the cut points stay fixed, but 210 of the
1 087 boots never settle — 31 alternate between two values, 78 drift one
way, 101 jitter — by a median of 2.4 s per iteration (90 % 10 s); their
range over iterations 2 – 6 is a median of 3 s (90 % 10 s, max 53 s: 34 p1,
setup day). The published boot (after the second iteration) lies a median of
1 s from the mean of the later iterations (95 % 6 s, max 33 s). There is no
exact fixed point: the evidence is re-evaluated discretely (a contact boot
passes or fails the reliability test, a readout enters or leaves the H4
consensus sequence) and a boot moved by a few seconds changes it. The jitter
lies within the precision classes of section 21 (13 – 90 s) and is not
corrected; it is the reason why skipping an iteration (to save run time)
changes boots by seconds.

**Longest delay for reboots (`MAX_DELAY_S` = 8 h).** A reboot is a
candidate only where it is possible with a delay up to 8 h. The largest
delay of the solution is 7.58 h (14, 158, 161 on 2026-08-13 at 08:33, the
logger stall from 00:58, section 21); the model prints a note for readouts
within 1 h of the bound (`DELAY_NOTE_S`), since a longer real delay could
have been cut by it (2026-11-02: these 5).

**Result (2026-10-17).** Converged after 3 iterations: 1 088 periods
(before 1 095); 13 cut points changed. Hard rules in the deployment: H4 97
readouts out of order (before 174), H5 0 (before 5), H6 0 (before 1), H7
satisfied for all 33 replacements (24 with a boot in the window, 9 with a
period boundary across it), one period with a contradiction in the data (7
p2, IDs 0–9 / 48–57 on 2026-08-13). Splits: 5 at 2026-08-25 13:27, 6 at
2026-08-27 18:54 (both earlier contact-sync conflicts); 73 boots at
2026-08-23 18:26 (battery replacement 18:30, before "late"). 10 pairs of old
periods merged (reboots that were late readouts). The model does not split
15 p2 although one readout's contacts suggest it: an earlier readout of the
period still runs on the old boot, so H5 forbids the split (a timer step,
section 19.2).

**Result with the consensus H4 bounds (2026-10-18).** 1 088 periods, the
same cut points; boots moved by less than 1 min in 2 410 readouts, by
1–15 min in 12 (readouts with a long logging delay that the old bounds did
not see, e.g. beacon 52 at 2026-08-24 08:51: 14 min). The hard rule check
now reports H4 only beyond the timer precision eps (as H1). Deployment: H4
36 readouts (the current timer-zero solution: 146), H5 0, H6 0, H7
satisfied. Contradictions in the data: 7 p2 as before and 9 p2 (29 s, on
2026-08-13 before the deployment, while 9 / 57 were still mixed). Of the 36
H4 readouts, 2 are the timer step of beacon 15; the other 34 are 14–87 s
out of order, within the boot precision of periods without contact evidence
(19.1), most of them on 2026-08-22 between 09:50 and 13:45.

**Stability (`--evidence-days even|odd`).** The model is run twice more,
each time with the reliable contact boots of only the even or only the odd
calendar days (by the readout's line time); outputs get the suffix
`_even_days` / `_odd_days`. Result (2026-10-17): both halves give the same
1 088 periods with exactly the same cut points as the full run (8 413 pairs
of consecutive readouts, 0 differ). Boots: of the 309 periods with reliable
evidence in both halves, the two halves agree within 1 s (median), 13 s
(95 %), 31 s (max). Larger shifts (up to 19 min) occur only in periods whose
reliable evidence lies entirely in the left-out half; their boot then falls
back to the delay cost and moves by the logging delay of the readout (e.g.
the batch readout at 2026-08-22 19:06). The cutting is therefore decided by
the hard rules and the `Current Timer` sequence, not by single contact
boots; the contact boots fix the boot within seconds where they exist.
Rerun with the consensus H4 bounds (2026-10-19): again 0 of 8 413 cut
decisions differ; the 310 periods with evidence in both halves agree within
0 s (median), 11 s (95 %), 30 s (max); one readout moves by more than
10 min (beacon 76, evidence only in the even half). The odd-day run reports
one more contradiction than the full run, 3 s (22 p14).

**Readouts without `Current Timer`** (lost header). After the model has
converged, each is assigned to one of its two candidate periods, those of
the beacon's readouts with `Current Timer` right before and after it in log
order:

1. Logger delay first: the readout took place within its period, i.e. before
   the beacon rebooted into the next one. A candidate whose next boot lies
   more than the allowed logger delay before the readout's first line time
   is impossible. Allowed: 3 h (the longest delay expected, BI 3.6), or,
   where the readouts with `Current Timer` logged within ±1 h show a
   backlog, the largest delay that at least two different beacons reach
   + 30 min (backlogs over 3 h occur: the delays then grow steadily over
   hours, consistently across all beacons; a single misdated readout does
   not widen the allowance).
   One candidate left: that one ("only possible period (logger delay)").
   Example: 88 on 2026-08-28 08:36 (`dsa_20260828_0807.log` line 221414):
   under p11 it would have been read out before the reboot on 08-27 21:29,
   i.e. 11 h before it was logged → p12.
   Otherwise both candidates the same period: that period. Only one
   candidate (before the beacon's first or after its last readout with
   `Current Timer`): the beacon can have rebooted in between, so it is
   checked as in 2; without contacts that period ("only neighbouring period
   (no contacts)"). (Before 2026-10-27 it was taken without check: 112 on
   2026-08-13 10:50, `dsa_20260813_1135.log` line 109061, went into p2 and
   was dated about 10 h too early.)
2. Otherwise the possible candidates: under which its contacts lie between
   the beacon's last complete readout before it and their own log lines —
   the boot not after the latest boot its contacts allow (per contact: line
   time − timer, + eps), and its oldest contacts (10 % quantile of the
   timers, robust against contacts sent twice) not before the real time of
   that readout (− eps): a complete readout empties the storage (BI 2.1).
   Complete: `Transfer complete` and all contacts of `Contact Count`
   received, counting only the stored ones (timer ≤ `Current Timer`;
   contacts recorded during the readout are not part of `Contact Count`,
   BI 2.4); an incomplete readout leaves contacts for the next one and gives
   no bound in real time. But it sends its contacts oldest first, so under a
   candidate the readout's oldest contacts (10 % quantile) lie at most 30 min
   below the newest stored contact of the candidate's previous readout, also
   an incomplete one — the last one that holds stored contacts (2026-10-27:
   a readout without contacts bounds nothing; 102 on 2026-08-20 in test D
   passed p10 because p10's last readout was empty, although its readout
   before reached timer 36 000 s and the hidden one started at 1 900 s)
   (2026-10-25; on all 3 014 consecutive readouts of one
   period after an incomplete readout the timers fall back at most 1 111 s,
   99 % within 84 s: the last batch of 48 sent again after a lost
   confirmation). One possible: that one. None: a candidate whose boot fits after
   moving at most 90 s earlier (within the precision of a boot). Otherwise
   undecided, probably a period of its own (a reboot before it and one after
   it, never read out with `Current Timer`).
3. Both possible: **scan times** (2026-10-25). A beacon records its
   contacts at its scan times; within one uptime period a stretch of timers
   is scanned once, and contacts sent twice after a lost confirmation repeat
   the same timers. A candidate is impossible if one of its readouts holds
   at least 10 scan times (distinct contact timers) within the readout's
   timer range and none of them is one of the readout's. One left: that one
   ("only possible period (other scan times in period N)"). The test only
   removes one of two candidates the timers allow; it never chooses a
   period the timers exclude. (The former step 3, matching encounter edges,
   decided 4 of 7 test cases wrongly and was removed on 2026-10-21, 19.1.)
4. Still undecided (no period possible, or both possible): the boot from the
   partners' records. A record of partner P at the readout's own timer t
   and P's dated record of the beacon at real time T from the same
   encounter give boot = T − t. Per partner, both record series are binned
   (10 s) and all pairs counted by their lag; the sum over the partners
   peaks at the boot. Searched only within the allowed range (last complete
   readout .. latest boot its contacts allow): daily routines give
   look-alike peaks about a day apart, a long joint stay a plateau.
   Accepted if the peak's 90 % width is at most 25 min, the peak at least
   1.1 times the best score more than 10 min outside its own 90 % band (a
   broad peak is not compared with its own shoulder), at least 50 matches
   and 4 times the median. Both possible: the candidate whose boot lies
   within ±10 min of the peak. No candidate in the peak: **a period of its
   own** with the fitted boot (a reboot before and after it, never read
   out with `Current Timer`), if the boot lies after the beacon's last
   readout before it (H5) and its newest contact not after the boot of the
   beacon's next period (+ eps + 30 s; the same condition wherever an own
   period is made, 2026-10-30, below). No period possible: only a period of its own is
   left, so the peak is searched only after that readout (2026-10-27; the
   median and the rivals still over the whole range — daily routines give a
   look-alike a day early, which won for 102 in test D, and a narrower range
   raises the median of a broad peak); its records get the precision of the contacts
   class (16 s, 21) + half the width of the peak.
   **Forced own period** (2026-10-25): no period possible and no accepted
   peak — the hard rules already require a period of its own, only its
   boot is open. The best peak is then accepted up to a 90 % width of 60 min
   and without the rival test (the other criteria and H5 stay); the
   precision of half its width says how coarse the boot is.
5. Cross-check of "only possible period" with the same fit: a confident
   boot away from the chosen period's boot withdraws the decision — own
   period, if the boot lies after the beacon's last readout (H5). A fitted
   boot before it is impossible and withdraws nothing (2026-10-27; before,
   undecided: 75 on 2026-08-29 09:18, `dsa_20260829_0918.log` line 1, 867
   contacts, lost p5 to a fit a day early, before two later reboots).
6. **Co-presence** (2026-10-25; all decisions except "same period before
   and after", where a hidden reboot is impossible). Dated with the chosen
   period's boot, the readout's contacts are checked against the partners:
   of the contacts whose partner was recording at that time (any record
   within ±10 min), the share for which that partner recorded this beacon
   within ±10 min — an encounter is seen on both sides. Below 10 % of at
   least 50 such contacts the period is rejected; the readout then gets a
   period of its own with the boot from the partners' records (accepted as
   for the forced own period of 4), else it stays undecided. Calibrated on
   readouts with `Current Timer` (2 163 with at least 50 such contacts):
   under their own period 99 % have a share above 71 %, under the beacon's
   neighbouring period 28 % lie below 10 %. The one readout with
   `Current Timer` below 10 % under its own period is 42 p8 (2026-08-25
   08:42, 2 %, below). Record times binned to 60 s.

   Result (pipeline run 2026-10-25): in the data no decision is rejected
   (98 on 2026-08-20 12:27, which the rule moves from p1 — 0 % of 433
   contacts — to a period of its own on the fitted model, gets the same
   own period from step 5 in this run, boot 10:39). Test D: alone in its
   period 0 wrong (before 2: 86 on 2026-08-16, put into p1 — co-presence
   now rejects p1; the partners' records give no accepted boot, so it stays
   undecided), at period boundaries unchanged 343 / 0 / 6.

   **42 p8 (open).** The readout of 2026-08-25 08:42 (`dsa_20260825_0820.log`
   line 267886, `Current Timer` 51 213, the only readout of p8, boot
   2026-08-24 18:15, incomplete: 2 112 of 15 120 contacts) holds contacts
   with timers 16 949 – 22 220: dated with p8 they lie on 2026-08-24 22:58 –
   2026-08-25 00:26, where their partners recorded 42 at 2 % of 620 contacts
   (42's other readouts: 80 – 100 %). Its first stored contact lies 4.7 h
   after the boot, although the partners recorded 42 at 400 – 700 per hour
   from 18:00 to 21:00; from 22:00 to 07:00 they hardly recorded it. A scan
   over other boots finds 99 % near 2026-08-23 15:30, but such scans are not
   reliable in long joint stays (many shifts and other beacons fit as
   well). Not resolved; the scan of all readouts (2026-10-26) shows
   stretches of one-sided visibility for other beacons too (96, 75, 80, 52,
   55: the beacon recorded its partners, they did not record it, also at
   the RSSI both sides record), with contacts in storage order — so a low
   share alone does not prove a second clock.
   Checked 2026-10-26: p8's boot is right — the readout was logged with a
   delay of 814 s among readouts of other beacons logged in the same
   seconds with 650 – 1 130 s (21, reliable contact boots: 653 s); no boot
   allowed by H1 and H5 brings the share above about 60 %. The contacts stay
   dated with p8; the one-sided visibility (ToDo 37) is left to the
   evaluation.

Result (pipeline run 2026-10-27, after the data scan): 112's readout of
2026-08-13 10:50 (one candidate, formerly taken unchecked, about 10 h too
early) → no period possible → own period forced by the hard rules, boot
10:16 (10:10:00 – 10:50:30, broad peak 722, ratio 2.80); its records get a
precision of about 20 min. 75 on 2026-08-29 stays in p5. Methods: same
period before and after 40 288, only possible period (logger delay) 1 568,
only neighbouring period (no contacts) 614, only possible period 46, own
period 9, undecided (both possible, no contacts) 355; no contact without a
time. Test D: at period boundaries 346 / 0 / 3 (before 343 / 0 / 6: the
three readouts undecided by an impossible fit are now right), alone in
their period 0 / 0 / 62 with 49 own periods (before 45; 102 on 2026-08-20
was wrong in an intermediate run, see step 2).

Result (2026-10-20): 64 on 2026-08-17 19:07 → own period, boot 11:58
(11:57:00 – 11:59:40, peak 1 110, ratio 1.27); 90 on 2026-08-24 19:22 → own
period, boot 13:00 (12:53:30 – 13:07:30, ratio 1.22); 83 on 2026-08-22 18:58
→ own period, boot 12:41 (12:20:40 – 12:45:30, peak 426, ratio 1.24, 4.3 ×
the median: in company the whole time, hence the high background); 84 on
2026-08-23 → p9 (peak exactly at p9's boot, 646 against 23 at p8's). Not
accepted: 61 (2026-08-24, plateau over 10 h), 71 and 88 (too few records).
Contacts not datable: 4 405 (before 32 975).

Result of step 3 and the forced own period (2026-10-25, tested on the fitted model before the
pipeline run; only these two readouts changed):

- 71 on 2026-08-25 16:53 (`dsa_20260825_1605.log` line 220960, 189
  contacts, timers 7 418–7 572, an eco session; the end of 152's readout and
  71's header were not logged): p11 and p12 possible. 71's readout of
  2026-08-24 19:25 in p11 delivered one continuous range of timers
  952–11 229, with 58 contacts in 7 418–7 572; none of the 21 scan times of
  this readout is among their scan times (timer mod 7: 0 there, 5 here), and no
  contact is identical → p12.
- 61 on 2026-08-24 19:25 (`dsa_20260824_1837.log` line 280286, 4 120
  contacts, timers 6 592–11 450, 2 self-reports; the end of 71's readout and
  61's header were not logged): no period possible (p3 would make its timers
  a day too small, p4 boots at 21:24 after the readout) → forced own period,
  boot 06:33 (06:31:00 – 06:58:20, peak 351, ratio 1.12 against the plateau
  of a long joint stay); precision 16 s + 14 min.

Pipeline run (2026-10-25): the same two decisions. 61's boot 06:21:10 –
06:58:20 (the fit of the final model), its 4 120 contacts dated 08:23 – 09:44
with a precision of 1 131 s; 71's contacts dated with p12 (delay class,
90 s). No record is left without a time (`not_dated.csv` empty; before
4 309 contacts, 2 self-reports, 1 eco session). Test D: neighbouring period
344 / 0 / 5, alone in its period 59 not assigned / 3 wrong (before 58 / 4);
the forced own period did not fire in test D (the 13 readouts with no
period possible there have no peak with 50 matches).

**An own period ends before the next one (2026-10-30).** An own period
was only checked at its start (H5). 90 on 2026-08-13
(`dsa_20260813_1209.log` line 1064126): its timers allowed only p1 (boot
10:06:56); the cross-check (step 5) fitted 09:51 – 09:56 from the partners'
records, 14 min away, beyond the ±10 min of `FIT_MATCH_S`, and gave the
readout a period of its own from 09:52:50 — but its contacts reached 12:32,
2.5 h into p1: two clocks at once. Now an own period must also end before
the boot of the beacon's next period (its newest contact, `own_fits`); else
the fit is no evidence against the timers. Result (pipeline run
2026-10-30): the readout stays in p1 ("only possible period", co-presence
passed), its contacts are dated 14 min later; its own period and its lost
interval of 2.4 h are gone, 10:06:56 is 90's first boot. The only such
case, before the observation window.

Assigned readouts bound the boot of their period like any readout (H1 for
their contacts: boot ≤ line time − timer + eps). The model is then solved
again with these bounds and the readouts are assigned once more. Output
`timer_zero/uptime_model_headerless.csv` (period, method, latest boot from
its contacts, support of both candidates); used by the dating (section 21).

Examples that shaped the rules (2026-10-20): 47 on 2026-08-13 12:48 (only
the earlier period possible; its boot, fixed by a single readout without
contact evidence, had to move 53 s earlier); nine readouts a few minutes
after a reboot (their contacts span minutes, too few encounter edges for
support; the lower bound excludes the earlier period, under which their
contacts would lie days before that period's last readout); 3 on 2026-08-25
08:48 (the rest of an incomplete readout six minutes earlier: hence the last
*complete* readout as lower bound); 83 on 2026-08-22 and 64 on 2026-08-17
(no candidate fits; moving the later period's boot by hours made its own
readout 22 h / 59 h late and broke the log order: hence the 90 s limit).

Result (2026-10-20): of 42 854 readouts without `Current Timer`, 40 905 same period
before and after, 35 only possible period, 4 by their contacts; undecided
1 903 without contacts, 4 no period possible, 3 both possible and the
contacts do not decide. 218 boots are bounded by them; no cut point
changes; H4 in the deployment 40 readouts (largest apart from the timer step
of 15: 87 s).

**Output** (`timer_zero/`): `uptime_model_periods.csv` (per period: beacon,
number, first and last readout, readouts, boot, drift, reliable contact
boots, readouts with a log-order prediction, old periods, old boot and the
change, violated hard rules);
`uptime_model_readouts.csv` (per readout: period, boot, real time, delay,
delay lower bound, boot from its contacts with peak and second peak and
whether it is reliable, old period).

### 19.1 Synthetic validation (`validate_model.py`)

The model is fitted once; each test changes the inputs of one beacon in a
known way and solves that beacon again, with the contact boots of its other
readouts, its H4 bounds and the cut points of the fit unchanged. Re-solving
an unchanged beacon reproduces the fit exactly (checked for all beacons).

- **A Injected reboot:** in a period of at least two readouts, a reboot at a
  random time between the real times of readouts j − 1 and j (at least 30 s
  from both); the readouts j … of the period get the `Current Timer` of the
  new boot, line times unchanged. Variants: their contact boots move with
  the reboot, or they have none. Found if the solution cuts exactly at j.
- **B Late readout:** the line time of one readout (not delay-free) moves
  later by 10 min, 1 h or 8 h, its contacts' line times with it (the delay
  lower bound stays). Variants: its H4 bounds kept or dropped. Correct if no
  cut point changes.
- **C Delay-free readouts as ground truth:** each delay-free readout in turn
  loses its H6 status; its observed timer-zero time is the true boot. Error =
  boot of its period without H6 − that time.

Result (2026-10-25, final pipeline: log-order evidence, H6 delay 0–10 s,
switch relabels of 5a, the rules for readouts without `Current Timer` and
the symmetric delay cost of section 19; 3 cases per beacon and test, seed 1, 4 244 cases with
test D):

| Test | Case | n | Found | Boot error median / 95 % |
|---|---|---|---|---|
| A, with contacts | `Current Timer` falls | 378 | 100 % | 0 s / 14 s |
| A, with contacts | `Current Timer` does not fall | 26 | 100 % | 0 s / 10 s |
| A, no contacts | `Current Timer` falls | 378 | 100 % | 4 s / 25 s |
| A, no contacts | `Current Timer` does not fall | 26 | 100 % | 4 s / 10 s |
| B, H4 kept or dropped | 10 min, 1 h, 8 h late | 6 × 487 | 100 % | 0 s / 0 s |
| C | delay-free in the window | 61 | — | 2 s / 11 s |
| C | delay-free outside the window | 42 | — | 1 s / 10 s |

Before (2026-10-18, without the log-order evidence): A with contacts
0 s / 48 s, A without contacts 17 s / 88 s, C 5 s / 16 s and 13 s / 22 s.
The precision classes of the dating (section 21) are these 95 % bounds: a
delay-free readout 13 s (C, outside the window, the larger), reliable
contact boots 16 s (A with contacts), a boot from the delay with log-order
predictions 28 s (A without contacts, 27–28 s in the runs of 2026-10-24:
the injected period keeps the readouts' log-order positions); a boot from the delay alone keeps 90 s
(A without contacts before the log-order evidence).
With the symmetric delay cost (2026-10-25) the bounds are smaller (10 / 14 /
25 s); the classes keep the values of 2026-10-24 (13 / 16 / 28 s) as the
more conservative ones.
Split by beacon type (2026-10-26), A without contacts has a 95 % bound of
38 s for person-worn beacons and 21 s for room tags: persons move between
groups, so the readouts logged around theirs say less about their time.
The log-order class is split accordingly: persons 40 s, room tags (and
the few other beacons) 28 s.

**Several seeds and the delay class (2026-11-02).** The bounds above come
from one seed (3 cases per beacon and test). Rerun with seeds 1 – 4 (seed 1
reproduces the table above exactly), and test A with a third variant, `no
contacts, no log order`: the injected readouts keep their H4 bounds but
lose the log-order prediction — a boot from the delay cost alone, the class
`delay` of the dating, which no test had measured since the log-order
evidence came in (2026-10-24). 95 % bound of |boot error| per seed and
pooled over the 1 309 distinct cases of test A (C: 103 cases, the same in
every seed):

| Class (precision of the dating) | Seeds 1 / 2 / 3 / 4 | Pooled |
|---|---|---|
| delay-free (13 s), test C in / outside the window | 10 / 10 / 10 / 10 s | 10 s |
| contacts (16 s), A with contacts | 14 / 14 / 17 / 13 s | 14 s |
| log order, persons (40 s), A no contacts | 33 / 24 / 19 / 19 s | 23 s |
| log order, room tags (28 s), A no contacts | 20 / 21 / 25 / 22 s | 22 s |
| delay (90 s), A no contacts, no log order | 63 / 51 / 48 / 47 s | 51 s |
| — persons | 75 / 69 / 65 / 71 s | 72 s |
| — room tags | 37 / 42 / 34 / 38 s | 35 s |

All classes hold pooled; per seed the contacts class reaches 17 s once (the
sampling noise of a 95 % quantile from about 400 cases is a few seconds),
the log-order class for persons varies most (19 – 33 s). The delay class of
90 s is now backed by a test (persons 72 s, room tags 35 s: kept as one
conservative value). Without log order 2 injected reboots are not found
(99 on 2026-08-22 12:50, the case below, and 90 on 2026-08-20 21:19, both
`Current Timer` not falling); with log order or contacts all are found.
The precision classes are not changed.

No test changed a cut point outside the injected period; all injected
reboots are found (2026-10-18: one missed, beacon 99, 2026-08-22 12:50, a
readout with `Current Timer` 22 and no contacts that fitted both boots).
Without contact evidence the boot of a new period falls back to the delay
cost and lies by about the readout's logging delay too early or late; with
contact evidence it is fixed within seconds. A late readout never creates a
period: its delay costs less than a reboot. The delay-free readouts confirm
the boots of the model within the timer precision.

While writing the test, the candidate criterion in the code was found to
compare with the previous readout's line time instead of its line time − 8 h
as described above; corrected (`MAX_DELAY_S`). The fit is unchanged.

**Test D: readouts without `Current Timer`** (`--headerless ROUNDS`, needs
a full fit). Per round one readout with `Current Timer` and contacts per
beacon at a period boundary (its nearest readouts with `Current Timer` before
and after lie in different periods) loses its `Current Timer`; its contact
limit is computed from its own log lines; `assign_headerless` assigns it.
The boots stay those of the fit, which used the readout's own evidence.
Result (2026-10-21, 3 rounds, 411 cases):

| True period | n | Correct | Wrong |
|---|---|---|---|
| A neighbouring period | 349 | 343 (+ 2 left open) | 4 |
| A period of its own (the only readout of its period) | 62 | 39 not assigned (28 own period from the partners' records, 11 "no period possible") | 23 |

By method: "only possible period" 340 of 340 right where the truth is a
neighbour, but it also takes 23 of the 62 readouts alone in their period;
"contacts" (encounter edges, step 3) 3 right, 4 wrong of 7. Both rules are
too weak: the lower bound checks only the newest contact against the last
complete readout (a complete readout empties the storage, BI 2.1, so every
contact of the next readout must lie after it — the oldest contact is the
right test), and the edge support is no better than chance.

**Fix (2026-10-21).** (1) Lower bound with the oldest contacts: the 10 %
quantile of the readout's contact timers (robust against contacts sent twice
after a lost confirmation, BI 3.1.4) must lie after the last complete
readout, which now counts only the stored contacts against `Contact Count`.
(2) The encounter-edge rule (former step 3) is removed; both candidates
possible → the partners'-records fit, searched from the lowest boot the bound
allows (before, from the last complete readout's real time on, which excluded
the earlier candidate). (3) Cross-check: every "only possible period" with
contacts is fitted from the partners' records as well; a confident boot away
from the chosen period's boot withdraws the decision (own period if after the
beacon's last readout, H5; else undecided). (4) "Boot in the peak" within
±10 min (`FIT_MATCH_S`): in 13 of 16 false objections of the first version
the fit lay 2–7 min before the true boot (the record match sees
co-presence, not the encounter edge).

| Test D (same 411 cases) | Neighbouring period: right / wrong / open | Alone in its period: not assigned / wrong |
|---|---|---|
| before | 343 / 4 / 2 | 39 / 23 |
| bound with oldest contacts, no edge rule | 347 / 0 / 2 | 46 / 16 |
| + cross-check, ±60 s | 330 / 0 / 19 | 58 / 4 |
| + cross-check, ±10 min (final) | 343 / 0 / 6 | 58 / 4 |
| final pipeline (2026-10-24, 411 cases) | 344 / 0 / 5 | 58 / 4 |
| + scan times, forced own period (2026-10-25) | 344 / 0 / 5 | 59 / 3 |
| + bound from the previous readout, symmetric delay cost (2026-10-25) | 343 / 0 / 6 | 60 / 2 |
| + co-presence (2026-10-25) | 343 / 0 / 6 | 62 / 0 |
| + single candidate checked, fitted boots after H5 (2026-10-27) | 346 / 0 / 3 | 62 / 0 |

The 4 errors left before the co-presence check were readouts alone in their
period without a confident fit from the partners' records. On the data (pipeline run
2026-10-21): two readouts get a period of their own instead of the only
period their timers allowed (beacon periods 9 and 14 rejected by the
partners' records); H4 in the deployment 36 → 30; 0 contradictions.

### 19.2 Two clocks in one period (beacon 15 p2, evening of 2026-08-14)

Two readouts of 15 p2 (2026-08-15 10:35 and 12:31) are 2 h 22 min out of
log order: their `Current Timer` is 8 560 s lower than the boot of the
period (2026-08-14 07:53, fixed by two reliable readouts of 2026-08-14 12:59
and 14:48) allows. A reboot is excluded: the readout of 10:35 holds 19 277
contacts whose timers continue without a gap from the readout of 14:48
(24 950 … 87 581), so nothing was erased (3.4.1). Matching its contacts in
1-h blocks of their timer against the partners' records: up to timer 46 550
they fit the boot 07:53 (offset 0 s), from 78 950 on a boot 8 550–8 560 s
later. In between the beacon was in one eco session (Enter 49 479, Leave
79 090) and recorded few contacts. The timer lost 8 560 s without a reboot;
all contacts recorded after it are dated 2 h 22 min too early on the
period's boot.

**Where the time was lost (2026-10-22).** Contacts of the readout of 10:35
in 250-s blocks of their timer, best offset against the partners' records
(share of contacts with a reverse record within 30 s; scanned 0–12 000 s):
offset 0 up to timer 47 250 (shares 0.5–0.8, sharp peak); 47 250–49 250
(about 21:00–21:30 on the original clock, over 1 000 contacts with 55
partners, beacon active) a broad peak at 2 700–4 300 s (shares 0.2–0.35,
background 0); 49 250–49 500, right before the eco session, already
8 520 s; during the eco session not decidable (the partners see 15 the whole
night under any offset); from 77 000 on 8 550 s. So the loss happened
before the eco session, within about 2 000 s of its timer. The timers
continue without a drop or gap (no jump back, no skip forward; the largest
step between consecutive contact lines is the eco burst period of 300 s). The readouts are complete (`Contact Count` 19 277, received 19 278)
and correctly labelled: no beacon booted at the later clock (08-14 10:15;
nearest 9 at 10:04 and 78 at 10:06, and 78 is among the later contacts), no
self-sighting, no Base mislabelling finding. A repair would need one more
boot for the later records (2026-08-14 10:15:31; delays of the two readouts
then 19 s and 56 s), but the change of clock itself cannot be dated; the
case is therefore not repaired and not taken as explained.

General checks (scratch scripts, 2026-10-18):
- **Eco sessions:** of 599 readouts with an eco session ≥ 1 h, 139 have
  reliable contact boots before and after the session; 138 show no loss
  (≤ 30 s), only beacon 15 above. The step is not a property of eco mode
  (15's next eco session, 27 486 s, lost 10 s).
- **All readouts**, contacts in 4-h blocks of their timer: 781 pairs of
  consecutive reliable blocks in 1 067 readouts; steps > 2 min: beacon 15
  (8 550 s) and beacon 91 at 2026-08-28 08:18 (43 120 s, second block peak
  18; physically impossible: the boot would lie after the readout, H1, and
  later readouts of the period confirm the old boot with peaks 175 and 128).

So beacon 15 p2 is the only period with two clocks in the data. The model
cannot represent it (a period has one boot); it keeps 15 p2 as one period
and the two readouts as 2 h 22 min late.

**Two stops, beacon silent (2026-10-23).** Models scored by the number of
15's 1 838 contacts in timer 46 500–49 600 with a reverse record within
30 s: no loss 263; one stop (0 → 8 550 s) 292; timer running slow
(offset rising linearly 0 → 8 550 s) 344; two stops (0 → 3 750 → 8 550 s
at timer 47 300 and 49 250) 538; slow – normal – slow with free ramp
lengths 532, choosing the shortest ramps the grid allows (≤ 125 timer-s for
about 3 900 s); a constant offset at random: median 41, 95 % 243. So the
timer stopped twice (or ran at least 30 times too slowly for a moment) and
ran normally in between. Scan by scan (every 7 s): the first stop lies
between timer 47 252 (5 of 12 contacts on the original clock) and 47 259,
the second at about 49 262. Real time: stop 1 about 21:01 – 22:04
(3 750 s), stop 2 about 22:36 – 23:56 (4 800 s); the eco session began at
about 00:00 (Enter 49 479) and ended at about 08:13 (Leave 79 090), both ends
on the shifted clock (the night itself lost at most about 30 s). During
both stops no partner recorded 15 (20:50–21:00: 219 records from 39
partners; 21:10–22:00: none; 22:00–22:40: 44–110 per 10 min; 22:40–23:50:
none), while the groups around it stayed together. The whole beacon was
silent — no advertising, no scanning, no uptime — and went on by itself
without a reboot: the contacts in RAM survived, the uptime continued where
it had stopped. The end of stop 2 was no motion interrupt (it would have
restarted the 1 800-s inactivity timer, but eco mode began 230 s later) and
no self-report. This fits a sleeping chip whose 32 kHz clock (LFCLK) did not
run: the GRTC `SYSCOUNTER` runs off the LFCLK in sleep, so neither the
kernel's nor the radio's compare events fire until the clock runs again. A
blocked CPU would not fit (the counter runs on the fast clock while the CPU
is active, the uptime would include the hang).

The earlier description (2026-10-18: lost during the eco session, the
beacon kept advertising) was wrong: the night (eco session) was normal, the
stops were in the evening before it.

**Context.** Experiment log, 2026-08-14 21:00: eco mode was enabled on all
participant beacons in the Aula by broadcast commands; the first stop began
at about 21:01. The beacon receives such a command in every scan while the
control device advertises it; the first time it saves the motion parameters
(settings write to RRAM, in an MPSL timeslot) and starts the inactivity
timer. No logged event at the second stop. 21 self-reports between timer
46 661 and 48 222 (button pressed 3 s each), but none at either stop (the
nearest 91 s before stop 1 and about 1 000 s before stop 2) — bursts like
this occur on 32 beacons without any loss. Other beacons that evening
(2026-08-14 18:00 – 08-15 02:00, worn beacons with a gap of ≥ 15 min in the
partners' records while the group that saw them before stayed together and
saw them again): 11 gaps of 10 beacons. Only 15 has a stopped clock that
can be shown. 58 (21:53–23:10, group of 28) looks the same from outside, but
rebooted at 00:30 before its next readout (its records of the evening were
erased), 88 (21:01–21:53, one minute after 15) rebooted the next morning —
neither can be checked; 85 rebooted during its gap (a reset); 6, 7, 13, 46,
61, 64, 103 kept their clock (later readouts on the old boot with normal
delays). Firmware analysis, errata and hypotheses: ToDo 24 (with ToDo 21), both in
the firmware ToDo (`BeaconNRF54/ToDo.md`).

**Handling (`timer_step_check.py`, run after `uptime_model.py`).** The test
above as a general check: contacts of each readout in 4-h blocks of their
timer; a change of clock between two reliable consecutive blocks of more
than 2 min; found only if the earlier clock is the period's (its boot within
120 s of the period's boot: a change leaves the period's clock), the later
clock obeys H1 (its boot not after the readout's upper limit) and is
confirmed by at least two reliable 1-h blocks (one block can be a chance
peak: 107 on 2026-08-26, peak 15 against 10, appeared and disappeared with
small changes of the partners' boots). A period whose step the accepted
clock stops of section 19.3 explain (their shifts add up to the step within
300 s) is repaired by the dating and not marked. Any other found period is
not repaired and the change is not dated: all records of the beacon in that
period
(contacts, self-reports and eco sessions in all its readouts, with and
without `Current Timer`) are unreliable. They are listed in
`quality/unreliable_time_records.csv` (every candidate in
`quality/timer_step_check.csv`) and marked by `generate_output.py` (status
`unreliable time (two clocks in one period)`, section 21). The partners'
records of the same encounters stay.

Result (2026-10-18, then still with the range from the last block on the
old clock): 15 p2 at the readout of 2026-08-15 10:35 (8 550 s, peaks 129 /
102), unreliable from timer 42 950: 15 516 contacts, 37 self-reports, 1 eco
session. Rejected: 91 at 2026-08-28 08:18 (H1). Run time about 4 min.

The condition on the earlier clock was added on 2026-10-22, after the run
with the log-order cap (section 19) had accepted 13 p1 at 2026-08-15 12:20:
one 1-h block with a weak peak (15 / 10) 44 min before the period's boot,
the next block on it (peak 331) — a single outlier block; 113 825 records
had been marked unreliable. On the same day the range was widened to the
whole period (the change of clock cannot be dated, see above). Result
(2026-10-22): 15 p2 the only period with two clocks; 44 097 records of it
unreliable (44 057 contacts, 39 self-reports, 1 eco session; timers 2 to
94 518, all five readouts of the period); 13 p1 and 168 p4 rejected (outlier
blocks).

### 19.3 Clock stops (`clock_stop_check.py`)

A beacon whose 32 kHz clock stops while it sleeps is silent until the clock
runs again — no advertising, no scanning, no uptime — and then goes on
where it stopped, without a reboot (19.2). Its records after the stop are
dated too early on the period's boot by the length of the stop. The check
finds such stops from the outside (the partners) and confirms them from the
inside (the beacon's own records), run after `uptime_model.py` and before
`timer_step_check.py`, on the model's own boots (`Boot before H4` where the H4
enforcement, 19.4, has written it: since 2026-11-02 a second run gives the same
result; before, a run after the pipeline judged the silences on the boots
after H4 and changed `clock_stops.csv`):

1. **Unexpected silence.** A worn beacon not recorded by any partner for at
   least 15 min, after at least two partners recorded it at least twice in
   the 10 min before (its group). At the rate it was recorded in those
   10 min, at least 100 records would have fallen into the silence (in eco
   mode, with a scan burst every 5 min, 15 min without a record are
   normal). The group stayed active: its members recorded each other during
   the silence (3 min from its ends) at least half as often as before.
2. **Reset.** A boot of the beacon within the silence (± 60 s): a reset,
   not a stop.
3. **Own records needed.** At least 30 of the beacon's own records in the
   same period within 1 h of its timer after the silence. Without them a
   stop cannot be told from a beacon that was shielded (in a pocket, under
   clothing: below the −80 dBm threshold of its partners) or out of range,
   and nothing would have to be shifted; such silences are listed, not
   accepted. The data shows why: of the 268 silences that can be checked,
   the clock ran on in 244, among those with a group as strong as 58's
   below (≥ 20 partners, ≥ 1 000 records expected, ≥ 10 seeing it again)
   in 2 of 3.
4. **Shift test.** The stop lies at the beacon's timer of its last sighting
   (its own timers run on without a gap). Its records from there on (1 h of
   its timer) are shifted against the partners' reverse records (within
   30 s), in steps of 10 s from −300 s to the silence + 1 200 s. The shift
   is the best one within ± 180 s of the silence (the beacon is silent
   exactly as long as its clock stands still); it must match at least 10 %
   of the records more than no shift and lie 10 % above the median of all
   shifts.
5. **H1 with the shift.** Every later readout of the period must still not
   lie after its own log line: the shifts of the period up to it at most
   its delay + eps. If the shift must be cut by more than 180 s, the
   readouts keep the period's boot and refute the stop; a smaller excess is
   cut from the shift.

Stops of one period add up. **Repair** (`model_dating.py`, section 21): a
record with timer t is dated boot + t (1 + drift) + the shifts of the
period's stops before t, with 60 s more time precision per stop; the
readouts too (their real times, the delays and the lost-records check). The
coverage marks the silence as `silent (clock stopped)`. The uptime model
itself is not changed (it keeps 15 p2 as one period with two late
readouts, H4).

**Result (2026-10-23, whole data).** 769 unexpected silences of worn
beacons in an active group: 2 stops, 244 clock ran on, 4 refuted by H1, 20
resets, 1 not explained by the silence, 498 not checkable. The stops:
15 p2 from timer 47 236 (silent 2026-08-14 21:00:06 – 22:03:56, shift
3 820 s) and from timer 49 235 (22:37:04 – 23:56:50, shift 4 763 s, cut by
H1 from about 4 790 s). Together 8 583 s; the two-clocks check of 19.2 found
8 550 s, the delays of 15's neighbours in the log suggest about 8 540 s.
Final run (2026-10-25): 3 820 s from timer 47 238 and 4 763 s from timer
49 235, together 8 583 s (between runs the split moves by about 10 s).
H1 refuted four candidates of a first version without it (48, 51, 97, 104:
later readouts of the period with delays of 0.7 – 10.7 s on the old boot).
Not checkable: 58 (2026-08-14 21:53 – 23:10, group of 28 that stayed
together, 12 of them seeing it again; rebooted at 00:30 before its next
readout). 88 (silent 21:01 – 21:53) is no candidate: it faded from its group
in the Aula before 21:00 and came back among other partners. With the
repair, 15's symmetric records are reciprocated within 60 s between the
stops in 71 % (before 18 %), the morning after in 68 % (39 %), before the
stops unchanged 81 %; all 44 097 records of 15 p2 have the status `ok`
again (before `unreliable time (two clocks in one period)`), no record is
dated after its own log line (H1 per record).

### 19.4 H4 as a hard rule (`h4_enforce.py`)

The uptime model solves each beacon on its own; H4 couples the boots of
different beacons (readout i logged before readout j: R_i ≤ R_j + eps), so
the model can only treat it as a cost (and bound it from one side, H4c).
After the final model and the clock stops (19.3) a linear programme
enforces it for all beacons together, with the smallest change of the
periods' boots and drifts:

- **Condition** (exactly the check of section 18): every readout lies at
  most eps after every readout logged after it within 50 positions (pairs
  of different periods; 413 013 pairs), minus 1 s because the boots are
  written to whole seconds. Then no readout is more than eps outside the
  log-ordered sequence of its neighbours.
- **The other hard rules** stay constraints: H1 per readout (with its
  delay lower bound), H6 for the delay-free readouts, H5 between
  consecutive periods of a beacon, H9; the clock-stop shifts move the
  readouts after them. A rule the model already breaks is not tightened.
- **Changes allowed:** a boot moves at most twice its precision (the class
  of section 21: a delay-free readout 13 s, reliable contact boots 16 s,
  log-order predictions 28 s, for person-worn beacons 40 s, the delay
  alone 90 s), a drift
  at most 3 s/day (twice the drift uncertainty in eps); the cost is the
  move divided by the precision, for a drift the shift it causes at the
  period's last readout divided by the precision.
- **Contradictions:** an H4 condition the data cannot meet within these
  bounds gets a slack (cost 10 per second) and is listed
  (`quality/h4_enforce.csv`).

The boots and drifts are written back to
`timer_zero/uptime_model_periods.csv` (the readouts' boot, real time and
delay to `uptime_model_readouts.csv`); the model's own values stay in the
columns `Boot before H4` and `Drift before H4 [s/day]`, from which a second
run starts again (since 2026-10-30: before, a second run of the script alone
found nothing to move and wrote an empty `h4_enforce.csv`, and the dating lost
the "precision at least the move"; checked: two runs give identical files); `model_dating.py` dates with the
period's drift from there (before: the beacon's drift of the contact sync,
the same value unless changed here). Order in the pipeline: clock stop
check, H4 enforcement, then in parallel the timer step check and the
battery check followed by the hard rule check (since 2026-11-02; before
all one after the other).

**Result (2026-10-24).** 257 pairs out of order beyond eps before, 0 after,
no contradiction; 62 boots moved (median 3.3 s, largest 37 s), 13 drifts
changed. The largest: 147 p1 (14 days, 33 readouts, the last readouts 33 –
49 s out of order) −16.84 → −19.84 s/day (its own drift from the contact
sync; now nearer the common −21 s/day), 43 s at its last readout. Hard rule
check: 0 violations of H1 – H9 (before: H4 12 in the deployment, 26 before
it). Both-sides check, mean signed offset: delay only +3.5 → +3.1 s,
anchors +5.6 → +5.4 s, reliable contact boots −0.1 s unchanged.

**Final run (2026-10-26, H6 delay 0–10 s, moves bounded by the
precision classes of section 21: 26 / 32 / 56 / 80 / 180 s, switch
relabels of 5a, the rules of 19 for readouts without `Current Timer`, the
symmetric delay cost, co-presence, the log-order class for persons).** 166
pairs out of order before (with the one-sided delay cost 281), 0 after, no
contradiction; 27 boots moved (median 4.2 s, largest 58 s), 6 drifts
changed (at most 3 s/day). The largest move, 99 p2 (2026-08-13, setup day,
log-order class of a person) by 58 s, stays within its bound (2 × 40 s; in
the run of 2026-10-25 with 28 s it used its full bound of 56 s). Hard rule
check: 0 violations.

99 p2 in detail: 5 readouts (2026-08-13 11:49 – 13:05), none with a
reliable contact boot. Its last two readouts were logged at 13:04 / 13:05
right after readouts of 38, 55, 101, 114, 119, 125 and 126 (none with
reliable contact boots either) in a logger backlog of about 5 min; with the
old boot (10:48:08) they lay about 55 s before them. Moving 99 later costs
less than moving the seven: boot 10:49:06, delays of its readouts 77 / 80 /
79 / 411 / 465 s → 19 / 22 / 21 / 353 / 407 s. The other beacons' delays at
the same times do not decide between the two boots (11:49: about 9 s,
favouring the new one; 12:57: about 71 s, favouring the old one); its boot
is uncertain by about ±1 min. Its records lie before the observation window;
their precision is the move (58 s, section 21).

### 20. From the original logs to the data (`run_pipeline.py`)

Runs the steps in the order they depend on each other, starting from the
logs in `Original/` (never modified); the model and the checks go to
`Output/pipeline/`, the data for the evaluation to `Output/data/`. Since
2026-10-28 it is the one script to run: step 9 is the dating (section 21,
`generate_output.py`), step 10 (`--reports` only) runs the both-sides check
and `data_quality_reports.py` in parallel. Since the same day the scripts
are sorted by role (`POSTPROCESSING.md`, Layout): the pipeline steps in
`steps/`, the dating in `dating/`, the reports in `reports/`, the shared code
in `lib/`; `uptime_pipeline.py` was renamed `run_pipeline.py`,
`uptime_model_validation.py` `validate_model.py` (the names in this document
are the new ones).

| Step | What | Why before the uptimes |
|---|---|---|
| 1 | Cache of the original logs (`lib/logcache.py`; the only step that reads the logs), duplicate log files (stop), logger clock: line times must not go back within a file or from one file to the next (`quality/log_clock_check.csv`) | Every line of a duplicate counts twice; H1 and H4 rely on the line times and on the log order (BI 3.2.5) |
| 2 | Fake records on the original logs (`quality/fake_records_original.csv`); contacts of the next beacon under the previous beacon's ID (5a, `id_correction/switch_relabels.csv`) | They corrupt the delay lower bound, the contact boots and the ID split |
| 3 | First split of ID1 0–9 into 0–9 and 48–57 (section 15), without the fake records | A readout under the wrong ID corrupts the periods of two beacons |
| 4 | Working copies with the decided IDs, the switch relabels (5a) and the fake records reconstructed (`logs/`; unchanged files are hard links): every complete logger message hidden in a fake block is decoded as the logger would have written it (its format from `logger.toml`, ID from the message's own byte) and replaces the fake lines in order, with their line time; the remaining fake lines become the marker line `# fake record removed (BI 3.2.2)`, which the parsers skip. Line numbers stay the same (no block yields more lines than it has fake lines). The copies are made from the cache of the originals, each with its own cache (`pipeline/cache/`), which all later steps read; a copy is written only when its content changed, a copy without an original (left from an earlier run) is deleted | The fakes hide genuine readouts and headers of other beacons (e.g. 2026-08-13 00:54: a complete readout of 168 with 35 contacts and the header of 153 inside 137's fake block); 12 blocks, 145 fake lines → 113 recovered lines: 35 contacts, 3 readout headers, Transfer complete and `Status` lines |
| 5 | Timer-zero analysis, contact sync, battery check, uptime model (its fake record check runs while the cache is written), contact sync on the model's periods (drift per beacon), uptime model again | |
| 6 | Second split: undecided readouts with `Current Timer` get the label whose boot in the uptime model their timer-zero time fits (at most 60 s before, 30 min after; exactly one label), or else the only label under which the uptime model keeps all hard rules (method F: both candidate beacons solved again with the readout under each label, `uptime_model.label_test`, from the model state `timer_zero/uptime_model_state.pkl`); first decisions that fit only the other label are reported. If labels changed: steps 4 and 5 again. Then, on the final model and in parallel with steps 7 and 8: the clock stop check (19.3), the H4 enforcement (19.4: boots and drifts changed so that all hard rules hold), then in parallel the timer step check (19.2) and the battery check on the final model (17) followed by the hard rule check | The uptime model gives corrected boots of both candidates; two beacons booted within minutes cannot be told apart by their boots, but a wrong label breaks the hard rules |
| 7 | Result of the split against the original logs (`id_correction/id_split_analysis.csv`). The split was checked once against the hand-cleaned logs (section 15.3; last check 2026-10-20: all readouts with data agree, except the two that method F decided against the hand decision because the hand labels break the hard rules) | |
| 8 | Base mislabelling check (section 16) on the working copies (`quality/base_mislabel_check.csv`); a check only, nothing is changed | After the uptimes: T4 / T5 use the timer-zero times of `readout_overview.csv` |
| 9 | Dating of all records (section 21) → `Output/data/` | Needs the final model, the clock stops, the H4 enforcement and the timer step check |
| 10 | `--reports` only: both-sides check (section 21) and `data_quality_reports.py` (section 11), in parallel | The both-sides check reads the dated contacts |

Run time (2026-10-29). The steps read the logs (64.6 M lines) as text,
most of them several times per round: half of the run time. Since
2026-10-29 the lines are cached as number arrays after the working copies
(`lib/logcache.py`, `logs/.cache/`, about 1.5 GB; written in about 70 s,
then only for the changed files, 15 files in 23 s; loaded in 2 s) and the
steps read the cache; the timer-zero analysis and the contact sync run as
two steps again (before: one process, to read the logs once). The cache
holds the same lines as the text readers (lines of fewer than three
fields, notes such as `# fake record removed`, are left out by all of
them) and stops with an error if a line does not have the logger's format.
Also: the encounter transitions of the contact sync (one sort of
(pair, bin) instead of two), and in the lost-records check of the dating a
search per beacon on the 16-bit ID array, which copied the array on every
search (about 600 s). Full run with `--reports`: 6 262 s → 3 248 s; per
step (old → new): timer-zero analysis + contact sync 683 → about 250 s, contact
sync on the model's periods 518 → 150 s, battery check 64 → 10 s, uptime
model 280 / 480 → 200 s, checks after round 2 (waiting) 260 → 307 s, dating
1 035 → 333 s, reports 624 → 683 s (`data_quality_reports.py` still reads
the text). Check: old and new code on the same input give byte-identical
outputs (timer-zero analysis, contact sync, uptime model's contact reading,
battery check, dating with and without the observation window), and the
full run reproduces all of `Output/data/`, the working copies and the
pipeline files of the previous run byte for byte, with the same printed
summaries.

Logs read once (2026-10-29). Steps 1-4 still read the original logs as text
(duplicate check, logger clock, fake records, switch relabels, first ID
split, working copies: about 6 passes, 200 s), and the hard rule check, the
timer step check, the Base mislabelling check, `data_quality_reports.py` and
the slides read the working copies or the originals as text again. Since
then every log is read once: step 1 writes the cache of the original logs
(`Output/pipeline/original_logs_cache/`), which holds every line (also the
lines the parsers skip, and the original text of the 22 contact lines with
`RSSI: -0`) and the SHA-256 of the file, so that the file can be rebuilt
from it byte for byte; the working copies are made from that cache with
their own cache, and a copy is written again only when its recipe (source,
relabels, recovered lines) changed. All checks and reports read the cache.
The cache stops with an error if a log could be split into lines
differently by different readers (no final line end, mixed line ends, a
carriage return or another line break character inside a line, text not
UTF-8). Run time (old → new): steps 1-4 198 → 42 s, round 2 working copies
41 → 3 s, Base mislabelling check and the checks after round 2 307 → 121 s
(hard rule check alone 301 → 14 s), reports 683 → 74 s (no longer all lines
as Python objects, which needed up to 17 GB); full run with `--reports` 3 248 → 2 212 s. Check: old and new
code on the same input give the same results (steps 1-4 incl. the working
copies byte for byte, Base mislabelling check on the original logs and the
working copies, H1 contact excess, timer step records, data quality
reports, the line counts of the slides), and the full run reproduces all
258 output files and the 112 working copies of the previous run byte for
byte, with the same printed summaries. The old text readers were removed.

Model steps (2026-10-29). Of the remaining time the uptime model (4 runs of
about 200 s) and the contact sync (4 runs of 150-170 s) were two thirds.
Contact sync: the matching of encounter edges between two beacons for the
drift fit searched each pair of units on its own (about 600 000 small
searches per pass); now one search over all pairs (each pair's edge times
shifted into a range of their own; the times are whole multiples of the
bin, so the search is exact). Uptime model: the beacons of an iteration are
solved in parallel (6 processes; each beacon's cutting depends only on its
own readouts and on inputs fixed before the solve), and the offsets
between a readout's encounter edges and its partners' are computed in one
batch, searched only within the readout's possible boot range (widened to
whole seconds; the exact bounds are applied after, as before). Per run:
contact sync 167 → 83 s, on the model's periods 150 → 65 s, uptime model
200 → 116 s; full run with `--reports` 2 212 → 1 482 s. Check: contact sync
and uptime model of old and new code on the same input give identical
outputs, and the full run reproduces all 258 output files, the working
copies and the printed summaries byte for byte.

Dating and plots (2026-10-29). In the duplicate check of the dating, a
search per readout in the 32-bit line numbers of its log file copied the
array on every search (163 s); the daily contact files are written in
parallel, the local time text made once per second. The timer-zero
analysis spent most of its time drawing the 168 beacon images
(`timer_zero_plots/`); they are drawn in parallel with the same calls.
Per run: dating 327 → 83 s, timer-zero analysis 68 → 23 s; full run with
`--reports` 1 482 → 1 124 s. Check: dating and timer-zero analysis of old
and new code on the same input give identical files (the images byte for
byte), and the full run reproduces all 258 output files, the working copies
and the printed summaries. Tried without gain: dropping neighbouring
duplicates before the sort of the encounter transitions.

Step 8 (first run 2026-10-21 on the working copies, 4 min): no contacts under
a wrong ID; 221 ID switches between contact lines (T5), all the next beacon's
own contacts (A's end and B's header lost); no fake block left (T7, the fakes
are reconstructed in step 4); 3 `Transfer complete` lines under another ID on
2026-08-13 before the deployment (T6, "check", no contacts affected).
On the working copies the IDs 0–9 / 48–57 are corrected, so the
self-exclusion (T2, T5) is tested for all readouts except those the ID split
left undecided (`id_split_analysis.csv`, passed as `id_split_csv` /
`--id-split-csv`); before, all readouts with ID1 0–9 before 2026-08-15 22:00
counted as not testable. Result (2026-10-21): the 95 readouts not testable
before (59 before the deployment, 36 in the deployment before the Base fix)
are tested, no self-sighting (this also confirms their ID split); 4 more ID
switches before the deployment decided by self-exclusion.

Result (2026-10-17): no duplicates, the logger clock never goes back; 145
fake records; 321 disputed readouts, 176 → 48–57, 105 kept 0–9, 40
undecided (38 without `Current Timer` and contacts); the second split
decides none of them (the two with `Current Timer` lie where d and d + 48
booted within minutes) and contradicts no first decision; against the
hand-cleaned logs 171 changed and 94 kept agree, 5 stubs differ. Uptime
model on the working copies as in section 19; its one contradiction in the
data lies at beacon 7 on 2026-08-13, where a readout stayed undecided
between 7 and 55. Run time about 50 min.

Rerun (2026-10-19, consensus H4 bounds, with `timer_step_check.py` as the
last step of 5): the same 145 fake records, split, second split (0 new, 0
contradicted) and validation (171 + 94 agree, 5 stubs differ, 39
undecided). Uptime model on the working copies: 1 088 periods, H4 36
readouts in the deployment, H5 0, H7 satisfied, 2 contradictions before the
deployment (7 p2, 9 p2) — identical to the model on the hand-cleaned logs
(all 8 542 readouts matched by file and line have the same ID and boot).
Timer step check: 15 p2 accepted, 15 554 records marked. Run time 61 min.

**Recheck of the whole pipeline (2026-10-30).** Every step checked for its
justification and its inputs; changes (`run_pipeline.py --reports`, then
the dating again; compared with the outputs before, file by file):

- **Reproducibility.** `timer_zero/reboot_hints.csv` (rule a″, 13.2, 14.8)
  was appended to and kept from run to run. A run from an empty output
  folder (2026-10-29) found 9 hints instead of 13 and reproduced all other
  outputs byte for byte; the hints of round 1 are used in round 2. Without
  rule a″ altogether (tried) the cut points stayed the same, but the boots
  of 336 periods moved (median 0 s, 95 % 6 s; 61 p7 by 120 s, its contact
  boot falling below the threshold for reliable), with the same quality
  (both-sides check per class within noise, H4 before the enforcement 154 →
  169 pairs). Kept therefore, but per run: `run_pipeline.py` clears the file
  at its start.
- **Own period between the periods** (section 19): 90 on 2026-08-13 stays in
  p1.
- **Battery check on the final model** (section 17): run again with
  `--model uptime` after the H4 enforcement; the periods never read out and
  `reboots.csv` now use the final boots (4, 24, 36, 60 move by 1 – 91 s;
  36's period never read out started 44 s before the end of its recorded
  records and was missing from the coverage — 38.8 h shown as `erased by
  reboot` —, now it starts after them). `beacon_resets.csv` and `reset_histogram.png`
  (boots before the model, read by nothing; `reboots.csv` has every boot) are
  removed.
- **H4 enforcement idempotent** (19.4).
- **Contact accounting on the model's periods** (13.8, dating): the lost
  records "inside a readout" and "at the start of a readout" use it.
  Readouts without `Current Timer` and without contacts get no period in it,
  as in the timer-zero analysis (a stub is no "next readout"; with them, 4
  short gaps at the start of a readout had dropped out). Result: `lost_records.csv`
  unchanged outside 2026-08-13 (there 90 and the partner counts of 31 gaps).
- **Coverage without overlaps** (section 21).
- **Removed:** the images, delays per file and issues of the timer-zero
  analysis (they showed its own periods, only the start of the model),
  `beacon_summary.csv` and `current_issues.csv` (sections 8, 9).
- **Not changed, for the record:** steps 7 (result of the split) and 8
  (Base mislabelling check), the timer step check and the hard rule check
  find nothing to change in this data; they stay as guards (they run in
  parallel with other steps). 3.7 M records with the same beacon and timer
  in one readout are several advertisements caught in one scan (BI 2.1),
  not duplicates.

Effect on the data: contacts unchanged except 90's readout on 2026-08-13
(before the observation window); status counts unchanged.

**Second recheck (2026-10-30): the assumptions the steps rest on.**

- **H6, delay-free first readouts.** No first readout of a log file has a
  lower bound from its own contacts above 10 s. In 44 files the logger
  stalled right after the first readout (the following readouts with median
  delays of 5–25 min; e.g. `dsa_20260824_0837.log`, a 14 min stall, the
  readouts after it dated 2–4 s after the first, in log order): the first
  readout is on time, the stall comes after it (H4).
- **Same beacon and timer within one readout.** No run of copies is longer
  than 9 lines, the copies are 4–15 lines apart: several advertisements in
  one scan, not a batch sent twice; `ok` stays right.
- **Synthetic validation** (`validate_model.py --refit --headerless 3`, after
  the own-period rule of section 19): unchanged — test A 100 % found (95 %
  14 s, without contacts 25 s), test C 2 / 10 s, test D boundary 346 right /
  0 wrong / 3 undecided, own period 0 wrong.
- **Logger PC clock** (BI 3.2.5): the only time reference, not checkable
  independently; the delay-free readouts fit the model to a median of 1.6 s,
  so an error of the clock can only be seconds.
- **Drift fixed point.** One more contact sync `--model-periods` on the
  final model: drift change median 0.03 s/day, 95 % 0.39 s/day — the loop is
  practically converged. Exceptions: 154 (2.4 s/day, reliability 0.5) and
  147 (−19.02 → −16.86 s/day): its contacts want about −17 s/day, the H4
  enforcement (19.4) −19 to −20; about 30 s at the end of its 14-day period,
  within the precision of its records. Open (`ToDo.md` 41).
- **Round 2 also on hints** (14.8): `run_pipeline.py` ran round 2 only when
  the second split decided a readout; the hints of round 1 depended on that.
  Now round 2 also runs when round 1 found hints. Outputs unchanged (round 2
  ran anyway in this data).

**Third check (2026-11-02): unbacked assumptions.** Changes, each described
where it applies: the readout test of the lost records (section 21, new
state `not recorded (no readout possible)`), the drift term of the
precision per beacon (21), the one-sided window of the reboot hints (13.2,
14.8), guards for convergence, round-2 hints and the delay bound (19), the
validation with four seeds and the delay class (19.1). Full run with
`--reports` 1 173 s. Compared with the outputs before, file by file: the
uptime model, the checks and the both-sides check byte-identical; changed
only `reboot_hints.csv` (9 → 3), the precision column of the contacts,
self-reports and eco sessions, and 60 causes in `lost_records.csv` /
`coverage.csv`; the dating run again with both new rules switched off
reproduces all of `Output/data/` byte for byte (`lost_records.csv` apart
from its two new columns). Found and not changed: on the setup night (until
2026-08-13 10:55) beacons recorded below the default RSSI thresholds
(`ToDo.md` 47); the boots do not converge exactly (19).

Same day, second part: `erased by reboot` only where the partners saw the
beacon, the rest `not seen (off or alone)` (section 21; `coverage.csv`,
`reboots.csv`); the checks after round 2 in two parallel branches after the
H4 enforcement (`run_pipeline.py`; check phase alone 120 – 123 s → 102 –
106 s, measured twice each on the same outputs; full run with `--reports`
1 204 s); `clock_stop_check.py` on the model's own boots (19.3; it changed
`clock_stops.csv` when run again after the pipeline). Compared with the
outputs before: only `coverage.csv` and `reboots.csv` differ, identical to
a dating run with the new rule alone; the checks run twice more reproduce
all other files byte for byte. Outputs checked afterwards for need and age:
all files of `Output/data/` and `Output/pipeline/` have a writer in the
pipeline (or `validate_model.py`) and a reader or a documented use; stale
were test E (`lost_records_validation.csv`, from 2026-10-29: rerun with
`--validate-lost 500`, the same result as on 2026-10-28 — 167 of 167
found, 18 more, gap end median 13 s; the rest of `Output/data/` unchanged
byte for byte) and the slides (from 2026-10-30: regenerated; fixed texts
brought up to date — the new states on the loss slide, the clock stops of
beacon 15 repaired, the holes inside readouts located). The validation
files (`uptime_model_validation*`) are current: the uptime model has been
byte-identical since their run.

**Output files combined (2026-11-03).** `not_dated_contacts.csv`,
`not_dated_self_reports.csv` and `not_dated_eco_sessions.csv` became one
`not_dated.csv` (column `Record`; 21); `both_sides_check_summary.csv` went
into `both_sides_check.csv` (the totals per precision class as rows with
`ID1` = `all`, column `within_30s` for every row); `contact_sync_readouts.csv`
(14.9) is no longer written: nothing read it, the final delays per readout
are in `uptime_model_readouts.csv` (writer removed). 59 → 55 result
files. Full run with `--reports` 1 123 s; compared with the outputs before,
only these files differ; their content is the same (the per-beacon rows of
the both-sides check byte for byte, the totals equal to the old summary, the
not-dated records empty as before); the slides' source differs only in the
date. Second step the same day: `lost_records.csv` went into `coverage.csv`
(13 columns `Lost from` … `Readout threshold` on the pieces of each gap: the
coverage cuts a gap by eco sessions, clock stops and seen stretches, so the
evidence is per gap, repeated on each piece; empty on silent pieces);
`logger_stalls.csv`, `lost_records_validation.csv` (written by the dating)
and `both_sides_check.csv` (option `--quality-dir`) moved to
`pipeline/quality/`: checks of the dating, not data for the evaluation.
`Output/data/` now holds 25 files (19 daily contact files, self-reports, eco
sessions, battery, coverage, reboots, not dated); 54 result files in all.
Dating rerun with `--validate-lost 500` (107 s), both-sides check, slides:
the 8 old columns of `coverage.csv` byte-identical; all 1 824 gaps found on
their pieces (2 029 pieces: 1 931 read out not logged, 65 not recorded, 22
storage full, 10 erased by reboot and 1 not seen before a first boot) with
the same evidence and the cause as the first piece's state; no loss-state
piece without evidence; the pieces' hours sum to the gap's within the
rounding (0.006 h); the three moved files byte-identical; slides' source
only the date. A full run with `--reports` (1 096 s) reproduced all outputs
byte for byte.
Third step: `timer_zero_readouts.csv` (13.7) went into `readout_overview.csv`
(the same 51 466 readouts; columns `Current Timer line`, `Line time of
Current Timer`, `Zero observed`, `New period reason`, `Flags` added, the rest
it shared); `hard_rule_check.py` reads the line time from there instead of
joining the two by file and line; the writer is removed. 53 result
files. Full run with `--reports` (1 118 s): only `readout_overview.csv`
differs, its old columns byte-identical, and the old `timer_zero_readouts.csv`
rebuilt from it row for row; all other outputs (the hard rule check's among
them) byte-identical.
Slides (`make_presentation.py`, in `Presentation/` since 2026-11-04): new slide *Data for the evaluation* (the
files of `Output/data/` with their rows, counted in each run, and contents;
where the checks are); the open point on the evaluation names `coverage.csv`
and the window.

**Study folder and configs (2026-11-03, after the move to this repository).**
The inputs are in a study folder outside the repository (`--study-dir`,
default `../../Studies/DSA2026`): `Logs/`, the beacon lists, optionally
`battery_replacements.csv` (without it there are no documented replacements),
`study.toml` and `logger.toml`. `study.toml` holds every value of the study
(section 7), including the logger's ID bug with a switch (`[id_bug] enabled`;
off: no readout is disputed). `logger.toml` holds the logger's format: the
messages on the wire (fake records, BI 3.2.2: decoded without the logger's
code) and the log lines, each under a key; all patterns of the log lines, the
ID field and the line time format are made from it (a space after `:` or `,`
any white space, a `-` before a field its sign), checked against the former
patterns on 10.4 million line texts of the logs and variants of them: no
difference. The cache file names carry a fingerprint of the format.
`run_pipeline.py` records the inputs it used (`pipeline/inputs.json`); the
dating refuses a pipeline output made from other inputs (tested: a changed
comment in `study.toml` is no change, a changed value is). `id_split_analysis.py`
run alone does steps 2–4 with the pipeline's code (the comparison with the
hand-cleaned logs and `relabeled_logs/` removed); `validate_model.py` reads
the cache instead of the log text; the check of `without_pipeline.py` against
the logger's old code (`--validate`) removed. Column `Log File` → `Log file`
(`not_dated.csv`, `sanity_findings.csv`, `fake_records.csv`,
`unreliable_time_records.csv`), `Line Time` → `Line time` (`fake_records.csv`).
Full run with `--reports` on a copy of the study folder with the battery
replacements (1 439 s) against the outputs of 2026-11-03 09:37: 161 files
byte-identical, the 112 working copies among them; the four files with the
renamed columns identical apart from the header; new `inputs.json`. The dating
run alone reproduces `Output/data/` byte for byte; `id_split_analysis.py` alone
gives the fake records and switch relabels of the pipeline and its first split
(the one readout decided in round 2 by method F apart). The study folder of
DSA2026 has no `battery_replacements.csv`: full run with `--reports` (1 006 s)
against the run with it: only the battery check, `unread_uptime_periods.csv`,
`reboots.csv`, `coverage.csv` and the model state differ; the uptime periods,
boots, drifts, all records and the working copies are byte-identical.
`reboots.csv`: 168 first boots and 927 resets instead of 168, 899 resets and
33 battery replacements - the 5 periods that only the documented
replacements show (started by one and never read out) are missing.

**Robustness (2026-11-04).** No change of method; the outputs stay the same.
The cache is in one place, `pipeline/cache/` of the pipeline's output folder,
one subfolder per log folder named by the folder and a digest of its full
path: nothing is written into a log folder, and two folders with files of
the same name, size and time no longer share a cache file. Step 4 deletes
working copies without an original (left from an earlier run), with their
cache. The tolerance eps of the hard rules is computed in one place
(`hard_rule_check.eps`, `eps_array`), also where the uptime model, the dating
and the validation used their own copy of the formula; the constants that
were copies of each other are defined once (`DAY_S`, the plausible contact
timer of 30 days, the drift terms from `TOL_PER_DAY_S`), the two limits for
contacts above `Current Timer` are named by their question
(`READOUT_TIMER_EXCESS_S` 1 h: a contact of the readout, 14.1;
`DELAY_BOUND_EXCESS_S` 1 day: the delay lower bound, 13.1), and the timer of a
`Status` message read as a contact (524301, T1) is computed from `logger.toml`.
Every script takes `--study-dir`. `timer_zero_analysis.py` and
`contact_sync_analysis.py` use the reboot hints (`reboot_hints.csv`) only with
`--reboot-hints`, which the pipeline passes; run alone without it they do not
depend on earlier runs. Tests in `tests/` (24, seconds: configs, line
patterns, fake records, ID bug, eps; 27 from the next entry on); `tools/compare_outputs.py` compares two
output folders. Full run with `--reports` on the copy of the study folder with
the battery replacements (1 461 s; a working copy without an original planted
before it: deleted) against the run of 2026-11-03: all 166 files
byte-identical; the dating run alone reproduces `data/` byte for byte and
writes no cache file.

**Delay bound and module state (2026-11-04).** The delay lower bound (13.1)
uses contacts at most 8 h above `Current Timer` instead of 1 day: the same
limit as the longest logging delay assumed for the reboots (`MAX_DELAY_S`,
19), now one constant in `lib/postprocessing.py` (`DELAY_BOUND_EXCESS_S =
MAX_DELAY_S`). `--valid-date-start` / `--valid-date-end` take the full
`YYYY-MM-DD HH:MM:SS` in every script (`pp.parse_time`; before, the steps also
took a date alone). No setting is kept as module state any more: the undecided
readouts of the Base mislabelling check are an object passed along
(`base_mislabel_check.Undecided`), the location tags are cached per file
(`pp.location_tags`), and the uptime model's process pool lives as long as one
fit. Tests: 27. Full runs with `--reports` on `Studies/DSA2026`: with the 8 h
limit (1 085 s) all 166 files byte-identical to the run with 1 day before it,
so no contact between 8 h and 1 day above its `Current Timer` set a bound;
with the module state removed (1 033 s) again all 166 identical. The slides
(`make_presentation.py`) are no longer part of the postprocessing: they live
in `Presentation/` beside `Postprocessing/` and read its outputs.

**Beacon 147's drift (2026-11-04, analysis only, ToDo 41).** 147 (location
tag, Mensa) has one period of 14 days (2026-08-13 00:32 – 2026-08-27 09:09,
33 readouts); contact sync −19.01 s/day, after the H4 enforcement −20.41
(about 20 s apart at its last readout). Do the contacts show a drift that
changes within the period? Three read-only tests on the dated data: encounter
edges (as the both-sides check) exist only on its first 2 days of uptime
(445 edges, 106 partners); later its records are few (1 000 – 1 600 per day,
median RSSI −80 to −88 dBm) and give no clean edge. The peak of the time
differences to the partners' records of 147 per day has no contrast (1.2 –
2.4: long joint stays). The share of its records inside the partners'
encounters with it, over offsets ±900 s, peaks at −15 … +5 s on days 0 – 1.5
(221 000 records) but is flat within a few points over ±5 min on days 9.6 –
14.2 (6 100 records; best offsets −160 … +440 s). About 95 % of its records
lie in the first 1.5 days: the contact sync's drift comes from there, the
drift over the whole period from the log order of its late readouts (logged
10 min – 1.8 h late; H4). The contacts neither show nor exclude a change of
drift; 147's dating stays as it is. Open: its late records carry the
precision of the boot's class (contacts, about 16 s) plus the drift term from
the nearest readout, although after day 3 only the log order (28 s for a
location tag) and the drift difference (about 20 s) fix their time (changed
in the next entry).

**Time precision from each readout's own evidence (2026-11-04).** The
precision of a record was the best class of its period (wherever the readout
with that class lay) plus the drift term from the nearest `Current Timer` of
any class. A period of a location tag runs 10 – 17 days; one delay-free
readout (the first of a log file) gave all its records 13 s, also those 10
days from it next to a readout fixed only by log order (28 s). Check over the
`ok` contacts with the bound from each readout's own class: 8.84 M (13.7 %)
more than 5 s too optimistic, all of 43 location tags, by at most 15 s (the
log-order readouts bound it). Now (`model_dating.Model._envelope`): the best
over the period's readouts of (its class, at least the H4 move, + drift term
× distance of the timers); the lower envelope of these cones, computed from
both sides in one pass per period. The boot precision of `reboots.csv` stays
the period's best class. Tests: 29 (the envelope against brute force).
Dating and reports rerun on the pipeline output of the same day: all times
and all other columns unchanged; `Time precision [s]` never smaller, larger
for 44.6 % of the `ok` contacts (22.7 M of location tags, 6.0 M of persons;
median +3.0 s, 95 % +15.0 s, max +15.1 s); median of the `ok` contacts 16.1
→ 16.3 s, 95 % 29.0 → 29.3 s, within 20 s 86.6 → 74.2 %. Battery readings,
self-reports and eco sessions: only their precision (max +15.0 / +10.2 /
+8.7 s). `reboots.csv`, `coverage.csv`, `not_dated.csv` byte-identical.
Both-sides check: edges move from delay-free (26 874 → 12 263, within 30 s
70.0 → 69.5 %) to contacts (176 145 → 189 374, 67.9 → 68.1 %) and log order
(3 072 → 4 454, 58.1 → 60.8 %): the moved edges behave as their new class.

**Review of the code (2026-11-04).** Two module states were left despite the
entry "Delay bound and module state" above: the eco sessions of the uptime
model (now `eco` of `uptime_model.contact_boots`, filled by `read_contacts`
in the fit; `timer_step_check.py` passed none, as before; since
2026-11-05 it passes them, entry of that day) and the rows of
test E of the dating (now returned by `model_dating.lost_records`). Removed:
the unused `model_dating._contact_key`. `h4_enforce.py` deletes
`quality/h4_enforce.csv` when its linear programme finds no solution (before,
a list from an earlier run stayed and gave the dating its moves).
Docstrings of `both_sides_check.py` (the class is read from the record's
precision) and `hard_rule_check.py` (which list of fake records) corrected.
Full run with `--reports` (1 051 s): all 189 files byte-identical to the run
before. Tests: 29.

**Limits for other studies (2026-11-04).** Docstrings of `base_mislabel_check`,
`id_split_analysis`, `contact_sync_analysis`, `hard_rule_check` and
`timer_zero_analysis` named DSA2026 values (ID-bug end, Base fix, delay-free
time, flash start, common drift); they now name the `study.toml` keys. Cases
DSA2026 does not have, handled now:
- contact sync without any pair (`solve`: nothing corrected), without a
  delay-free readout of a synced unit (the median of nothing made every shift
  NaN; now the level of the timer-zero analysis is kept, with a message) and
  without a beacon of low noise on both day halves (the drift fit gave NaN;
  now every beacon gets the common drift, with a message); the printed
  summaries no longer fail on empty arrays;
- ID pair keys (`contact_sync_analysis.transitions`, `clock_stop_check`,
  `uptime_model` co-presence and fit) assumed IDs below 1000 or 1024; the
  base is now the largest ID + 1 of the data (same key order);
- the duplicate check never compared files under 64 KiB (no head digest).
  Such a file is now compared with every file that starts with the same
  line; the cache stores the SHA-256 of the first line (`logcache.VERSION`
  5, all caches rebuilt once). DSA2026: 6 files under 64 KiB, none a copy.
Full run with `--reports` (1 252 s, 130 s of it the cache): all 189 files
byte-identical. Tests: 33 (`test_study_limits.py`: small duplicates, IDs above
999, no pairs). The base check's three "check" readouts (16) are single
`Transfer complete` lines: no contacts, no self-reports.

**Validation cache, record evidence removed, ToDo split (2026-11-05).**
`validate_model.py` reused `uptime_model_validation_state.pkl` without a check;
the state now holds a digest of `uptime_model_periods.csv`,
`uptime_model_readouts.csv` and `uptime_model.py` and is fitted again when one
changed (the cache of the earlier runs had none: fitted again once, 8 min;
the next run took the cache, same results). The option `USE_RECORD_EVIDENCE`
(switched off since 2026-10-20, section 19) is removed with
`record_evidence`, its constants and its two always empty columns of
`uptime_model_readouts.csv` (`Boot from the partners' records`, `Records
peak`; read by nothing). Full run with `--reports` (1 391 s): 188 of 189
files byte-identical, `uptime_model_readouts.csv` the same apart from the two
columns. `ToDo.md` split: the postprocessing and evaluation points in
`SocialEnergyPrivate/ToDo.md`, logger and firmware (1, 21–24, 28) stay in
`BeaconNRF54/ToDo.md`; the numbers are kept, new: 51 (end-to-end test), 52
(eco sessions in `timer_step_check.py`); 13, 17, 20 and 27 marked done
(nothing left for the postprocessing).

**Gaps with both causes, regression test (2026-11-05).** A gap between
readouts named one cause: `dropped (storage full)` if a `Contact Count` of the
readouts around it reached 215 000, else `read out, not logged`. Room 118
(ToDo 29) had both: full on 2026-08-21, 183 427 contacts left on the beacon
after its last logged readout that day, 502 at the next logged readout
(2026-08-29). A full flash discards its oldest blocks but its count stays
full, so a count that falls below what was left needs a readout that was not
logged. New state `dropped (storage full) and read out, not logged`
(`model_dating.BOTH_CAUSES`): the readout after the gap holds more than 1 000
(`EMPTIED_MARGIN`) contacts fewer than the one before left (`Contact Count`
− received), and the flash was full (a count ≥ 215 000) or the contacts left
plus the own records expected in the gap (readout test) reach 215 000. 9 gaps,
800 h, all location tags (117, 118, 121, 123, 124, 127, 131, 134 from `read
out, not logged`, 150 from `dropped (storage full)`); 155 (full, then 0
contacts) not decidable: the readout after has no `Contact Count`. Only
`data/coverage.csv` changed (same rows; checked with the new regression
test). The slides show the new state. ToDo 45 (boots jitter by a few seconds)
accepted as it is. ToDo 51: a local end-to-end regression test,
`Postprocessing/regression/regression_test.py` (not in the repository,
`.git/info/exclude`): `save` stores the SHA-256 of every output file (and for
CSV files header and line count) as the baseline, `check` compares, `run`
runs the tests and `run_pipeline.py --reports` first (`Output/presentation/` of the
slides is not compared).

**Eco sessions in the timer step check (2026-11-05, ToDo 52).**
`timer_step_check.py` matched its timer blocks (section 19.2) with all own
records, also those made during the beacon's eco sessions (one scan per
300 s, no edge evidence), unlike the uptime model. It now reads the sessions
with `read_contacts` and gives each block those of its readout. Regression
test: only `quality/timer_step_check.csv` changed; the one step found (15
p2, 8 550 s, explained by its clock stops) stays, its later clock now taken
from the block at timer 68 150 – 82 550 s (peaks 148 / 79, before
82 550 – 96 950 s, 88 / 37); `unreliable_time_records.csv` and all data
unchanged.

### 21. Dating the records with the uptime model (`model_dating.py`, `generate_output.py`)

`generate_output.py` dates every record with the uptime model (the previous
dating from the nearest `Current Timer`, which took the line time as the
readout time and ignored the drift, was removed on 2026-10-22):

    real time = boot of the record's period + timer × (1 + drift of the period, 19.4)
                + the shifts of the period's clock stops before the timer (19.3)

Input: the output of `run_pipeline.py` (`Output/pipeline/`): the working
copies of the original logs (`logs/`), the uptime model, drifts and fake
records (`timer_zero/`), `quality/unreliable_time_records.csv` and the
undecided IDs (`id_correction/id_split_analysis.csv`). Before writing,
`generate_output.py` refuses duplicate original log files and a stale
pipeline (working copies not the same files as the originals, or a pipeline
output made from other inputs: `inputs.json`, section 20).
Output to `Output/data/`. `run_pipeline.py` runs it as step 9; alone it
only repeats the dating on an existing pipeline output.

**Period of a record.** Contacts: the period of their readout (a reboot
erases all stored contacts). Self-reports and eco sessions, which survive a
reboot in flash: the period of their readout if they fit it (not after the
readout's line time + eps), otherwise the latest earlier period they fit
(not after the boot that ended it + eps). Readouts without `Current Timer`:
the period the uptime model assigned (section 19,
`uptime_model_headerless.csv`); otherwise the one of the two neighbouring
periods their contact timers fit (not after the readout's line time, not
before the previous readout's real time, ± eps), if exactly one.

**Every record is kept**, with two columns:

- `Time precision [s]`: 95 % bound from the validation (19.1), the best
  over the period's readouts with `Current Timer` of (how that readout fixes
  the clock + the drift term between it and the record; since 2026-11-04,
  see the entry of that day in section 20) — a delay-free readout 13 s, a
  readout with a reliable contact boot 16 s, the other readouts of a period
  with a log-order prediction (references of other beacons before and after
  in the same log file at most 600 s apart, column `Log-order readouts` of
  `uptime_model_periods.csv`) 28 s, for person-worn beacons 40 s (19.1),
  only the delay cost 90 s; the drift term is the drift uncertainty per day
  between that readout's `Current Timer` and the record's timer (before: the
  best class of the period plus the drift term from the nearest `Current
  Timer` of any class): 1.5 s/day, or
  twice the beacon's drift uncertainty (`Drift sd` of `contact_sync_drift.csv`,
  95 %) where larger (since 2026-11-02; before 1.5 s/day for all, although 22
  of 168 beacons have a drift sd above 0.75 s/day, up to 1.12: 2, 30, 69, 78,
  86, …) — and 60 s per clock stop before the record's timer (19.3). Effect:
  1.0 % of the `ok` contacts in the observation window get a larger
  precision, by a median of 0.1 s (95 % 0.3 s, max 2.3 s): most records lie
  within a day of a `Current Timer`. A period whose boot the H4 enforcement moved (19.4) has at
  least the move as the precision of each of its readouts: the move shows how far its evidence
  was off (2026-10-26; of 30 changed periods three lie above their class:
  99 p2 40 → 58 s and 34 p1 16 → 29 s, both on 2026-08-13 before the
  observation window, and 61 p4 16 → 19 s).
- `Status`: `ok`; `fake` (BI 3.2.2); `unreliable time (two clocks in one period)` (19.2);
  `ID undecided (0-9 / 48-57)` (readouts listed as undecided in
  `id_correction/id_split_analysis.csv`);
  `invalid: ID2 out of range`, `invalid: RSSI out of range`, `invalid: timer
  after its log line (H1)` (H1 for the single record: dated with its period's
  boot, it lies after its own log line + eps, so its timer is corrupt; this
  replaces the fixed upper timer limit);
  `duplicate` (the same contact — ID2, timer, RSSI — was already received in
  an earlier readout of the same uptime period, BI 3.1.4; the first copy
  stays `ok`; identical records within one readout are not duplicates).

Records without a period get no time and go to `not_dated.csv` (column
`Record`: contact, self-report or eco session; with log file, line, timer and
reason; the name does not match `contacts_*.csv`), as do
records dated outside the hard limits of the data (below). Self-reports and eco
sessions with the same beacon and time are written once (sent twice, BI
3.1.4). The previous outputs are not used as seeds. The evaluation has to select the status it uses (normally `ok`).

**Result on the pipeline output (2026-10-19, window 2026-08-13 18:00 to
2026-08-29 09:00).** Contacts in the window 55 078 016: ok 55 029 223,
duplicate 33 247, unreliable time 15 516, fake 30, invalid 0, ID undecided 0
(the undecided readouts have no contacts in the window); not datable
51 179, outside the window 9 284 237. Self-reports and eco sessions as below.
Run time 16 min. Both-sides check: 33.29 M records, |Δt| median 6 s, 90 %
59 s.

First run on the hand-cleaned logs (2026-10-19, before the duplicate
status). Contacts in the window 55 078 016: ok 55 062 470,
unreliable time 15 516, fake 30, invalid 0; outside the window 9 284 237;
not datable 51 179 (47 051 in readouts without `Current Timer` whose
period stays ambiguous, 4 128 in readouts with `Current Timer` but without
a period, e.g. IDs out of range). Self-reports: ok 5 442, unreliable 37, not
datable 11; eco sessions: ok 2 273, unreliable 1. Run time 13 min.

**Test E: lost-records check** (`generate_output.py --validate-lost N`,
`quality/lost_records_validation.csv`). A complete readout with `Current Timer`
between two others of its period is treated as not logged; expected: a gap
from the end of the readout before it to the oldest contact of the one after
it. Result (2026-10-21, 310 cases): of the 175 with at least 10 partner
records in the removed stretch, 175 found (100 %); 16 more found although
the stretch itself had fewer (the gap runs on to the next own contact and
collects partner records there). Gap end after the removed readout's real
time: median 15 s, 90 % 5.6 h — the gap runs on through a time without own
contacts (e.g. a quiet night) up to the next own contact; the lost interval
is therefore an upper bound in time, the loss itself is certain.

**Lost interval = where the partners saw the beacon (2026-10-22).** Inside
a gap, the lost interval runs from the partners' first to their last sighting
of the beacon (± 60 s): before and after, it had no contacts, so nothing of
its own is missing there (the full gap stays in the lost-records columns of
`coverage.csv`, `Gap from` / `Gap to`). Read out, not logged 14 986 → 14 782 h. Test E (307
cases): 173 of 173 found; lost interval end after the removed readout median
17 s (partners' last sighting before it: median 22 s); the 90 % tail of about
6 h remains — there, more readouts were lost after the removed one, and the
loss really goes on.

Test E rerun on the final model (2026-10-28, `--validate-lost 500`, 300
cases): 167 of 167 detectable found (100 %), 18 more found although not
detectable; lost interval end after the removed readout median 13 s (10 %
5 s; partners' last sighting before it: median 20 s); the 90 % tail of
about 5 h remains.

**Both sides on encounter edges (`both_sides_check.py`).** The nearest
reverse record (below) is no check of the offset during long joint stays: a
reverse record lies within seconds of any time. Edges carry the timing: clean
encounter starts (at least 5 min without a record of the pair before) and
ends (5 min after), of encounters at least 60 s long, records at a symmetric
RSSI (14.2); each start (end) of A→B matched with the nearest of B→A within
±120 s; chance level from the partner's edges shifted by 1 h (about 6 % of
the matches). Result (all days):

| Boot of A fixed by | Edges | Median Δt | Median \|Δt\| | 90 % | ≤ 30 s |
|---|---|---|---|---|---|
| delay-free readout (13 s) | 26 878 | 0 s | 20 s | 80 s | 70 % |
| reliable contact boots (16 s) | 176 471 | 0 s | 20 s | 80 s | 68 % |
| log-order predictions, room tags (28 s) | 2 675 | 0 s | 30 s | 100 s | 56 % |
| log-order predictions, persons (40 s) | 1 139 | 0 s | 20 s | 90 s | 61 % |
| delay only (90 s) | 247 | 0 s | 20 s | 94 s | 65 % |

Final run 2026-10-26 (symmetric delay cost, co-presence, log-order class
for persons), 207 410 matched edges. The class is read from the
precision of A's record (the best class of its period plus the drift term;
since 2026-11-04 the best over the period's readouts of their class plus
the drift term from each, section 20 entry of that day), so a record far
from its period's `Current Timer` can count in the next class. Before (2026-10-21, classes 48 / 16 / 90 s): reliable contact boots
200 084 edges, 68 %; delay-free 1 909, median +10 s, 61 %; delay only 5 093,
median +10 s, 56 %. Edge noise is about 20 s (10-s bins, a 7-s scan on each
side). Median offset per beacon (≥ 30 edges): no beacon beyond 30 s (before:
beacon 3, +50 s). The classes are consistent with their precision; no class
is offset any more (the boots from the delay alone were +10 s late before the
symmetric delay cost, section 19).

What the check can and cannot show (2026-10-30): for boots fixed by
reliable contact boots it is not an independent test — the model fitted
those boots to the same kind of evidence (encounter edges against the
partners', section 19), so a median of 0 s there is expected by
construction; it shows that the dating keeps what the model fitted. It is a
test for the boots fixed otherwise (delay-free readouts, log order, delay
only), whose evidence does not contain the edges. The independent evidence
on the accuracy is the synthetic validation (19.1: injected reboots, late
readouts, delay-free readouts as ground truth, readouts without `Current
Timer`) and the stability of the boots between the even and odd days
(section 19).

**Validation: both sides of an encounter.** For every contact A→B (status
ok), the time to the nearest record B→A of the same day (within 300 s);
independent of the costs of the model.

| | Legacy dating | Model dating |
|---|---|---|
| Records with a reverse record within 300 s | 32.40 M | 33.31 M |
| \|Δt\| median / 90 % | 7 s / 81 s | 6 s / 59 s |
| within 10 / 30 / 60 s | 64.7 / 82.2 / 87.8 % | 66.7 / 85.1 / 90.2 % |

No beacon (≥ 1 000 records) is shifted as a whole (median signed Δt within
±3 s for all 163). The remaining tail includes one-sided sightings near the
RSSI threshold, where the nearest reverse record belongs to another part of
the encounter.

**Coverage (`coverage.csv`).** Per beacon, non-overlapping intervals that
cover the whole data (from the first boot to the last readout; the window,
if one is given), so that the evaluation can tell "no contact" from "no
data":

| State | Interval | Own records |
|---|---|---|
| `recorded` | boot of an uptime period to the end of its delivered records (a complete readout: its real time; an incomplete one or one without `Current Timer`: its newest contact) | exist |
| `erased by reboot` | end of the delivered records of a period to the next boot (also the rest of an incomplete last readout); before the first boot: from the first sighting by its partners (`coverage.csv`, `Where`: `before the first boot`); since 2026-11-02 only where the partners saw the beacon (below) | recorded, erased by the reboot |
| `not seen (off or alone)` | within such a stretch, where the partners did not see the beacon (below) | none: off, or without company (no contacts) |
| `never read out` | period started by a battery replacement and never read out (`quality/unread_uptime_periods.csv`) | lost |
| `not read out` | end of the beacon's delivered records to the end of the data | not available |
| `before first boot` | start of the data to the first boot, or to the first sighting by its partners before it | none |
| `no data` | beacon in `list_persons.csv` / `list_rooms.csv` without any uptime period | none |
| `dropped (storage full)` | within a period: a gap between consecutive readouts in which the partners recorded the beacon, with full contact flash (lost records) | discarded by the beacon (oldest blocks) |
| `read out, not logged` | the same without full storage | read out by the Base, not logged: lost |
| `dropped (storage full) and read out, not logged` | the same, both causes (since 2026-11-05, section 20): the flash ran full and a readout not logged emptied it | both |
| `not recorded (no readout possible)` | the same, but no readout of the beacon was possible in the gap (readout test, below) | none: the beacon did not record although its partners saw it |
| `silent (clock stopped)` | a clock stop (19.3): the beacon's last sighting before it to its first after it | none: the beacon neither recorded nor was seen |

The intervals are cut where an eco session of the beacon starts or ends;
column `Eco` marks the pieces with reduced scanning. A period's delivered
records can reach a few seconds past the next boot (within the precision of
the boots); `recorded` then ends at the next boot (since 2026-10-30: before,
4 overlaps — 9 by 7 s and 22 by 4 s on 2026-08-13 and 08-27, two from the own
period of 90, section 19 — and one hole; now none). The partners' records
of the beacon exist in every state except `not seen (off or alone)`.
Columns: ID, beacon type, from, to, hours, state, period, eco.

**Erased only where the beacon was seen (2026-11-02).** `erased by reboot`
assumed that the beacon kept recording from the end of its delivered
records to the reboot. The partners show where it did not: share of the
10-min bins of the observation window in which any partner recorded the
beacon — `recorded` persons 88 %, room tags 98 %; `erased by reboot` 74 %
and 66 %. The long stretches without a sighting are beacons that were off:
the outdoor room tags 86, 67, 30, 63, 87 from the setup readout on
2026-08-13 09:xx to their activation on 2026-08-15 01:33 – 11:44 (experiment
log: "not active yet"), 93 and 94 for 5 – 7 days, the replacement beacon 99
until it was handed out (2026-08-15 21:30, boot 21:48), the unused 69
(15 days, seen only in its first hour), 38, 55, 98 (7 – 9 days); each was
`erased by reboot` up to its boot and the boot a `reset` that "erased" up
to 366 h. Where the partners did not see a beacon (at the RSSI it records
too, 14.2) it had no contacts — off, or without company — so the reboot
erased nothing of that time, the same rule as for the lost records. Rule
(`_seen_split`): within `erased by reboot` (also before the first boot),
the partners' sightings at most `SEEN_GAP_S` = 30 min apart form a
stretch, widened by 60 s (`LOST_EDGE_S`); the stretches stay `erased by
reboot`, the rest is `not seen (off or alone)`. 30 min: partners in eco
mode record a beacon at most once per 300-s scan, so 10 min would cut real
company. Sensitivity (dating on the same model): `not seen` 3 849 h at
10 min, 3 265 h at 30 min, 2 932 h at 60 min, of 11 656 h. Check against
the 10-min bins (all RSSI): what stays `erased by reboot` is seen in 97 %
(persons) / 99 % (room tags) of its bins, like `recorded`; `not seen` in
12 % / 19 % (records below the symmetric RSSI). Result: `erased by reboot`
11 656 → 8 391 h, `not seen (off or alone)` 3 265 h; e.g. 69 2.2 h erased,
365.6 h not seen; 38 5.4 h / 219.5 h. The evaluation must not count `not
seen` as time without contacts of a present person: the beacon may have
been off.

**Reboots (`reboots.csv`, 2026-10-28).** Every boot of a beacon in time
order: the boots of the uptime periods with a dated record (the same
periods as the coverage) and of the periods never read out
(`quality/unread_uptime_periods.csv`, start = the partners' last sighting
before the replacement, no precision). Time precision: that of the boot
(its class, at least the H4 move), without the drift term. Cause: `first
boot`; `battery replacement <documented time>` when a documented replacement
(`battery_replacements.csv`) has its window (section 17, up to 75 min late)
around the boot (the nearest boot per replacement); otherwise `reset`. Own
records erased: from the end of the previous period's delivered records (all
of a period never read out) to the boot; since 2026-11-02 only the stretches
in which the partners saw the beacon (as `erased by reboot` in the coverage,
above): `Own records erased from` is the start of the first, `[h]` the sum
(11 777 → 8 476 h). Result (2026-10-28): 1 101 boots,
168 first boots, 28 battery replacements, 5 replacements whose period was
never read out, 900 resets; 11 777 h of own records erased (= `erased by
reboot` in the coverage without the 13 h before the first boot, plus the
96 h never read out).

**Battery (`battery.csv`, 2026-10-28).** The `Voltage` and `Contact Count`
of every readout with `Current Timer`, at the readout's real time with its
precision; status `ok` or `ID undecided (0-9 / 48-57)`. Replaces
`quality/measurements.csv` (logger time, section 10).

**Logger stalls (`quality/logger_stalls.csv`).** A stall shows itself (BI 3.2.3,
3.2.4): the logger stops reading the serial port (internal postprocessing,
PC asleep, logger hanging), the PC's receive buffer keeps the last data,
and these lines are stamped when the logger resumes. Rule: a gap of at least 30 min between consecutive lines of
a log file, followed within 60 s by a readout with `Current Timer` whose
real time lies before the gap (at most 60 s + eps after the last line
before it). The stall lasts from the newest real time of the buffered
readouts to the first line logged after the burst (the next file's first
line if the file ends with the burst). Columns: from, to, hours, log file,
line, last line before the gap, buffered readouts. Example: 2026-08-13
(`dsa_20260812_2350.log` line 148466): last line 00:58:10, then 415 lines
stamped 08:32:46–08:33:04 (158 with `Current Timer` 1 178, 2 s after the
newest contact of its readout at 00:58:10; 14 and 161 likewise at about
00:58:20), file ends, next file from 08:42:46 → stall 00:58 – 08:42; the
buffered readouts are dated with 7.6 h delay.

The stall list is informative; the loss is determined per beacon,
independent of its cause:

**Lost records (columns `Lost from` … `Readout threshold` of `coverage.csv`, on
the pieces of each gap).** Within a period the readouts in log
order must continue each other: the oldest contact of a readout reaches back
to the end of the readout before (with `Current Timer`: its real time;
without, assigned by the uptime model: its newest contact; for the first
readout: the boot), within eps. A gap means the Base read out the records in
between and they were not logged — or the beacon had no contacts. The gap is
kept only if the partners recorded the beacon at least 10 times inside it
(more than 120 s from its ends), counting only records at an RSSI the
beacon itself records too (14.2): it had contacts, so its own records are
missing. Without a reboot the firmware deletes contacts only after the
Base's confirmation or, with full flash, by discarding the oldest block
(BI 2.1, 3.4.2; checked in the beacon firmware 2026-10-21). Cause per gap:
`dropped (storage full)` if a readout of the period from the one before the
gap to the one after shows `Contact Count` ≥ 215 000, else `read out, not
logged` (BI 3.2.3: whole readouts lost while the logger pauses). Incomplete
readouts end at their newest received contact, not at their real time (the
rest stays on the beacon).

Result (2026-10-21): 1 668 gaps; read out, not logged 14 975 h, dropped
(storage full) 696 h (22 gaps, all room tags). Checks of the rule: eco
sessions overlap only 228 h of the gaps; with partner records ≥ −80 dBm only
(location tags) nearly all gaps stay (2026-08-20: 48 of 50 room gaps); the
readout after a gap holds mostly 5 000–10 000 contacts, just above the
Base's readout threshold (BI 2.1, button 0): one regular readout lost per
gap, no sign of the RAM ring (24 000); the lost hours per day grow with the
logger delay (about 0 before 2026-08-16, 1 100–2 000 h per day at a median
delay of 7–43 min). Examples: 147, no readout with data logged between
2026-08-16 13:54 and 2026-08-23 19:xx, whose oldest contact is from
2026-08-22 10:53 (same period, 354 339 partner records in the gap); room
118: full until 2026-08-15 (3 gaps "storage full"), then 184 747 contacts on
2026-08-21 19:51 and 502 on 2026-08-29 21:50 in the same period — emptied by
readouts not logged (the flash ran full again in between; the rule named the
last cause only; since 2026-11-05 both, entry of that day in section 20). Columns: ID, period, from, to, hours, cause, largest
`Contact Count`, partner records, readout before and after (file:line),
during a logger stall. A first attempt marked every
period spanning a stall from its last readout before to the next one after;
it charged losses to the stall that had other causes (room 18: a header-less
readout without data on 2026-08-25 23:52, a day before the stall of
2026-08-26 19:41) and marked times without contacts as lost.

**Readout test (2026-11-02).** The partner rule assumes that a beacon its
partners record has contacts of its own. That fails where the beacon was
seen but did not record (contact tracking off, eco mode, or one-sided
visibility, ToDo 37); test E checks only that real losses are found, not
that the gaps found are losses. A gap `read out, not logged` needs a readout
of the beacon in it, and the Base reads a beacon only from its readout
threshold on (BI 2.1). The threshold shows in the data: in some stretches no
readout has a `Contact Count` below 5 001 (button 0), in others readouts of
2 – 10 contacts occur. Per gap between readouts (not storage full):

- threshold: the smallest `Contact Count` of all readouts with `Current
  Timer` (any beacon) whose real time lies in the gap or within 1 h of it;
- expected own records: the partners' records of the beacon in the gap
  (symmetric RSSI, 14.2) × the beacon's own records per partner record in
  the delivered range of the readout before and of the one after the gap
  (the larger of the two; each from at least 100 partner records);
- factor: calibrated in every run on the logged readouts — a complete
  readout right after a complete one of its period, without a gap, holds as
  `Contact Count` what was recorded since; its ratio to the expectation
  computed the same way (from its neighbours) has a median of 0.81 and a
  99 % quantile of 2.61 (543 readouts).

If factor × expected < threshold, no readout was possible in the gap. Then
the beacon's records of that time, had it made any, would still have been
stored and delivered by the next readout, which starts after the gap: the
beacon did not record. Cause and coverage state `not recorded (no readout
possible)`; columns `Own records expected` and `Readout threshold` in
`coverage.csv` (empty where not testable).

Result (pipeline run 2026-11-02): of 1 646 gaps between readouts (not full),
1 524 testable (27 without a readout near them, 95 without enough partner
records in the neighbouring readouts), 435 of them under a threshold of at
least 5 000; 60 gaps (59.1 h of 14 798 h, 47 beacons) are `not recorded`.
19 of them last about 5 min — the beacon's eco scan interval (BI 2.3; 15 of
the 60 overlap an eco session of the beacon, e.g. 135, 137, 138, 139, 141,
144, 147 on 2026-08-14 around 23:00, after eco mode was enabled at 21:00);
11 last more than 1 h (46 h), the longest 40 p6 (2026-08-22 18:43 –
2026-08-23 12:37, 17.9 h, 390 partner records, expected 1 797 own records
against a threshold of 5 004). Under a low threshold (2 – 10 contacts) the
test cannot decide and the gap stays `read out, not logged`; 42 p8 (ToDo 37)
is not testable (no readout before it, too few partner records in the one
after). Everything else unchanged.

Other logger outages are not marked. The logger writes only during readouts and
has no heartbeat, so an outage cannot be told from a time without readouts:
gaps between consecutive lines, and even between consecutive log files,
are mostly nights (76 gaps of more than 10 min between files in the
deployment). An outage loses nothing by itself — the records stay on the
beacon until the next logged readout. Records are lost only if the Base
read a beacon while the logger was not writing; that shows as a gap in the
contact timers between two logged readouts of one period (lost records,
above). Contacts missing inside a readout are located as well (below).

**Incomplete readouts (2026-10-23).** A readout is incomplete if fewer of its
stored contacts arrived than its `Contact Count` announced and they did not
come in a later readout either: 652 of 8 586 readouts with `Current Timer`
(404 in the deployment), 5.2 – 5.7 M contacts (lost at least / at most), a
median of 59 % of their `Contact Count`; 585 of them without `Transfer
complete` (interrupted). The firmware sends the oldest contacts first, so
the received ones are mostly an unbroken run from the oldest on (largest gap
inside: median 36 s) and what is missing is the newest part (median 1.0 h,
90 % 14 h before the readout's real time). Where it went: in 325 readouts
the next readout of the period does not reach back to it — a gap of the
lost-records check (`read out, not logged`); in 160 a reboot followed
before the next readout (erased); in 169 the oldest part is missing instead
(median 230 contacts, mostly the setup night, below the partner rule). 67
readouts with `Transfer complete` miss contacts too, 42 of them with a hole
of more than 0.5 h in the middle (lines lost inside the stream; e.g. 143 on
2026-08-16: 62 k of 114 k, a hole of 13 h). Two changes:

- **End of the recorded time.** The coverage ended `recorded` at a period's
  last readout; for an incomplete last readout the rest stayed on the
  beacon and was erased by the reboot (or never read out). `recorded` now
  ends at the end of the delivered records — a complete readout with
  `Current Timer` at its real time, any other at its newest contact.
- **Holes inside a readout.** In the merged timeline of all own contacts of
  the period (every readout of it), two consecutive contacts at least
  10 min apart (the beacon scans every 7 s) that both lie in one readout's
  delivered range, not within 5 min of an eco session of the beacon (one
  scan burst per 5 min), with the partner rule above, and — for a readout
  with `Current Timer` — missing contacts by its `Contact Count` (lost at
  most > 0): lines lost inside the readout's stream, `read out, not logged`
  (lost-records column `Where`: `inside a readout`). Without the last
  condition, 7 holes (35 h) of outdoor location tags (67, 87, 94) passed
  with sparse partner records over hours although nothing was missing.
- **Start of a readout** (2026-10-26). A gap before a readout with
  `Current Timer` is kept without the partner rule if the readout misses
  contacts by its `Contact Count` (lost at least > 0) although its delivered
  run reaches its newest contact (within 10 min of its real time): the count
  proves the loss, and the two delivered runs of the same clock bound it;
  the whole gap is lost (`read out, not logged`, `Where`: `start of a
  readout`). Recount of the 169 readouts "with the oldest part missing"
  (2026-10-25, from the delivered timer ranges): of the incomplete readouts
  whose newest contact was delivered, 37 had a gap at the start (median
  144 s of uptime, mostly the setup night), 76 none (the delivered runs
  continue each other; their deficit, median 208 contacts, has no place in
  time — probably contacts counted twice, not lost). Result: 52 gaps at the
  start of a readout, 1.9 h (median 1 min, longest 51 min: 25 p13 on
  2026-08-24/25); 43 of them on 2026-08-13.
- **Before the first boot** (2026-10-27). A beacon that the partners
  recorded before its first boot was running then: its records of that time
  were erased by a reboot before its first readout with `Current Timer`
  (e.g. battery contact problems on the setup morning). From the first such
  sighting to the first boot is `erased by reboot` (`Where`: `before the
  first boot`), not `before first boot`. Conditions: the partner rule above
  (at least 10 sightings more than 2 min before the boot), by at least 3
  different partners (one partner's misdated readout must not create it: 112's broad
  own boot on 2026-08-13, section 19, put 7, 55 and 69 there with 1 – 2
  partners). Result: 9 beacons, 13 h, all on 2026-08-13 — 105 from 10:41
  to its first boot at 19:29 (8.8 h, 10 724 sightings), 91 from 10:00 to
  13:07 (3.1 h), 15, 32, 42, 75 0.2 – 0.4 h, 24, 41, 51 a few minutes.

Result (2026-10-23): 97 holes inside readouts (302 h, in 90 readouts; all
81 in readouts with `Current Timer` have a deficit); the contacts expected
in them from the beacon's own rate are a median 57 % of the readout's
deficit (the rest lies in its tail or in holes shorter than 10 min).
Coverage: `recorded` 32 928 → 31 640 h, `erased by reboot` 11 157 →
11 685 h, `not read out` 7 771 → 8 228 h, `read out, not logged`
14 780 → 15 082 h.

**Fix (2026-10-19): readouts across two log files.** The timer-zero
analysis split readouts over the merged lines of all files. When a file
ended with `Status` lines of a beacon and the next file started with a
readout of the same beacon, both were merged into one readout filed under
the previous file, with line numbers of the next one; its `Current Timer`
was then not found and the readout fell out of the uptime model (e.g. 127
at `dsa_20260819_1513.log` line 1, 4 128 contacts). A new log file now
always starts a new readout (`collect_readouts`).

Rerun after the fix (2026-10-19, `generate_output.py --run-pipeline`, 80
min): 51 437 readouts (3 more), 1 088 periods; H4 40 readouts in the
deployment (4 more, 57–67 s, within the timer precision), H5–H7 satisfied;
ID split and validation unchanged. Readouts without `Current Timer`: 40 905
same period before and after, 9 by their contacts, 1 940 undecided (no
reliable contact boot: short readouts with few contacts). Contacts in the
window 55 084 586: ok 55 035 793, duplicate 33 247, unreliable time 15 516,
fake 30; not datable 44 628 (all in undecided readouts without `Current
Timer`; 40 594 of them from logs inside the window, 0.07 %). Coverage: 170
beacons, 6 203 intervals; hours recorded 46 426, erased by reboot 10 733,
not read out 5 744, never read out 96, no data 750 (one person and one room
tag in the lists without any readout), before first boot 1.

**No observation window in the conversion (2026-10-20).** The observation
window belongs to the evaluation: `generate_output.py` writes all records
whatever their time (`--valid-date-start` / `--valid-date-end` remain as an
option); the coverage table spans the whole data, from the first boot to the
last readout. Instead, hard limits of the data apply
(`postprocessing.FLASH_START` / `LATEST_RECORD`): the beacons were flashed
from 2026-08-12 23:00, so no boot can be earlier (H9 in the uptime model and
the hard rule check) and a record dated before it or after 2026-08-30 00:05 has a
corrupt timer (`invalid: dated before the flashing … or after 2026-08-30 00:05`;
no time, `not_dated.csv`).

Result (2026-10-20): contacts 64 380 391 in 19 daily files (2026-08-12 to
2026-08-30): ok 64 329 383, duplicate 35 219, unreliable time 15 516,
invalid ID2 190, fake 79, ID undecided 4; not datable 32 975 plus 66 dated
outside the hard limits. Self-reports ok 5 695 (not datable 14), eco
sessions ok 2 273 (not datable 2). No boot before the flashing (H9).

Final run (2026-10-25): contacts 64 413 322 in 19 daily files: ok
64 377 913, duplicate 35 219, invalid ID2 190; no fake records left (they are
reconstructed in the working copies), no unreliable time (the clock stops of
15 p2 are repaired, 19.3), none ID undecided; none without a time (before:
4 309, the readouts of 61 and 71 without `Current Timer`, section 19).
Self-reports ok 5 746, eco sessions ok 2 276, all dated. Coverage
(2026-10-27): recorded 31 639 h, read out, not logged 15 091 h, erased by
reboot 11 695 h, not read out 8 226 h, before first boot 1 145 h, dropped
(storage full) 696 h; 1 825 gaps of lost records (then `lost_records.csv`, now in `coverage.csv`; between readouts
1 647 not logged, 14 800 h, and 22 storage full, 696 h; 96 holes inside
readouts, 289 h; 51 at the start of a readout, 2 h; 9 before the first
boot, 13 h).

**Second split by the hard rules (method F, 2026-10-20).** The two periods
that contradicted the hard rules (7 p2, 9 p2 on 2026-08-13) were caused by
one undecided readout each: 7 at 13:03:39 (`Current Timer` 7 492; 7 and 55
booted 25 s apart, so the timer-zero time fits both) and 9 at 09:54:22
(`Current Timer` 238, logged 15 s after 57's readout with 237). Under the
logged label each forces the next readout of that beacon into a period with
an impossible boot (H5/H6 violated by 7 101 s / 29 s); under 55 / 57 all
hard rules hold. A test run with the two readouts relabelled gave no
contradiction, H5 0, H4 one readout fewer. Method F decides such readouts by
re-solving both candidate beacons with the readout under each label; a
readout whose two labels both break a rule (e.g. a correctly decided
neighbour of the culprit) is not decided by it.


### 22. Without the pipeline: the logger's own dating (`reports/without_pipeline.py`, 2026-10-29)

**Question.** What would the data have been without the postprocessing pipeline,
i.e. with the contacts as the logger dated them itself during the deployment?

**The logger's rule** (its `postprocessing.py` of 2026-08-15, git 2e1f52d, the
version that ran during the deployment: `aggregate_into` over all logs as its
`main()`; its limits are in `study.toml` `[logger_own_dating]`): the lines of all files in
line-time order; a contact is dated with the latest `Current Timer` line of its
logged ID1 before it (none before: the first one of that ID1), time = line
time of the `Current Timer` − (`Current Timer` − contact timer). Kept if ID1 and
ID2 are in 0–170 or 252–254, the RSSI in −110 … −10, the time is not after the
contact's own line time and its calendar day is 2026-08-12 … 2026-08-29. No ID
split, no fake records, no duplicates, no logging delay, no drift, no reboot
between a readout's records and its `Current Timer`.

**Method.** `without_pipeline.py` applies the rule vectorised to the cache of the
original logs. Checked once (2026-10-29) against `aggregate_into` itself on 6
files each at the start, in the middle and at the end: the contact lists
identical, in order (the check and the logger's code are no longer part of the
postprocessing). The pipeline's dating comes per contact line from
`model_dating.run(..., keep=...)` (nothing written). Both are paired by log file
and line (the working copies keep the line numbers; 35 contact lines exist only
in the working copies: recovered from fake records). Base: the contacts with
status ok in the window (pipeline). Slide "Without the pipeline" of
`make_presentation.py` (`Presentation/`).

**Result (2026-10-29, window 2026-08-13 18:00 to 2026-08-29 09:00).** Of
55 092 420 contacts with status ok:

- lost (the logger gives no time): 29 651 (0.05 %), all dated after their own
  log line (the `Current Timer` used belongs to another uptime period: a reboot
  or a readout without `Current Timer`);
- under the wrong beacon (logged 0–9 instead of 48–57): 622 009 (1.1 %);
- dated out of the window by the logger: 9 734; the logger also keeps 33 199
  duplicates in the window;
- time error (logger − pipeline) of the dated ones: mean +68 s, mean absolute
  14 min, median absolute 130 s, 90 % 20 min, 99 % 79 min; 79 % too late, 41 %
  within 1 min, 1.8 % over 1 h, 0.16 % over 1 day.

The logger loses almost nothing; its error is in the time. The line time of
the `Current Timer` includes the logging delay (BI 3.2.3), so the contacts come
out too late by about the delay; the rare large errors (hours, days) are records
dated with the `Current Timer` of another uptime period.
