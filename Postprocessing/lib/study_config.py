#!/usr/bin/env python3
"""The values of the study: <study folder>/study.toml (lib/study.py), read once.

Dates and limits of the capture, the fielded IDs, the RSSI limits and thresholds, the beacons'
storage and drift, the logger's ID bug (with its switch) and the logger's own dating rule. The code
holds no value of a particular study; lib/postprocessing.py and the steps take these from here.
Not run directly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path

from lib import study

CONFIG_NAME = study.STUDY_CONFIG


@dataclass(frozen=True)
class IdBug:
    enabled: bool
    until: datetime            # ID1 48-57 written as 0-9 in lines logged before this (if enabled)
    witnesses_until: datetime  # readouts used by the split: logged before this


@dataclass(frozen=True)
class LoggerOwnDating:
    id_max: int
    extra: tuple[int, ...]
    rssi_min: int
    rssi_max: int
    first_day: date
    last_day: date


@dataclass(frozen=True)
class StudyConfig:
    flash_start: datetime
    latest_record: datetime
    observation_start: datetime
    observation_end: datetime
    delay_free_from: datetime
    base_fix: datetime
    id_min: int
    id_max: int
    id_extra: frozenset[int]
    rssi_valid_min: int
    rssi_valid_max: int
    rssi_threshold_location_tag: int
    rssi_threshold_beacon: int
    storage_full_count: int
    drift_common_s_per_day: float
    drift_min_s_per_day: float
    drift_max_s_per_day: float
    id_bug: IdBug
    logger_own_dating: LoggerOwnDating


class _Reader:
    def __init__(self, path: Path, cfg: dict):
        self.path, self.cfg = path, cfg

    def get(self, section: str, key: str, kind):
        try:
            value = self.cfg[section][key]
        except KeyError:
            raise SystemExit(f"{self.path}: [{section}] {key} is missing") from None
        ok = (isinstance(value, kind) and not (kind in (int, float) and isinstance(value, bool))
              and not (kind is date and isinstance(value, datetime)))
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value, ok = float(value), True
        if kind is list:
            ok = isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        if not ok:
            raise SystemExit(f"{self.path}: [{section}] {key} = {value!r} is not a {kind.__name__}")
        return value


def load(path: Path) -> StudyConfig:
    with path.open("rb") as f:
        r = _Reader(path, tomllib.load(f))
    t = lambda key: r.get("times", key, datetime)                       # noqa: E731
    cfg = StudyConfig(
        flash_start=t("flash_start"), latest_record=t("latest_record"),
        observation_start=t("observation_start"), observation_end=t("observation_end"),
        delay_free_from=t("delay_free_from"), base_fix=t("base_fix"),
        id_min=r.get("ids", "min", int), id_max=r.get("ids", "max", int),
        id_extra=frozenset(r.get("ids", "extra", list)),
        rssi_valid_min=r.get("rssi", "valid_min", int), rssi_valid_max=r.get("rssi", "valid_max", int),
        rssi_threshold_location_tag=r.get("rssi", "threshold_location_tag", int),
        rssi_threshold_beacon=r.get("rssi", "threshold_beacon", int),
        storage_full_count=r.get("beacon", "storage_full_count", int),
        drift_common_s_per_day=r.get("beacon", "drift_common_s_per_day", float),
        drift_min_s_per_day=r.get("beacon", "drift_min_s_per_day", float),
        drift_max_s_per_day=r.get("beacon", "drift_max_s_per_day", float),
        id_bug=IdBug(r.get("id_bug", "enabled", bool), r.get("id_bug", "until", datetime),
                     r.get("id_bug", "witnesses_until", datetime)),
        logger_own_dating=LoggerOwnDating(
            r.get("logger_own_dating", "id_max", int), tuple(r.get("logger_own_dating", "extra", list)),
            r.get("logger_own_dating", "rssi_min", int), r.get("logger_own_dating", "rssi_max", int),
            r.get("logger_own_dating", "first_day", date), r.get("logger_own_dating", "last_day", date)),
    )
    checks = [(cfg.flash_start < cfg.latest_record, "times: flash_start before latest_record"),
              (cfg.observation_start < cfg.observation_end, "times: observation_start before observation_end"),
              (cfg.id_min <= cfg.id_max, "ids: min <= max"),
              (cfg.rssi_valid_min < cfg.rssi_valid_max, "rssi: valid_min < valid_max"),
              (cfg.drift_min_s_per_day <= cfg.drift_common_s_per_day <= cfg.drift_max_s_per_day,
               "beacon: drift_min <= drift_common <= drift_max"),
              (cfg.logger_own_dating.first_day <= cfg.logger_own_dating.last_day,
               "logger_own_dating: first_day <= last_day")]
    for ok, what in checks:
        if not ok:
            raise SystemExit(f"{path}: expected {what}")
    return cfg


@cache
def config() -> StudyConfig:
    """The values of this study (<study folder>/study.toml), read once."""
    path = study.study_dir() / CONFIG_NAME
    if not path.exists():
        raise SystemExit(f"{path} is missing: the values of the study (lib/study_config.py)")
    return load(path)
