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


class LogRotationCheck(unittest.TestCase):
    def run_check(self, rotate_file_exists, tool, schedule="logrotate.timer"):
        lines = []
        rep = hb.Report(lines.append)
        saved = hb.os.path.exists, hb.logrotate_path, hb.logrotate_schedule, hb.file_log
        old_exists = saved[0]
        try:
            hb.os.path.exists = lambda p: rotate_file_exists if p == hb.ROTATE_FILE else old_exists(p)
            hb.logrotate_path = lambda: tool
            hb.logrotate_schedule = lambda: schedule
            hb.file_log = lambda *a, **k: None
            with tempfile.TemporaryDirectory() as d:
                hb.check_logging({"LOG_FILE": os.path.join(d, "t.log"), "LOG_LEVEL": "INFO"}, rep)
        finally:
            hb.os.path.exists, hb.logrotate_path, hb.logrotate_schedule, hb.file_log = saved
        return rep, "\n".join(lines)

    def test_ok_when_installed_and_scheduled(self):
        rep, out = self.run_check(True, "/usr/sbin/logrotate")
        self.assertEqual((rep.fails, rep.warns), (0, 0))
        self.assertIn("[ OK ] log rotation", out)

    def test_fails_when_logrotate_missing(self):
        rep, out = self.run_check(True, None)
        self.assertEqual((rep.fails, rep.warns), (1, 0))
        self.assertIn("[FAIL] logrotate is not installed (required)", out)
        self.assertIn("run install.sh again (it installs logrotate)", out)

    def test_warns_when_not_set_up(self):
        rep, out = self.run_check(False, "/usr/sbin/logrotate")
        self.assertEqual((rep.fails, rep.warns), (0, 1))
        self.assertIn("log rotation is not set up", out)

    def test_warns_when_not_scheduled(self):
        rep, out = self.run_check(True, "/usr/sbin/logrotate", schedule=None)
        self.assertEqual((rep.fails, rep.warns), (0, 1))
        self.assertIn("systemctl enable --now logrotate.timer", out)


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


class RecordFolders(unittest.TestCase):
    CFG = dict(CFG, RECORD_FOLDERS=True)

    def test_off_by_default(self):
        f = hb.Folders()
        ref = {"appId": "a1", "app": "AP", "wsId": "w1", "ws": "DO Log", "rowId": "r1", "title": "DO-1"}
        self.assertEqual(f.dest(ref, CFG), os.path.join("AP", "DO Log"))

    def test_title_folder_and_duplicate_titles(self):
        f = hb.Folders()
        a = {"appId": "a1", "app": "AP", "wsId": "w1", "ws": "DO Log", "rowId": "row-aaaaaa", "title": "DO/1"}
        b = dict(a, rowId="row-bbbbbb")
        self.assertEqual(f.dest(a, self.CFG), os.path.join("AP", "DO Log", "DO-1"))
        self.assertEqual(f.dest(a, self.CFG), os.path.join("AP", "DO Log", "DO-1"))
        self.assertEqual(f.dest(b, self.CFG), os.path.join("AP", "DO Log", "DO-1 (bbbbbb)"))
        self.assertEqual(f.dest(dict(a, rowId="r9", title=""), self.CFG), os.path.join("AP", "DO Log", "r9"))

    def test_setting_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "backup.conf")
            for val, want in (("yes", True), ("no", False), ("", False)):
                with open(p, "w") as fh:
                    fh.write(f"RECORD_FOLDERS={val}\n")
                self.assertIs(hb.load_conf(p)[0]["RECORD_FOLDERS"], want)
            with open(p, "w") as fh:
                fh.write("RECORD_FOLDERS=maybe\n")
            with self.assertRaises(hb.ConfigError):
                hb.load_conf(p)


class FindRemoved(unittest.TestCase):
    def row(self, rid, fid, name="a.pdf", size="10", title="T1"):
        return {"record_id": rid, "field_id": "f1", "file_id": fid, "original_name": name, "size": size,
                "title": title}

    def test_removed_rows(self):
        backed = [self.row("r1", "x1"), self.row("r1", "x2", "b.pdf"), self.row("r2", "x3", "c.pdf", "7")]
        refs = [{"rowId": "r1", "fieldId": "f1", "fid": "x1"},                              # still there
                {"rowId": "r2", "fieldId": "f1", "fid": "new", "oname": "C.pdf", "size": 7}]  # uploaded again
        self.assertEqual([r["file_id"] for r in hb.removed_rows(backed, refs)], ["x2"])

    def test_status(self):
        r = self.row("r1", "x1")
        self.assertEqual(hb.removed_status(r, True, 1, "_"), "removed from record")
        self.assertIn("recycle bin", hb.removed_status(r, True, 9, "_"))
        self.assertIn("record deleted", hb.removed_status(r, True, None, "_"))
        self.assertIn("field deleted", hb.removed_status(r, False, 1, "_"))
        self.assertIn("attach by hand", hb.removed_status(self.row("r1", "x1", title="A_B"), True, 1, "_"))

    def test_reimported_file_counts_as_present(self):
        # Import Attachments keeps the uploaded name: '<title>_<original name>', new file ID
        backed = [self.row("r1", "x1", "Plan", "10", title="DO-1")]
        refs = [{"rowId": "r1", "fieldId": "f1", "fid": "new", "oname": "DO-1_Plan", "size": 10, "title": "DO-1"}]
        self.assertEqual(hb.removed_rows(backed, refs, "_"), [])


class ReimportedName(unittest.TestCase):
    def test_title_not_doubled(self):
        ref = {"fid": "f1", "title": "DO-1", "name": "x.pdf", "oname": "DO-1_Invoice"}
        self.assertEqual(hb.file_name(ref, CFG), "DO-1_Invoice.pdf")
        ref = {"fid": "f1", "title": "DO-1", "name": "x.pdf", "oname": "DO-1_"}
        self.assertEqual(hb.file_name(ref, CFG), "DO-1_DO-1_.pdf")
