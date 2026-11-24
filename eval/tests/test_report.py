"""Tests for eval/src/report.py: token-file refusal, the anomaly scan, the records refusal.

Run: python3 -I -B eval/tests/test_report.py
The token is a fake made at run time; the real token file is never read.
"""
import os
import secrets
import subprocess
import sys
import tempfile
import unittest

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "report.py")
PREFIX = "sk-ant-oat"


def write_token(path, value, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(value + "\n")
    os.chmod(path, mode)


class ReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.token = PREFIX + secrets.token_urlsafe(30)
        self.token_file = os.path.join(self.tmp, "token")
        write_token(self.token_file, self.token)
        self.job = os.path.join(self.tmp, "job")
        os.makedirs(os.path.join(self.job, "trial-1"))
        with open(os.path.join(self.job, "trial-1", "result.json"), "w") as handle:
            handle.write("{}")

    def run_report(self, *args, token_file=None):
        return subprocess.run(
            [sys.executable, "-I", "-B", REPORT, args[0], self.job, token_file or self.token_file,
             *args[1:]], capture_output=True, text=True, timeout=10)

    def assert_refused(self, done):
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("token file refused", done.stderr)
        self.assertNotIn(self.token, done.stdout + done.stderr)

    def test_symlinked_token_refused(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.token_file, link)
        self.assert_refused(self.run_report("scan", token_file=link))

    def test_fifo_token_refused_without_hanging(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo, 0o600)
        self.assert_refused(self.run_report("scan", token_file=fifo))

    def test_token_mode_644_refused(self):
        loose = os.path.join(self.tmp, "loose")
        write_token(loose, self.token, 0o644)
        self.assert_refused(self.run_report("scan", token_file=loose))

    def test_scan_clean_folder(self):
        done = self.run_report("scan")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("anomaly scan: 0 found", done.stdout)

    def test_scan_fifo_in_folder(self):
        os.mkfifo(os.path.join(self.job, "trial-1", "pipe"))
        done = self.run_report("scan")
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        self.assertIn("not a regular file or folder", done.stdout)

    def test_records_written(self):
        done = self.run_report("records", "job-r1", "plain", "1.00")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.job, "records.jsonl")))

    def test_records_token_like_text_exits_3(self):
        # The token holds a fixed record key (17 characters), so every record holds token text.
        os.unlink(self.token_file)
        write_token(self.token_file, PREFIX + "rate_limit_events" + secrets.token_urlsafe(15))
        done = self.run_report("records", "job-r1", "plain", "1.00")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("records: not written (token-like text in a record)", done.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.job, "records.jsonl")))


if __name__ == "__main__":
    unittest.main()
