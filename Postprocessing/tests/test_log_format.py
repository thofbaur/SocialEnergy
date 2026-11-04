"""The log lines and the fake records, as built from the study's logger.toml (DSA2026 texts).

Run from Postprocessing/: python -m unittest discover -s tests
"""

import unittest
from datetime import datetime
from pathlib import Path

from lib import logcache
from lib import postprocessing as pp

LOG = pp.LOG


class LinePatterns(unittest.TestCase):
    def test_contact(self):
        for text, values in [("ID2: 17, Timer: 12345, RSSI: -67", (17, 12345, -67)),
                             ("ID2:17,Timer:12345,RSSI:-67", (17, 12345, -67)),
                             ("ID2: 3,  Timer: 0, RSSI: -0", (3, 0, 0)),
                             ("ID2: 3, Timer: 9, RSSI: 5", (3, 9, 5))]:
            with self.subTest(text):
                m = pp.CONTACT_RE.match(text)
                self.assertIsNotNone(m)
                self.assertEqual((int(m["id2"]), int(m["timer"]), int(m["rssi"])), values)

    def test_not_a_contact(self):
        for text in ["ID2: 17, Timer: 12345", "ID2: x, Timer: 1, RSSI: -1", "ID2: 1, Timer: -1, RSSI: -1",
                     "Current Timer: 5", "ID2: 1, Timer: 1, RSSI: -1 trailing"]:
            with self.subTest(text):
                self.assertIsNone(pp.CONTACT_RE.match(text))

    def test_other_lines(self):
        cases = [(pp.CURRENT_TIMER_RE, "Current Timer: 524301", {"timer": "524301"}),
                 (pp.CONTACT_COUNT_RE, "Contact Count: 215000", {"count": "215000"}),
                 (pp.VOLTAGE_RE, "Voltage: 2950", {"voltage": "2950"}),
                 (pp.STATUS_RE, "Status: 0", {"status": "0"}),
                 (pp.SELF_REPORT_RE, "Self-report time: 77", {"timer": "77"}),
                 (pp.ECO_SESSION_RE, "Eco Session Enter: 10, Leave: 20", {"enter": "10", "leave": "20"})]
        for pattern, text, groups in cases:
            with self.subTest(text):
                m = pattern.match(text)
                self.assertIsNotNone(m)
                self.assertEqual(m.groupdict(), groups)

    def test_text_round_trip(self):
        """LOG.text writes what the pattern reads, for every key the postprocessing needs."""
        for key, fields in pp.logger_protocol.NEEDED.items():
            values = {f: 100 + i for i, f in enumerate(sorted(fields))}
            if key == "contact":
                values["rssi"] = -71
            with self.subTest(key):
                m = LOG.regex(key).match(LOG.text(key, **values))
                self.assertIsNotNone(m)
                self.assertEqual({k: int(v) for k, v in m.groupdict().items()}, values)
                self.assertTrue(LOG.text(key, **values).startswith(pp.TEXT_PREFIX[key]))

    def test_log_line(self):
        when = datetime(2026, 8, 14, 10, 0, 5)
        raw = LOG.log_line(when, 5, "Current Timer: 100")
        self.assertEqual(raw, "2026-08-14 10:00:05,ID: 5,Current Timer: 100")
        line = pp.parse_line(raw, Path("x.log"), 1)
        self.assertEqual((line.timestamp, line.beacon_id, line.rest), (when, "5", "Current Timer: 100"))
        self.assertEqual(pp.format_log_line(line), raw)
        self.assertIsNone(pp.parse_line("2026-08-14 10:00:05,Current Timer: 100", Path("x.log"), 1))
        self.assertIsNone(pp.parse_line("Connecting to beacon", Path("x.log"), 1))
        self.assertEqual(LOG.log_file_name(datetime(2026, 8, 14, 9, 7)), "dsa_20260814_0907.log")


def _message(beacon: int, message_type: int, payload: bytes) -> bytes:
    return LOG.prefix + bytes([beacon, message_type]) + payload + LOG.terminator


