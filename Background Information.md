# Background Information

## 0. About this document

This document contains what is needed to process the logs recorded during
the deployment without knowledge of the firmware or the code. It describes:

- how the data is meant to flow from the beacons to the log files (chapter 2);
- the known issues that make the logs deviate from that (chapter 3);
- the changes to the system during the deployment (chapter 4);
- how the IDs 0–9 and 48–57 can be told apart (chapter 5);
- what a processing of the logs has to treat (chapter 6).

How the postprocessing implements this, and what it found in the data, is
described in `Postprocessing/Postprocessing Method.md`, not here.

**Scope.** One Base station and one logger PC. Deployment window:
**2026-08-13 18:00 to 2026-08-29 09:00** (local time). Only changes inside
this window are described as changes. Everything else is described as the
state of the system during the window.

**Hard limits of the data.** The beacons were flashed with the deployed
firmware from **2026-08-12 23:00** on: no boot and no record can be earlier.
No record can be later than **2026-08-30 00:05**. A record whose converted time
lies outside these limits has a corrupt timer.

The individual operations – which commands were sent when, battery
replacements, exchanged tags, which tags are location tags – are recorded in
the separate experiment log. This document describes what such an operation
does to the data.

**Log files.**

| Folder | Content |
| --- | --- |
| `Network_Log+Postprocessing/Original/` | Raw log files as written by the logger. Never modified. |

The logs contain the ID1 defect described in 3.2.1; a processing has to
correct it first (chapter 5).

---

## 1. System overview

| Device | Role |
|---|---|
| **Beacon (tag)** | Worn by a person. Advertises its ID, scans for other beacons, stores each sighting as a contact, stores self-reports. Advertising name `DSA`. |
| **Location tag** | A beacon at a fixed place (room). Same firmware and behaviour, advertising name `DSL`. Other beacons record it with a looser signal threshold (2.1). Which IDs are location tags is noted in the experiment log. |
| **Base** | A development kit connected to the logger PC. Scans for beacons, connects to them, reads out their stored data and forwards it over a serial line (UART) to the PC. |
| **Control** | A development kit that sends configuration commands to beacons by radio (2.7). Not part of the data path. |
| **Logger PC** | Runs the logger, which decodes the serial data and writes the log files. |

**IDs.** Each beacon has a fixed one-byte ID, assigned by its radio address
in a table built into the firmware.

- Beacons: 1–170; The split of personal to location tags is noted in the experiment log.
- Development kits (test devices, not worn): 252, 253, 254.
- A device whose address is not in the table reports ID 255.
- A beacon only records beacons whose address is in the table. A device not
  in the table is never recorded as a contact.

---

## 2. Intended data flow

```
Beacon                                        Base                          Logger PC
scan ─► RAM buffer ─► flash ─► readout (BLE) ─► output queue ─► UART ─► receive buffer ─► logger ─► *.log
                                              │
                                              └─ Base confirms each data batch once it has
                                                 queued it for the UART; only then does the
                                                 beacon delete the batch
```

### 2.1 Contacts

**What a contact record is.** A beacon scans for 120 ms every 7 s. Every
advertisement of a known beacon received during a scan, with a signal
stronger than the threshold (-80 dBm), is stored as one record:

| Field | Size    | Meaning                                                           |
| ----- | ------- | ----------------------------------------------------------------- |
| ID2   | 1 byte  | ID of the beacon that was seen (taken from its own advertisement) |
| Timer | 3 bytes | Uptime of the recording beacon in seconds when it was seen        |
| RSSI  | 1 byte  | Signal strength, stored as magnitude (80 = −80 dBm)               |

Consequences for the evaluation:

- A record is a **sighting**, not an encounter. Two beacons together for one
  minute produce several records, about one per 7-s scan (beacons advertise
  every 90–120 ms, so a 120-ms scan usually catches one advertisement and
  occasionally two or none).
- Contacts are **one-sided**. A sees B and B sees A independently. Each
  direction has its own records, with its own timer. This allows for reconstruction of a contact if the data on one side is lost
- **Signal threshold** (by the type of the beacon seen):
  - person-worn beacon (`DSA`): −80 dBm or stronger;
  - location tag (`DSL`): −100 dBm or stronger.

  Both thresholds can be changed by command (2.7).
- No records are made while contact tracking is switched off (by command), and
  scanning is reduced in eco mode (2.3).

**Storage on the beacon.**

| Stage      | Capacity                               | Behaviour                                                                                  |
| ---------- | -------------------------------------- | ------------------------------------------------------------------------------------------ |
| RAM buffer | 24 000 records                         | When it holds 23 700 records, the oldest 804 are written to flash as one block.            |
| Flash      | 239 blocks × 804 = **192 156 records** | When all blocks are used, writing a new block **discards the oldest block** (804 records). |
| Total      | **215 856 records**                    | Beyond that, records are lost (3.4.2).                                                     |

