#!/usr/bin/env python3
"""The logger's message format and log line format, from logger.toml in the study folder.

The postprocessing does not use the logger's code: what it needs of it - the frame of a message
on the wire, the message types with their payload layout, the log lines the logger writes for
each and the layout of a log line - is in <study folder>/logger.toml (lib/study.py). Used to read
the log lines (the patterns of lib/postprocessing.py and lib/logcache.py are made from the line
texts here), to find the fake contact records (BI 3.2.2: complete messages inside the bytes of a
run of contact records) and to write them back as the log lines the logger would have written.
Not run directly.

Reading a line text: a space after ":" or "," may be any amount of white space; a "-" right before
a field is the sign of a negative value, read with the field (RSSI: -60 is -60).
"""

from __future__ import annotations

import hashlib
import re
import string
import tomllib
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

from lib import study

CONFIG_NAME = study.LOGGER_CONFIG
_FIELD = re.compile(r"^u(8|16|24|32)@(\d+)$")
# the line texts the postprocessing reads, with their fields
NEEDED = {"contact": {"id2", "timer", "rssi"}, "current_timer": {"timer"}, "contact_count": {"count"},
          "voltage": {"voltage"}, "status": {"status"}, "self_report": {"timer"}, "eco_session": {"enter", "leave"},
          "transfer_complete": set()}


@dataclass(frozen=True)
class Message:
    type: int
    name: str
    length: int | None                  # fixed payload length, or
    entry_length: int | None            # counted: entry length after a count byte
    fields: dict[str, tuple[int, int]]  # name -> (offset, bytes)
    lines: tuple[tuple[str, str], ...]  # (key, text) per line written
    match: bytes | None = None


def _parts(text: str) -> list[tuple[str, str | None]]:
    """(literal, field) pieces of a line text."""
    return [(lit, field or None) for lit, field, _, _ in string.Formatter().parse(text)]


def _literal_pattern(literal: str) -> str:
    out, prev = [], ""
    for ch in literal:
        out.append(r"\s*" if ch == " " and prev in (":", ",") else re.escape(ch))
        prev = ch
    return "".join(out)