class FakeRecords(unittest.TestCase):
    def test_contact_bytes_decode_back(self):
        id2s, timers, rssis = [17, 3, 120], [12345, 0, 2 ** 24 - 1], [-67, 0, -110]
        stream = pp.contact_records_bytes(id2s, timers, rssis)
        self.assertEqual(len(stream), 3 * LOG.contact.entry_length)
        texts = LOG.decode(LOG.contact.type, bytes([3]) + stream)
        # the logger writes the RSSI magnitude after a "-": RSSI 0 as "-0" (the cache keeps such lines verbatim)
        self.assertEqual(texts, ("ID2: 17, Timer: 12345, RSSI: -67", "ID2: 3, Timer: 0, RSSI: -0",
                                 "ID2: 120, Timer: 16777215, RSSI: -110"))
        self.assertEqual(texts[0], LOG.text("contact", id2=17, timer=12345, rssi=-67))

    def test_message_inside_contact_records(self):
        """A Current Timer message (type 5: timer u32, count u24, voltage u16) after three genuine
        records is found and decoded as the logger would have logged it."""
        genuine = pp.contact_records_bytes([1, 2, 3], [100, 200, 300], [-50, -60, -70])
        payload = (1000).to_bytes(4, "big") + (3).to_bytes(3, "big") + (2900).to_bytes(2, "big")
        stream = genuine + _message(7, 0x05, payload)
        self.assertEqual(list(pp.embedded_messages(genuine)), [])
        spans = list(pp.embedded_messages(stream))
        self.assertEqual(spans, [(len(genuine), len(stream))])
        beacon, message_type, body = LOG.split(stream[spans[0][0]:spans[0][1]])
        self.assertEqual((beacon, message_type), (7, 0x05))
        self.assertEqual(LOG.decode(message_type, body), ("Current Timer: 1000", "Contact Count: 3", "Voltage: 2900"))

    def test_truncated_message_is_not_found(self):
        payload = (1000).to_bytes(4, "big") + (3).to_bytes(3, "big") + (2900).to_bytes(2, "big")
        self.assertEqual(list(pp.embedded_messages(_message(7, 0x05, payload)[:-1])), [])

    def test_status_timer(self):
        """A Status message from its ID byte on, read as a contact entry: the timer 524301 (08 00 0D)."""
        self.assertEqual(LOG.status_timer(), 524301)
        raw = _message(9, 0x08, bytes([0]))
        entry = raw[len(LOG.prefix):len(LOG.prefix) + LOG.contact.entry_length]
        text = LOG.decode(LOG.contact.type, bytes([1]) + entry)[0]
        self.assertEqual(int(pp.CONTACT_RE.match(text)["timer"]), LOG.status_timer())

    def test_fake_flags(self):
        """The cache's fake-record check flags the records whose bytes hold the message, not the others:
        two genuine records, then a whole message (15 bytes: 3 entries) as the rest of the data message."""
        payload = (1000).to_bytes(4, "big") + (3).to_bytes(3, "big") + (2900).to_bytes(2, "big")
        size = LOG.contact.entry_length
        stream = pp.contact_records_bytes([1, 2], [100, 200], [-50, -60]) + _message(7, 0x05, payload)
        self.assertEqual(len(stream) % size, 0)
        import numpy as np
        rows = np.frombuffer(stream, dtype=np.uint8).reshape(-1, size)
        values = {name: sum(rows[:, o + b].astype(np.int64) << (8 * (n - 1 - b)) for b in range(n))
                  for name, (o, n) in LOG.contact.fields.items()}
        k = len(rows)
        flags = logcache.fake_flags(np.zeros(k, dtype=np.int16), np.arange(1, k + 1), np.zeros(k, dtype=np.int16),
                                    np.ones(k, dtype=np.int8), values["id2"], values["timer"], -values["rssi"])
        self.assertEqual(flags.tolist(), [False, False] + [True] * (k - 2))


if __name__ == "__main__":
    unittest.main()
