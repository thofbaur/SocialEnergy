#!/usr/bin/env python3
"""The study folder: the inputs of one capture, kept outside this repository.

Layout of the folder:

    Logs/                       the original *.log files of the logger, never modified
    list_persons.csv            beacons worn by persons (ID; ...)
    list_rooms.csv              location tags (ID, ...)
    battery_replacements.csv    documented battery replacements (optional: without it, none)
    logger.toml                 the logger's message and log line format (lib/logger_protocol.py)
    study.toml                  the values of the study: dates, IDs, RSSI, beacons, the ID bug (lib/study_config.py)

The folder is set with --study-dir of any script (study_dir reads it from the command line when
lib/postprocessing.py is imported, which fixes its paths then); it is put into the environment
variable DSA_STUDY_DIR, so the scripts a script starts (run_pipeline.py its steps) use the same
folder. Without --study-dir: DSA_STUDY_DIR if set, else DEFAULT_STUDY_DIR. Has no imports from this
project.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ENV = "DSA_STUDY_DIR"
ROOT = Path(__file__).resolve().parents[1]               # the Postprocessing folder
DEFAULT_STUDY_DIR_RELATIVE = "../../Studies/DSA2026"    # relative to ROOT
DEFAULT_STUDY_DIR = (ROOT / DEFAULT_STUDY_DIR_RELATIVE).resolve()

LOGS_SUBDIR = "Logs"
LIST_PERSONS = "list_persons.csv"
LIST_ROOMS = "list_rooms.csv"
BATTERY_REPLACEMENTS = "battery_replacements.csv"
LOGGER_CONFIG = "logger.toml"
STUDY_CONFIG = "study.toml"
REQUIRED = (LOGS_SUBDIR, LIST_PERSONS, LIST_ROOMS, LOGGER_CONFIG, STUDY_CONFIG)   # battery_replacements.csv: optional


def study_dir(argv: list[str] | None = None) -> Path:
    """The study folder: --study-dir of the command line (argv, default sys.argv[1:]; then also put
    into DSA_STUDY_DIR for the scripts this one starts), else DSA_STUDY_DIR if set, else
    DEFAULT_STUDY_DIR."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--study-dir", type=Path, default=None)
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if args.study_dir is not None:
        os.environ[ENV] = str(args.study_dir.resolve())
    value = os.environ.get(ENV)
    return Path(value).resolve() if value else DEFAULT_STUDY_DIR


def add_argument(parser: argparse.ArgumentParser) -> None:
    """--study-dir for a script's parser (study_dir has read it already, at import)."""
    parser.add_argument("--study-dir", type=Path, default=None,
                        help=f"Study folder with {LOGS_SUBDIR}/, {LIST_PERSONS}, {LIST_ROOMS}, "
                             f"{LOGGER_CONFIG}, {STUDY_CONFIG} (optional: {BATTERY_REPLACEMENTS}). Default: ${ENV}, else {DEFAULT_STUDY_DIR_RELATIVE} "
                             f"(relative to {ROOT.name}/, i.e. {DEFAULT_STUDY_DIR})")


def missing(folder: Path) -> list[str]:
    """The inputs the study folder lacks (empty: complete)."""
    out = [name for name in REQUIRED if not (folder / name).exists()]
    if LOGS_SUBDIR not in out and not any((folder / LOGS_SUBDIR).glob("*.log")):
        out.append(f"{LOGS_SUBDIR}/*.log")
    return out
