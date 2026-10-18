"""Offline guard for the patch consumed by clean native builds."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NativeBuildPatchTests(unittest.TestCase):
    def test_macos_anisette_patch_is_valid_unified_diff(self):
        result = subprocess.run(
            ["git", "apply", "--numstat", str(ROOT / "patches" / "macos-anisette-fallback.patch")],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("omnisette/src/lib.rs", result.stdout)


if __name__ == "__main__":
    unittest.main()