Records stay in RAM until the RAM buffer is nearly full. With normal readout
intervals, most or all records are only in RAM at readout time.

**Readout.**

1. The beacon advertises a *data level* derived from its stored record count.
   The Base connects only to beacons whose data level is at least a minimum
   level. The minimum is chosen with the button used to start the Base:

   | Button | Minimum level | Read out: beacons with |
   |---|---|---|
   | Button 0 (usual) | 3 | more than 5 000 stored contacts |
   | Button 2 | 2 | 2 or more stored contacts |
   | Button 3 | 1 | 1 or more stored contacts |

   A beacon below the minimum is not read out, and its self-reports and eco
   sessions wait as well. A beacon with no stored contacts is never read out.
   Which button was used when is recorded in the experiment log.
2. After connecting, the Base writes a `Status` line for that beacon
   (2.4) and sends a start command.
3. The beacon sends the readout header (`Current Timer`, `Contact Count`,
   `Voltage`), then its contacts (oldest first, flash before RAM), then
   self-reports, then eco sessions, then `finished`.
4. Data is sent in batches. The Base confirms each batch after queuing it for
   the serial line. The beacon deletes the batch only after this
   confirmation. Without a confirmation within 2 s, the beacon stops sending
   that kind of data and keeps the rest for the next readout.
5. After `finished` the connection is closed. The logger writes
   `Transfer complete. Disconnecting`.

A complete readout therefore **empties** the beacon's storage. The
`Contact Count` of the next readout counts only contacts recorded since then,
plus anything left over from an incomplete readout.

### 2.2 Self-reports

A participant presses the button for 3 s. When the 3 s are reached, the
beacon stores one record with its uptime in seconds and lights its LED for
2 s. Holding longer does not create more records.

| Stage | Capacity | Behaviour |
|---|---|---|
| RAM buffer | 256 records | Written to flash in batches of 64 when 254 are held |
| Flash | 4 blocks × 64 = 256 records | Oldest block discarded when full |

Self-reports are read out in the same readout as the contacts, so they are
only read out when the beacon's contact level triggers a readout (2.1).
There is no count of self-reports in the header.

### 2.3 Eco sessions

**Eco mode** saves energy when a beacon is not moving.

- **Automatic eco mode:** if motion detection is switched on (by command;
  off by default), the beacon enters eco mode after 1 800 s (30 min) without
  movement and leaves it on the next movement.
- **Manual eco mode:** can be set by command (affected by an issue, 3.1.3);

In eco mode the beacon:

- keeps advertising normally, so **other beacons still record it**;
- scans only for 100 ms every 300 s, so it **records far fewer contacts**
  itself (about 1/50 of the normal scan time), and receives commands only during
  these short scans;
- switches off its status LED.

Each session is stored as one record with an enter time and a leave time
(beacon uptime in seconds). The record is written **only when the session
ends**.

| Stage | Capacity | Behaviour |
|---|---|---|
| RAM buffer | 64 records | Written to flash in batches of 16 when 63 are held |
| Flash | 4 blocks × 16 = 64 records | Oldest block discarded when full |

Eco mode itself is not kept across a reset: every boot starts in normal
mode. The motion-detection on/off setting is kept.

### 2.4 The readout in the log

One readout produces this sequence of lines, all with the same ID1:

```
Status: 32
Current Timer: 361847
Contact Count: 18234
Voltage: 2981
ID2: 44, Timer: 355102, RSSI: -71        (one line per contact)
...
Self-report time: 349920                  (one line per self-report)
Eco Session Enter: 340100, Leave: 343560  (one line per eco session)
Transfer complete. Disconnecting
```

(The values are illustrative.)

