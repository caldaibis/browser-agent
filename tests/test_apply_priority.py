from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import apply_priority


class TestApplyPriority(unittest.TestCase):
    def test_claim_sets_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as td:
            flag = Path(td) / "apply_priority.flag"
            with patch.object(apply_priority, "PRIORITY_FLAG", flag):
                self.assertFalse(apply_priority.priority_pending())
                with apply_priority.priority_claim():
                    self.assertTrue(apply_priority.priority_pending())
                self.assertFalse(apply_priority.priority_pending())

    def test_claim_clears_even_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            flag = Path(td) / "apply_priority.flag"
            with patch.object(apply_priority, "PRIORITY_FLAG", flag):
                with self.assertRaises(RuntimeError):
                    with apply_priority.priority_claim():
                        raise RuntimeError("apply crashed")
                self.assertFalse(apply_priority.priority_pending())

    def test_stale_flag_from_a_crashed_claimant_is_ignored(self):
        """A hard-killed orchestrator leaves the flag behind; it must not
        wedge the poller forever."""
        with tempfile.TemporaryDirectory() as td:
            flag = Path(td) / "apply_priority.flag"
            with patch.object(apply_priority, "PRIORITY_FLAG", flag):
                flag.write_text("pid=1 epoch=0\n", encoding="utf-8")
                old = time.time() - apply_priority.STALE_SECONDS - 10
                os.utime(flag, (old, old))
                self.assertFalse(apply_priority.priority_pending())


if __name__ == "__main__":
    unittest.main()
