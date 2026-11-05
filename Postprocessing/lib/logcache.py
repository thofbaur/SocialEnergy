#!/usr/bin/env python3
"""The log files as number arrays, cached per log file: the only place the logs are read.

Each log file is read once, when its cache is written: the original logs in step 1 of
run_pipeline.py, the working copies are made from the cache of their originals (derive) and get
their cache with them. Every script reads the cache, never the text. A cache file is written again
whenever its log file changed: its name carries the size and modification time of the log file.

All cache files are under one root (root(): <pipeline folder>/cache, set by run_pipeline.py and
generate_output.py for their --output-dir / --pipeline-dir and passed to the scripts they start as
DSA_CACHE_DIR), never in a log folder: one subfolder per log folder, named by the folder and a
digest of its full path (cache_dir), so two folders with files of the same name, size and time
never share a cache file.

The cache holds every line: the lines postprocessing.parse_line reads (those with a line time and
an ID field), contact lines as numbers and all other lines as text, and the lines it skips
(notes written into the working copies; text) by line number, and the text of the contact lines
not written as contact_text (RSSI 0 is logged as "-0"). The file can be rebuilt from it
byte for byte (file_bytes). Also: the SHA-256 of the file, of its first HEAD_BYTES and of its first
line (duplicate check, in a small .json beside the cache file, meta) and the fake contact records as
uptime_model.py finds them while reading (runs of consecutive contact lines of one ID1,
postprocessing.embedded_messages).

The scripts rely on the format of the logger's lines; the cache stops with an error where a line
does not have it (a line of three fields without line time or ID field, line time not exactly
"YYYY-MM-DD HH:MM:SS", ID field not "ID: <ID1>", spaces around the text after the ID field,
contact line with ID2 not a plain number) or
where the readers would count lines differently (no line end at the end of the file, mixed line
ends, carriage returns or other line breaks inside a line, text that is not UTF-8).
Not run directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import shutil
import sys
from array import array
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np

from lib import logger_protocol
from lib import postprocessing as pp

VERSION = 5                   # of the cache content; a new version makes all cache files stale
CACHE_ENV = "DSA_CACHE_DIR"   # the cache root for the scripts a script starts (set_root)
CACHE_SUBDIR = "cache"        # the cache root in the pipeline folder
HEAD_BYTES = 1 << 16          # duplicate check: digest of the first bytes (postprocessing.find_duplicate_log_files)
EPOCH = datetime(1970, 1, 1)  # line times as whole seconds since EPOCH (naive, as in the logs)
WORKERS = min(6, os.cpu_count() or 1)
_PLAIN_NUMBER = re.compile(r"0|[1-9][0-9]*")
_OTHER_BREAK = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85  ]")    # str.splitlines() breaks there
COLUMNS = ("line", "t", "id1", "kind", "id2", "timer", "rssi")
DTYPES = dict(line=np.int32, t=np.int64, id1=np.int16, kind=np.int8, id2=np.int16, timer=np.int32, rssi=np.int16)


def root() -> Path:
    """The cache root: DSA_CACHE_DIR if set (set_root), else <default pipeline folder>/cache."""
    value = os.environ.get(CACHE_ENV)
    return Path(value).resolve() if value else pp.PIPELINE_DIR / CACHE_SUBDIR


def set_root(pipeline_dir: Path) -> None:
    """The cache root <pipeline_dir>/cache, for this process and the scripts it starts."""
    os.environ[CACHE_ENV] = str((pipeline_dir / CACHE_SUBDIR).resolve())


@lru_cache(maxsize=None)
def _cache_dir(log_dir: str, cache_root: str) -> Path:
    full = Path(log_dir).resolve()
    tag = hashlib.sha256(os.path.normcase(str(full)).encode("utf-8")).hexdigest()[:12]
    return Path(cache_root) / f"{full.name}-{tag}"


def cache_dir(log_dir: Path) -> Path:
    """The cache folder of the log files in log_dir."""
    return _cache_dir(str(log_dir), str(root()))


def cache_file(path: Path) -> Path:
    st = path.stat()
    # the fake records in the cache depend on the logger's format (logger.toml): another format, another file
    return cache_dir(path.parent) / f"{path.name}.{st.st_size}.{st.st_mtime_ns}.v{VERSION}.{logger_protocol.protocol().digest}.pkl"


def drop(path: Path) -> None:
    """Deletes the cache files of log file path (also when the log file is gone)."""
    for old in cache_dir(path.parent).glob(f"{path.name}.*"):
        old.unlink(missing_ok=True)


def meta_file(path: Path) -> Path:
    return cache_file(path).with_suffix(".json")


def line_time(t: int) -> datetime:
    return EPOCH + timedelta(seconds=int(t))


def contact_text(id2: int, timer: int, rssi: int) -> str:
    """The text of a contact line as the logger writes it (logger.toml; RSSI with its sign)."""
    return pp.LOG.text("contact", id2=id2, timer=timer, rssi=rssi)


def fake_runs(file, line, id1, kind, id2, timer, rssi):
    """Runs of consecutive contact lines (neighbours in the given order, same file, consecutive line
    numbers, same ID1) whose records, put back into their bytes (logger.toml), contain complete logger
    messages (BI 3.2.2). Yields per such run: its positions in
    the arrays, the sorted positions of the fake lines within the run and {message type: count}."""
    n = len(line)
    if n < 2:
        return
    contact = kind == 1
    joined = np.zeros(n, dtype=bool)
    joined[1:] = (contact[1:] & contact[:-1] & (file[1:] == file[:-1]) & (line[1:] == line[:-1] + 1)
                  & (id1[1:] == id1[:-1]))
    starts = np.flatnonzero(contact & ~joined)
    ends = np.flatnonzero(contact & ~np.r_[joined[1:], False]) + 1
    long = ends - starts >= 2
    terminator = logger_protocol.protocol().terminator
    size = logger_protocol.protocol().contact.entry_length
    for a, b in zip(starts[long].tolist(), ends[long].tolist()):
        stream = pp.contact_records_bytes(id2[a:b], timer[a:b], rssi[a:b])
        hit: set[int] = set()
        types: dict[int, int] = {}
        # the CR LF the logger found after the data message can be the end of the last embedded message
        for start, end in pp.embedded_messages(stream + terminator):
            end = min(end, len(stream))
            hit.update(range(start // size, (end - 1) // size + 1))
            types[stream[start + 3]] = types.get(stream[start + 3], 0) + 1
        if hit:
            yield np.arange(a, b), sorted(hit), types


def fake_flags(file, line, id1, kind, id2, timer, rssi) -> np.ndarray:
    fake = np.zeros(len(line), dtype=bool)
    for rows, hit, _ in fake_runs(file, line, id1, kind, id2, timer, rssi):
        fake[rows[hit]] = True
    return fake


def _file_order_fakes(data: dict) -> np.ndarray:
    """Fake contact records as uptime_model.py finds them while reading: runs in file order."""
    return fake_flags(np.zeros(len(data["line"]), dtype=np.int8), *(data[k] for k in COLUMNS[:1] + COLUMNS[2:]))


def _fail(name: str, number: int, what: str):
    raise ValueError(f"{name}:{number}: {what} (lib/logcache.py relies on the logger's line format)")


class _Parser:
    """The lines of one log file into columns (rows: the lines postprocessing.parse_line reads)."""

    def __init__(self, name: str):
        self.name = name
        self.cols = {k: array(np.dtype(DTYPES[k]).char) for k in COLUMNS}
        self.ids: dict[str, int] = {}
        self.rest: list[str] = []
        self.unparsed: dict[int, str] = {}
        self.verbatim: dict[int, str] = {}         # contact lines not written as contact_text
        self.seconds: dict[str, int] = {}

    def add(self, number: int, raw: str) -> None:
        """raw: the line without line end."""
        parsed = pp.parse_line(raw, Path(self.name), number)
        parts = raw.split(pp.LOG_SEPARATOR, 2)
        if parsed is None:
            if len(parts) == 3:
                _fail(self.name, number, "line with three fields but without line time or ID field")
            self.unparsed[number] = raw
            return
        if parts[1] != pp.LOG_ID_PREFIX + parsed.beacon_id or parts[2] != parsed.rest:
            _fail(self.name, number, "ID field or spaces not as written by the logger")
        s = self.seconds.get(parts[0])
        if s is None:
            if parts[0] != parsed.timestamp.strftime(pp.LOG_TIME_FORMAT):
                _fail(self.name, number, "line time not as written by the logger")
            s = self.seconds[parts[0]] = (parsed.timestamp - EPOCH) // timedelta(seconds=1)
        c = self.cols
        c["line"].append(number); c["t"].append(s); c["id1"].append(self.ids.setdefault(parsed.beacon_id, len(self.ids)))
        m = pp.CONTACT_RE.match(parsed.rest)
        if m:
            if not _PLAIN_NUMBER.fullmatch(m.group("id2")) or int(m.group("id2")) > 32767 \
                    or int(m.group("timer")) >= 2 ** 31 or not -32768 <= int(m.group("rssi")) <= 32767:
                _fail(self.name, number, "contact line with ID2, Timer or RSSI out of the expected range")
            id2, timer, rssi = int(m.group("id2")), int(m.group("timer")), int(m.group("rssi"))
            if parsed.rest != contact_text(id2, timer, rssi):
                self.verbatim[number] = raw
            c["kind"].append(1); c["id2"].append(id2); c["timer"].append(timer); c["rssi"].append(rssi)
            return
        if parsed.rest.startswith(pp.TEXT_PREFIX["contact"]):
            _fail(self.name, number, "contact line not matching CONTACT_RE")
        c["kind"].append(0); c["id2"].append(-1); c["timer"].append(0); c["rssi"].append(0)
        self.rest.append(parsed.rest)

    def data(self) -> dict:
        out = {k: np.frombuffer(v, dtype=DTYPES[k]) if len(v) else np.zeros(0, dtype=DTYPES[k])
               for k, v in self.cols.items()}
        out.update(ids=list(self.ids), rest=self.rest, unparsed=self.unparsed, verbatim=self.verbatim)
        return out


def _split_lines(name: str, content: bytes) -> tuple[list[str], str]:
    """The lines of a log file (without line end) and its line end; the checks that every reader
    counts the same lines."""
    if not content:
        return [], "\n"
    if not content.endswith(b"\n"):
        _fail(name, content.count(b"\n") + 1, "no line end at the end of the file")
    lf, crlf = content.count(b"\n"), content.count(b"\r\n")
    if crlf not in (0, lf):
        _fail(name, 0, "mixed line ends")
    if content.count(b"\r") != crlf:
        _fail(name, content[:content.index(b"\r")].count(b"\n") + 1, "carriage return inside a line")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        _fail(name, content[:e.start].count(b"\n") + 1, "text not UTF-8")
    m = _OTHER_BREAK.search(text)
    if m:
        _fail(name, text[:m.start()].count("\n") + 1, "line break character inside a line")
    eol = "\r\n" if crlf else "\n"
    return text.split(eol)[:-1], eol


def _digests(content: bytes) -> dict:
    return dict(size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                head=hashlib.sha256(content[:HEAD_BYTES]).hexdigest(),
                first_line=hashlib.sha256(content[:content.find(b"\n") + 1 or len(content)]).hexdigest())


def _build(path: Path) -> dict:
    content = path.read_bytes()
    lines, eol = _split_lines(path.name, content)
    parser = _Parser(path.name)
    for number, raw in enumerate(lines, start=1):
        parser.add(number, raw)
    data = parser.data()
    data.update(n_lines=len(lines), eol=eol, **_digests(content))
    data["fake"] = _file_order_fakes(data)
    return data


def store(path: Path, data: dict) -> None:
    """Writes data as the cache of log file path (replacing its stale cache files)."""
    target = cache_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{path.name}.*"):
        if old not in (target, target.with_suffix(".json")):
            old.unlink(missing_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({k: data[k] for k in ("size", "sha256", "head", "first_line", "n_lines", "recipe", "relabelled")
                               if k in data}), encoding="utf-8")
    os.replace(tmp, target.with_suffix(".json"))
    with tmp.open("wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, target)


def _write(path: Path) -> None:
    store(path, _build(path))


def ensure(paths: list[Path]) -> int:
    """Writes the missing or stale cache files of the log files (in parallel); returns their number."""
    missing = [p for p in paths if not cache_file(p).exists()]
    if missing:
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(_write, sorted(missing, key=lambda p: p.stat().st_size, reverse=True)))
    return len(missing)


def read(path: Path) -> dict:
    """The cached lines of one log file (written first if missing or stale)."""
    target = cache_file(path)
    if not target.exists():
        _write(path)
    with target.open("rb") as f:
        return pickle.load(f)


def meta(path: Path) -> dict:
    """size, sha256, head (SHA-256 of the first HEAD_BYTES), first_line (SHA-256 of the first line)
    and n_lines of a log file."""
    target = meta_file(path)
    if not target.exists():
        _write(path)
    return json.loads(target.read_text(encoding="utf-8"))


def link_cache(source: Path, path: Path) -> None:
    """The cache of source as the cache of path, a hard link to source (same content)."""
    src, dst = cache_file(source), cache_file(path)
    if not src.exists():
        _write(source)
    if dst.exists() and dst.with_suffix(".json").exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    for old in dst.parent.glob(f"{path.name}.*"):
        old.unlink(missing_ok=True)
    for a, b in ((src.with_suffix(".json"), dst.with_suffix(".json")), (src, dst)):
        try:
            os.link(a, b)
        except OSError:
            shutil.copyfile(a, b)


def texts(data: dict) -> list[str]:
    """The lines of the log file, without line end, as written."""
    out: list = [None] * data["n_lines"]
    u, inv = np.unique(data["t"], return_inverse=True)
    heads = [line_time(x).strftime(pp.LOG_TIME_FORMAT) + pp.LOG_SEPARATOR + pp.LOG_ID_PREFIX for x in u.tolist()]
    ids, rest, sep = data["ids"], iter(data["rest"]), pp.LOG_SEPARATOR
    for n, h, b, k, i2, tm, rs in zip(data["line"].tolist(), inv.tolist(), data["id1"].tolist(),
                                      data["kind"].tolist(), data["id2"].tolist(), data["timer"].tolist(),
                                      data["rssi"].tolist()):
        out[n - 1] = f"{heads[h]}{ids[b]}{sep}{contact_text(i2, tm, rs)}" if k else f"{heads[h]}{ids[b]}{sep}{next(rest)}"
    for number, raw in [*data["unparsed"].items(), *data["verbatim"].items()]:
        out[number - 1] = raw
    return out


def file_bytes(data: dict) -> bytes:
    """The log file, byte for byte."""
    lines = texts(data)
    return (data["eol"].join(lines) + data["eol"]).encode("utf-8") if lines else b""


def file_digest(path: Path, limit: int | None = None) -> str:
    """SHA-256 of the log file, or of its first limit bytes (postprocessing.find_duplicate_log_files)."""
    m = meta(path)
    if limit is None or limit >= m["size"]:
        return m["sha256"]
    if limit == HEAD_BYTES:
        return m["head"]
    return hashlib.sha256(file_bytes(read(path))[:limit]).hexdigest()


def find_duplicate_log_files(paths: list[Path]) -> list[tuple[str, list[Path]]]:
    """postprocessing.find_duplicate_log_files from the cache."""
    ensure(paths)
    return pp.find_duplicate_log_files(paths, HEAD_BYTES, digest=file_digest,
                                       first_line=lambda path: meta(path)["first_line"])


def recipe(source: Path, relabel: dict[int, tuple[str, str]], replace: dict[int, str]) -> str:
    """Fingerprint of derive(source, relabel, replace): a copy made from the same recipe is the same."""
    return hashlib.sha256(repr((meta(source)["sha256"], sorted(relabel.items()), sorted(replace.items())))
                          .encode("utf-8")).hexdigest()


def derive(source: Path, relabel: dict[int, tuple[str, str]], replace: dict[int, str]) -> tuple[dict, bytes, int]:
    """A copy of log file source with lines replaced (line number -> new text without line end) and
    the ID field of lines relabelled (line number -> (old ID1, new ID1); only where the line's ID1 is
    the old one; replaced lines are not relabelled): its cache data, its bytes and the number of
    relabelled lines. The data equal _build of the written copy."""
    d = read(source)
    line = d["line"]
    ids = list(d["ids"])
    id1 = d["id1"].astype(np.int64)
    relabelled = 0
    verbatim = {n: r for n, r in d["verbatim"].items() if n not in replace}
    pairs: dict[tuple[str, str], list[int]] = {}
    for n, pair in relabel.items():
        if n not in replace:
            pairs.setdefault(pair, []).append(n)
    for (old, new), numbers in pairs.items():
        if old not in ids:
            continue
        sel = np.isin(line, numbers) & (id1 == ids.index(old))
        relabelled += int(sel.sum())
        for n in line[sel].tolist():
            if n in verbatim:
                stamp, _, text = verbatim[n].split(pp.LOG_SEPARATOR, 2)
                verbatim[n] = pp.LOG.log_line(stamp, new, text)
        if new not in ids:
            ids.append(new)
        id1[sel] = ids.index(new)
    keep = ~np.isin(line, list(replace)) if replace else np.ones(len(line), dtype=bool)
    parser = _Parser(source.name)
    parser.ids = {b: i for i, b in enumerate(ids)}            # the codes of the kept lines stay valid
    for n in sorted(replace):
        parser.add(n, replace[n])
    new = parser.data()
    ids = list(parser.ids)
    rest_of = np.full(len(line), -1, dtype=np.int64)
    rest_of[d["kind"] == 0] = np.arange(len(d["rest"]))
    new_rest_of = np.full(len(new["line"]), -1, dtype=np.int64)
    new_rest_of[new["kind"] == 0] = np.arange(len(d["rest"]), len(d["rest"]) + len(new["rest"]))
    cols = {k: np.concatenate([d[k][keep] if k != "id1" else id1[keep], new[k]]) for k in COLUMNS}
    order = np.argsort(cols["line"], kind="stable")
    cols = {k: v[order] for k, v in cols.items()}
    all_rest = d["rest"] + new["rest"]
    rest_idx = np.concatenate([rest_of[keep], new_rest_of])[order]
    # ID1 codes in order of first appearance, as _build numbers them
    used, first = np.unique(cols["id1"], return_index=True)
    used = used[np.argsort(first)]
    recode = np.zeros(len(ids), dtype=np.int64)
    recode[used] = np.arange(len(used))
    cols["id1"] = recode[cols["id1"]]
    data = {k: v.astype(DTYPES[k]) for k, v in cols.items()}
    unparsed = {n: r for n, r in d["unparsed"].items() if n not in replace}
    unparsed.update(new["unparsed"])
    verbatim.update(new["verbatim"])
    data.update(ids=[ids[c] for c in used.tolist()], rest=[all_rest[k] for k in rest_idx[rest_idx >= 0].tolist()],
                unparsed=dict(sorted(unparsed.items())), verbatim=dict(sorted(verbatim.items())), n_lines=d["n_lines"], eol=d["eol"])
    content = file_bytes(data)
    data.update(_digests(content))
    data["fake"] = _file_order_fakes(data)
    data.update(recipe=recipe(source, relabel, replace), relabelled=relabelled)
    return data, content, relabelled


class Lines:
    """The lines of several log files: arrays over all lines, file after file (in the order of
    paths), each file's lines in file order.

    file, line, t (line time, seconds since EPOCH), id1 (index into ids), kind (1 contact line, 0
    other), id2, timer, rssi (contact lines; 0 / -1 elsewhere), fake (fake contact record),
    other (index into rest for the other lines, -1 for contact lines), start[f]:start[f + 1] the
    lines of file f. order: the lines merged in time order (stable sort by line time: lines of the
    same second keep the order of the files and lines). Per file: unparsed (line number -> text of
    the lines postprocessing.parse_line skips), verbatim (line number -> text of the contact lines not written as
    contact_text) and n_lines (all lines)."""

    def __init__(self, paths: list[Path]):
        ensure(paths)
        self.paths = list(paths)
        code: dict[str, int] = {}
        parts: dict[str, list] = {k: [] for k in ("line", "t", "id1", "kind", "id2", "timer", "rssi", "fake")}
        self.rest: list[str] = []
        self.unparsed: list[dict[int, str]] = []
        self.verbatim: list[dict[int, str]] = []
        self.n_lines: list[int] = []
        others, sizes = [], []
        for path in self.paths:
            data = read(path)
            remap = np.array([code.setdefault(b, len(code)) for b in data["ids"]] or [0], dtype=np.int16)
            for k in parts:
                parts[k].append(remap[data[k]] if k == "id1" else data[k])
            n_other = len(self.rest)
            o = np.full(len(data["line"]), -1, dtype=np.int64)
            o[data["kind"] == 0] = np.arange(n_other, n_other + len(data["rest"]))
            others.append(o)
            self.rest.extend(data["rest"])
            self.unparsed.append(data["unparsed"])
            self.verbatim.append(data["verbatim"])
            self.n_lines.append(data["n_lines"])
            sizes.append(len(data["line"]))
        self.ids: list[str] = list(code)
        for k, v in parts.items():
            setattr(self, k, np.concatenate(v) if v else np.zeros(0, dtype=np.int64))
        self.other = np.concatenate(others) if others else np.zeros(0, dtype=np.int64)
        self.file = np.repeat(np.arange(len(self.paths), dtype=np.int16), sizes)
        self.start = np.r_[0, np.cumsum(sizes)]
        t = self.t
        self.order = None if len(t) < 2 or bool(np.all(t[1:] >= t[:-1])) else np.argsort(t, kind="stable")

    def text_after_id(self, x: int) -> str:
        """The text after the ID field of line x (postprocessing.LogLine.rest)."""
        if self.kind[x] == 0:
            return self.rest[self.other[x]]
        raw = self.verbatim[self.file[x]].get(int(self.line[x]))
        return raw.split(pp.LOG_SEPARATOR, 2)[2] if raw is not None else contact_text(self.id2[x], self.timer[x], self.rssi[x])

    def merged(self, name: str) -> np.ndarray:
        """Column name in the merged (time) order."""
        x = getattr(self, name)
        return x if self.order is None else x[self.order]

    def merged_index(self) -> np.ndarray:
        return np.arange(len(self.t)) if self.order is None else self.order

    def mask_of(self, pairs) -> np.ndarray:
        """Per line: whether (log file name, line number) is in pairs."""
        out = np.zeros(len(self.line), dtype=bool)
        by_name: dict[str, list[int]] = {}
        for name, number in pairs:
            by_name.setdefault(name, []).append(number)
        for f, path in enumerate(self.paths):
            if path.name in by_name:
                a, b = self.start[f], self.start[f + 1]
                out[a:b] = np.isin(self.line[a:b], by_name[path.name])
        return out

    def readout_of(self, ranges: list[tuple[str, int, int]]) -> np.ndarray:
        """Per line: the index in ranges of the (log file name, first line, last line) holding it,
        -1 for none (as bisect over the first lines of the file's ranges)."""
        out = np.full(len(self.line), -1, dtype=np.int64)
        by_file: dict[str, list[tuple[int, int, int]]] = {}
        for i, (name, first, last) in enumerate(ranges):
            by_file.setdefault(name, []).append((first, last, i))
        for f, path in enumerate(self.paths):
            items = sorted(by_file.get(path.name, []))
            if not items:
                continue
            firsts = np.array([x[0] for x in items], dtype=np.int64)
            lasts = np.array([x[1] for x in items], dtype=np.int64)
            index = np.array([x[2] for x in items], dtype=np.int64)
            a, b = self.start[f], self.start[f + 1]
            numbers = self.line[a:b]
            k = np.searchsorted(firsts, numbers, side="right") - 1
            ok = (k >= 0) & (numbers <= lasts[np.maximum(k, 0)])
            out[a:b][ok] = index[k[ok]]
        return out


def load(log_dir: Path) -> Lines:
    """The lines of the *.log files of log_dir.
    Exits with an error message if the directory holds no log files."""
    paths = sorted(log_dir.glob("*.log"))
    if not paths:
        print(f"No .log files found in {log_dir}", file=sys.stderr)
        raise SystemExit(1)
    return Lines(paths)


def local_timestamps(t: np.ndarray) -> np.ndarray:
    """datetime.timestamp() of the line times t (seconds since EPOCH, naive local time)."""
    u, inv = np.unique(t, return_inverse=True)
    return np.array([line_time(x).timestamp() for x in u.tolist()], dtype=np.float64)[inv]