| Line                               | Meaning                                                                                                                                                                                |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Status`                           | The beacon's fault byte as advertised at the moment of connecting. Written by the Base, extracted from the advertisment, not sent by the beacon in a readout. Bits: Appendix A.        |
| `Current Timer`                    | Beacon uptime in seconds at the start of the readout (32 bit).                                                                                                                         |
| `Contact Count`                    | Contacts stored on the beacon at the start of the readout (RAM + flash). Contacts recorded during the readout are also sent, so the number of contact lines can slightly exceed it.    |
| `Voltage`                          | Battery voltage in mV, from the last measurement. The beacon measures every 4 h, so the value can be up to 4 h old. `65535` means no measurement yet.                                  |
| `Transfer complete. Disconnecting` | The beacon sent everything. Missing if the readout was interrupted or data was lost (3.2, 3.3).                                                                                        |

### 2.5 Time base

Beacons have no clock. All event times (contacts, self-reports, eco
sessions) are **seconds since the beacon last booted**. They are converted
to real time with the readout header:

```
event time = line time of Current Timer − (Current Timer − event timer)
```

The **timer-zero time** of a readout is

```
timer-zero = line time of Current Timer − Current Timer
```

the real time at which the beacon booted. It stays the same for all readouts
until the beacon reboots. A change marks a reset (3.4.1). It also identifies
the physical beacon independently of the ID written in the log (chapter 5).

Event timers have 24 bits and wrap after 194 days. `Current Timer` has 32
bits. For beacons that ran less than 194 days without a reboot, the two can
be compared directly.

### 2.6 Log file format

- One file per logger start, named `dsa_YYYYMMDD_HHMM.log` after the start
  time. A restart within the same minute appends to the same file.
- Each line:

  ```
  <YYYY-MM-DD HH:MM:SS>,ID: <ID1>,<record>
  ```

- The **line time** is the logger PC's local time (CEST, automatically
  synchronised), one-second resolution, taken **when the logger writes the
  line**, not when the data arrived (see 3.2.3).
- ID1 is the beacon being read out. The Base adds it to every forwarded
  message from the beacon's advertisement.

### 2.7 Configuration commands

The Control device sends commands as a special advertisement. A command
targets one ID or all beacons. Beacons apply it when they receive it during a
scan (in eco mode, only during the short eco scans). Most settings are kept
across a reset (exception: 3.1.1). Commands that change the data:

| Setting | Effect on the data |
|---|---|
| Contact tracking on/off | Off: no contact records |
| RSSI threshold (person-worn / location) | Changes which sightings are recorded |
| Motion detection on/off, inactivity time | Enables/disables automatic eco mode |
| Radio mode (normal / eco) | Manual eco mode (3.1.3) |
| Scan interval/window, eco scan window/period | Changes the sampling of contacts |
| Reset of settings | Back to the defaults (Appendix B) |

Which commands were sent and when is recorded in the experiment log.

---

## 3. Known issues

### 3.1 Firmware

The beacon firmware did not change during the deployment. The issues below
were found by code analysis and host-side tests (test suite, `Testsuite/`).
They have not been confirmed on the hardware.

#### 3.1.1 Stored contacts are deleted at every boot

When a beacon boots, it checks its contact flash. Because of an error in this
check, **any non-empty contact flash is treated as invalid and cleared**.
In addition, the beacon then fails to load its saved network settings (RSSI
thresholds, tracking on/off) and uses the defaults until the next command.

Self-reports and eco sessions in flash are not affected.

Effect: see 3.4.1.

#### 3.1.2 Full storage is not signalled

When the contact flash is full, the oldest block is discarded silently. The
"storage full" bit in `Status` is never set for this case. Data loss through
full storage is therefore invisible in `Status` (see 3.4.2 for how to detect
it).

#### 3.1.3 Manual eco mode stops scanning

If eco mode is set by command (not by motion detection), the beacon stops
scanning completely instead of scanning in short bursts. It records **no
contacts** and receives **no further commands**, so it cannot be switched
back by command. The setting is kept across reboots, so a reboot does not
help either; only reflashing the beacon does. Automatic eco mode is not
affected. (not used in this experiment)

#### 3.1.4 Data can be sent twice

- **Lost confirmation.** If the Base received a batch but its confirmation
  did not reach the beacon within 2 s, the beacon keeps the batch and sends
  it again in the next readout.
- **Deferred bookkeeping.** To save flash writes, the beacon records the
  progress of a readout in flash only once at the end. A reset between the
  readout and this bookkeeping makes it send already confirmed self-reports
  and eco sessions again.

A duplicate is an identical record (same ID1, ID2, timer, RSSI) in **two
different readouts**. Within one readout, identical contact records can be
genuine (the same beacon seen twice in one second at the same strength).

#### 3.1.5 Data level is capped

The data level is computed from a count capped at 65 535, so it never rises
above level 4. This has no effect on the readouts, whose minimum level is at
most 3 (2.1).

### 3.2 Logger

#### 3.2.1 IDs 48–57 written as 0–9

The logger wrote ID1 values 48–57 as `0`–`9` (the byte values are the
character codes of the digits). **Affected:** log lines with a line time up
to **2026-08-15 22:00** whose ID1 is 0–9 or 48–57. The pairs are 0↔48,
1↔49, …, 9↔57. **ID2 is never affected.**

The rule uses the line time, not the reconstructed event time. Records read
out before the cutoff can have event times long before it.

Not corrected in the original logs; chapter 5 describes how the correct ID
can be determined.

#### 3.2.2 Fake records from incomplete messages (whole deployment)

Every message on the serial line has the form `I` `D` ⟨beacon ID⟩
⟨message type⟩ ⟨payload⟩ `CR` `LF`. A data message carries a count byte and
then 5 bytes per contact (ID2, timer as 3 bytes, RSSI). The logger takes the
count, reads that many records and only checks that `CR` `LF` follows.
Whenever bytes of a data message are missing, the logger fills the message
up with the bytes of the following messages and cuts them into **fake
contact records**. Two cases:

| Case | What happens | Period |
|---|---|---|
| Framing error | A lost or corrupted byte makes the logger read a byte as a record count that is none. The following real messages are cut into records | Until 2026-08-15 13:53; since then the logger rejects a count that does not fit a single message and skips to the next message start |
| Truncated data message | Bytes of a data message are lost on the logger PC (3.2.3). The count is genuine, but its records are incomplete; the logger takes the missing bytes from the following messages, usually the Base's short `Status` messages. These end with `CR` `LF` too, so the end check passes by chance | Whole deployment; not caught by the change of 2026-08-15 13:53 |

Most fake records have IDs, RSSI values or timers outside the valid ranges
(6.2). Some of them have valid values and a plausible timer and cannot be
detected individually. As a group they can: put back into their 5-byte form,
consecutive fake records contain complete messages of the logger format
(`I` `D` … `CR` `LF`). The record where the real data ends is mixed: its
first bytes are real, its last bytes belong to the next message.

The embedded messages are genuine: they are the messages that followed the
truncated one (other beacons' readout headers, contacts, `Status` lines,
`Transfer complete`), which the logger consumed instead of writing them. They
can be recovered by decoding the embedded messages as the logger would have
written them; their ID byte is the real ID (also for 48–57 before the fix of
3.2.1). The real records of the truncated message after the loss are lost.

#### 3.2.3 Postprocessing inside the logger (whole deployment)

During the whole deployment the logger ran the complete postprocessing after
**every** finished readout and rewrote all output tables. This took longer
the more data had been logged. While it ran, the logger did not read the
serial line. The Base does not wait for the logger, and the serial line has
no flow control.

**When the pause runs.** The logger starts the postprocessing when it writes
the `finished` message of a readout (the line `Transfer complete.
Disconnecting`), and reads the serial line again only when it is done. It does
not run it at start-up and not after a readout without `finished`. A newly
started logger opens the serial port with an empty receive buffer. The first
readout of a log file therefore reaches the logger without delay from it (its
own `finished` starts the first pause).

**Receive buffer.** The logger opens the port with pyserial, which requests a
receive buffer of 4 096 bytes from Windows (the USB serial driver may use a
different size). During a pause the buffer fills with the first bytes that
arrive; when it is full, further bytes are discarded. One contact takes about
5 bytes on the serial line, so the buffer holds the header and the first few
hundred contacts of a readout.

Two effects follow:

1. **Data loss.** Everything that arrives during a pause after the buffer is
   full is discarded. The Base had already confirmed these batches, so the
   beacons had deleted them. The data is lost permanently.
   - The readout in progress when the buffer fills is cut off. Traces: a
     readout without `Transfer complete. Disconnecting`; fewer contact lines
     than `Contact Count`; a missing or incomplete header; messages discarded
     by the logger when it found no valid message start.
   - **Readouts that start after the buffer is full are lost completely and
     leave no line at all.** The Base reads out several beacons during a long
     pause, so a long pause loses whole readouts. Their trace is only in each
     beacon's next logged readout of the same uptime period: its oldest
     contacts start well after the end of the previous logged readout (3.2.4).
     The longer the pauses, the more readouts are lost.
2. **Late line times.** Lines that waited in the buffer are stamped when they
   are finally written, i.e. too late by the length of the pause. Trace: a
   gap in the log followed by many lines with the same second. If the
   `Current Timer` line is stamped late, **all event times of that readout
   are shifted later** by the same amount. A check comparing the converted
   times with the line time cannot detect this, because the line time is
   late too.

If an error occurred inside this postprocessing, the logger could end
completely (3.2.4).

**From 2026-08-29 19:30** (after the deployment window) all earlier log files
were moved from the logger's folder into a separate folder. From then on the
internal postprocessing had practically no data to process and no longer
delayed the logging. Readouts logged after this time carry no delay from it
and serve as references for the timer-zero times (3.6).

#### 3.2.4 Logger not capturing

The Base reads out beacons whether or not the logger is running. Everything
read out while the logger is stopped, crashed or the PC is asleep is lost,
because the Base has confirmed it. When the logger stops reading the serial
port without ending (e.g. the PC falls asleep or the logger hangs), the
PC's receive buffer still holds the last data received (a few KB). It is
logged when the logger resumes, stamped with that time, so these lines carry
a delay as long as the stall. The logger ends (with an error message)
when the serial port disappears, e.g. when the USB cable is unplugged or the
Base resets, and when an error occurs during its internal postprocessing
(3.2.3).

The logger writes lines only during readouts and has no heartbeat. A time
without log lines is therefore normal (no beacon was read out) and cannot be
told apart from a logger outage; an outage loses nothing by itself, the
records stay on the beacons. Only a readout during the outage loses data, and
it leaves no line at all. Its trace is in the beacon's next logged readout:
within the same uptime period, its contact timers start well after the
`Current Timer` of the previous logged readout (the contacts in between went
to the lost readout).

#### 3.2.5 Logger PC clock

All real times derive from the line times, i.e. from the logger PC's clock
(CEST, automatically synchronised). A clock correction of the PC shifts the
times of the readouts after it.

### 3.3 Base

#### 3.3.1 Lost readout header and mislabeling (until 2026-08-15 18:43)

Only data batches are confirmed. The header lines (`Status`, `Current Timer`,
`Contact Count`, `Voltage`) are not repeated if lost. Until 2026-08-15 18:43
the Base could connect to the next beacon while the previous readout was
still being sent to the PC, and the new header could be lost for the new beacon. Since then the
Base waits until everything has been sent before connecting to new beacon.

Effect: No `Current Timer` of
the beacon readout available.

**Mislabeling.** The beacon's data messages do not contain its ID. The Base
adds the ID (`ID: <ID1>` in the log) to every message it forwards. Until
2026-08-15 18:43 it used the ID of the beacon it had **most recently
connected to**. A message of beacon A that was still in transit or being
processed when the Base connected to beacon B was therefore labelled with
B's ID. In the log, such contacts of A appear right after B's header
(`Status`, …) under B's ID.

Effect: B's readout contains contacts that belong to A; A's readout lacks
them. Their timers continue A's timers and do not fit B's `Current Timer`.
A check that only looks for consecutive contact lines with different ID1
does not find this case, because B's header line lies between A's last
contact and the mislabelled one.

#### 3.3.2 Messages between two connections

Since 2026-08-15 18:43, a message still in transit from a beacon when the
Base has already switched to the next beacon is discarded by the Base, so
that it is not filed under the wrong ID (before, see 3.3.1). It is lost.

A remaining risk: if a readout's header is lost (3.2.3), its contacts follow
the previous beacon's contacts without a header line in between, under the
new ID. Such a place shows up as two consecutive contact lines with
different ID1 and no other line between them (6.4, item 13).

### 3.4 Beacons

#### 3.4.1 Power resets

A beacon reboots on power loss (battery change, loose or empty battery) and
after a crash. There is no automatic restart after a hang: a hung beacon
stays silent until its power is interrupted.

After a reboot:

| What | Effect |
|---|---|
| Uptime | Restarts at 0. New timer-zero time. |
| Contacts in RAM (all since the last flash write) | **Lost** |
| Contacts in flash | **Lost** (3.1.1) |
| Self-reports, eco sessions in RAM (up to 254 / 63) | **Lost** |
| Self-reports, eco sessions in flash | Kept, read out later; may be sent twice (3.1.4) |
| Eco session in progress | **Lost** (record only written at the end) |
| Network settings (RSSI thresholds, tracking) | Defaults, until the next command (3.1.1) |
| Other settings (motion detection, radio mode, LED) | Kept |
| Eco mode | Starts in normal mode |

Since contacts normally stay in RAM between readouts, **a reset loses all
contacts recorded since the last readout**. The time between the last
readout before the reset and the reset itself is a **gap in the data**, not a
period without contacts.

**Detecting a reset:**

- `Current Timer` is smaller than in the beacon's previous readout;
- the timer-zero time jumps;
- the reset time is the new timer-zero time.

Records kept in flash from before a reset (self-reports, eco sessions) are
read out with the new, smaller `Current Timer`. Converted with it, they land
in the future. They have to be converted with the reference from before the
reset (6.3).

#### 3.4.2 Full storage

If a beacon is not read out for a long time, its contact storage fills up
(2.1).

- **Flash full:** each further flash write discards the oldest 804 contacts.
- **RAM full during a readout or flash write:** the newest contacts are
  dropped.

Not signalled in `Status` (3.1.2).

**Detection:** before the flash is full, `Contact Count` cannot exceed about
215 000 (238 full flash blocks + RAM). A `Contact Count` of **about 215 000
or more** means the flash is full, and the oldest contacts of the interval
have been or are being discarded.

#### 3.4.3 Clock drift

The beacon's uptime counter runs on the beacon's own oscillator. Its
deviation from real time is not known. A converted time is exact at the
readout and the error grows with the time between event and readout. Events
from long readout intervals are the least accurate.

#### 3.4.4 Periods with reduced or no recording

| Situation                                                                     | Recording                                                               |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Automatic eco mode                                                            | Reduced to 100 ms every 300 s (2.3); the beacon is still seen by others |
| Manual eco mode                                                               | None (3.1.3)                                                            |
| Tracking switched off                                                         | None                                                                    |
| Hung beacon                                                                   | None, until power is interrupted (3.4.1)                                |


#### 3.4.5 Battery

The voltage in the header is the last 4-hourly measurement (2.4). Below about
2 550 mV the beacon reports its lowest battery level in its advertisement. An
empty battery ends in a reset (3.4.1) or in silence. A battery change is a
reset.

### 3.5 Operation

- **Development kits** 252–254 appear in the data as beacons and as contact
  partners. They are test devices, not participants.
- **Setup and test activity** around the start and end of the deployment
  produces records outside the deployment window. These must be excluded by
  the observation window (6.4, item 1).
- **Commands, battery changes, exchanged tags:** see the experiment log. An
  exchanged tag with the same ID continues under that ID with a new
  timer-zero time; this looks like a reset.

### 3.6 Logging delays and shifted event times

All event times of a readout are converted with **one** reference: the line
time of its `Current Timer` line (2.5). The conversion assumes that this line
was stamped at the moment the beacon read its uptime. Any delay between the
two moves **all events of that readout later by the same amount**. The line
times of the contact lines themselves do not enter the conversion; their
delays are harmless.

The delays can only make the line time later, never earlier (apart from the
rounding in the last two rows of the table). A converted event time is
therefore an upper bound: the event happened at or before that time.

**Path of the `Current Timer` value and the delay at each step:**

| Step | What happens | Delay of the `Current Timer` line |
|---|---|---|
| Beacon | Reads its uptime when it builds the header, i.e. after the connection is set up and the Base has sent the start command. Connection setup therefore adds no delay. | none |
| Radio link | The header is the first message of the readout and is sent at once. | milliseconds |
| Base output queue | Messages wait here for the serial line (16 messages, up to about 4 KB). Since 2026-08-15 18:43 the Base connects only when the queue is empty, so the header does not wait. Before, it could wait behind the rest of the previous readout: at most a full queue, about 0.35 s. | none / up to 0.35 s |
| Serial line (UART) | 115 200 baud = 11 520 bytes/s. The header messages are 22 bytes (about 2 ms). | about 2 ms |
| PC receive buffer and logger | The logger reads the serial port continuously and stamps each line when it writes it. While it is busy with its internal postprocessing (3.2.3), it does not read. Arriving data waits in the PC's receive buffer and is stamped only after the pause. The pause starts right after a `Transfer complete`, and the Base usually connects to the next beacon soon after. The next readout's header therefore often arrives **during** the pause. | **0 to the remaining length of the pause**; grows with the amount of data already logged |
| Logger time stamp | Whole seconds, truncated. | 0–1 s |
| Beacon uptime | Whole seconds, truncated. | 0–1 s (either direction) |

The logger's internal postprocessing is the only step with a large delay; it
grows with the amount of data already logged (3.2.3). The logger's delay is
expected to stay below about 3 h. All
other steps together stay below about 1 s (up to about 1.5 s before
2026-08-15 18:43).

**Readout duration.** The contacts of a readout follow the header over the
serial line. A data message carries up to 48 contacts in 247 bytes, so the
line needs at least about 0.45 ms per contact. A full beacon (2.1, about
216 000 contacts) takes at least about 95 s. When the Base's queue is full,
the Base waits, which slows down the beacon's transfer as well. The
readout's duration does not affect the converted times. It only means that
contacts recorded during the readout carry a timer above `Current Timer`
(2.4).

**Other effects on the reference time:**

- **Late stamping of a whole readout:** if a readout arrives during a logger
  pause, all its lines get nearly the same line time. Readout durations
  computed from line times are then too short, sometimes shorter than the
  serial line allows.
- **Lost header (3.3.1, 3.2.3):** the readout's events are converted with an
  older `Current Timer` of the same beacon. That is correct as long as the
  beacon did not reboot in between and that older line was not itself stamped
  late.
- **Logger PC clock (3.2.5)** and **beacon clock drift (3.4.3)** add to the
  above.

**Detection and correction.** Within one uptime period, the timer-zero time
(`line time of Current Timer − Current Timer`) of all readouts of a beacon is
the same, apart from clock drift. A readout whose timer-zero time is later
than that of the other readouts of the period was stamped late by the
difference. The earliest timer-zero time of the period is the best reference
(6.4, item 3). Readouts logged after 2026-08-29 19:30 and the first readout
of each log file have no delay from the logger's postprocessing (3.2.3);
where such a readout exists in an uptime period, its timer-zero time is the
exact reference, apart from the whole-second rounding and the clock drift
(3.4.3). A record whose timer exceeds `Current Timer` by more than
the logged time between the header and the record also shows that the header
was stamped late.

---

## 4. Timeline of changes during the deployment

The beacon firmware did not change during the deployment.

| Time                   | Component | Change                                                   | Logs affected          |
| ---------------------- | --------- | -------------------------------------------------------- | ---------------------- |
| whole window           | Logger    | Runs the full postprocessing after every readout (3.2.3) | all                    |
| until 2026-08-15 13:53 | Logger    | Framing error can create fake records (3.2.2)            | lines before this time |
| whole window           | Logger    | Truncated data messages can create fake records (3.2.2)  | all                    |
| until 2026-08-15 18:43 | Base      | Readout header can be lost; contacts can be filed under the next beacon's ID (3.3.1) | lines before this time |
| until 2026-08-15 22:00 | Logger    | ID1 48–57 written as 0–9 (3.2.1)                         | lines before this time |
| 2026-08-23 20:57       | Base      | Single-beacon mode available, off by default             | only if used           |
| 2026-08-29 19:30 (after the window) | Logger | Earlier log files moved to a separate folder; the internal postprocessing no longer delays the logging (3.2.3) | lines after this time: no delay, references for the timer-zero times (3.6) |

---

## 5. Telling ID1 0–9 and 48–57 apart

In log lines up to 2026-08-15 22:00, an ID1 of 0–9 can be the beacon 0–9 or
the beacon 48–57 (3.2.1). The correct ID has to be determined per readout
before the logs are processed. ID2 is never affected, so the other beacons'
records serve as independent witnesses.

**Basis: timer-zero times.** The timer-zero time (2.5) of a readout
identifies one physical beacon during one uptime period, regardless of the
ID written in the log.

**Deciding which beacon a readout belongs to.** For each affected readout:

1. **Self-exclusion.** A beacon never records itself. A readout whose own
   contacts contain ID2 = 5 cannot be beacon 5 (and vice versa for 53).
2. **Witnesses.** Beacons outside the ambiguous ranges that saw the disputed
   beacon report, as ID2, which of the two candidate IDs it was.
3. **Timer-zero match.** A readout with nearly the same timer-zero time
   (within a few minutes) after the cutoff comes from the same uninterrupted
   beacon. Its label is reliable. Because beacons reset during the window, a
   match has to be made per uptime period.
4. **First sighting.** A beacon is first seen by others (as ID2) shortly
   after it is switched on. If only one candidate's first sighting lies close
   to the disputed readout's timer-zero time, that candidate is the beacon.

Readouts that none of these decide (typically single `Status` lines without
data) keep the logged ID and should be marked as undecided.


---

## 6. What a processing of the logs has to treat

This chapter lists what a processing of the logs has to take into account,
derived from chapters 2 and 3.

### 6.1 Reading the logs

- Read all log files of `Original/` and correct ID1 0–9 / 48–57 first
  (chapter 5). Skip lines that do not match the line format (2.6):
  incomplete lines, debug output, corrupted text.
- Process the lines of all files in the order of their line times. Lines
  with the same line time must keep their order within the file: a readout's
  `Current Timer` line comes before the records that refer to it.
- Make sure no log file is processed twice (e.g. copies of the same file);
  every line would then be counted twice.

### 6.2 Validity

| Field | Valid values | Invalid values come from |
|---|---|---|
| ID1 | 1–170, development kits 252–254 (1) | Corrupted bytes, unknown devices (255) |
| ID2 | same | Corrupted contact records, fake records (3.2.2) |
| RSSI | Physically plausible, e.g. −110 … −10 dBm; at least as strong as the recording threshold (2.1) | Corrupted contact records |
| Event timer | Not larger than the beacon's possible uptime in the deployment | Fake records (3.2.2) |

(1) Whether development kits are kept depends on the evaluation (6.4, item 10).

### 6.3 Time conversion

- Convert each event with the formula of 2.5, using the `Current Timer` of
  the **same readout**. If a readout has no `Current Timer` of its own
  (3.3.1, 3.2.3), an older `Current Timer` of the same beacon can be used
  only if the beacon did not reboot in between.
- A converted time cannot lie after the line time of the record (beyond the
  rounding of a few seconds and the readout duration, 3.6). Later times
  indicate a wrong reference or a corrupted timer.
- Records from before a reset (3.4.1) convert into the future with the new
  `Current Timer`. Convert them with the last `Current Timer` from before the
  reset, but only if their timer continues the old uptime period (higher than
  the timers already seen in it).
- Eco sessions: convert enter and leave times separately; a session is only
  valid if both are.
- Line times can be late (3.6); correct with the timer-zero time (6.4,
  item 3).

### 6.4 Issues to treat

| # | Issue | Suggested treatment |
|---|---|---|
| 1 | Observation window (3.5) | Restrict converted times to the observation window. The final window is defined separately; the deployment window is given in chapter 0 |
| 2 | Incomplete readouts (3.2.3, 3.3.2) | Compare `Contact Count` with the contact lines received; flag readouts without `Transfer complete. Disconnecting` |
| 3 | Late line times (3.2.3, 3.6) | Within one uptime period the timer-zero time is constant; a readout with a later timer-zero time than the others was stamped late by the difference. Use the earliest timer-zero time of the period for the conversion |
| 4 | Duplicate records (3.1.4) | Remove identical records (ID1, ID2, timer, RSSI) that appear in two different readouts of the same uptime period; keep identical records within one readout |
| 5 | Resets (3.4.1) | Mark the interval from the last readout before the reset to the reset as missing data |
| 6 | Logger outages (3.2.4) | Times without log lines are normal (no heartbeat) and not missing data by themselves. Detect readouts lost during an outage as a gap in the contact timers between two logged readouts of one uptime period; mark that interval as missing data and exclude it from rates |
| 7 | Eco mode and no recording (3.4.4) | Use the eco session records to mark reduced observation per beacon |
| 8 | Full storage (3.4.2) | Flag readouts with `Contact Count` ≥ about 215 000: the start of the interval is incomplete |
| 9 | Lost header (3.3.1, 3.2.3) | Flag readouts with contact lines but no own `Current Timer` |
| 10 | Development kits (3.5) | Exclude IDs 252–254 from the evaluation |
| 11 | Beacons not read out (2.1) | Missing data of a beacon means "not read out", not "no contacts". Which beacons qualify depends on the Base's minimum level (button 0: more than 5 000 contacts) |
| 12 | Mislabeling by the Base (3.3.1) | In readouts before 2026-08-15 18:43, look for contacts right after a beacon's header whose timers continue the previous beacon's readout instead of fitting the new beacon's `Current Timer`; reassign them to the previous beacon |
| 13 | Contacts under a wrong ID after the Base fix (3.3.2) | Two consecutive contact lines with different ID1 and no other line between them, whose timers continue each other, indicate a wrong ID. Beacons booted at about the same time have similar timers and can produce such pairs by chance; check with the timer-zero times |
| 14 | Fake records (3.2.2) | Records built from the bytes of following messages. Put consecutive contact records back into their 5-byte form (ID2, timer, RSSI) and look for complete logger messages (`I` `D` ⟨ID⟩ ⟨type⟩ ⟨payload⟩ `CR` `LF`); every record that contains such bytes is fake. Single values are no proof: the bytes of `I` (73), `D` (68), CR (13) and LF (10) are also valid IDs, and a timer that looks like message bytes can be a real uptime |

---

## 7. Glossary

| Term | Meaning |
|---|---|
| ID1 | ID of the beacon being read out (the recording beacon) |
| ID2 | ID of the beacon that was seen (the contact partner) |
| Readout | One connection of the Base to a beacon, from `Status` to `Transfer complete` |
| Uptime / timer | Seconds since the beacon last booted |
| Uptime period | Time between two reboots of a beacon |
| Timer-zero time | Real time at which the uptime was 0, i.e. the boot time |
| Line time | Logger PC time written at the start of each log line |
| Data level | Coarse fill level of a beacon's contact storage, advertised; triggers the readout |
| Eco mode | Energy-saving mode with reduced scanning |
| Location tag | Stationary beacon with the name `DSL` |
| Confirmation | Message from the Base to the beacon that a batch was received; the beacon then deletes the batch |

---

## Appendix A – `Status` bits

| Bit | Value | Meaning |
|---|---|---|
| 0 | 1 | Scan error: the beacon is not recording contacts |
| 1 | 2 | Readout interface unavailable |
| 2 | 4 | Storage full (never set for contacts, 3.1.2) |
| 3 | 8 | Settings could not be stored or loaded |
| 4 | 16 | Contact, self-report or eco storage error |
| 5 | 32 | Motion sensor unavailable: no automatic eco mode |
| 6 | 64 | Together with bit 5: the motion sensor never answered at startup |
| 7 | 128 | Reserved |

## Appendix B – Settings that affect the data

Defaults of the deployed firmware. Those marked * can be changed by command.

| Setting | Value |
|---|---|
| Scan interval / window * | 7 000 ms / 120 ms |
| Advertising interval * | 90–120 ms |
| RSSI threshold, person-worn beacons * | −80 dBm |
| RSSI threshold, location tags * | −100 dBm |
| Contact tracking * | on |
| Motion detection (automatic eco) * | off |
| Inactivity time before eco * | 1 800 s |
| Eco scan window / period * | 100 ms / 300 s |
| Contact RAM buffer / flash write threshold | 24 000 / 23 700 records |
| Contact flash | 239 blocks × 804 records = 192 156 |
| Self-report RAM / flash | 256 / 256 records |
| Eco-session RAM / flash | 64 / 64 records |
| Self-report button hold time | 3 s |
| Battery measurement interval | 4 h |
| Base minimum readout level (chosen by button, 2.1) | 3 (button 0, more than 5 000 contacts), 2 (button 2, 2 or more), 1 (button 3, 1 or more) |
| Confirmation timeout (beacon) | 2 s |
