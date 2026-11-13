"""Unit tests for the parts that do not need Docker or HAP: python3 -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hap_attachment_backup as hb  # noqa: E402

CFG = {"TITLE_MAX_CHARS": 100, "NAME_MAX_CHARS": 150, "DELIMITER": "_"}


class Naming(unittest.TestCase):
    def test_file_name(self):
        ref = {"fid": "f1", "title": "INV-001", "name": "abc.pdf", "oname": "Invoice March.pdf"}
        self.assertEqual(hb.file_name(ref, CFG), "INV-001_Invoice March.pdf")

    def test_bad_characters_and_empty_title(self):
        ref = {"fid": "f1", "rowId": "row-9", "title": "", "name": "x.png", "oname": 'a/b:c*?"<>|.png'}
        self.assertEqual(hb.file_name(ref, CFG), "row-9_a-b-c------.png")

    def test_unique(self):
        used = set()
        self.assertEqual(hb.unique("A_b.pdf", used), "A_b.pdf")
        self.assertEqual(hb.unique("a_B.pdf", used), "a_B (2).pdf")

    def test_long_name_fits_bytes(self):
        ref = {"fid": "f1", "title": "中" * 100, "name": "x.docx", "oname": "文" * 150}
        self.assertLessEqual(len(hb.file_name(ref, CFG).encode("utf-8")), 240)

    def test_bucket_of(self):
        self.assertEqual(hb.bucket_of("https://hap.example.com/file/mdpic/"), "mdpic")


class SetsAndRetention(unittest.TestCase):
    def make(self, base, name, copied, failed):
        os.makedirs(os.path.join(base, name))
        with open(os.path.join(base, name, "summary.txt"), "w") as f:
            f.write(f"files_copied={copied}\ncopy_failures={failed}\n")

    def test_order_and_prune_keeps_good_fulls(self):
        with tempfile.TemporaryDirectory() as base:
            for d in ("2026-10-01", "2026-10-08", "2026-10-15"):
                self.make(base, f"full-{d}_0200", 20, 0)
            self.make(base, "full-2026-10-22_0200", 0, 20)          # failed night
            self.make(base, "incr-2026-10-15_0200", 1, 0)
            sets = hb.list_sets(base)
            self.assertLess(sets.index("full-2026-10-15_0200"), sets.index("incr-2026-10-15_0200"))
            self.assertEqual(sets[-1], "full-2026-10-22_0200")
            hb.prune({"BACKUP_DIR": base, "KEEP_FULL": 2, "KEEP_INCR_DAYS": 100000})
            self.assertEqual([d for d in hb.list_sets(base) if d.startswith("full")],
                             ["full-2026-10-08_0200", "full-2026-10-15_0200", "full-2026-10-22_0200"])


class Config(unittest.TestCase):
    def test_load_conf(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("BACKUP_DIR=/backups\nKEEP_FULLS=3\nLOG_LEVEL=warn\nINCLUDE_APPS=a, b\n")
        try:
            cfg, warns = hb.load_conf(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(cfg["BACKUP_DIR"], "/backups")
        self.assertEqual(cfg["LOG_LEVEL"], "WARNING")
        self.assertEqual(cfg["INCLUDE_APPS"], ["a", "b"])
        self.assertEqual(cfg["MONGO_CONTAINER"], "auto")
        self.assertTrue(any("KEEP_FULLS" in w for w in warns))

    def test_bad_number(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("KEEP_FULL=four\n")
        try:
            with self.assertRaises(hb.ConfigError):
                hb.load_conf(f.name)
        finally:
            os.remove(f.name)

    def test_mask(self):
        self.assertEqual(hb.mask("mongodb://admin:s3cret@db:27017/admin"), "mongodb://admin:***@db:27017/admin")


if __name__ == "__main__":
    unittest.main()
