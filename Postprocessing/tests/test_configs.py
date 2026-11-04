"""The study folder's configs: logger.toml (lib/logger_protocol.py) and study.toml (lib/study_config.py).

Each test loads a copy of the study's file, changed in one place. Run from Postprocessing/:
python -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from pathlib import Path

from lib import logger_protocol, study, study_config
from lib import postprocessing as pp


def _copy(name: str, old: str = "", new: str = "") -> Path:
    """The study's config name in a temporary folder, with old replaced by new (once)."""
    text = (pp.STUDY_DIR / name).read_text(encoding="utf-8")
    if old:
        assert text.count(old) == 1, (name, old)
        text = text.replace(old, new)
    folder = Path(tempfile.mkdtemp())
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


class LoggerConfig(unittest.TestCase):
    def test_study_file_loads(self):
        self.assertEqual(logger_protocol.load(_copy(study.LOGGER_CONFIG)), pp.LOG)

    def test_comment_does_not_change_the_digest(self):
        changed = logger_protocol.load(_copy(study.LOGGER_CONFIG, "[frame]", "# another comment\n[frame]"))
        self.assertEqual(changed.digest, pp.LOG.digest)

    def test_value_changes_the_digest(self):
        changed = logger_protocol.load(_copy(study.LOGGER_CONFIG, 'terminator = "\\r\\n"', 'terminator = "\\n"'))
        self.assertNotEqual(changed.digest, pp.LOG.digest)

    def test_refused(self):
        cases = {
            "field size": ('id2 = "u8@0"', 'id2 = "u12@0"'),
            "field beyond the entry": ('rssi = "u8@4"', 'rssi = "u8@5"'),
            "no contact message": ("contact = true", "contact = false"),
            "needed line key missing": ('key = "contact_count"', 'key = "count_of_contacts"'),
            "unknown field in a text": ('text = "Voltage: {voltage}"', 'text = "Voltage: {volts}"'),
            "line layout": ('line = "{time},ID: {id},{text}"', 'line = "{id},{time},{text}"'),
        }
        for what, (old, new) in cases.items():
            with self.subTest(what), self.assertRaises(SystemExit):
                logger_protocol.load(_copy(study.LOGGER_CONFIG, old, new))


class StudyConfig(unittest.TestCase):
    def test_study_file_loads(self):
        self.assertEqual(study_config.load(_copy(study.STUDY_CONFIG)), pp.STUDY)

    def test_id_bug_switched_off(self):
        cfg = study_config.load(_copy(study.STUDY_CONFIG, "\nenabled = true", "\nenabled = false"))
        self.assertFalse(cfg.id_bug.enabled)

    def test_refused(self):
        text = (pp.STUDY_DIR / study.STUDY_CONFIG).read_text(encoding="utf-8")
        start = next(line for line in text.splitlines() if line.startswith("observation_start"))
        cases = {
            "missing key": ("storage_full_count", "storage_full_count_x"),
            "wrong type": ("\nenabled = true", '\nenabled = "yes"'),
            "observation window reversed": (start, "observation_start = 2026-08-30T00:00:00"),
        }
        for what, (old, new) in cases.items():
            with self.subTest(what), self.assertRaises(SystemExit):
                study_config.load(_copy(study.STUDY_CONFIG, old, new))


class StudyFolder(unittest.TestCase):
    def test_from_the_command_line(self):
        before = os.environ.get(study.ENV)
        try:
            folder = Path(tempfile.mkdtemp())
            self.assertEqual(study.study_dir(["--log-dir", "x", "--study-dir", str(folder)]), folder.resolve())
            self.assertEqual(os.environ[study.ENV], str(folder.resolve()))       # for the scripts it starts
        finally:
            if before is None:
                os.environ.pop(study.ENV, None)
            else:
                os.environ[study.ENV] = before

    def test_missing_inputs(self):
        folder = Path(tempfile.mkdtemp())
        self.assertIn(study.STUDY_CONFIG, study.missing(folder))
        self.assertEqual(study.missing(pp.STUDY_DIR), [])
        self.assertNotIn(study.BATTERY_REPLACEMENTS, study.REQUIRED)            # optional


if __name__ == "__main__":
    unittest.main()