@dataclass(frozen=True)
class Protocol:
    file_name: str
    line: str
    time_format: str
    prefix: bytes
    terminator: bytes
    messages: dict[int, Message]
    contact: Message

    @property
    def digest(self) -> str:
        """Short fingerprint of the format (not of the file's comments): part of the cache names."""
        return hashlib.sha256(repr(self).encode("utf-8")).hexdigest()[:12]

    @property
    def fixed(self) -> dict[int, int]:
        """message type -> payload length"""
        return {t: m.length for t, m in self.messages.items() if m.length is not None}

    @property
    def counted(self) -> dict[int, int]:
        """message type -> entry length after the count byte"""
        return {t: m.entry_length for t, m in self.messages.items() if m.entry_length is not None}

    # ---------------------------------------------------------------- the log lines

    @property
    def separator(self) -> str:
        """Between line time, ID field and text (load checks the layout)."""
        return _parts(self.line)[2][0]

    @property
    def id_prefix(self) -> str:
        """The ID field before the ID ("ID: ")."""
        return _parts(self.line)[1][0][len(self.separator):]

    @property
    def time_length(self) -> int:
        """Length of a line time (the formats are of fixed width)."""
        return len(datetime(2000, 1, 1).strftime(self.time_format))

    @property
    def texts(self) -> dict[str, str]:
        """key -> line text"""
        return {key: text for m in self.messages.values() for key, text in m.lines}

    def id_field_regex(self) -> re.Pattern:
        """The ID field, white space around it allowed: group 1 is the ID."""
        return re.compile(r"^\s*" + _literal_pattern(self.id_prefix) + r"(.+?)\s*$")

    def regex(self, key: str) -> re.Pattern:
        """Pattern of the line text key, one named group per field (signed where a "-" precedes it)."""
        out = []
        for lit, field in _parts(self.texts[key]):
            signed = field is not None and lit.endswith("-")
            out.append(_literal_pattern(lit[:-1] if signed else lit))
            if field is not None:
                out.append(f"(?P<{field}>{'-?' if signed else ''}\\d+)")
        return re.compile("^" + "".join(out) + "$")

    def text_prefix(self, key: str) -> str:
        """The text of line key before its first field (without trailing spaces); the whole text
        if it has no field."""
        return _parts(self.texts[key])[0][0].rstrip()

    def text(self, key: str, **values: int) -> str:
        """Line text key with the values (signed fields with their own sign)."""
        out = []
        for lit, field in _parts(self.texts[key]):
            if field is None:
                out.append(lit)
            elif lit.endswith("-"):
                out.append(f"{lit[:-1]}{values[field]}")
            else:
                out.append(f"{lit}{values[field]}")
        return "".join(out)

    def log_file_name(self, when: datetime) -> str:
        return when.strftime(self.file_name)

    def log_line(self, time: datetime | str, beacon_id: int | str, text: str) -> str:
        """A log line; time as datetime or as the line time already written."""
        stamp = time if isinstance(time, str) else time.strftime(self.time_format)
        return self.line.format(time=stamp, id=beacon_id, text=text)

    # ---------------------------------------------------------------- the messages

    def decode(self, message_type: int, payload: bytes) -> tuple[str, ...]:
        """The text of the log lines the logger writes for one message (empty: none, or invalid)."""
        m = self.messages.get(message_type)
        if m is None:
            return ()
        if m.length is not None:
            if len(payload) != m.length or (m.match is not None and payload != m.match):
                return ()
            return tuple(t.format(**_values(m, payload)) for _, t in m.lines)
        if len(payload) < 1 or len(payload) - 1 != payload[0] * m.entry_length:
            return ()
        out = []
        for k in range(payload[0]):
            entry = payload[1 + k * m.entry_length:1 + (k + 1) * m.entry_length]
            out.extend(t.format(**_values(m, entry)) for _, t in m.lines)
        return tuple(out)

    def message_spans(self, stream: bytes):
        """Yield (start, end) of every complete message (terminator included) inside a byte stream."""
        start = stream.find(self.prefix)
        head = len(self.prefix) + 2                  # prefix, beacon ID, type
        fixed, counted = self.fixed, self.counted
        while start >= 0:
            if start + head <= len(stream):
                message_type = stream[start + head - 1]
                end = None
                if message_type in fixed:
                    end = start + head + fixed[message_type]
                elif message_type in counted and start + head + 1 <= len(stream):
                    end = start + head + 1 + stream[start + head] * counted[message_type]
                if end is not None and stream[end:end + len(self.terminator)] == self.terminator:
                    yield start, end + len(self.terminator)
            start = stream.find(self.prefix, start + 1)

    def split(self, raw: bytes) -> tuple[int, int, bytes]:
        """A complete message (from message_spans) -> beacon ID, type, payload."""
        head = len(self.prefix)
        return raw[head], raw[head + 1], raw[head + 2:len(raw) - len(self.terminator)]

    def status_timer(self) -> int | None:
        """The contact timer of a contact entry made of the bytes of a Status message (line key
        status, status 0) from its beacon ID byte on (BI 3.2.2: the fake records made of the Base's
        Status messages; 524301 in DSA2026); None if that message is shorter than an entry."""
        m = next(m for m in self.messages.values() if any(key == "status" for key, _ in m.lines))
        raw = self.prefix + bytes([0, m.type]) + bytes(m.length or 0) + self.terminator
        entry = raw[len(self.prefix):len(self.prefix) + self.contact.entry_length]
        return _values(self.contact, entry)["timer"] if len(entry) == self.contact.entry_length else None

    def contact_bytes(self, id2s, timers, rssis) -> bytes:
        """The entries of the contact message the contact lines were decoded from (RSSI as its
        magnitude), joined; vectorised."""
        import numpy as np
        m = self.contact
        values = {"id2": np.asarray(id2s, dtype=np.int64), "timer": np.asarray(timers, dtype=np.int64),
                  "rssi": np.abs(np.asarray(rssis, dtype=np.int64))}
        out = np.zeros((len(values["id2"]), m.entry_length), dtype=np.uint8)
        for name, (offset, size) in m.fields.items():
            for b in range(size):
                out[:, offset + b] = (values[name] >> (8 * (size - 1 - b))) & 0xFF
        return out.tobytes()


