"""Offline checks: all accounts and exception payloads are synthetic fixtures."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_account_setup.py"
SPEC = importlib.util.spec_from_file_location("local_account_setup_test_subject", PATH)
assert SPEC is not None and SPEC.loader is not None
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)


class AccountSetupFailureTests(unittest.TestCase):
    def test_ids_status_preserves_only_numeric_code(self):
        codes = [("5052", "alias_recently_changed"), ("6001", "ids_incompatible"),
                 ("6004", "ids_rejected"), ("6005", "ids_rejected"),
                 ("6009", "imessage_disabled")]
        for code, reason in codes:
            for prefix in [f"IDS failed ({code})", f"unknown IDS error {code}", f"status {code}"]:
                with self.subTest(code=code, prefix=prefix):
                    result = SETUP.public_failure(RuntimeError(
                        f"{prefix}; account=private@example.invalid; opaque-private-value"
                    ))
                    self.assertEqual(result, {"reason": reason, "ids_status": code})
                    self.assertNotIn("private@example.invalid", json.dumps(result))
                    self.assertNotIn("opaque-private-value", json.dumps(result))

    def test_negative_anisette_error_does_not_become_ids_status(self):
        self.assertEqual(SETUP.public_failure(RuntimeError("AOSKit anisette failed (-45070)")), {
            "reason": "anisette_unavailable",
        })

    def test_unknown_error_does_not_expose_response_text(self):
        self.assertEqual(SETUP.public_failure(RuntimeError("opaque-private-value private@example.invalid")), {
            "reason": "unknown",
        })

    def test_credential_and_verification_failures_remain_fixed_categories(self):
        for message, reason in [("password rejected: opaque-private-value", "bad_credentials"),
                                ("verification code required", "verification_failed")]:
            with self.subTest(reason=reason):
                self.assertEqual(SETUP.public_failure(RuntimeError(message)), {"reason": reason})


if __name__ == "__main__":
    unittest.main()
