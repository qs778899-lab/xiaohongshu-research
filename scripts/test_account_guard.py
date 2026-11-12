"""Offline cross-session serialization and stop propagation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xhs_research as r


class AccountGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.guard = self.root / "guards"
        self.guard.mkdir(mode=0o700)
        self.patcher = patch.object(r, "account_root", return_value=self.guard)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.runs = [self.root / "one", self.root / "two"]
        for run in self.runs:
            r.write_json(run / "run.json", {"closed": False})
            r.bind_account(run, self.root / "fake-profile", "Profile 1")

    def test_stop_cannot_be_bypassed_by_another_run(self):
        with self.assertRaises(r.StopRun):
            r.stop(self.runs[0], "synthetic verification")
        with self.assertRaises(r.StopRun):
            r.ensure_active(self.runs[1])

    def test_nested_lock_and_other_process_exclusion(self):
        code = '''
import sys
from pathlib import Path
import xhs_research as r
r.account_root=lambda:Path(sys.argv[2])
try:
    with r.account_lock(Path(sys.argv[1])): pass
except r.ResearchError:
    raise SystemExit(7)
'''
        with r.account_lock(self.runs[0]), r.account_lock(self.runs[0]):
            proc = subprocess.run([sys.executable, "-c", code, str(self.runs[1]), str(self.guard)],
                                  cwd=Path(__file__).parent, capture_output=True)
            self.assertEqual(proc.returncode, 7, proc.stderr)
        with r.account_lock(self.runs[1]):
            pass

    def test_bind_cannot_change_account(self):
        with self.assertRaises(r.ResearchError):
            r.bind_account(self.runs[0], self.root / "fake-profile", "Profile 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