def _values(m: Message, data: bytes) -> dict[str, int]:
    return {name: int.from_bytes(data[o:o + n], "big") for name, (o, n) in m.fields.items()}


def _fail(path: Path, what: str):
    raise SystemExit(f"{path}: {what}")


def load(path: Path) -> Protocol:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    messages: dict[int, Message] = {}
    keys: dict[str, set[str]] = {}
    contacts = []
    for item in cfg.get("message", []):
        name = item.get("name")
        fields = {}
        for field, spec in item.get("fields", {}).items():
            hit = _FIELD.match(spec)
            if not hit:
                _fail(path, f"message {name}: field {field} = {spec!r}, expected u8|u16|u24|u32@<offset>")
            fields[field] = (int(hit.group(2)), int(hit.group(1)) // 8)
        if ("length" in item) == ("entry_length" in item):
            _fail(path, f"message {name}: exactly one of length and entry_length")
        size = item.get("length", item.get("entry_length"))
        if any(o + n > size for o, n in fields.values()):
            _fail(path, f"message {name}: a field lies beyond its {size} bytes")
        lines = []
        for entry in item.get("lines", []):
            if not isinstance(entry, dict) or set(entry) != {"key", "text"}:
                _fail(path, f"message {name}: each line is {{ key = ..., text = ... }}")
            used = {f for _, f in _parts(entry["text"]) if f}
            if used - set(fields):
                _fail(path, f"message {name}: unknown field(s) {sorted(used - set(fields))} in {entry['text']!r}")
            if entry["key"] in keys:
                _fail(path, f"line key {entry['key']} twice")
            keys[entry["key"]] = used
            lines.append((entry["key"], entry["text"]))
        match = item.get("match")
        messages[item["type"]] = m = Message(item["type"], name, item.get("length"), item.get("entry_length"),
                                             fields, tuple(lines), match.encode("ascii") if match is not None else None)
        if item.get("contact"):
            contacts.append(m)
    if len(contacts) != 1 or contacts[0].entry_length is None or set(contacts[0].fields) != {"id2", "timer", "rssi"}:
        _fail(path, "exactly one counted message with contact = true and the fields id2, timer, rssi")
    for key, need in NEEDED.items():
        if keys.get(key) != need:
            _fail(path, f"line key {key} with the fields {sorted(need)} is needed (found: "
                        f"{sorted(keys[key]) if key in keys else 'none'})")
    log, frame = cfg["log"], cfg["frame"]
    parts = _parts(log["line"])
    if [f for _, f in parts] != ["time", "id", "text"] or parts[0][0] or not parts[2][0] \
            or not parts[1][0].startswith(parts[2][0]) or len(parts[2][0]) != 1:
        _fail(path, 'log.line: expected "{time}<separator><ID field>{id}<separator>{text}", one separator character')
    return Protocol(log["file_name"], log["line"], log["time_format"], frame["prefix"].encode("ascii"),
                    frame["terminator"].encode("ascii"), messages, contacts[0])


@cache
def protocol() -> Protocol:
    """The logger's format of this study (<study folder>/logger.toml), read once."""
    path = study.study_dir() / CONFIG_NAME
    if not path.exists():
        raise SystemExit(f"{path} is missing: the logger's message and line format (lib/logger_protocol.py)")
    return load(path)
